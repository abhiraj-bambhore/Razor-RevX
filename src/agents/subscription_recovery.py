"""
Subscription recovery agent.
Handles: mandate retry → recovery link → plan downgrade offer → cancellation prevention
"""
from __future__ import annotations

import logging

from src.actions.retry_payment import retry_payment
from src.actions.send_nudge import send_nudge
from src.actions.send_recovery_link import send_recovery_link
from src.audit.audit_trail import AuditTrail
from src.data.schemas import (
    AuditEntry,
    FailureMode,
    RecoveryAttempt,
    RiskAssessment,
    SubscriptionFailureEvent,
)
from src.utils.llm import llm_compose_recovery_message

logger = logging.getLogger(__name__)

AGENT_NAME = "subscription_recovery_agent"


def run_subscription_recovery(
    event: SubscriptionFailureEvent,
    detection: dict,
    risk: RiskAssessment,
    audit: AuditTrail,
    config: dict,
) -> RecoveryAttempt:
    """
    Execute subscription failure recovery workflow.

    Flow:
        1. Check stopping rules
        2. If mandate active + retryable → mandate retry
        3. If mandate exhausted → send recovery link with plan options
        4. If high churn risk → offer plan downgrade
        5. If critical → escalate to human
    """
    max_attempts = config.get("stopping_rules", {}).get("max_attempts_per_case", 3)
    prior_attempts = audit.get_attempts_for_event(event.event_id) + event.previous_attempts

    mandate_retry_eligible = detection.get("mandate_retry_eligible", False)
    churn_risk = detection.get("churn_risk", "medium")

    # ── Stopping rules ───────────────────────────────────────────────────
    if event.opted_out:
        attempt = _stopped_attempt(event, "customer_opted_out", prior_attempts + 1)
        _log_audit(audit, event, risk, attempt, ["opt_out_enforced"])
        return attempt

    if prior_attempts >= max_attempts:
        attempt = _stopped_attempt(event, "max_attempts_reached", prior_attempts + 1)
        _log_audit(audit, event, risk, attempt, ["max_attempts_enforced"])
        return attempt

    # ── Human review for premium/high-value ──────────────────────────────
    if risk.needs_human_review or (event.plan_tier == "premium" and churn_risk in ("high", "critical")):
        attempt = _escalate_attempt(
            event, prior_attempts + 1,
            f"Premium subscriber churn risk={churn_risk} | ₹{event.charge_amount_inr:,.2f} — human review"
        )
        _log_audit(audit, event, risk, attempt, ["premium_churn_escalation"])
        return attempt

    # ── Mandate retry ────────────────────────────────────────────────────
    if mandate_retry_eligible:
        attempt = retry_payment(
            event_id=event.event_id,
            payment_id=event.subscription_id,
            amount_inr=event.charge_amount_inr,
            failure_reason=event.error.reason,
            attempt_number=prior_attempts + 1,
        )
        attempt.action_taken = "mandate_retry"
        attempt.channel = "mandate"
        _log_audit(audit, event, risk, attempt, ["mandate_retry"])
        return attempt

    # ── Plan downgrade offer for high churn risk ─────────────────────────
    if churn_risk in ("high", "critical") and event.plan_tier != "basic":
        message = llm_compose_recovery_message(
            event_type="subscription_failure",
            customer_name=event.customer_email.split("@")[0],
            amount=event.charge_amount_inr,
            failure_reason=f"{event.error.reason.value} | churn_risk={churn_risk}",
            channel="email",
        )

        # Simulate: downgrade offer sometimes converts
        attempt = send_recovery_link(
            event_id=event.event_id,
            customer_email=event.customer_email,
            customer_phone=event.customer_phone,
            amount_inr=event.charge_amount_inr * 0.6,  # Discounted amount for lower tier
            order_id=event.subscription_id,
            channel="email",
            attempt_number=prior_attempts + 1,
            message=f"Plan downgrade offer: {message}",
        )
        attempt.action_taken = "plan_downgrade_offer"
        _log_audit(audit, event, risk, attempt, ["plan_downgrade_offer", "churn_prevention"])
        return attempt

    # ── Standard recovery link ───────────────────────────────────────────
    message = llm_compose_recovery_message(
        event_type="subscription_failure",
        customer_name=event.customer_email.split("@")[0],
        amount=event.charge_amount_inr,
        failure_reason=event.error.reason.value,
        channel="whatsapp",
    )

    attempt = send_recovery_link(
        event_id=event.event_id,
        customer_email=event.customer_email,
        customer_phone=event.customer_phone,
        amount_inr=event.charge_amount_inr,
        order_id=event.subscription_id,
        channel="whatsapp",
        attempt_number=prior_attempts + 1,
        message=message,
    )
    _log_audit(audit, event, risk, attempt, ["recovery_link_sent"])
    return attempt


def _stopped_attempt(event: SubscriptionFailureEvent, rule: str, attempt_num: int) -> RecoveryAttempt:
    logger.info("[STOPPED] event=%s | rule=%s", event.event_id, rule)
    return RecoveryAttempt(
        event_id=event.event_id,
        attempt_number=attempt_num,
        action_taken="stopped",
        channel="none",
        result="stopped",
        amount_recovered_inr=0.0,
        stopping_rule_triggered=rule,
        agent_reasoning=f"Subscription recovery stopped: {rule}",
    )


def _escalate_attempt(event: SubscriptionFailureEvent, attempt_num: int, reason: str) -> RecoveryAttempt:
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
    event: SubscriptionFailureEvent,
    risk: RiskAssessment,
    attempt: RecoveryAttempt,
    flags: list[str],
):
    entry = AuditEntry(
        event_id=event.event_id,
        failure_mode=FailureMode.SUBSCRIPTION_FAILURE,
        agent_name=AGENT_NAME,
        action_taken=attempt.action_taken,
        result=attempt.result,
        amount_at_risk_inr=event.charge_amount_inr,
        amount_recovered_inr=attempt.amount_recovered_inr,
        risk_score=risk.risk_score,
        llm_reasoning=risk.reasoning + " | " + attempt.agent_reasoning,
        compliance_flags=flags,
        stopping_rule_triggered=attempt.stopping_rule_triggered,
        escalated_to_human=(attempt.action_taken == "escalate_to_human"),
    )
    audit.log(entry)
