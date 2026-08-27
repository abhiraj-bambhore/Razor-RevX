"""
Simulates generating and sending a payment recovery link.
In production, this calls Razorpay Payment Links API.
"""
from __future__ import annotations

import random
import uuid
import logging
from datetime import datetime

from src.data.schemas import RecoveryAttempt

logger = logging.getLogger(__name__)

# Recovery link conversion rates by channel
LINK_CONVERSION_RATES: dict[str, float] = {
    "sms": 0.18,       # ~18% click-and-pay
    "email": 0.12,     # ~12% open-and-pay
    "whatsapp": 0.25,  # ~25% — highest conversion in India
}


def send_recovery_link(
    event_id: str,
    customer_email: str,
    customer_phone: str,
    amount_inr: float,
    order_id: str,
    channel: str = "whatsapp",
    attempt_number: int = 1,
    message: str = "",
) -> RecoveryAttempt:
    """
    Simulate sending a payment recovery link.

    Generates a simulated Razorpay payment link and tracks conversion.
    """
    link_id = f"plink_{uuid.uuid4().hex[:14]}"
    simulated_link = f"https://rzp.io/i/{link_id}"

    conversion_rate = LINK_CONVERSION_RATES.get(channel, 0.15)
    # Decay on repeated attempts
    adjusted_rate = conversion_rate * (0.6 ** (attempt_number - 1))

    converted = random.random() < adjusted_rate

    if converted:
        result = "success"
        amount_recovered = amount_inr
        logger.info(
            "[CONVERTED] Recovery link CONVERTED: %s via %s | ₹%.2f | link=%s",
            event_id, channel, amount_inr, simulated_link,
        )
    else:
        result = "pending"
        amount_recovered = 0.0
        logger.info(
            "[SENT] Recovery link SENT: %s via %s | ₹%.2f | link=%s (awaiting conversion)",
            event_id, channel, amount_inr, simulated_link,
        )

    return RecoveryAttempt(
        event_id=event_id,
        attempt_number=attempt_number,
        timestamp=datetime.utcnow(),
        action_taken="send_recovery_link",
        channel=channel,
        result=result,
        amount_recovered_inr=amount_recovered,
        agent_reasoning=(
            f"Recovery link sent via {channel}. Link: {simulated_link}. "
            f"Conversion rate: {adjusted_rate:.1%}. "
            f"Message: {message[:80]}..." if message else f"Recovery link sent via {channel}."
        ),
    )
