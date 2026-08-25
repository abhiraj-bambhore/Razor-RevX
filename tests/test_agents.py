"""
Tests for agent layer — recovery agents and audit trail.
"""
import os
import pytest
import tempfile
from datetime import datetime

from src.agents.risk_scorer import score_risk
from src.audit.audit_trail import AuditTrail
from src.data.schemas import (
    AuditEntry,
    CheckoutAbandonedEvent,
    CustomerSegment,
    ErrorReason,
    ErrorSource,
    FailureMode,
    PaymentFailedEvent,
    RazorpayError,
    ReceivableOverdueEvent,
    RiskAssessment,
    SubscriptionFailureEvent,
)
from src.detection.payment_detector import classify_payment_failure
from src.agents.payment_recovery import run_payment_recovery


class TestRiskScorer:
    """Test risk scoring with deterministic fallback."""

    def test_gateway_error_recommends_retry(self):
        event = PaymentFailedEvent(
            amount_inr=5000,
            error=RazorpayError(reason=ErrorReason.GATEWAY_ERROR, source=ErrorSource.GATEWAY),
        )
        detection = classify_payment_failure(event)
        assessment = score_risk(event, detection)

        assert isinstance(assessment, RiskAssessment)
        assert assessment.risk_score > 0
        assert assessment.recommended_action in ("auto_retry", "send_recovery_link")

    def test_fraud_gets_high_score(self):
        event = PaymentFailedEvent(
            amount_inr=100000,
            error=RazorpayError(reason=ErrorReason.FRAUD_FLAGGED, source=ErrorSource.RAZORPAY),
        )
        detection = classify_payment_failure(event)
        assessment = score_risk(event, detection)

        assert assessment.risk_score >= 85
        assert assessment.recommended_action == "escalate_to_human"

    def test_high_value_needs_human(self):
        event = PaymentFailedEvent(
            amount_inr=75000,
            customer_segment=CustomerSegment.PREMIUM,
            error=RazorpayError(reason=ErrorReason.INSUFFICIENT_FUNDS, source=ErrorSource.CUSTOMER),
        )
        detection = classify_payment_failure(event)
        assessment = score_risk(event, detection)

        assert assessment.needs_human_review is True


class TestAuditTrail:
    """Test audit trail persistence."""

    def _make_audit(self, tmp_dir: str) -> AuditTrail:
        return AuditTrail(
            db_path=os.path.join(tmp_dir, "test_audit.db"),
            jsonl_path=os.path.join(tmp_dir, "test_audit.jsonl"),
        )

    def test_log_and_retrieve(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = self._make_audit(tmp)

            entry = AuditEntry(
                event_id="evt_test123",
                failure_mode=FailureMode.PAYMENT_FAILURE,
                agent_name="test_agent",
                action_taken="auto_retry",
                result="success",
                amount_at_risk_inr=5000,
                amount_recovered_inr=5000,
                risk_score=70,
            )
            audit.log(entry)

            entries = audit.get_all_entries()
            assert len(entries) == 1
            assert entries[0]["event_id"] == "evt_test123"
            assert entries[0]["amount_recovered_inr"] == 5000

    def test_attempt_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = self._make_audit(tmp)

            for i in range(3):
                entry = AuditEntry(
                    event_id="evt_multi",
                    failure_mode=FailureMode.PAYMENT_FAILURE,
                    agent_name="test_agent",
                    action_taken=f"attempt_{i}",
                    result="failed",
                    amount_at_risk_inr=1000,
                    amount_recovered_inr=0,
                    risk_score=50,
                )
                audit.log(entry)

            assert audit.get_attempts_for_event("evt_multi") == 3

    def test_summary_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = self._make_audit(tmp)

            # Log a success and a failure
            audit.log(AuditEntry(
                event_id="evt_1",
                failure_mode=FailureMode.PAYMENT_FAILURE,
                agent_name="payment_agent",
                action_taken="auto_retry",
                result="success",
                amount_at_risk_inr=10000,
                amount_recovered_inr=10000,
                risk_score=60,
            ))
            audit.log(AuditEntry(
                event_id="evt_2",
                failure_mode=FailureMode.CHECKOUT_ABANDONMENT,
                agent_name="checkout_agent",
                action_taken="send_nudge",
                result="failed",
                amount_at_risk_inr=5000,
                amount_recovered_inr=0,
                risk_score=40,
            ))

            stats = audit.get_summary_stats()
            assert stats["totals"]["entries"] == 2
            assert stats["totals"]["at_risk_inr"] == 15000
            assert stats["totals"]["recovered_inr"] == 10000
            assert stats["totals"]["successful"] == 1


class TestPaymentRecoveryAgent:
    """Test payment recovery agent with audit trail."""

    def test_opted_out_stops_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditTrail(
                db_path=os.path.join(tmp, "test.db"),
                jsonl_path=os.path.join(tmp, "test.jsonl"),
            )
            event = PaymentFailedEvent(
                amount_inr=5000,
                opted_out=True,
                error=RazorpayError(reason=ErrorReason.GATEWAY_ERROR, source=ErrorSource.GATEWAY),
            )
            detection = classify_payment_failure(event)
            risk = RiskAssessment(risk_score=60, recommended_action="auto_retry")
            config = {"stopping_rules": {"max_attempts_per_case": 3}}

            attempt = run_payment_recovery(event, detection, risk, audit, config)

            assert attempt.result == "stopped"
            assert attempt.stopping_rule_triggered == "customer_opted_out"

    def test_fraud_escalates(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditTrail(
                db_path=os.path.join(tmp, "test.db"),
                jsonl_path=os.path.join(tmp, "test.jsonl"),
            )
            event = PaymentFailedEvent(
                amount_inr=25000,
                error=RazorpayError(reason=ErrorReason.FRAUD_FLAGGED, source=ErrorSource.RAZORPAY),
            )
            detection = classify_payment_failure(event)
            risk = RiskAssessment(risk_score=95, recommended_action="escalate_to_human")
            config = {"stopping_rules": {"max_attempts_per_case": 3}}

            attempt = run_payment_recovery(event, detection, risk, audit, config)

            assert attempt.result == "escalated"
            assert attempt.action_taken == "escalate_to_human"
