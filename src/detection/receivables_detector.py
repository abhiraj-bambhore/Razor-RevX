"""
Receivables aging detector.
Computes invoice aging buckets, assigns risk tier, and determines dunning urgency.
"""
from __future__ import annotations

from src.data.schemas import (
    CustomerSegment,
    FailureMode,
    ReceivableOverdueEvent,
    RiskTier,
)


def classify_receivable(event: ReceivableOverdueEvent) -> dict:
    """
    Classify an overdue receivable.

    Returns:
        {
            "event": ReceivableOverdueEvent,
            "failure_mode": FailureMode,
            "aging_risk": str,   # low_risk | medium_risk | high_risk | write_off_risk
            "urgency": str,
            "recommended_first_action": str,
            "risk_tier": RiskTier,
            "collection_probability": float,  # 0-1
        }
    """
    days = event.days_overdue

    # Aging risk classification
    if days <= 30:
        aging_risk = "low_risk"
        collection_prob = 0.85
    elif days <= 60:
        aging_risk = "medium_risk"
        collection_prob = 0.65
    elif days <= 90:
        aging_risk = "high_risk"
        collection_prob = 0.40
    else:
        aging_risk = "write_off_risk"
        collection_prob = 0.15

    # Adjust by payment history: serial late payers are harder to collect
    if event.previous_payment_delays >= 3:
        collection_prob *= 0.7
    elif event.previous_payment_delays >= 1:
        collection_prob *= 0.85

    # Active dispute = pause dunning, escalate
    if event.has_active_dispute:
        return {
            "event": event,
            "failure_mode": FailureMode.RECEIVABLE_OVERDUE,
            "aging_risk": aging_risk,
            "urgency": "hold",
            "recommended_first_action": "escalate_to_human",
            "risk_tier": RiskTier.HIGH,
            "collection_probability": round(collection_prob, 3),
        }

    # Urgency and first action by aging bucket
    if aging_risk == "write_off_risk":
        urgency = "immediate"
        first_action = "escalate_to_human"
        risk_tier = RiskTier.CRITICAL
    elif aging_risk == "high_risk":
        urgency = "within_24h"
        first_action = "phone_followup"
        risk_tier = RiskTier.HIGH
    elif aging_risk == "medium_risk":
        urgency = "within_72h"
        first_action = "email_reminder"
        risk_tier = RiskTier.MEDIUM
    else:
        urgency = "within_7d"
        first_action = "email_reminder"
        risk_tier = RiskTier.LOW

    # Enterprise accounts with large amounts escalate faster
    if (
        event.customer_segment == CustomerSegment.ENTERPRISE
        and event.amount_inr >= 200000
    ):
        urgency = "within_24h"
        risk_tier = RiskTier.CRITICAL

    return {
        "event": event,
        "failure_mode": FailureMode.RECEIVABLE_OVERDUE,
        "aging_risk": aging_risk,
        "urgency": urgency,
        "recommended_first_action": first_action,
        "risk_tier": risk_tier,
        "collection_probability": round(collection_prob, 3),
    }


def detect_overdue_receivables(events: list) -> list[dict]:
    """Filter and classify all overdue receivable events from a batch."""
    results = []
    for event in events:
        if isinstance(event, ReceivableOverdueEvent):
            results.append(classify_receivable(event))
    return results
