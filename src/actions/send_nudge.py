"""
Simulates sending SMS/WhatsApp nudge for recovery.
"""
from __future__ import annotations

import random
import logging
from datetime import datetime

from src.data.schemas import RecoveryAttempt

logger = logging.getLogger(__name__)

# Nudge engagement rates
NUDGE_ENGAGEMENT_RATES: dict[str, float] = {
    "sms": 0.10,
    "whatsapp": 0.20,
    "email": 0.08,
    "phone": 0.35,
}


def send_nudge(
    event_id: str,
    customer_phone: str,
    customer_email: str,
    amount_inr: float,
    message: str,
    channel: str = "whatsapp",
    attempt_number: int = 1,
) -> RecoveryAttempt:
    """
    Simulate sending a nudge (reminder without payment link).

    Nudges are softer touches — cart reminders, subscription renewal reminders.
    """
    link_id = f"plink_{uuid.uuid4().hex[:10]}"
    simulated_link = f"https://rzp.io/i/{link_id}"

    if "{link}" in message:
        message = message.replace("{link}", simulated_link)

    engagement_rate = NUDGE_ENGAGEMENT_RATES.get(channel, 0.10)
    adjusted_rate = engagement_rate * (0.5 ** (attempt_number - 1))

    engaged = random.random() < adjusted_rate

    if engaged:
        # Engagement means customer returned and paid
        result = "success"
        amount_recovered = amount_inr
        logger.info(
            "[CONVERSION] Nudge led to CONVERSION: %s via %s | ₹%.2f",
            event_id, channel, amount_inr,
        )
    else:
        result = "pending"
        amount_recovered = 0.0
        logger.info(
            "[SENT] Nudge SENT: %s via %s | ₹%.2f (awaiting engagement)",
            event_id, channel, amount_inr,
        )

    return RecoveryAttempt(
        event_id=event_id,
        attempt_number=attempt_number,
        timestamp=datetime.utcnow(),
        action_taken="send_nudge",
        channel=channel,
        result=result,
        amount_recovered_inr=amount_recovered,
        agent_reasoning=message if message else f"Nudge via {channel}: {simulated_link}",
    )
