"""
Checkout abandonment detector.
Detects abandoned checkouts and classifies by abandonment stage and intent.
"""
from __future__ import annotations

from src.data.schemas import (
    CheckoutAbandonedEvent,
    FailureMode,
    RiskTier,
)


# Stages ordered by purchase intent (higher = closer to paying)
STAGE_INTENT_SCORE: dict[str, float] = {
    "address": 0.3,
    "payment_method": 0.5,
    "otp": 0.8,
    "review": 0.9,
}


def classify_checkout_abandonment(event: CheckoutAbandonedEvent) -> dict:
    """
    Classify a checkout abandonment event.

    Returns:
        {
            "event": CheckoutAbandonedEvent,
            "failure_mode": FailureMode,
            "intent_score": float,  # 0-1, how close to paying
            "urgency": str,
            "recommended_first_action": str,
            "risk_tier": RiskTier,
            "time_decay_factor": float,
        }
    """
    intent = STAGE_INTENT_SCORE.get(event.abandonment_stage, 0.5)

    # Time spent: more time = more invested = higher recovery chance
    if event.time_spent_seconds > 180:
        intent = min(1.0, intent + 0.1)

    # Urgency: OTP stage = they were about to pay, act fast
    if event.abandonment_stage == "otp":
        urgency = "immediate"
        first_action = "send_recovery_link"
    elif event.abandonment_stage == "review":
        urgency = "within_30min"
        first_action = "send_recovery_link"
    elif event.abandonment_stage == "payment_method":
        urgency = "within_4h"
        first_action = "send_nudge"
    else:
        urgency = "within_24h"
        first_action = "send_nudge"

    # Risk tier by amount
    if event.amount_inr >= 25000:
        risk_tier = RiskTier.CRITICAL
    elif event.amount_inr >= 5000:
        risk_tier = RiskTier.HIGH
    elif event.amount_inr >= 1000:
        risk_tier = RiskTier.MEDIUM
    else:
        risk_tier = RiskTier.LOW

    # Time decay: older abandonments are less recoverable
    hours_since = (
        __import__("datetime").datetime.utcnow() - event.created_at
    ).total_seconds() / 3600
    time_decay = max(0.1, 1.0 - (hours_since / 48.0))

    return {
        "event": event,
        "failure_mode": FailureMode.CHECKOUT_ABANDONMENT,
        "intent_score": round(intent, 2),
        "urgency": urgency,
        "recommended_first_action": first_action,
        "risk_tier": risk_tier,
        "time_decay_factor": round(time_decay, 3),
    }


def detect_checkout_abandonments(events: list) -> list[dict]:
    """Filter and classify all checkout abandoned events from a batch."""
    results = []
    for event in events:
        if isinstance(event, CheckoutAbandonedEvent):
            results.append(classify_checkout_abandonment(event))
    return results
