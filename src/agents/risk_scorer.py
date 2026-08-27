"""
Risk scorer agent with 3-tier resilience.

Tier 1: Gemini LLM (via llm_risk_assessment)
Tier 2: ML GradientBoosting model (direct call when event object available)
Tier 3: Static heuristic (absolute last resort)

Logs which model was used for audit transparency.
"""
from __future__ import annotations

import logging
from typing import Any

from src.data.schemas import (
    CheckoutAbandonedEvent,
    FailureMode,
    PaymentFailedEvent,
    ReceivableOverdueEvent,
    RiskAssessment,
    RiskTier,
    SubscriptionFailureEvent,
)
from src.utils.llm import llm_risk_assessment

logger = logging.getLogger(__name__)


def _event_to_summary(event: Any, detection_result: dict) -> str:
    """Convert an event + detection result into a text summary for the LLM."""
    lines = []

    if isinstance(event, PaymentFailedEvent):
        lines.append(f"TYPE: Payment Failure")
        lines.append(f"Amount: ₹{event.amount_inr:,.2f}")
        lines.append(f"Method: {event.method.value}")
        lines.append(f"Error: {event.error.reason.value} (source: {event.error.source.value})")
        lines.append(f"Customer segment: {event.customer_segment.value}")
        lines.append(f"Customer LTV: ₹{event.customer_ltv_inr:,.2f}")
        lines.append(f"Previous attempts: {event.previous_attempts}")
        lines.append(f"Root cause: {detection_result.get('root_cause_category', 'unknown')}")

    elif isinstance(event, CheckoutAbandonedEvent):
        lines.append(f"TYPE: Checkout Abandonment")
        lines.append(f"Amount: ₹{event.amount_inr:,.2f}")
        lines.append(f"Abandonment stage: {event.abandonment_stage}")
        lines.append(f"Time spent: {event.time_spent_seconds}s")
        lines.append(f"Cart items: {len(event.cart_items)}")
        lines.append(f"Customer segment: {event.customer_segment.value}")
        lines.append(f"Customer LTV: ₹{event.customer_ltv_inr:,.2f}")
        lines.append(f"Intent score: {detection_result.get('intent_score', 0)}")
        lines.append(f"Time decay: {detection_result.get('time_decay_factor', 1.0)}")

    elif isinstance(event, SubscriptionFailureEvent):
        lines.append(f"TYPE: Subscription Failure")
        lines.append(f"Plan: {event.plan_name} ({event.plan_tier})")
        lines.append(f"Charge amount: ₹{event.charge_amount_inr:,.2f}")
        lines.append(f"Billing cycle: {event.billing_cycle}")
        lines.append(f"Failed charges: {event.failed_charge_count}")
        lines.append(f"Mandate status: {event.mandate_status}")
        lines.append(f"Error: {event.error.reason.value}")
        lines.append(f"Customer segment: {event.customer_segment.value}")
        lines.append(f"Customer LTV: ₹{event.customer_ltv_inr:,.2f}")
        lines.append(f"Churn risk: {detection_result.get('churn_risk', 'medium')}")

    elif isinstance(event, ReceivableOverdueEvent):
        lines.append(f"TYPE: Receivable Overdue")
        lines.append(f"Invoice: {event.invoice_number}")
        lines.append(f"Amount: ₹{event.amount_inr:,.2f}")
        lines.append(f"Days overdue: {event.days_overdue}")
        lines.append(f"Aging bucket: {event.aging_bucket}")
        lines.append(f"Company: {event.company_name}")
        lines.append(f"Previous late payments: {event.previous_payment_delays}")
        lines.append(f"Active dispute: {event.has_active_dispute}")
        lines.append(f"Customer segment: {event.customer_segment.value}")
        lines.append(f"Collection probability: {detection_result.get('collection_probability', 0.5)}")

    return "\n".join(lines)


def score_risk(event: Any, detection_result: dict) -> RiskAssessment:
    """
    Score risk for a detected event using 3-tier fallback.

    Tier 1: Gemini LLM (via text summary)
    Tier 2: ML model (direct feature extraction from event object)
    Tier 3: Heuristic rules (via llm_risk_assessment fallback chain)

    Returns a RiskAssessment with score, reasoning, and recommended action.
    """
    summary = _event_to_summary(event, detection_result)

    # Attempt the 3-tier call (LLM → ML-from-text → heuristic)
    llm_result = llm_risk_assessment(summary)

    # If LLM failed and we got ML/heuristic from text summary,
    # try direct ML scoring using the event object for better accuracy
    model_used = llm_result.get("model_used", "unknown")
    if model_used != "gemini_llm":
        ml_direct = _try_direct_ml_scoring(event, detection_result)
        if ml_direct is not None:
            llm_result = ml_direct
            model_used = "ml_gradient_boosting"

    risk_score = float(llm_result.get("risk_score", 50.0))
    reasoning = llm_result.get("reasoning", "")
    action = llm_result.get(
        "recommended_action",
        detection_result.get("recommended_first_action", "send_recovery_link"),
    )
    recovery_prob = float(llm_result.get("estimated_recovery_probability", 0.5))
    llm_used = llm_result.get("llm_used", False)
    model_used = llm_result.get("model_used", "unknown")

    # Determine risk tier from score
    if risk_score >= 85:
        tier = RiskTier.CRITICAL
    elif risk_score >= 65:
        tier = RiskTier.HIGH
    elif risk_score >= 40:
        tier = RiskTier.MEDIUM
    else:
        tier = RiskTier.LOW

    # Determine if human review needed
    needs_human = False
    if isinstance(event, PaymentFailedEvent) and event.amount_inr >= 50000:
        needs_human = True
    elif isinstance(event, ReceivableOverdueEvent) and event.amount_inr >= 200000:
        needs_human = True
    elif risk_score >= 85:
        needs_human = True

    assessment = RiskAssessment(
        risk_score=round(risk_score, 1),
        risk_tier=tier,
        recommended_action=action,
        reasoning=reasoning,
        needs_human_review=needs_human,
        estimated_recovery_probability=round(recovery_prob, 3),
        llm_used=llm_used,
    )

    logger.info(
        "Risk scored: event=%s | score=%.1f | tier=%s | action=%s | "
        "human=%s | model=%s",
        event.event_id, risk_score, tier.value, action, needs_human, model_used,
    )

    return assessment


def _try_direct_ml_scoring(event: Any, detection: dict) -> dict | None:
    """
    Attempt direct ML model scoring using the event object.
    Provides better accuracy than text-parsing since features
    are extracted directly from structured data.
    """
    try:
        from src.ml.risk_model import get_risk_model
        model = get_risk_model()
        if model.is_fitted:
            result = model.predict(event, detection)
            logger.debug("Direct ML scoring succeeded: score=%.1f", result["risk_score"])
            return result
    except Exception as exc:
        logger.debug("Direct ML scoring failed: %s", exc)
    return None
