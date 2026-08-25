"""
Payment failure detector.
Parses payment.failed events, classifies by error source, assigns urgency.
"""
from __future__ import annotations

from src.data.schemas import (
    ErrorReason,
    ErrorSource,
    FailureMode,
    PaymentFailedEvent,
    RiskTier,
)


# Immediate-retry reasons (transient): don't wait, retry now
TRANSIENT_REASONS = {
    ErrorReason.GATEWAY_ERROR,
    ErrorReason.NETWORK_ERROR,
    ErrorReason.TIMEOUT,
}

# Never-retry reasons: escalate or stop
NEVER_RETRY_REASONS = {
    ErrorReason.FRAUD_FLAGGED,
}

# Customer-action-needed: send recovery link, don't auto-retry
CUSTOMER_ACTION_REASONS = {
    ErrorReason.INSUFFICIENT_FUNDS,
    ErrorReason.INVALID_OTP,
    ErrorReason.PAYMENT_CANCELLED,
    ErrorReason.INVALID_EXPIRY,
    ErrorReason.LIMIT_EXCEEDED,
    ErrorReason.CARD_DECLINED,
}


def classify_payment_failure(event: PaymentFailedEvent) -> dict:
    """
    Classify a payment failure and return structured detection result.

    Returns:
        {
            "event": PaymentFailedEvent,
            "failure_mode": FailureMode,
            "is_retryable": bool,
            "urgency": str,  # immediate | within_1h | within_24h | do_not_retry
            "root_cause_category": str,
            "recommended_first_action": str,
            "risk_tier": RiskTier,
        }
    """
    reason = event.error.reason
    source = event.error.source

    # Determine retryability and urgency
    if reason in NEVER_RETRY_REASONS:
        is_retryable = False
        urgency = "do_not_retry"
        root_cause = "fraud_or_block"
        first_action = "escalate_to_human"
    elif reason in TRANSIENT_REASONS:
        is_retryable = True
        urgency = "immediate"
        root_cause = "transient_infra"
        first_action = "auto_retry"
    elif reason in CUSTOMER_ACTION_REASONS:
        is_retryable = False  # Auto-retry won't help; customer must act
        urgency = "within_1h"
        root_cause = "customer_action_needed"
        first_action = "send_recovery_link"
    else:
        is_retryable = True
        urgency = "within_24h"
        root_cause = "unknown"
        first_action = "send_recovery_link"

    # Risk tier based on amount
    if event.amount_inr >= 50000:
        risk_tier = RiskTier.CRITICAL
    elif event.amount_inr >= 10000:
        risk_tier = RiskTier.HIGH
    elif event.amount_inr >= 2000:
        risk_tier = RiskTier.MEDIUM
    else:
        risk_tier = RiskTier.LOW

    return {
        "event": event,
        "failure_mode": FailureMode.PAYMENT_FAILURE,
        "is_retryable": is_retryable,
        "urgency": urgency,
        "root_cause_category": root_cause,
        "recommended_first_action": first_action,
        "risk_tier": risk_tier,
    }


def detect_payment_failures(events: list) -> list[dict]:
    """Filter and classify all payment failure events from a batch."""
    results = []
    for event in events:
        if isinstance(event, PaymentFailedEvent):
            results.append(classify_payment_failure(event))
    return results
