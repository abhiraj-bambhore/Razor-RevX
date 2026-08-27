"""
Multi-Agent LangGraph Orchestrator — Hierarchical Supervisor Architecture.

Architecture:
    Supervisor → Detect → Score Risk → Supervisor Route → Specialist Agent
    → Supervisor Review (reflection) → Compliance Gate → Execute & Audit

This replaces the simple linear pipeline with a true multi-agent system where:
    1. Supervisor agent makes dynamic routing decisions
    2. Specialist agents are tool-using recovery agents
    3. Compliance agent independently validates every action
    4. Supervisor can reflect and re-route bad specialist output
    5. All agents communicate via shared message bus
"""
from __future__ import annotations

import logging
from typing import Any, TypedDict

import yaml
from langgraph.graph import END, StateGraph

from src.agents.checkout_recovery import run_checkout_recovery
from src.agents.compliance_agent import apply_verdict, validate_action
from src.agents.payment_recovery import run_payment_recovery
from src.agents.receivables_chaser import run_receivables_chaser
from src.agents.risk_scorer import score_risk
from src.agents.subscription_recovery import run_subscription_recovery
from src.agents.supervisor import supervisor_review_output, supervisor_route
from src.audit.audit_trail import AuditTrail
from src.data.schemas import (
    AgentMessage,
    AuditEntry,
    CheckoutAbandonedEvent,
    ComplianceVerdict,
    FailureMode,
    PaymentFailedEvent,
    ReceivableOverdueEvent,
    RecoveryAttempt,
    RiskAssessment,
    SubscriptionFailureEvent,
    SupervisorDecision,
)
from src.detection.checkout_detector import classify_checkout_abandonment
from src.detection.payment_detector import classify_payment_failure
from src.detection.receivables_detector import classify_receivable
from src.detection.subscription_detector import classify_subscription_failure

logger = logging.getLogger(__name__)


# ── Multi-Agent Graph State ──────────────────────────────────────────────────

class MultiAgentState(TypedDict):
    """State passed through the hierarchical multi-agent graph."""
    # Core event data
    event: Any
    detection: dict
    risk_assessment: RiskAssessment
    attempt: RecoveryAttempt
    failure_mode: str
    config: dict
    audit: AuditTrail

    # Multi-agent communication
    supervisor_decision: SupervisorDecision
    compliance_verdict: ComplianceVerdict
    agent_messages: list[AgentMessage]

    # Control flow
    reflection_count: int
    max_reflections: int


# ── Graph Nodes ──────────────────────────────────────────────────────────────

def detect_node(state: MultiAgentState) -> dict:
    """Node 1: Detect and classify the event by failure mode."""
    event = state["event"]

    if isinstance(event, PaymentFailedEvent):
        detection = classify_payment_failure(event)
    elif isinstance(event, CheckoutAbandonedEvent):
        detection = classify_checkout_abandonment(event)
    elif isinstance(event, SubscriptionFailureEvent):
        detection = classify_subscription_failure(event)
    elif isinstance(event, ReceivableOverdueEvent):
        detection = classify_receivable(event)
    else:
        raise ValueError(f"Unknown event type: {type(event)}")

    logger.info(
        "[DETECT] event=%s | mode=%s | urgency=%s",
        event.event_id,
        detection["failure_mode"].value,
        detection.get("urgency", "unknown"),
    )

    return {
        "detection": detection,
        "failure_mode": detection["failure_mode"].value,
    }


def score_risk_node(state: MultiAgentState) -> dict:
    """Node 2: Score risk using LLM → ML model → heuristic fallback."""
    assessment = score_risk(state["event"], state["detection"])
    return {"risk_assessment": assessment}


def supervisor_route_node(state: MultiAgentState) -> dict:
    """Node 3: Supervisor agent decides which specialist to invoke."""
    decision = supervisor_route(
        event=state["event"],
        detection=state["detection"],
        risk_assessment={
            "risk_score": state["risk_assessment"].risk_score,
            "recommended_action": state["risk_assessment"].recommended_action,
            "reasoning": state["risk_assessment"].reasoning,
        },
        agent_messages=state["agent_messages"],
    )
    return {"supervisor_decision": decision}


def route_to_specialist(state: MultiAgentState) -> str:
    """Conditional edge: route to the specialist selected by the Supervisor."""
    agent = state["supervisor_decision"].selected_agent
    valid_agents = {"payment_agent", "checkout_agent", "subscription_agent", "receivables_agent"}
    if agent not in valid_agents:
        agent = "payment_agent"
    return agent


def payment_agent_node(state: MultiAgentState) -> dict:
    """Specialist: Payment recovery agent with tools."""
    attempt = run_payment_recovery(
        event=state["event"],
        detection=state["detection"],
        risk=state["risk_assessment"],
        audit=state["audit"],
        config=state["config"],
    )
    state["agent_messages"].append(AgentMessage(
        from_agent="payment_recovery_agent",
        to_agent="supervisor_agent",
        message_type="proposal",
        content=f"Proposed action: {attempt.action_taken} | Result: {attempt.result}",
        metadata={"amount_recovered": attempt.amount_recovered_inr},
    ))
    return {"attempt": attempt}


def checkout_agent_node(state: MultiAgentState) -> dict:
    """Specialist: Checkout recovery agent with tools."""
    attempt = run_checkout_recovery(
        event=state["event"],
        detection=state["detection"],
        risk=state["risk_assessment"],
        audit=state["audit"],
        config=state["config"],
    )
    state["agent_messages"].append(AgentMessage(
        from_agent="checkout_recovery_agent",
        to_agent="supervisor_agent",
        message_type="proposal",
        content=f"Proposed action: {attempt.action_taken} | Result: {attempt.result}",
        metadata={"amount_recovered": attempt.amount_recovered_inr},
    ))
    return {"attempt": attempt}


def subscription_agent_node(state: MultiAgentState) -> dict:
    """Specialist: Subscription recovery agent with tools."""
    attempt = run_subscription_recovery(
        event=state["event"],
        detection=state["detection"],
        risk=state["risk_assessment"],
        audit=state["audit"],
        config=state["config"],
    )
    state["agent_messages"].append(AgentMessage(
        from_agent="subscription_recovery_agent",
        to_agent="supervisor_agent",
        message_type="proposal",
        content=f"Proposed action: {attempt.action_taken} | Result: {attempt.result}",
        metadata={"amount_recovered": attempt.amount_recovered_inr},
    ))
    return {"attempt": attempt}


def receivables_agent_node(state: MultiAgentState) -> dict:
    """Specialist: Receivables chaser agent with tools."""
    attempt = run_receivables_chaser(
        event=state["event"],
        detection=state["detection"],
        risk=state["risk_assessment"],
        audit=state["audit"],
        config=state["config"],
    )
    state["agent_messages"].append(AgentMessage(
        from_agent="receivables_chaser_agent",
        to_agent="supervisor_agent",
        message_type="proposal",
        content=f"Proposed action: {attempt.action_taken} | Result: {attempt.result}",
        metadata={"amount_recovered": attempt.amount_recovered_inr},
    ))
    return {"attempt": attempt}


def supervisor_review_node(state: MultiAgentState) -> dict:
    """
    Node 5: Supervisor reviews specialist output (reflection loop).

    If the output is poor, the supervisor can increment reflection_count
    to trigger re-routing (up to max_reflections).
    """
    if not state["supervisor_decision"].allow_reflection:
        # No reflection needed — pass through
        return {}

    approved = supervisor_review_output(
        event=state["event"],
        specialist_output={
            "action_taken": state["attempt"].action_taken,
            "result": state["attempt"].result,
            "agent_reasoning": state["attempt"].agent_reasoning,
            "amount_recovered": state["attempt"].amount_recovered_inr,
        },
        risk_assessment={
            "risk_score": state["risk_assessment"].risk_score,
            "recommended_action": state["risk_assessment"].recommended_action,
        },
        agent_messages=state["agent_messages"],
    )

    if not approved:
        new_count = state["reflection_count"] + 1
        logger.info("[REFLECTION] Attempt %d/%d", new_count, state["max_reflections"])
        return {"reflection_count": new_count}

    return {}


def should_reflect(state: MultiAgentState) -> str:
    """Conditional edge: decide whether to re-route or proceed to compliance."""
    if state["reflection_count"] > 0 and state["reflection_count"] < state["max_reflections"]:
        # Re-route: go back to supervisor for different routing
        return "reflect"
    return "proceed"


def compliance_gate_node(state: MultiAgentState) -> dict:
    """
    Node 6: Independent Compliance Gate agent validates the proposed action.

    Can BLOCK, MODIFY, or APPROVE any action before it reaches audit/execution.
    """
    verdict = validate_action(
        event=state["event"],
        proposed_attempt=state["attempt"],
        risk=state["risk_assessment"],
        audit=state["audit"],
        config=state["config"],
        agent_messages=state["agent_messages"],
    )

    # Apply verdict — may modify the attempt
    if not verdict.approved:
        modified_attempt = apply_verdict(state["attempt"], verdict)
        return {
            "compliance_verdict": verdict,
            "attempt": modified_attempt,
        }

    return {"compliance_verdict": verdict}


# ── Build the Multi-Agent Graph ──────────────────────────────────────────────

def build_recovery_graph() -> StateGraph:
    """
    Build the hierarchical multi-agent LangGraph workflow.

    Graph:
        detect → score_risk → supervisor_route → (conditional)
        → [payment|checkout|subscription|receivables]
        → supervisor_review → (conditional: reflect or proceed)
        → compliance_gate → END
    """
    graph = StateGraph(MultiAgentState)

    # Add nodes
    graph.add_node("detect", detect_node)
    graph.add_node("score_risk", score_risk_node)
    graph.add_node("supervisor_route", supervisor_route_node)
    graph.add_node("payment_agent", payment_agent_node)
    graph.add_node("checkout_agent", checkout_agent_node)
    graph.add_node("subscription_agent", subscription_agent_node)
    graph.add_node("receivables_agent", receivables_agent_node)
    graph.add_node("supervisor_review", supervisor_review_node)
    graph.add_node("compliance_gate", compliance_gate_node)

    # Entry point
    graph.set_entry_point("detect")

    # Flow: detect → score_risk → supervisor_route
    graph.add_edge("detect", "score_risk")
    graph.add_edge("score_risk", "supervisor_route")

    # Supervisor conditional routing to specialist agents
    graph.add_conditional_edges(
        "supervisor_route",
        route_to_specialist,
        {
            "payment_agent": "payment_agent",
            "checkout_agent": "checkout_agent",
            "subscription_agent": "subscription_agent",
            "receivables_agent": "receivables_agent",
        },
    )

    # All specialists → supervisor review (reflection)
    graph.add_edge("payment_agent", "supervisor_review")
    graph.add_edge("checkout_agent", "supervisor_review")
    graph.add_edge("subscription_agent", "supervisor_review")
    graph.add_edge("receivables_agent", "supervisor_review")

    # Supervisor review → conditional: reflect (re-route) or proceed to compliance
    graph.add_conditional_edges(
        "supervisor_review",
        should_reflect,
        {
            "reflect": "supervisor_route",   # Re-route through supervisor
            "proceed": "compliance_gate",     # Proceed to compliance
        },
    )

    # Compliance gate → END
    graph.add_edge("compliance_gate", END)

    return graph


def load_config(config_path: str = "config/recovery_config.yaml") -> dict:
    """Load recovery configuration from YAML."""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning("Config not found at %s, using defaults", config_path)
        return {
            "stopping_rules": {
                "max_attempts_per_case": 3,
                "cooldown_hours": 48,
                "opt_out_check": True,
            },
        }


def run_recovery_batch(
    events: list,
    config_path: str = "config/recovery_config.yaml",
    audit_db_path: str = "audit/recovery_audit.db",
    audit_jsonl_path: str = "audit/recovery_audit.jsonl",
) -> tuple[list[RecoveryAttempt], AuditTrail]:
    """
    Run the full multi-agent recovery workflow on a batch of events.

    This is the main entry point for the hierarchical agent system.

    Architecture per event:
        1. Detect (classify failure mode)
        2. Score Risk (LLM → ML → heuristic)
        3. Supervisor Route (LLM routing decision)
        4. Specialist Agent (recovery action with tools)
        5. Supervisor Review (reflection loop)
        6. Compliance Gate (validate & modify)
        7. Audit & Execute

    Args:
        events: List of event objects (mixed types).
        config_path: Path to recovery config YAML.
        audit_db_path: Path to SQLite audit DB.
        audit_jsonl_path: Path to JSONL audit log.

    Returns:
        Tuple of (list of RecoveryAttempt results, AuditTrail).
    """
    config = load_config(config_path)
    audit = AuditTrail(db_path=audit_db_path, jsonl_path=audit_jsonl_path)

    # Build and compile the multi-agent graph
    graph = build_recovery_graph()
    workflow = graph.compile()

    results: list[RecoveryAttempt] = []

    for i, event in enumerate(events):
        try:
            logger.info(
                "\n═══ Processing event %d/%d: %s ═══",
                i + 1, len(events), event.event_id,
            )

            initial_state: MultiAgentState = {
                "event": event,
                "detection": {},
                "risk_assessment": RiskAssessment(),
                "attempt": RecoveryAttempt(),
                "failure_mode": "",
                "config": config,
                "audit": audit,
                # Multi-agent state
                "supervisor_decision": SupervisorDecision(),
                "compliance_verdict": ComplianceVerdict(),
                "agent_messages": [],
                # Control flow
                "reflection_count": 0,
                "max_reflections": 2,
            }

            # Run the multi-agent graph
            final_state = workflow.invoke(initial_state)
            attempt = final_state.get("attempt", RecoveryAttempt())
            results.append(attempt)

            # Log agent message count for observability
            msg_count = len(final_state.get("agent_messages", []))
            compliance = final_state.get("compliance_verdict", ComplianceVerdict())

            logger.info(
                "[RESULT] status=%s | action=%s | recovered=INR %.2f | "
                "agents_involved=%d messages | compliance=%s",
                attempt.result,
                attempt.action_taken,
                attempt.amount_recovered_inr,
                msg_count,
                "APPROVED" if compliance.approved else f"MODIFIED ({compliance.violations})",
            )

        except Exception as exc:
            logger.error("Error processing event %s: %s", event.event_id, exc, exc_info=True)
            results.append(RecoveryAttempt(
                event_id=event.event_id,
                action_taken="error",
                result="failed",
                agent_reasoning=f"Multi-agent processing error: {str(exc)}",
            ))

    return results, audit
