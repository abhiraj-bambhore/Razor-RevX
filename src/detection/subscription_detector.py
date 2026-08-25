"""
Subscription failure detector.
Scans for subscription.charged_halted events, checks mandate status, retry eligibility.
"""
from __future__ import annotations

from src.data.schemas import (
    ErrorReason,
    FailureMode,
    RiskTier,
    SubscriptionFailureEvent,
)


# Max mandate retries before we should offer alternative
MAX_MANDATE_RETRIES = 3

# Reasons where mandate retry is safe
MANDATE_RETRYABLE = {
    ErrorReason.GATEWAY_ERROR,
    ErrorReason.NETWORK_ERROR,
    ErrorReason.TIMEOUT,
    ErrorReason.INSUFFICIENT_FUNDS,  # Retryable after a delay
}


def classify_subscription_failure(event: SubscriptionFailureEvent) -> dict:
    """
    Classify a subscription charge failure.

    Returns:
        {
            "event": SubscriptionFailureEvent,
            "failure_mode": FailureMode,
            "mandate_retry_eligible": bool,
            "churn_risk": str,  # low | medium | high | critical
            "urgency": str,
            "recommended_first_action": str,
            "risk_tier": RiskTier,
        }
    """
    reason = event.error.reason

    # Mandate retry eligibility
    mandate_ok = (
        event.mandate_status == "active"
        and event.failed_charge_count < MAX_MANDATE_RETRIES
        and reason in MANDATE_RETRYABLE
    )

    # Churn risk assessment
    if event.failed_charge_count >= 3:
        churn_risk = "critical"
    elif event.plan_tier == "premium" and event.failed_charge_count >= 2:
        churn_risk = "high"
    elif event.failed_charge_count >= 2:
        churn_risk = "medium"
    else:
        churn_risk = "low"

    # Urgency and action
    if mandate_ok and reason in {ErrorReason.GATEWAY_ERROR, ErrorReason.NETWORK_ERROR, ErrorReason.TIMEOUT}:
        urgency = "immediate"
        first_action = "mandate_retry"
    elif mandate_ok:
        urgency = "within_24h"
        first_action = "mandate_retry"
    elif event.failed_charge_count >= MAX_MANDATE_RETRIES:
        urgency = "within_4h"
        first_action = "send_recovery_link"
    else:
        urgency = "within_24h"
        first_action = "send_recovery_link"

    # Risk tier by LTV and plan tier
    if event.plan_tier == "premium" or event.customer_ltv_inr >= 100000:
        risk_tier = RiskTier.CRITICAL
    elif event.charge_amount_inr >= 5000:
        risk_tier = RiskTier.HIGH
    elif event.charge_amount_inr >= 1000:
        risk_tier = RiskTier.MEDIUM
    else:
        risk_tier = RiskTier.LOW

    return {
        "event": event,
        "failure_mode": FailureMode.SUBSCRIPTION_FAILURE,
        "mandate_retry_eligible": mandate_ok,
        "churn_risk": churn_risk,
        "urgency": urgency,
        "recommended_first_action": first_action,
        "risk_tier": risk_tier,
    }


def detect_subscription_failures(events: list) -> list[dict]:
    """Filter and classify all subscription failure events from a batch."""
    results = []
    for event in events:
        if isinstance(event, SubscriptionFailureEvent):
            results.append(classify_subscription_failure(event))
    return results
