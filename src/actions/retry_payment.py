"""
Simulates payment retry via Razorpay API.
In production, this calls Razorpay's retry/re-auth endpoints.
Simulation uses realistic success rates based on failure reason.
"""
from __future__ import annotations

import random
import logging
from datetime import datetime

from src.data.schemas import ErrorReason, RecoveryAttempt

logger = logging.getLogger(__name__)

# Success rates for auto-retry by failure reason
RETRY_SUCCESS_RATES: dict[ErrorReason, float] = {
    ErrorReason.GATEWAY_ERROR: 0.75,      # Transient — high retry success
    ErrorReason.NETWORK_ERROR: 0.80,      # Transient — very high
    ErrorReason.TIMEOUT: 0.70,            # Transient — high
    ErrorReason.INSUFFICIENT_FUNDS: 0.15, # Rarely works immediately
    ErrorReason.INVALID_OTP: 0.05,        # Customer must re-enter
    ErrorReason.CARD_DECLINED: 0.10,      # Bank-level block
    ErrorReason.PAYMENT_CANCELLED: 0.02,  # Customer chose to cancel
    ErrorReason.FRAUD_FLAGGED: 0.00,      # Never retry
    ErrorReason.INVALID_EXPIRY: 0.00,     # Card info wrong
    ErrorReason.LIMIT_EXCEEDED: 0.05,     # Rarely clears quickly
}


def retry_payment(
    event_id: str,
    payment_id: str,
    amount_inr: float,
    failure_reason: ErrorReason,
    attempt_number: int = 1,
) -> RecoveryAttempt:
    """
    Simulate a payment retry.

    Returns a RecoveryAttempt with the outcome.
    """
    success_rate = RETRY_SUCCESS_RATES.get(failure_reason, 0.20)

    # Reduce success rate on repeated attempts
    adjusted_rate = success_rate * (0.7 ** (attempt_number - 1))
    succeeded = random.random() < adjusted_rate

    if succeeded:
        result = "success"
        amount_recovered = amount_inr
        logger.info(
            "[RETRY] status=success | payment=%s | amount=INR %.2f",
            payment_id, amount_inr,
        )
    else:
        result = "failed"
        amount_recovered = 0.0
        logger.info(
            "[RETRY] status=failed | payment=%s | reason=%s",
            payment_id, failure_reason.value,
        )

    return RecoveryAttempt(
        event_id=event_id,
        attempt_number=attempt_number,
        timestamp=datetime.utcnow(),
        action_taken="auto_retry",
        channel="razorpay_api",
        result=result,
        amount_recovered_inr=amount_recovered,
        agent_reasoning=f"Auto-retry for {failure_reason.value}. Success rate: {adjusted_rate:.1%}",
    )
