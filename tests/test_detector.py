"""
Tests for detection layer — all 4 failure mode detectors.
"""
import pytest
from datetime import datetime, timedelta

from src.data.schemas import (
    CheckoutAbandonedEvent,
    CustomerSegment,
    ErrorReason,
    ErrorSource,
    FailureMode,
    PaymentFailedEvent,
    PaymentMethod,
    RazorpayError,
    ReceivableOverdueEvent,
    RiskTier,
    SubscriptionFailureEvent,
)
from src.detection.checkout_detector import classify_checkout_abandonment
from src.detection.payment_detector import classify_payment_failure
from src.detection.receivables_detector import classify_receivable
from src.detection.subscription_detector import classify_subscription_failure


class TestPaymentDetector:
    """Test payment failure detection and classification."""

    def test_transient_gateway_error_is_retryable(self):
        event = PaymentFailedEvent(
            amount_inr=5000,
            error=RazorpayError(reason=ErrorReason.GATEWAY_ERROR, source=ErrorSource.GATEWAY),
        )
        result = classify_payment_failure(event)
        assert result["is_retryable"] is True
        assert result["urgency"] == "immediate"
        assert result["root_cause_category"] == "transient_infra"
        assert result["recommended_first_action"] == "auto_retry"

    def test_insufficient_funds_needs_customer_action(self):
        event = PaymentFailedEvent(
            amount_inr=2500,
            error=RazorpayError(reason=ErrorReason.INSUFFICIENT_FUNDS, source=ErrorSource.CUSTOMER),
        )
        result = classify_payment_failure(event)
        assert result["is_retryable"] is False
        assert result["root_cause_category"] == "customer_action_needed"
        assert result["recommended_first_action"] == "send_recovery_link"

    def test_fraud_flagged_never_retry(self):
        event = PaymentFailedEvent(
            amount_inr=100000,
            error=RazorpayError(reason=ErrorReason.FRAUD_FLAGGED, source=ErrorSource.RAZORPAY),
        )
        result = classify_payment_failure(event)
        assert result["is_retryable"] is False
        assert result["urgency"] == "do_not_retry"
        assert result["recommended_first_action"] == "escalate_to_human"

    def test_high_amount_is_critical_risk(self):
        event = PaymentFailedEvent(
            amount_inr=75000,
            error=RazorpayError(reason=ErrorReason.GATEWAY_ERROR, source=ErrorSource.GATEWAY),
        )
        result = classify_payment_failure(event)
        assert result["risk_tier"] == RiskTier.CRITICAL

    def test_low_amount_is_low_risk(self):
        event = PaymentFailedEvent(
            amount_inr=500,
            error=RazorpayError(reason=ErrorReason.GATEWAY_ERROR, source=ErrorSource.GATEWAY),
        )
        result = classify_payment_failure(event)
        assert result["risk_tier"] == RiskTier.LOW

    def test_failure_mode_is_payment(self):
        event = PaymentFailedEvent(
            amount_inr=1000,
            error=RazorpayError(reason=ErrorReason.TIMEOUT, source=ErrorSource.GATEWAY),
        )
        result = classify_payment_failure(event)
        assert result["failure_mode"] == FailureMode.PAYMENT_FAILURE


class TestCheckoutDetector:
    """Test checkout abandonment detection."""

    def test_otp_stage_is_high_intent(self):
        event = CheckoutAbandonedEvent(
            amount_inr=3000,
            abandonment_stage="otp",
            time_spent_seconds=120,
            created_at=datetime.utcnow() - timedelta(minutes=10),
        )
        result = classify_checkout_abandonment(event)
        assert result["intent_score"] >= 0.7
        assert result["urgency"] == "immediate"

    def test_address_stage_is_low_intent(self):
        event = CheckoutAbandonedEvent(
            amount_inr=1000,
            abandonment_stage="address",
            time_spent_seconds=20,
            created_at=datetime.utcnow() - timedelta(minutes=5),
        )
        result = classify_checkout_abandonment(event)
        assert result["intent_score"] <= 0.5

    def test_time_decay_reduces_over_time(self):
        old_event = CheckoutAbandonedEvent(
            amount_inr=2000,
            abandonment_stage="payment_method",
            created_at=datetime.utcnow() - timedelta(hours=40),
        )
        fresh_event = CheckoutAbandonedEvent(
            amount_inr=2000,
            abandonment_stage="payment_method",
            created_at=datetime.utcnow() - timedelta(minutes=30),
        )
        old_result = classify_checkout_abandonment(old_event)
        fresh_result = classify_checkout_abandonment(fresh_event)
        assert old_result["time_decay_factor"] < fresh_result["time_decay_factor"]


class TestSubscriptionDetector:
    """Test subscription failure detection."""

    def test_mandate_retry_eligible_for_gateway_error(self):
        event = SubscriptionFailureEvent(
            charge_amount_inr=999,
            mandate_status="active",
            failed_charge_count=1,
            error=RazorpayError(reason=ErrorReason.GATEWAY_ERROR, source=ErrorSource.GATEWAY),
        )
        result = classify_subscription_failure(event)
        assert result["mandate_retry_eligible"] is True
        assert result["recommended_first_action"] == "mandate_retry"

    def test_mandate_not_eligible_after_max_retries(self):
        event = SubscriptionFailureEvent(
            charge_amount_inr=999,
            mandate_status="active",
            failed_charge_count=4,  # Over max (3)
            error=RazorpayError(reason=ErrorReason.GATEWAY_ERROR, source=ErrorSource.GATEWAY),
        )
        result = classify_subscription_failure(event)
        assert result["mandate_retry_eligible"] is False

    def test_high_churn_risk_after_multiple_failures(self):
        event = SubscriptionFailureEvent(
            charge_amount_inr=4999,
            failed_charge_count=3,
            plan_tier="premium",
            error=RazorpayError(reason=ErrorReason.INSUFFICIENT_FUNDS, source=ErrorSource.CUSTOMER),
        )
        result = classify_subscription_failure(event)
        assert result["churn_risk"] in ("high", "critical")


class TestReceivablesDetector:
    """Test receivables aging detection."""

    def test_fresh_invoice_is_low_risk(self):
        event = ReceivableOverdueEvent(
            amount_inr=50000,
            days_overdue=15,
            aging_bucket="0-30",
        )
        result = classify_receivable(event)
        assert result["aging_risk"] == "low_risk"
        assert result["recommended_first_action"] == "email_reminder"

    def test_old_invoice_is_write_off_risk(self):
        event = ReceivableOverdueEvent(
            amount_inr=200000,
            days_overdue=120,
            aging_bucket="90+",
        )
        result = classify_receivable(event)
        assert result["aging_risk"] == "write_off_risk"
        assert result["recommended_first_action"] == "escalate_to_human"

    def test_active_dispute_holds_dunning(self):
        event = ReceivableOverdueEvent(
            amount_inr=100000,
            days_overdue=45,
            aging_bucket="31-60",
            has_active_dispute=True,
        )
        result = classify_receivable(event)
        assert result["urgency"] == "hold"
        assert result["recommended_first_action"] == "escalate_to_human"

    def test_serial_late_payer_lower_collection_prob(self):
        good_payer = ReceivableOverdueEvent(
            amount_inr=50000,
            days_overdue=20,
            previous_payment_delays=0,
        )
        bad_payer = ReceivableOverdueEvent(
            amount_inr=50000,
            days_overdue=20,
            previous_payment_delays=4,
        )
        good_result = classify_receivable(good_payer)
        bad_result = classify_receivable(bad_payer)
        assert bad_result["collection_probability"] < good_result["collection_probability"]
