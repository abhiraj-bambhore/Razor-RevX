"""
Promise-to-Pay logger for B2B receivables.
Records PTP agreements and schedules follow-up.
"""
from __future__ import annotations

import random
import logging
from datetime import datetime, timedelta

from src.data.schemas import RecoveryAttempt

logger = logging.getLogger(__name__)


def log_promise_to_pay(
    event_id: str,
    invoice_id: str,
    amount_inr: float,
    company_name: str,
    contact_name: str,
    attempt_number: int = 1,
    promised_date: datetime | None = None,
) -> RecoveryAttempt:
    """
    Simulate recording a Promise-to-Pay agreement.

    In B2B collections, customers often agree to pay by a specific date.
    This records that promise and schedules follow-up.
    """
    if promised_date is None:
        # Simulate: customer promises to pay in 3-14 days
        days_out = random.randint(3, 14)
        promised_date = datetime.utcnow() + timedelta(days=days_out)

    # Simulate PTP honor rate: ~60% of promises are kept
    ptp_honored = random.random() < 0.60

    if ptp_honored:
        result = "success"
        amount_recovered = amount_inr
        logger.info(
            "✅ PTP HONORED: %s | %s | ₹%.2f | promised: %s",
            invoice_id, company_name, amount_inr, promised_date.strftime("%Y-%m-%d"),
        )
    else:
        result = "pending"
        amount_recovered = 0.0
        logger.info(
            "📝 PTP RECORDED: %s | %s | ₹%.2f | promised: %s (follow-up scheduled)",
            invoice_id, company_name, amount_inr, promised_date.strftime("%Y-%m-%d"),
        )

    return RecoveryAttempt(
        event_id=event_id,
        attempt_number=attempt_number,
        timestamp=datetime.utcnow(),
        action_taken="log_promise_to_pay",
        channel="phone",
        result=result,
        amount_recovered_inr=amount_recovered,
        agent_reasoning=(
            f"PTP logged for {company_name}. Invoice {invoice_id}. "
            f"Amount: ₹{amount_inr:,.2f}. Promised date: {promised_date.strftime('%Y-%m-%d')}. "
            f"Follow-up: {(promised_date + timedelta(days=1)).strftime('%Y-%m-%d')}"
        ),
    )
