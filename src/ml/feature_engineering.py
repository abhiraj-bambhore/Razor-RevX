"""
Feature engineering for ML-based risk scoring.
Extracts numerical feature vectors from any Razorpay event type.
"""
from __future__ import annotations

import math
from typing import Any

from src.data.schemas import (
    CheckoutAbandonedEvent,
    CustomerSegment,
    ErrorReason,
    ErrorSource,
    PaymentFailedEvent,
    PaymentMethod,
    ReceivableOverdueEvent,
    SubscriptionFailureEvent,
)

# ── Ordinal encodings ────────────────────────────────────────────────────────

SEGMENT_ORDINAL: dict[CustomerSegment, int] = {
    CustomerSegment.NEW: 0,
    CustomerSegment.RETURNING: 1,
    CustomerSegment.VIP: 2,
    CustomerSegment.PREMIUM: 3,
    CustomerSegment.ENTERPRISE: 4,
}

REASON_INDEX: dict[ErrorReason, int] = {
    ErrorReason.INSUFFICIENT_FUNDS: 0,
    ErrorReason.INVALID_OTP: 1,
    ErrorReason.GATEWAY_ERROR: 2,
    ErrorReason.CARD_DECLINED: 3,
    ErrorReason.PAYMENT_CANCELLED: 4,
    ErrorReason.NETWORK_ERROR: 5,
    ErrorReason.TIMEOUT: 6,
    ErrorReason.FRAUD_FLAGGED: 7,
    ErrorReason.INVALID_EXPIRY: 8,
    ErrorReason.LIMIT_EXCEEDED: 9,
}

SOURCE_INDEX: dict[ErrorSource, int] = {
    ErrorSource.CUSTOMER: 0,
    ErrorSource.GATEWAY: 1,
    ErrorSource.BUSINESS: 2,
    ErrorSource.RAZORPAY: 3,
}

METHOD_INDEX: dict[PaymentMethod, int] = {
    PaymentMethod.UPI: 0,
    PaymentMethod.CARD: 1,
    PaymentMethod.NETBANKING: 2,
    PaymentMethod.WALLET: 3,
    PaymentMethod.EMI: 4,
}

# Feature vector length
FEATURE_DIM = 18

# Feature names for interpretability
FEATURE_NAMES = [
    "amount_log",
    "customer_segment",
    "customer_ltv_log",
    "previous_attempts",
    "error_reason",
    "error_source",
    "payment_method",
    "is_transient_error",
    "is_fraud",
    "is_high_value",
    "is_premium_customer",
    "days_overdue",
    "failed_charge_count",
    "intent_score",
    "time_decay",
    "has_dispute",
    "event_type_code",
    "opted_out",
]


def _safe_log(value: float) -> float:
    """Log1p scaling for monetary amounts."""
    return math.log1p(max(0.0, value))


def extract_features(event: Any, detection: dict | None = None) -> list[float]:
    """
    Extract a fixed-length numerical feature vector from any event type.

    Returns a list of 18 floats suitable for sklearn models.
    """
    features = [0.0] * FEATURE_DIM
    det = detection or {}

    # ── Common fields ────────────────────────────────────────────────────
    if isinstance(event, PaymentFailedEvent):
        features[0] = _safe_log(event.amount_inr)
        features[1] = SEGMENT_ORDINAL.get(event.customer_segment, 1)
        features[2] = _safe_log(event.customer_ltv_inr)
        features[3] = float(event.previous_attempts)
        features[4] = float(REASON_INDEX.get(event.error.reason, 2))
        features[5] = float(SOURCE_INDEX.get(event.error.source, 1))
        features[6] = float(METHOD_INDEX.get(event.method, 0))
        features[7] = 1.0 if event.error.reason in (
            ErrorReason.GATEWAY_ERROR, ErrorReason.NETWORK_ERROR, ErrorReason.TIMEOUT
        ) else 0.0
        features[8] = 1.0 if event.error.reason == ErrorReason.FRAUD_FLAGGED else 0.0
        features[9] = 1.0 if event.amount_inr >= 50000 else 0.0
        features[10] = 1.0 if event.customer_segment in (
            CustomerSegment.PREMIUM, CustomerSegment.ENTERPRISE
        ) else 0.0
        features[16] = 0.0  # payment_failure
        features[17] = 1.0 if event.opted_out else 0.0

    elif isinstance(event, CheckoutAbandonedEvent):
        features[0] = _safe_log(event.amount_inr)
        features[1] = SEGMENT_ORDINAL.get(event.customer_segment, 1)
        features[2] = _safe_log(event.customer_ltv_inr)
        features[3] = float(event.previous_attempts)
        features[13] = float(det.get("intent_score", 0.5))
        features[14] = float(det.get("time_decay_factor", 1.0))
        features[9] = 1.0 if event.amount_inr >= 25000 else 0.0
        features[10] = 1.0 if event.customer_segment in (
            CustomerSegment.PREMIUM, CustomerSegment.ENTERPRISE
        ) else 0.0
        features[16] = 1.0  # checkout_abandonment
        features[17] = 1.0 if event.opted_out else 0.0

    elif isinstance(event, SubscriptionFailureEvent):
        features[0] = _safe_log(event.charge_amount_inr)
        features[1] = SEGMENT_ORDINAL.get(event.customer_segment, 1)
        features[2] = _safe_log(event.customer_ltv_inr)
        features[3] = float(event.previous_attempts)
        features[4] = float(REASON_INDEX.get(event.error.reason, 2))
        features[5] = float(SOURCE_INDEX.get(event.error.source, 1))
        features[12] = float(event.failed_charge_count)
        features[7] = 1.0 if event.error.reason in (
            ErrorReason.GATEWAY_ERROR, ErrorReason.NETWORK_ERROR, ErrorReason.TIMEOUT
        ) else 0.0
        features[10] = 1.0 if event.plan_tier == "premium" else 0.0
        features[16] = 2.0  # subscription_failure
        features[17] = 1.0 if event.opted_out else 0.0

    elif isinstance(event, ReceivableOverdueEvent):
        features[0] = _safe_log(event.amount_inr)
        features[1] = SEGMENT_ORDINAL.get(event.customer_segment, 3)
        features[2] = _safe_log(event.credit_limit_inr)
        features[3] = float(event.previous_attempts)
        features[11] = float(event.days_overdue)
        features[9] = 1.0 if event.amount_inr >= 200000 else 0.0
        features[10] = 1.0 if event.customer_segment == CustomerSegment.ENTERPRISE else 0.0
        features[15] = 1.0 if event.has_active_dispute else 0.0
        features[16] = 3.0  # receivable_overdue
        features[17] = 1.0 if event.opted_out else 0.0

    return features
