"""
Supervisor Agent — Central coordinator for the multi-agent system.

Responsibilities:
    1. Analyze incoming events using LLM (or ML fallback)
    2. Decide which specialist agent(s) to invoke
    3. Review specialist output before forwarding to compliance
    4. Reflection: reject and re-route if specialist output is poor
"""
from __future__ import annotations

import json
import logging
from typing import Any

from src.data.schemas import (
    AgentMessage,
    CheckoutAbandonedEvent,
    FailureMode,
    PaymentFailedEvent,
    ReceivableOverdueEvent,
    SubscriptionFailureEvent,
    SupervisorDecision,
)

logger = logging.getLogger(__name__)

AGENT_NAME = "supervisor_agent"

# Agent routing map
FAILURE_MODE_TO_AGENT = {
    FailureMode.PAYMENT_FAILURE.value: "payment_agent",
    FailureMode.CHECKOUT_ABANDONMENT.value: "checkout_agent",
    FailureMode.SUBSCRIPTION_FAILURE.value: "subscription_agent",
    FailureMode.RECEIVABLE_OVERDUE.value: "receivables_agent",
}


def _build_event_summary(event: Any, detection: dict) -> str:
    """Build a concise summary of the event for the supervisor."""
    lines = []

    if isinstance(event, PaymentFailedEvent):
        lines.append(f"EVENT: Payment failure | ₹{event.amount_inr:,.2f}")
        lines.append(f"Error: {event.error.reason.value} (source: {event.error.source.value})")
        lines.append(f"Method: {event.method.value} | Segment: {event.customer_segment.value}")
        lines.append(f"Root cause: {detection.get('root_cause_category', 'unknown')}")
    elif isinstance(event, CheckoutAbandonedEvent):
        lines.append(f"EVENT: Checkout abandoned | ₹{event.amount_inr:,.2f}")
        lines.append(f"Stage: {event.abandonment_stage} | Time: {event.time_spent_seconds}s")
        lines.append(f"Intent: {detection.get('intent_score', 0.5)} | Decay: {detection.get('time_decay_factor', 1.0)}")
    elif isinstance(event, SubscriptionFailureEvent):
        lines.append(f"EVENT: Subscription failure | ₹{event.charge_amount_inr:,.2f}")
        lines.append(f"Plan: {event.plan_name} ({event.plan_tier}) | Mandate: {event.mandate_status}")
        lines.append(f"Churn risk: {detection.get('churn_risk', 'medium')}")
    elif isinstance(event, ReceivableOverdueEvent):
        lines.append(f"EVENT: B2B receivable overdue | ₹{event.amount_inr:,.2f}")
        lines.append(f"Company: {event.company_name} | Days overdue: {event.days_overdue}")
        lines.append(f"Aging: {detection.get('aging_risk', 'low_risk')} | Dispute: {event.has_active_dispute}")

    return "\n".join(lines)


def supervisor_route(
    event: Any,
    detection: dict,
    risk_assessment: dict,
    agent_messages: list[AgentMessage],
) -> SupervisorDecision:
    """
    Supervisor agent: Decide which specialist to route to.

    Uses LLM for routing decisions when available, falls back to
    deterministic routing based on failure mode.

    Args:
        event: The raw event object.
        detection: Detection/classification result.
        risk_assessment: Risk score and recommendation.
        agent_messages: Prior messages in the agent pipeline (for context).

    Returns:
        SupervisorDecision with selected agent and reasoning.
    """
    failure_mode = detection.get("failure_mode", FailureMode.PAYMENT_FAILURE)
    mode_value = failure_mode.value if hasattr(failure_mode, "value") else str(failure_mode)

    event_summary = _build_event_summary(event, detection)

    # Attempt LLM-based routing for more nuanced decisions
    llm_decision = _llm_routing_decision(event_summary, risk_assessment)

    if llm_decision:
        selected = llm_decision.get("selected_agent", FAILURE_MODE_TO_AGENT.get(mode_value, "payment_agent"))
        reasoning = llm_decision.get("reasoning", "LLM-directed routing")
        confidence = float(llm_decision.get("confidence", 0.85))
        allow_reflection = llm_decision.get("needs_review", False)
        llm_used = True
    else:
        # Deterministic routing based on failure mode
        selected = FAILURE_MODE_TO_AGENT.get(mode_value, "payment_agent")
        reasoning = f"Deterministic routing: {mode_value} → {selected}"
        confidence = 0.95  # Deterministic routing is high-confidence
        allow_reflection = False
        llm_used = False

    # Special case: high-risk events might need multi-agent coordination
    risk_score = risk_assessment.get("risk_score", 50)
    if risk_score >= 85:
        allow_reflection = True
        reasoning += " | HIGH RISK: Supervisor will review specialist output."

    decision = SupervisorDecision(
        selected_agent=selected,
        reasoning=reasoning,
        confidence=confidence,
        requires_compliance_check=True,
        allow_reflection=allow_reflection,
        llm_used=llm_used,
    )

    logger.info(
        "[SUPERVISOR] event=%s → agent=%s | confidence=%.2f | llm=%s | reflection=%s",
        event.event_id, selected, confidence, llm_used, allow_reflection,
    )

    # Log supervisor decision as agent message
    agent_messages.append(AgentMessage(
        from_agent=AGENT_NAME,
        to_agent=selected,
        message_type="routing",
        content=reasoning,
        metadata={"risk_score": risk_score, "confidence": confidence},
    ))

    return decision


def supervisor_review_output(
    event: Any,
    specialist_output: dict,
    risk_assessment: dict,
    agent_messages: list[AgentMessage],
) -> bool:
    """
    Supervisor reflection: review specialist output before compliance.

    Returns True if output is acceptable, False if re-routing is needed.
    """
    action = specialist_output.get("action_taken", "")
    result = specialist_output.get("result", "")
    risk_score = risk_assessment.get("risk_score", 50)

    # Reflection checks
    issues = []

    # Check 1: High-risk event should not get a low-effort action
    if risk_score >= 80 and action in ("send_nudge",):
        issues.append(f"High risk ({risk_score}) but low-effort action ({action})")

    # Check 2: Fraud events must escalate
    event_summary = str(specialist_output.get("agent_reasoning", "")).lower()
    if "fraud" in event_summary and action != "escalate_to_human":
        issues.append("Fraud-related event not escalated")

    if issues:
        logger.warning(
            "[SUPERVISOR REFLECTION] Issues found: %s — requesting re-evaluation",
            issues,
        )
        agent_messages.append(AgentMessage(
            from_agent=AGENT_NAME,
            to_agent="specialist",
            message_type="reflection",
            content=f"Issues: {'; '.join(issues)}. Please re-evaluate.",
            metadata={"issues": issues},
        ))
        return False

    logger.info("[SUPERVISOR] Output approved for compliance check.")
    return True


def _llm_routing_decision(event_summary: str, risk_assessment: dict) -> dict | None:
    """
    Use LLM to make a nuanced routing decision.
    Returns None if LLM is unavailable.
    """
    from src.utils.llm import get_client, _call_gemini

    if get_client() is None:
        return None

    prompt = f"""You are a Supervisor Agent for a revenue recovery system.
Given this event, decide which specialist agent to route to.

EVENT:
{event_summary}

RISK ASSESSMENT:
Risk score: {risk_assessment.get('risk_score', 50)}
Recommended action: {risk_assessment.get('recommended_action', 'unknown')}

Available agents:
- payment_agent: Handles payment failures, retries, recovery links
- checkout_agent: Handles checkout abandonment, cart recovery
- subscription_agent: Handles subscription/mandate failures, plan downgrades
- receivables_agent: Handles B2B overdue invoices, PTP, dunning

Respond ONLY with valid JSON:
{{
    "selected_agent": "<agent_name>",
    "reasoning": "<1-2 sentence reasoning>",
    "confidence": <0.0-1.0>,
    "needs_review": <true if this is complex/high-risk>
}}"""

    try:
        text = _call_gemini(prompt)
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if "```" in text:
                text = text[:text.rfind("```")]
            text = text.strip()
        return json.loads(text)
    except Exception as exc:
        logger.debug("LLM routing failed: %s", exc)
        return None
