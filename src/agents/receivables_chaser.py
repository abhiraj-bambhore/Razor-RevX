"""
B2B receivables chaser agent.
Handles: email reminder → phone follow-up → legal flag → PTP tracking → escalation
"""
from __future__ import annotations

import logging

from src.actions.log_promise_to_pay import log_promise_to_pay
from src.actions.send_nudge import send_nudge
from src.actions.send_recovery_link import send_recovery_link
from src.audit.audit_trail import AuditTrail
from src.data.schemas import (
    AuditEntry,
    FailureMode,
    ReceivableOverdueEvent,
    RecoveryAttempt,
    RiskAssessment,
)
from src.utils.llm import llm_compose_recovery_message

logger = logging.getLogger(__name__)

AGENT_NAME = "receivables_chaser_agent"


def run_receivables_chaser(
    event: ReceivableOverdueEvent,
    detection: dict,
    risk: RiskAssessment,
    audit: AuditTrail,
    config: dict,
) -> RecoveryAttempt:
    """
    Execute B2B receivables recovery workflow.

    Flow:
        1. Check stopping rules (opt-out, max attempts, active dispute)
        2. By aging bucket:
           - 0-30 days: email reminder
           - 31-60 days: phone follow-up → PTP recording
           - 61-90 days: firm follow-up → legal flag
           - 90+ days: escalate to human (write-off risk)
    """
    max_attempts = config.get("stopping_rules", {}).get("max_attempts_per_case", 3)
    prior_attempts = audit.get_attempts_for_event(event.event_id) + event.previous_attempts

    aging_risk = detection.get("aging_risk", "low_risk")

    # ── Stopping rules ───────────────────────────────────────────────────
    if event.opted_out:
        attempt = _stopped_attempt(event, "customer_opted_out", prior_attempts + 1)
        _log_audit(audit, event, risk, attempt, ["opt_out_enforced"])
        return attempt

    if prior_attempts >= max_attempts:
        attempt = _stopped_attempt(event, "max_attempts_reached", prior_attempts + 1)
        _log_audit(audit, event, risk, attempt, ["max_attempts_enforced"])
        return attempt

    # Active dispute: hold all dunning
    if event.has_active_dispute:
        attempt = _escalate_attempt(
            event, prior_attempts + 1,
            f"Active dispute on invoice {event.invoice_number} — dunning paused, escalated"
        )
        _log_audit(audit, event, risk, attempt, ["active_dispute_hold", "human_escalation"])
        return attempt

    # ── Write-off risk: immediate escalation ─────────────────────────────
    if aging_risk == "write_off_risk":
        attempt = _escalate_attempt(
            event, prior_attempts + 1,
            f"Invoice {event.invoice_number} is {event.days_overdue} days overdue (90+). "
            f"Write-off risk. Amount: ₹{event.amount_inr:,.2f}. Company: {event.company_name}"
        )
        _log_audit(audit, event, risk, attempt, ["write_off_risk", "human_escalation"])
        return attempt

    # ── Human review for very high-value invoices ────────────────────────
    if risk.needs_human_review:
        attempt = _escalate_attempt(
            event, prior_attempts + 1,
            f"High-value receivable (₹{event.amount_inr:,.2f}) — human review"
        )
        _log_audit(audit, event, risk, attempt, ["high_value_escalation"])
        return attempt

    # ── High risk (61-90 days): phone + PTP ──────────────────────────────
    if aging_risk == "high_risk":
        # Simulate phone follow-up that may result in a PTP
        attempt = log_promise_to_pay(
            event_id=event.event_id,
            invoice_id=event.invoice_id,
            amount_inr=event.amount_inr,
            company_name=event.company_name,
            contact_name=event.contact_name,
            attempt_number=prior_attempts + 1,
        )
        _log_audit(audit, event, risk, attempt, ["phone_followup", "promise_to_pay", "legal_warning"])
        return attempt

    # ── Medium risk (31-60 days): phone follow-up ────────────────────────
    if aging_risk == "medium_risk":
        message = llm_compose_recovery_message(
            event_type="receivable_overdue",
            customer_name=event.contact_name,
            amount=event.amount_inr,
            failure_reason=f"{event.days_overdue} days overdue",
            channel="phone",
        )

        attempt = send_nudge(
            event_id=event.event_id,
            customer_phone=event.contact_phone,
            customer_email=event.contact_email,
            amount_inr=event.amount_inr,
            message=message,
            channel="phone",
            attempt_number=prior_attempts + 1,
        )
        _log_audit(audit, event, risk, attempt, ["phone_followup"])
        return attempt

    # ── Low risk (0-30 days): email reminder ─────────────────────────────
    message = llm_compose_recovery_message(
        event_type="receivable_overdue",
        customer_name=event.contact_name,
        amount=event.amount_inr,
        failure_reason=f"{event.days_overdue} days overdue",
        channel="email",
    )

    attempt = send_recovery_link(
        event_id=event.event_id,
        customer_email=event.contact_email,
        customer_phone=event.contact_phone,
        amount_inr=event.amount_inr,
        order_id=event.invoice_id,
        channel="email",
        attempt_number=prior_attempts + 1,
        message=message,
    )
    attempt.action_taken = "email_reminder"
    _log_audit(audit, event, risk, attempt, ["email_reminder"])
    return attempt


def _stopped_attempt(event: ReceivableOverdueEvent, rule: str, attempt_num: int) -> RecoveryAttempt:
    logger.info("[STOPPED] event=%s | rule=%s", event.event_id, rule)
    return RecoveryAttempt(
        event_id=event.event_id,
        attempt_number=attempt_num,
        action_taken="stopped",
        channel="none",
        result="stopped",
        amount_recovered_inr=0.0,
        stopping_rule_triggered=rule,
        agent_reasoning=f"Receivables recovery stopped: {rule}",
    )


def _escalate_attempt(event: ReceivableOverdueEvent, attempt_num: int, reason: str) -> RecoveryAttempt:
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
    event: ReceivableOverdueEvent,
    risk: RiskAssessment,
    attempt: RecoveryAttempt,
    flags: list[str],
):
    entry = AuditEntry(
        event_id=event.event_id,
        failure_mode=FailureMode.RECEIVABLE_OVERDUE,
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
