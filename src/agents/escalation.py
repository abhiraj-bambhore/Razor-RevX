"""
Escalation handler.
Manages human-in-the-loop review for high-risk/high-value cases.
In production, this would route to a human review dashboard.
Here it simulates the human decision with realistic approval rates.
"""
from __future__ import annotations

import random
import logging
from datetime import datetime

from src.data.schemas import RecoveryAttempt

logger = logging.getLogger(__name__)


# Simulated human approval rates by case type
HUMAN_APPROVAL_RATES = {
    "fraud_flag": 0.10,          # Rarely approved for retry
    "high_value": 0.70,          # Usually approved with caution
    "premium_churn": 0.85,       # Almost always approved — too valuable to lose
    "write_off_risk": 0.30,      # Often referred to legal
    "active_dispute": 0.05,      # Wait for dispute resolution
    "default": 0.50,
}


def simulate_human_review(
    event_id: str,
    amount_inr: float,
    escalation_reason: str,
    agent_reasoning: str,
) -> dict:
    """
    Simulate a human reviewing an escalated case.

    Returns:
        {
            "approved": bool,
            "human_decision": str,  # "approved_retry" | "approved_contact" | "rejected" | "referred_legal"
            "notes": str,
        }
    """
    # Determine case type for approval rate
    reason_lower = escalation_reason.lower()
    if "fraud" in reason_lower:
        case_type = "fraud_flag"
    elif "write_off" in reason_lower or "90+" in reason_lower:
        case_type = "write_off_risk"
    elif "dispute" in reason_lower:
        case_type = "active_dispute"
    elif "premium" in reason_lower or "churn" in reason_lower:
        case_type = "premium_churn"
    elif amount_inr >= 50000:
        case_type = "high_value"
    else:
        case_type = "default"

    approval_rate = HUMAN_APPROVAL_RATES.get(case_type, 0.50)
    approved = random.random() < approval_rate

    if approved:
        if case_type == "premium_churn":
            decision = "approved_contact"
            notes = f"Approved: Contact customer with retention offer. Case: {case_type}"
        elif case_type == "high_value":
            decision = "approved_retry"
            notes = f"Approved: Proceed with recovery. Amount ₹{amount_inr:,.2f} verified."
        else:
            decision = "approved_contact"
            notes = f"Approved: Proceed with caution. Case: {case_type}"
    else:
        if case_type == "fraud_flag":
            decision = "rejected"
            notes = "Rejected: Fraud confirmed. Block customer. Do not retry."
        elif case_type == "write_off_risk":
            decision = "referred_legal"
            notes = f"Referred to legal department. Invoice overdue >90 days. Amount: ₹{amount_inr:,.2f}"
        elif case_type == "active_dispute":
            decision = "rejected"
            notes = "On hold: Active dispute. Await resolution before any dunning."
        else:
            decision = "rejected"
            notes = f"Rejected: Not approved for further contact. Case: {case_type}"

    logger.info(
        "👤 HUMAN REVIEW: %s | decision=%s | %s",
        event_id, decision, notes,
    )

    return {
        "approved": approved,
        "human_decision": decision,
        "notes": notes,
    }
