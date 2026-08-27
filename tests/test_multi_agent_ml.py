"""
Unit tests for multi-agent hierarchical system and ML fallback model.
"""
import pytest
from datetime import datetime

from src.agents.compliance_agent import validate_action, apply_verdict
from src.agents.supervisor import supervisor_route, supervisor_review_output
from src.audit.audit_trail import AuditTrail
from src.data.schemas import (
    AgentMessage,
    CheckoutAbandonedEvent,
    ComplianceVerdict,
    CustomerSegment,
    ErrorReason,
    ErrorSource,
    FailureMode,
    PaymentFailedEvent,
    PaymentMethod,
    RazorpayError,
    ReceivableOverdueEvent,
    RecoveryAttempt,
    RiskAssessment,
    SubscriptionFailureEvent,
)
from src.ml.feature_engineering import extract_features, FEATURE_DIM
from src.ml.risk_model import RiskMLModel, get_risk_model
from src.utils.llm import llm_compose_recovery_message


class TestFeatureEngineering:
    """Test feature vector extraction across all event types."""

    def test_payment_event_features(self):
        event = PaymentFailedEvent(
            amount_inr=5000,
            error=RazorpayError(reason=ErrorReason.GATEWAY_ERROR, source=ErrorSource.GATEWAY),
            method=PaymentMethod.UPI,
            customer_segment=CustomerSegment.VIP,
        )
        features = extract_features(event)
        assert len(features) == FEATURE_DIM
        assert features[1] == 2.0  # VIP segment ordinal
        assert features[7] == 1.0  # is_transient_error

    def test_checkout_event_features(self):
        event = CheckoutAbandonedEvent(
            amount_inr=15000,
            abandonment_stage="payment_method",
            customer_segment=CustomerSegment.PREMIUM,
        )
        features = extract_features(event, {"intent_score": 0.8, "time_decay_factor": 0.9})
        assert len(features) == FEATURE_DIM
        assert features[13] == 0.8  # intent_score
        assert features[16] == 1.0  # event_type_code (checkout)

    def test_subscription_event_features(self):
        event = SubscriptionFailureEvent(
            charge_amount_inr=2999,
            failed_charge_count=2,
            plan_tier="premium",
            error=RazorpayError(reason=ErrorReason.INSUFFICIENT_FUNDS, source=ErrorSource.CUSTOMER),
        )
        features = extract_features(event)
        assert len(features) == FEATURE_DIM
        assert features[12] == 2.0  # failed_charge_count
        assert features[16] == 2.0  # event_type_code (subscription)

    def test_receivables_event_features(self):
        event = ReceivableOverdueEvent(
            amount_inr=250000,
            days_overdue=45,
            has_active_dispute=True,
            customer_segment=CustomerSegment.ENTERPRISE,
        )
        features = extract_features(event)
        assert len(features) == FEATURE_DIM
        assert features[11] == 45.0  # days_overdue
        assert features[15] == 1.0   # has_active_dispute
        assert features[16] == 3.0   # event_type_code (receivables)


class TestRiskMLModel:
    """Test ML model training, prediction, and fallback integration."""

    def test_model_training_and_predict(self):
        model = RiskMLModel()
        metrics = model.train(n_samples=200)

        assert model.is_fitted is True
        assert metrics["training_samples"] == 200
        assert metrics["risk_mae"] >= 0.0
        assert metrics["action_accuracy"] > 0.0

        event = PaymentFailedEvent(
            amount_inr=10000,
            error=RazorpayError(reason=ErrorReason.GATEWAY_ERROR, source=ErrorSource.GATEWAY),
        )
        prediction = model.predict(event)

        assert "risk_score" in prediction
        assert 0.0 <= prediction["risk_score"] <= 100.0
        assert prediction["model_used"] == "ml_gradient_boosting"
        assert prediction["llm_used"] is False

    def test_singleton_risk_model(self):
        model = get_risk_model()
        assert model.is_fitted is True


class TestSupervisorAgent:
    """Test Supervisor routing decisions and reflection loop."""

    def test_supervisor_routing_payment(self):
        event = PaymentFailedEvent(
            amount_inr=5000,
            error=RazorpayError(reason=ErrorReason.GATEWAY_ERROR, source=ErrorSource.GATEWAY),
        )
        messages = []
        decision = supervisor_route(
            event=event,
            detection={"failure_mode": FailureMode.PAYMENT_FAILURE},
            risk_assessment={"risk_score": 60, "recommended_action": "auto_retry"},
            agent_messages=messages,
        )

        assert decision.selected_agent == "payment_agent"
        assert decision.confidence > 0.5
        assert len(messages) == 1
        assert messages[0].from_agent == "supervisor_agent"

    def test_supervisor_reflection_flags_issues(self):
        event = PaymentFailedEvent(
            amount_inr=100000,
            error=RazorpayError(reason=ErrorReason.FRAUD_FLAGGED, source=ErrorSource.RAZORPAY),
        )
        messages = []
        approved = supervisor_review_output(
            event=event,
            specialist_output={
                "action_taken": "send_nudge",  # Poor action for high risk/fraud
                "result": "pending",
                "agent_reasoning": "fraud flagged event",
            },
            risk_assessment={"risk_score": 95, "recommended_action": "escalate_to_human"},
            agent_messages=messages,
        )

        assert approved is False
        assert len(messages) == 1
        assert messages[0].message_type == "reflection"


class TestComplianceGateAgent:
    """Test Compliance Gate validation and modification rules."""

    def test_opt_out_customer_gets_blocked(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditTrail(
                db_path=os.path.join(tmp, "audit.db"),
                jsonl_path=os.path.join(tmp, "audit.jsonl"),
            )
            event = PaymentFailedEvent(amount_inr=5000, opted_out=True)
            attempt = RecoveryAttempt(
                event_id="evt_test",
                action_taken="send_nudge",
                result="pending",
                amount_recovered_inr=0.0,
            )
            risk = RiskAssessment(risk_score=50)
            messages = []

            verdict = validate_action(
                event=event,
                proposed_attempt=attempt,
                risk=risk,
                audit=audit,
                config={"stopping_rules": {"max_attempts_per_case": 3}},
                agent_messages=messages,
            )

            assert verdict.approved is False
            assert verdict.action_modified == "stopped"

            modified = apply_verdict(attempt, verdict)
            assert modified.action_taken == "stopped"
            assert modified.result == "stopped"

    def test_high_risk_score_escalates(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditTrail(
                db_path=os.path.join(tmp, "audit.db"),
                jsonl_path=os.path.join(tmp, "audit.jsonl"),
            )
            event = PaymentFailedEvent(amount_inr=25000)
            attempt = RecoveryAttempt(
                event_id="evt_high_risk",
                action_taken="auto_retry",
                result="failed",
            )
            risk = RiskAssessment(risk_score=90)  # Over 85 threshold
            messages = []

            verdict = validate_action(
                event=event,
                proposed_attempt=attempt,
                risk=risk,
                audit=audit,
                config={"escalation": {"risk_score_threshold": 85}},
                agent_messages=messages,
            )

            assert verdict.approved is False
            assert verdict.action_modified == "escalate_to_human"


class TestHinglishVoiceTemplates:
    """Test Hinglish and voice channel recovery message generation."""

    def test_voice_channel_message(self):
        msg = llm_compose_recovery_message(
            event_type="payment_failure",
            customer_name="Rahul Sharma",
            amount=5000,
            failure_reason="insufficient_funds",
            channel="voice",
        )
        assert "Rahul" in msg or "Namaste" in msg or "INR" in msg
        assert len(msg) > 10

    def test_hinglish_sms_message(self):
        msg = llm_compose_recovery_message(
            event_type="payment_failure",
            customer_name="Rahul Sharma",
            amount=2500,
            failure_reason="insufficient_funds",
            channel="sms",
        )
        assert len(msg) > 10
