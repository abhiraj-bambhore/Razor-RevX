"""
Checkout abandonment recovery agent.
Handles: recovery link → WhatsApp nudge → time-decay drop
"""
from __future__ import annotations

import logging

from src.actions.send_nudge import send_nudge
from src.actions.send_recovery_link import send_recovery_link
from src.audit.audit_trail import AuditTrail
from src.data.schemas import (
    AuditEntry,
    CheckoutAbandonedEvent,
    FailureMode,
    RecoveryAttempt,
    RiskAssessment,
)
from src.utils.llm import llm_compose_recovery_message

logger = logging.getLogger(__name__)

AGENT_NAME = "checkout_recovery_agent"

# Minimum intent score to bother recovering (below this, it's not worth contacting)
MIN_INTENT_THRESHOLD = 0.25

# Minimum time decay factor (if too old, stop)
MIN_TIME_DECAY = 0.15


def run_checkout_recovery(
    event: CheckoutAbandonedEvent,
    detection: dict,
    risk: RiskAssessment,
    audit: AuditTrail,
    config: dict,
) -> RecoveryAttempt:
    """
    Execute checkout abandonment recovery workflow.

    Flow:
        1. Check stopping rules
        2. If high intent (OTP/review stage) → send recovery link immediately
        3. If medium intent → send WhatsApp nudge
        4. If low intent or time-decayed → drop (not worth the contact)
    """
    max_attempts = config.get("stopping_rules", {}).get("max_attempts_per_case", 3)
    prior_attempts = audit.get_attempts_for_event(event.event_id) + event.previous_attempts

    intent_score = detection.get("intent_score", 0.5)
    time_decay = detection.get("time_decay_factor", 1.0)

    # ── Stopping rules ───────────────────────────────────────────────────
    if event.opted_out:
        attempt = _stopped_attempt(event, "customer_opted_out", prior_attempts + 1)
        _log_audit(audit, event, risk, attempt, ["opt_out_enforced"])
        return attempt

    if prior_attempts >= max_attempts:
        attempt = _stopped_attempt(event, "max_attempts_reached", prior_attempts + 1)
        _log_audit(audit, event, risk, attempt, ["max_attempts_enforced"])
        return attempt

    if intent_score < MIN_INTENT_THRESHOLD:
        attempt = _stopped_attempt(event, "low_intent_score", prior_attempts + 1)
        _log_audit(audit, event, risk, attempt, ["low_intent_drop"])
        return attempt

    if time_decay < MIN_TIME_DECAY:
        attempt = _stopped_attempt(event, "time_decay_expired", prior_attempts + 1)
        _log_audit(audit, event, risk, attempt, ["time_decay_drop"])
        return attempt

    # ── Human review for high-value carts ────────────────────────────────
    if risk.needs_human_review:
        attempt = _escalate_attempt(
            event, prior_attempts + 1,
            f"High-value cart (₹{event.amount_inr:,.2f}) — human review"
        )
        _log_audit(audit, event, risk, attempt, ["human_review_threshold"])
        return attempt

    # ── High intent: recovery link ───────────────────────────────────────
    if intent_score >= 0.7 or event.abandonment_stage in ("otp", "review"):
        message = llm_compose_recovery_message(
            event_type="checkout_abandonment",
            customer_name=event.customer_email.split("@")[0],
            amount=event.amount_inr,
            failure_reason=f"abandoned at {event.abandonment_stage}",
            channel="whatsapp",
        )

        attempt = send_recovery_link(
            event_id=event.event_id,
            customer_email=event.customer_email,
            customer_phone=event.customer_phone,
            amount_inr=event.amount_inr,
            order_id=event.order_id,
            channel="whatsapp",
            attempt_number=prior_attempts + 1,
            message=message,
        )
        _log_audit(audit, event, risk, attempt, ["high_intent_recovery_link"])
        return attempt

    # ── Medium intent: nudge ─────────────────────────────────────────────
    message = llm_compose_recovery_message(
        event_type="checkout_abandonment",
        customer_name=event.customer_email.split("@")[0],
        amount=event.amount_inr,
        failure_reason=f"abandoned at {event.abandonment_stage}",
        channel="sms",
    )

    attempt = send_nudge(
        event_id=event.event_id,
        customer_phone=event.customer_phone,
        customer_email=event.customer_email,
        amount_inr=event.amount_inr,
        message=message,
        channel="sms",
        attempt_number=prior_attempts + 1,
    )
    _log_audit(audit, event, risk, attempt, ["medium_intent_nudge"])
    return attempt


def _stopped_attempt(event: CheckoutAbandonedEvent, rule: str, attempt_num: int) -> RecoveryAttempt:
    logger.info("[STOPPED] event=%s | rule=%s", event.event_id, rule)
    return RecoveryAttempt(
        event_id=event.event_id,
        attempt_number=attempt_num,
        action_taken="stopped",
        channel="none",
        result="stopped",
        amount_recovered_inr=0.0,
        stopping_rule_triggered=rule,
        agent_reasoning=f"Checkout recovery stopped: {rule}",
    )


def _escalate_attempt(event: CheckoutAbandonedEvent, attempt_num: int, reason: str) -> RecoveryAttempt:
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
    event: CheckoutAbandonedEvent,
    risk: RiskAssessment,
    attempt: RecoveryAttempt,
    flags: list[str],
):
    entry = AuditEntry(
        event_id=event.event_id,
        failure_mode=FailureMode.CHECKOUT_ABANDONMENT,
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
