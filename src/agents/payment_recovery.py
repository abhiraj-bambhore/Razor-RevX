"""
Payment failure recovery agent.
Handles: retry eligibility → auto-retry → recovery link → escalate
"""
from __future__ import annotations

import logging
from typing import Any

from src.actions.retry_payment import retry_payment
from src.actions.send_nudge import send_nudge
from src.actions.send_recovery_link import send_recovery_link
from src.audit.audit_trail import AuditTrail
from src.data.schemas import (
    AuditEntry,
    ErrorReason,
    FailureMode,
    PaymentFailedEvent,
    RecoveryAttempt,
    RecoveryStatus,
    RiskAssessment,
)
from src.utils.llm import llm_compose_recovery_message

logger = logging.getLogger(__name__)

AGENT_NAME = "payment_recovery_agent"

# Reasons where auto-retry is the right first move
AUTO_RETRY_REASONS = {
    ErrorReason.GATEWAY_ERROR,
    ErrorReason.NETWORK_ERROR,
    ErrorReason.TIMEOUT,
}


def run_payment_recovery(
    event: PaymentFailedEvent,
    detection: dict,
    risk: RiskAssessment,
    audit: AuditTrail,
    config: dict,
) -> RecoveryAttempt:
    """
    Execute payment failure recovery workflow.

    Flow:
        1. Check stopping rules (max attempts, opt-out)
        2. If transient error → auto-retry
        3. If customer error → send recovery link
        4. If high risk → escalate to human
    """
    max_attempts = config.get("stopping_rules", {}).get("max_attempts_per_case", 3)

    # ── Stopping rules check ─────────────────────────────────────────────
    prior_attempts = audit.get_attempts_for_event(event.event_id) + event.previous_attempts

    if event.opted_out:
        attempt = _stopped_attempt(event, "customer_opted_out", prior_attempts + 1)
        _log_audit(audit, event, risk, attempt, ["opt_out_enforced"])
        return attempt

    if prior_attempts >= max_attempts:
        attempt = _stopped_attempt(event, "max_attempts_reached", prior_attempts + 1)
        _log_audit(audit, event, risk, attempt, ["max_attempts_enforced"])
        return attempt

    # ── Fraud: never retry, always escalate ──────────────────────────────
    if event.error.reason == ErrorReason.FRAUD_FLAGGED:
        attempt = _escalate_attempt(event, prior_attempts + 1, "Fraud flagged — escalated to human review")
        _log_audit(audit, event, risk, attempt, ["fraud_flag", "human_escalation"])
        return attempt

    # ── Human review for high-risk ────────────────────────────────────────
    if risk.needs_human_review:
        attempt = _escalate_attempt(
            event, prior_attempts + 1,
            f"High-value case (₹{event.amount_inr:,.2f}) or risk score {risk.risk_score} — human review required"
        )
        _log_audit(audit, event, risk, attempt, ["human_review_threshold"])
        return attempt

    # ── Auto-retry for transient errors ──────────────────────────────────
    if event.error.reason in AUTO_RETRY_REASONS:
        attempt = retry_payment(
            event_id=event.event_id,
            payment_id=event.payment_id,
            amount_inr=event.amount_inr,
            failure_reason=event.error.reason,
            attempt_number=prior_attempts + 1,
        )
        _log_audit(audit, event, risk, attempt, ["auto_retry"])
        return attempt

    # Select channel: Voice call ONLY for high/critical risk tier (risk_score >= 65), WhatsApp for low/medium risk
    channel = "voice" if risk.risk_score >= 65 else "whatsapp"

    message = llm_compose_recovery_message(
        event_type="payment_failure",
        customer_name=event.customer_email.split("@")[0],
        amount=event.amount_inr,
        failure_reason=event.error.reason.value,
        channel=channel,
    )

    attempt = send_recovery_link(
        event_id=event.event_id,
        customer_email=event.customer_email,
        customer_phone=event.customer_phone,
        amount_inr=event.amount_inr,
        order_id=event.order_id,
        channel=channel,
        attempt_number=prior_attempts + 1,
        message=message,
    )
    _log_audit(audit, event, risk, attempt, [f"{channel}_recovery_sent"])
    return attempt


def _stopped_attempt(event: PaymentFailedEvent, rule: str, attempt_num: int) -> RecoveryAttempt:
    logger.info("[STOPPED] event=%s | rule=%s", event.event_id, rule)
    return RecoveryAttempt(
        event_id=event.event_id,
        attempt_number=attempt_num,
        action_taken="stopped",
        channel="none",
        result="stopped",
        amount_recovered_inr=0.0,
        stopping_rule_triggered=rule,
        agent_reasoning=f"Recovery stopped: {rule}",
    )


def _escalate_attempt(event: PaymentFailedEvent, attempt_num: int, reason: str) -> RecoveryAttempt:
    logger.info("[ESCALATED] event=%s | reason=%s", event.event_id, reason)
    return RecoveryAttempt(
        event_id=event.event_id,
        attempt_number=attempt_num,
        action_taken="escalate_to_human",
        channel="human_review",
        result="escalated",
        amount_recovered_inr=0.0,
        agent_reasoning=reason,
    )


def _log_audit(
    audit: AuditTrail,
    event: PaymentFailedEvent,
    risk: RiskAssessment,
    attempt: RecoveryAttempt,
    flags: list[str],
):
    entry = AuditEntry(
        event_id=event.event_id,
        failure_mode=FailureMode.PAYMENT_FAILURE,
        agent_name=AGENT_NAME,
        action_taken=attempt.action_taken,
        result=attempt.result,
        amount_at_risk_inr=event.amount_inr,
        amount_recovered_inr=attempt.amount_recovered_inr,
        risk_score=risk.risk_score,
        llm_reasoning=risk.reasoning + " | " + attempt.agent_reasoning,
        compliance_flags=flags,
        stopping_rule_triggered=attempt.stopping_rule_triggered,
        escalated_to_human=(attempt.action_taken == "escalate_to_human"),
    )
    audit.log(entry)
