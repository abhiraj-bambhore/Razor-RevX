"""
LangGraph Orchestrator — Supervisor graph.

Routes detected events to specialized recovery agents.
Enforces stopping rules, manages state, tracks audit trail.

Graph flow:
    detect → score_risk → route_to_agent → execute_agent → record_result
"""
from __future__ import annotations

import logging
from typing import Any, TypedDict

import yaml
from langgraph.graph import END, StateGraph

from src.agents.checkout_recovery import run_checkout_recovery
from src.agents.payment_recovery import run_payment_recovery
from src.agents.receivables_chaser import run_receivables_chaser
from src.agents.risk_scorer import score_risk
from src.agents.subscription_recovery import run_subscription_recovery
from src.audit.audit_trail import AuditTrail
from src.data.schemas import (
    CheckoutAbandonedEvent,
    FailureMode,
    PaymentFailedEvent,
    ReceivableOverdueEvent,
    RecoveryAttempt,
    RiskAssessment,
    SubscriptionFailureEvent,
)
from src.detection.checkout_detector import classify_checkout_abandonment
from src.detection.payment_detector import classify_payment_failure
from src.detection.receivables_detector import classify_receivable
from src.detection.subscription_detector import classify_subscription_failure

logger = logging.getLogger(__name__)


# ── Graph state ──────────────────────────────────────────────────────────────

class RecoveryState(TypedDict):
    """State passed through the recovery graph."""
    event: Any                        # The raw event object
    detection: dict                   # Detection/classification result
    risk_assessment: RiskAssessment   # Risk score + recommendation
    attempt: RecoveryAttempt          # Final recovery attempt result
    failure_mode: str                 # Which failure mode
    config: dict                      # Recovery config
    audit: AuditTrail                 # Audit trail reference


# ── Graph nodes ──────────────────────────────────────────────────────────────

def detect_node(state: RecoveryState) -> dict:
    """Detect and classify the event by failure mode."""
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
        "[DETECTED] event=%s | mode=%s | urgency=%s",
        event.event_id,
        detection["failure_mode"].value,
        detection.get("urgency", "unknown"),
    )

    return {
        "detection": detection,
        "failure_mode": detection["failure_mode"].value,
    }


def score_risk_node(state: RecoveryState) -> dict:
    """Score risk using LLM or deterministic fallback."""
    assessment = score_risk(state["event"], state["detection"])
    return {"risk_assessment": assessment}


def route_to_agent(state: RecoveryState) -> str:
    """Route to the appropriate specialized agent based on failure mode."""
    mode = state["failure_mode"]

    routing = {
        FailureMode.PAYMENT_FAILURE.value: "payment_agent",
        FailureMode.CHECKOUT_ABANDONMENT.value: "checkout_agent",
        FailureMode.SUBSCRIPTION_FAILURE.value: "subscription_agent",
        FailureMode.RECEIVABLE_OVERDUE.value: "receivables_agent",
    }

    target = routing.get(mode, "payment_agent")
    logger.info("[ROUTING] event=%s -> agent=%s", state["event"].event_id, target)
    return target


def payment_agent_node(state: RecoveryState) -> dict:
    """Execute payment recovery agent."""
    attempt = run_payment_recovery(
        event=state["event"],
        detection=state["detection"],
        risk=state["risk_assessment"],
        audit=state["audit"],
        config=state["config"],
    )
    return {"attempt": attempt}


def checkout_agent_node(state: RecoveryState) -> dict:
    """Execute checkout recovery agent."""
    attempt = run_checkout_recovery(
        event=state["event"],
        detection=state["detection"],
        risk=state["risk_assessment"],
        audit=state["audit"],
        config=state["config"],
    )
    return {"attempt": attempt}


def subscription_agent_node(state: RecoveryState) -> dict:
    """Execute subscription recovery agent."""
    attempt = run_subscription_recovery(
        event=state["event"],
        detection=state["detection"],
        risk=state["risk_assessment"],
        audit=state["audit"],
        config=state["config"],
    )
    return {"attempt": attempt}


def receivables_agent_node(state: RecoveryState) -> dict:
    """Execute receivables chaser agent."""
    attempt = run_receivables_chaser(
        event=state["event"],
        detection=state["detection"],
        risk=state["risk_assessment"],
        audit=state["audit"],
        config=state["config"],
    )
    return {"attempt": attempt}


# ── Build the graph ──────────────────────────────────────────────────────────

def build_recovery_graph() -> StateGraph:
    """
    Build the LangGraph recovery workflow.

    Graph:
        detect → score_risk → (conditional) → [payment|checkout|subscription|receivables] → END
    """
    graph = StateGraph(RecoveryState)

    # Add nodes
    graph.add_node("detect", detect_node)
    graph.add_node("score_risk", score_risk_node)
    graph.add_node("payment_agent", payment_agent_node)
    graph.add_node("checkout_agent", checkout_agent_node)
    graph.add_node("subscription_agent", subscription_agent_node)
    graph.add_node("receivables_agent", receivables_agent_node)

    # Entry point
    graph.set_entry_point("detect")

    # Edges
    graph.add_edge("detect", "score_risk")

    # Conditional routing after risk scoring
    graph.add_conditional_edges(
        "score_risk",
        route_to_agent,
        {
            "payment_agent": "payment_agent",
            "checkout_agent": "checkout_agent",
            "subscription_agent": "subscription_agent",
            "receivables_agent": "receivables_agent",
        },
    )

    # All agents terminate
    graph.add_edge("payment_agent", END)
    graph.add_edge("checkout_agent", END)
    graph.add_edge("subscription_agent", END)
    graph.add_edge("receivables_agent", END)

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
) -> list[RecoveryAttempt]:
    """
    Run the full recovery workflow on a batch of events.

    This is the main entry point for the agent system.

    Args:
        events: List of event objects (mixed types).
        config_path: Path to recovery config YAML.
        audit_db_path: Path to SQLite audit DB.
        audit_jsonl_path: Path to JSONL audit log.

    Returns:
        List of RecoveryAttempt results.
    """
    config = load_config(config_path)
    audit = AuditTrail(db_path=audit_db_path, jsonl_path=audit_jsonl_path)

    # Build and compile the graph
    graph = build_recovery_graph()
    workflow = graph.compile()

    results: list[RecoveryAttempt] = []

    for i, event in enumerate(events):
        try:
            logger.info(
                "\n--- Processing event %d/%d: %s ---",
                i + 1, len(events), event.event_id,
            )

            initial_state: RecoveryState = {
                "event": event,
                "detection": {},
                "risk_assessment": RiskAssessment(),
                "attempt": RecoveryAttempt(),
                "failure_mode": "",
                "config": config,
                "audit": audit,
            }

            # Run the graph
            final_state = workflow.invoke(initial_state)
            attempt = final_state.get("attempt", RecoveryAttempt())
            results.append(attempt)

            logger.info(
                "[RESULT] status=%s | action=%s | recovered=INR %.2f",
                attempt.result,
                attempt.action_taken,
                attempt.amount_recovered_inr,
            )

        except Exception as exc:
            logger.error("Error processing event %s: %s", event.event_id, exc, exc_info=True)
            results.append(RecoveryAttempt(
                event_id=event.event_id,
                action_taken="error",
                result="failed",
                agent_reasoning=f"Processing error: {str(exc)}",
            ))

    return results, audit
