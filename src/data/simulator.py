"""
Generates realistic batches of Razorpay-shaped events for testing.
Uses Faker for realistic Indian data + real Razorpay error code distributions.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Optional

from faker import Faker

from src.data.schemas import (
    CheckoutAbandonedEvent,
    CustomerSegment,
    ErrorReason,
    ErrorSource,
    PaymentFailedEvent,
    PaymentMethod,
    RazorpayError,
    ReceivableOverdueEvent,
    SubscriptionFailureEvent,
)

fake = Faker("en_IN")

# ── Weighted distributions matching real-world Razorpay patterns ─────────────

ERROR_REASON_WEIGHTS: dict[ErrorReason, float] = {
    ErrorReason.INSUFFICIENT_FUNDS: 0.22,
    ErrorReason.GATEWAY_ERROR: 0.18,
    ErrorReason.INVALID_OTP: 0.15,
    ErrorReason.CARD_DECLINED: 0.12,
    ErrorReason.PAYMENT_CANCELLED: 0.10,
    ErrorReason.NETWORK_ERROR: 0.08,
    ErrorReason.TIMEOUT: 0.07,
    ErrorReason.FRAUD_FLAGGED: 0.03,
    ErrorReason.INVALID_EXPIRY: 0.03,
    ErrorReason.LIMIT_EXCEEDED: 0.02,
}

ERROR_SOURCE_MAP: dict[ErrorReason, ErrorSource] = {
    ErrorReason.INSUFFICIENT_FUNDS: ErrorSource.CUSTOMER,
    ErrorReason.GATEWAY_ERROR: ErrorSource.GATEWAY,
    ErrorReason.INVALID_OTP: ErrorSource.CUSTOMER,
    ErrorReason.CARD_DECLINED: ErrorSource.GATEWAY,
    ErrorReason.PAYMENT_CANCELLED: ErrorSource.CUSTOMER,
    ErrorReason.NETWORK_ERROR: ErrorSource.GATEWAY,
    ErrorReason.TIMEOUT: ErrorSource.GATEWAY,
    ErrorReason.FRAUD_FLAGGED: ErrorSource.RAZORPAY,
    ErrorReason.INVALID_EXPIRY: ErrorSource.CUSTOMER,
    ErrorReason.LIMIT_EXCEEDED: ErrorSource.CUSTOMER,
}

PAYMENT_METHOD_WEIGHTS: dict[PaymentMethod, float] = {
    PaymentMethod.UPI: 0.45,
    PaymentMethod.CARD: 0.25,
    PaymentMethod.NETBANKING: 0.15,
    PaymentMethod.WALLET: 0.10,
    PaymentMethod.EMI: 0.05,
}

SEGMENT_WEIGHTS: dict[CustomerSegment, float] = {
    CustomerSegment.NEW: 0.30,
    CustomerSegment.RETURNING: 0.35,
    CustomerSegment.VIP: 0.15,
    CustomerSegment.PREMIUM: 0.12,
    CustomerSegment.ENTERPRISE: 0.08,
}

AMOUNT_RANGES: dict[CustomerSegment, tuple[float, float]] = {
    CustomerSegment.NEW: (199, 4999),
    CustomerSegment.RETURNING: (499, 14999),
    CustomerSegment.VIP: (2999, 49999),
    CustomerSegment.PREMIUM: (9999, 99999),
    CustomerSegment.ENTERPRISE: (49999, 999999),
}

INDIAN_BANKS = [
    "HDFC Bank", "ICICI Bank", "SBI", "Axis Bank", "Kotak Mahindra",
    "Yes Bank", "IndusInd Bank", "Punjab National Bank", "Bank of Baroda",
    "Canara Bank", "Union Bank", "IDFC First Bank",
]

ABANDONMENT_STAGES = ["address", "payment_method", "otp", "review"]
PLAN_NAMES = ["Starter", "Growth", "Pro", "Business", "Enterprise"]
PLAN_TIERS = ["basic", "standard", "premium"]
COMPANY_SUFFIXES = [
    "Technologies", "Solutions", "Infotech", "Digital", "Systems",
    "Enterprises", "Industries", "Services", "Consulting", "Labs",
]


def _weighted_choice(weights: dict) -> any:
    """Pick from dict using probability weights."""
    items = list(weights.keys())
    probs = list(weights.values())
    return random.choices(items, weights=probs, k=1)[0]


def _customer_phone() -> str:
    return f"+91{random.randint(7000000000, 9999999999)}"


def _customer_ltv(segment: CustomerSegment) -> float:
    ltv_ranges = {
        CustomerSegment.NEW: (0, 5000),
        CustomerSegment.RETURNING: (5000, 50000),
        CustomerSegment.VIP: (50000, 200000),
        CustomerSegment.PREMIUM: (200000, 500000),
        CustomerSegment.ENTERPRISE: (500000, 5000000),
    }
    lo, hi = ltv_ranges[segment]
    return round(random.uniform(lo, hi), 2)


def _random_past_datetime(hours_back: int = 72) -> datetime:
    return datetime.utcnow() - timedelta(hours=random.uniform(0, hours_back))


# ── Event generators ─────────────────────────────────────────────────────────

def generate_payment_failed_event() -> PaymentFailedEvent:
    """Generate a single realistic payment.failed event."""
    segment = _weighted_choice(SEGMENT_WEIGHTS)
    reason = _weighted_choice(ERROR_REASON_WEIGHTS)
    lo, hi = AMOUNT_RANGES[segment]
    method = _weighted_choice(PAYMENT_METHOD_WEIGHTS)

    return PaymentFailedEvent(
        created_at=_random_past_datetime(48),
        amount_inr=round(random.uniform(lo, hi), 2),
        method=method,
        bank=random.choice(INDIAN_BANKS) if method in (PaymentMethod.CARD, PaymentMethod.NETBANKING) else None,
        customer_id=f"cust_{fake.uuid4()[:12]}",
        customer_email=fake.email(),
        customer_phone=_customer_phone(),
        customer_segment=segment,
        customer_ltv_inr=_customer_ltv(segment),
        error=RazorpayError(
            code="BAD_REQUEST_ERROR" if ERROR_SOURCE_MAP[reason] == ErrorSource.CUSTOMER else "GATEWAY_ERROR",
            reason=reason,
            source=ERROR_SOURCE_MAP[reason],
            step="payment_authentication" if reason == ErrorReason.INVALID_OTP else "payment_initiation",
            description=f"Payment failed: {reason.value}",
        ),
        opted_out=random.random() < 0.02,
        previous_attempts=random.choices([0, 1, 2], weights=[0.7, 0.2, 0.1], k=1)[0],
    )


def generate_checkout_abandoned_event() -> CheckoutAbandonedEvent:
    """Generate a single realistic checkout.abandoned event."""
    segment = _weighted_choice(SEGMENT_WEIGHTS)
    lo, hi = AMOUNT_RANGES[segment]
    stage = random.choices(
        ABANDONMENT_STAGES, weights=[0.15, 0.40, 0.30, 0.15], k=1
    )[0]

    num_items = random.randint(1, 5)
    cart_items = [
        {
            "name": fake.bs().title(),
            "price": round(random.uniform(199, hi / num_items), 2),
            "qty": random.randint(1, 3),
        }
        for _ in range(num_items)
    ]

    return CheckoutAbandonedEvent(
        created_at=_random_past_datetime(24),
        order_id=f"order_{fake.uuid4()[:14]}",
        amount_inr=round(sum(i["price"] * i["qty"] for i in cart_items), 2),
        cart_items=cart_items,
        abandonment_stage=stage,
        time_spent_seconds=random.randint(15, 600),
        customer_id=f"cust_{fake.uuid4()[:12]}",
        customer_email=fake.email(),
        customer_phone=_customer_phone(),
        customer_segment=segment,
        customer_ltv_inr=_customer_ltv(segment),
        opted_out=random.random() < 0.03,
        previous_attempts=random.choices([0, 1, 2], weights=[0.75, 0.2, 0.05], k=1)[0],
    )


def generate_subscription_failure_event() -> SubscriptionFailureEvent:
    """Generate a single realistic subscription.charged_halted event."""
    segment = _weighted_choice({
        CustomerSegment.RETURNING: 0.35,
        CustomerSegment.VIP: 0.25,
        CustomerSegment.PREMIUM: 0.25,
        CustomerSegment.ENTERPRISE: 0.15,
    })
    tier = random.choice(PLAN_TIERS)
    plan_name = random.choice(PLAN_NAMES)
    reason = _weighted_choice({
        ErrorReason.INSUFFICIENT_FUNDS: 0.35,
        ErrorReason.GATEWAY_ERROR: 0.25,
        ErrorReason.CARD_DECLINED: 0.20,
        ErrorReason.NETWORK_ERROR: 0.10,
        ErrorReason.TIMEOUT: 0.10,
    })

    charge_amounts = {"basic": (299, 999), "standard": (999, 4999), "premium": (4999, 29999)}
    lo, hi = charge_amounts[tier]

    return SubscriptionFailureEvent(
        created_at=_random_past_datetime(72),
        plan_id=f"plan_{fake.uuid4()[:10]}",
        plan_name=f"{plan_name} {tier.title()}",
        plan_tier=tier,
        charge_amount_inr=round(random.uniform(lo, hi), 2),
        billing_cycle=random.choice(["monthly", "quarterly", "yearly"]),
        mandate_id=f"mdt_{fake.uuid4()[:14]}",
        mandate_status=random.choices(["active", "paused"], weights=[0.7, 0.3], k=1)[0],
        failed_charge_count=random.randint(1, 4),
        customer_id=f"cust_{fake.uuid4()[:12]}",
        customer_email=fake.email(),
        customer_phone=_customer_phone(),
        customer_segment=segment,
        customer_ltv_inr=_customer_ltv(segment),
        error=RazorpayError(
            reason=reason,
            source=ERROR_SOURCE_MAP[reason],
            description=f"Subscription charge failed: {reason.value}",
        ),
        opted_out=random.random() < 0.02,
        previous_attempts=random.choices([0, 1, 2], weights=[0.65, 0.25, 0.1], k=1)[0],
    )


def generate_receivable_overdue_event() -> ReceivableOverdueEvent:
    """Generate a single realistic receivable.overdue event."""
    days_overdue = random.choices(
        [random.randint(1, 30), random.randint(31, 60),
         random.randint(61, 90), random.randint(91, 180)],
        weights=[0.40, 0.30, 0.20, 0.10],
        k=1,
    )[0]

    if days_overdue <= 30:
        bucket = "0-30"
    elif days_overdue <= 60:
        bucket = "31-60"
    elif days_overdue <= 90:
        bucket = "61-90"
    else:
        bucket = "90+"

    company = f"{fake.last_name()} {random.choice(COMPANY_SUFFIXES)} Pvt. Ltd."
    amount = round(random.uniform(25000, 1500000), 2)
    due_date = datetime.utcnow() - timedelta(days=days_overdue)
    invoice_date = due_date - timedelta(days=random.randint(15, 45))

    return ReceivableOverdueEvent(
        created_at=_random_past_datetime(48),
        invoice_number=f"INV-{random.randint(2024, 2026)}-{random.randint(1000, 9999)}",
        invoice_date=invoice_date,
        due_date=due_date,
        amount_inr=amount,
        days_overdue=days_overdue,
        aging_bucket=bucket,
        customer_id=f"cust_{fake.uuid4()[:12]}",
        company_name=company,
        contact_name=fake.name(),
        contact_email=fake.company_email(),
        contact_phone=_customer_phone(),
        customer_segment=random.choices(
            [CustomerSegment.VIP, CustomerSegment.PREMIUM, CustomerSegment.ENTERPRISE],
            weights=[0.3, 0.3, 0.4], k=1,
        )[0],
        credit_limit_inr=round(amount * random.uniform(1.5, 5.0), 2),
        previous_payment_delays=random.choices([0, 1, 2, 3, 4], weights=[0.4, 0.25, 0.2, 0.1, 0.05], k=1)[0],
        has_active_dispute=random.random() < 0.08,
        opted_out=random.random() < 0.01,
        previous_attempts=random.choices([0, 1, 2], weights=[0.6, 0.3, 0.1], k=1)[0],
    )


# ── Batch generator ──────────────────────────────────────────────────────────

def generate_batch(
    batch_size: int = 100,
    mode: str = "all",
) -> list:
    """
    Generate a mixed batch of revenue-at-risk events.

    Args:
        batch_size: Total number of events to generate.
        mode: 'all' | 'payment' | 'checkout' | 'subscription' | 'receivables'

    Returns:
        List of event objects.
    """
    generators = {
        "payment": generate_payment_failed_event,
        "checkout": generate_checkout_abandoned_event,
        "subscription": generate_subscription_failure_event,
        "receivables": generate_receivable_overdue_event,
    }

    if mode == "all":
        # Realistic distribution of failure types
        weights = {"payment": 0.30, "checkout": 0.35, "subscription": 0.20, "receivables": 0.15}
        events = []
        for _ in range(batch_size):
            event_type = random.choices(
                list(weights.keys()), weights=list(weights.values()), k=1
            )[0]
            events.append(generators[event_type]())
        return events

    if mode not in generators:
        raise ValueError(f"Unknown mode: {mode}. Use: all, payment, checkout, subscription, receivables")

    return [generators[mode]() for _ in range(batch_size)]
