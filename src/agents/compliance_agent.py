"""
Compliance Gate Agent — Independent validation layer.

Every proposed recovery action passes through this agent BEFORE execution.
It enforces:
    1. Stopping rules (max attempts, opt-out, cooldown)
    2. Escalation thresholds (₹50k+, risk ≥ 85, premium tier)
    3. Regulatory compliance (DNC lists, dispute holds)
    4. Can BLOCK, MODIFY, or APPROVE any action
"""
from __future__ import annotations

import logging
from typing import Any

from src.audit.audit_trail import AuditTrail
from src.data.schemas import (
    AgentMessage,
    CheckoutAbandonedEvent,
    ComplianceVerdict,
    PaymentFailedEvent,
    ReceivableOverdueEvent,
    RecoveryAttempt,
    RiskAssessment,
    SubscriptionFailureEvent,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "compliance_gate_agent"


def validate_action(
    event: Any,
    proposed_attempt: RecoveryAttempt,
    risk: RiskAssessment,
    audit: AuditTrail,
    config: dict,
    agent_messages: list[AgentMessage],
) -> ComplianceVerdict:
    """
    Validate a proposed recovery action against all compliance rules.

    This agent runs AFTER the specialist agent proposes an action
    but BEFORE execution/audit logging.

    Returns:
        ComplianceVerdict — approved, modified, or blocked.
    """
    violations: list[str] = []
    max_attempts = config.get("stopping_rules", {}).get("max_attempts_per_case", 3)
    modified_action = proposed_attempt.action_taken

    # ── Rule 1: Opt-out check ────────────────────────────────────────────
    opted_out = getattr(event, "opted_out", False)
    if opted_out and proposed_attempt.action_taken not in ("stopped", "escalate_to_human"):
        violations.append("OPTED_OUT: Customer has opted out of communications")
        modified_action = "stopped"

    # ── Rule 2: Max attempts check ───────────────────────────────────────
    prior_attempts = audit.get_attempts_for_event(event.event_id)
    prev_attempts = getattr(event, "previous_attempts", 0)
    total_attempts = prior_attempts + prev_attempts

    if total_attempts >= max_attempts and proposed_attempt.action_taken not in ("stopped", "escalate_to_human"):
        violations.append(f"MAX_ATTEMPTS: {total_attempts} >= {max_attempts} limit")
        modified_action = "stopped"

    # ── Rule 3: High-value escalation ────────────────────────────────────
    amount = _get_amount(event)
    amount_threshold = config.get("escalation", {}).get("amount_threshold_inr", 50000)

    if amount >= amount_threshold and proposed_attempt.action_taken not in ("escalate_to_human", "stopped"):
        # For high-value, we don't block — but we flag for review
        if not risk.needs_human_review:
            violations.append(
                f"HIGH_VALUE: ₹{amount:,.2f} >= ₹{amount_threshold:,.2f} threshold. "
                f"Adding human review flag."
            )
            # Don't block, but mark for human review
            risk.needs_human_review = True

    # ── Rule 4: Risk score escalation ────────────────────────────────────
    risk_threshold = config.get("escalation", {}).get("risk_score_threshold", 85)
    if risk.risk_score >= risk_threshold and proposed_attempt.action_taken not in ("escalate_to_human", "stopped"):
        violations.append(
            f"HIGH_RISK: Score {risk.risk_score} >= {risk_threshold}. "
            f"Overriding to escalate_to_human."
        )
        modified_action = "escalate_to_human"

    # ── Rule 5: B2B active dispute hold ──────────────────────────────────
    if isinstance(event, ReceivableOverdueEvent) and event.has_active_dispute:
        if proposed_attempt.action_taken not in ("escalate_to_human", "stopped"):
            violations.append("DISPUTE_HOLD: Active dispute — all dunning paused")
            modified_action = "escalate_to_human"

    # ── Rule 6: Fraud freeze ─────────────────────────────────────────────
    if isinstance(event, PaymentFailedEvent):
        from src.data.schemas import ErrorReason
        if event.error.reason == ErrorReason.FRAUD_FLAGGED:
            if proposed_attempt.action_taken not in ("escalate_to_human", "stopped"):
                violations.append("FRAUD_FREEZE: Fraud flagged — must escalate, no auto-action")
                modified_action = "escalate_to_human"

    # ── Rule 7: DNC enforcement ──────────────────────────────────────────
    dnc_enabled = config.get("stopping_rules", {}).get("dnc_enforcement", True)
    if dnc_enabled and opted_out:
        if proposed_attempt.action_taken in ("send_nudge", "send_recovery_link", "phone_followup"):
            violations.append("DNC_ENFORCEMENT: Customer on Do Not Contact list")
            modified_action = "stopped"

    # ── Build verdict ────────────────────────────────────────────────────
    approved = len(violations) == 0
    escalated = modified_action == "escalate_to_human" and proposed_attempt.action_taken != "escalate_to_human"

    verdict = ComplianceVerdict(
        approved=approved,
        action_allowed=proposed_attempt.action_taken,
        action_modified=modified_action if not approved else "",
        violations=violations,
        reasoning=f"{'APPROVED' if approved else 'MODIFIED'}: {'; '.join(violations) if violations else 'All rules passed'}",
        escalated=escalated,
    )

    if violations:
        logger.warning(
            "[COMPLIANCE] event=%s | VIOLATIONS: %s | action: %s → %s",
            event.event_id, violations, proposed_attempt.action_taken, modified_action,
        )
    else:
        logger.info("[COMPLIANCE] event=%s | APPROVED: %s", event.event_id, proposed_attempt.action_taken)

    # Log compliance decision as agent message
    agent_messages.append(AgentMessage(
        from_agent=AGENT_NAME,
        to_agent="orchestrator",
        message_type="verdict",
        content=verdict.reasoning,
        metadata={
            "approved": approved,
            "violations": violations,
            "escalated": escalated,
        },
    ))

    return verdict


def apply_verdict(
    proposed_attempt: RecoveryAttempt,
    verdict: ComplianceVerdict,
) -> RecoveryAttempt:
    """
    Apply compliance verdict to the proposed attempt.
    Modifies the attempt if compliance rules require changes.
    """
    if verdict.approved:
        return proposed_attempt

    # Compliance modified the action
    modified = proposed_attempt.model_copy()

    if verdict.action_modified == "stopped":
        modified.action_taken = "stopped"
        modified.result = "stopped"
        modified.amount_recovered_inr = 0.0
        modified.stopping_rule_triggered = verdict.violations[0] if verdict.violations else "compliance_block"
        modified.agent_reasoning += f" | COMPLIANCE: {verdict.reasoning}"

    elif verdict.action_modified == "escalate_to_human":
        modified.action_taken = "escalate_to_human"
        modified.result = "escalated"
        modified.channel = "human_review"
        modified.amount_recovered_inr = 0.0
        modified.agent_reasoning += f" | COMPLIANCE ESCALATION: {verdict.reasoning}"

    return modified


def _get_amount(event: Any) -> float:
    """Extract amount from any event type."""
    if isinstance(event, PaymentFailedEvent):
        return event.amount_inr
    elif isinstance(event, CheckoutAbandonedEvent):
        return event.amount_inr
    elif isinstance(event, SubscriptionFailureEvent):
        return event.charge_amount_inr
    elif isinstance(event, ReceivableOverdueEvent):
        return event.amount_inr
    return 0.0
