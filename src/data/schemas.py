"""
Pydantic schemas for all Razorpay event types and internal models.
Mirrors actual Razorpay webhook payload structure.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional, Any
from pydantic import BaseModel, Field, ConfigDict
import uuid


# ── Razorpay error taxonomy ──────────────────────────────────────────────────

class ErrorSource(str, Enum):
    CUSTOMER = "customer"    # Customer action needed (wrong OTP, cancelled)
    GATEWAY = "gateway"      # Bank/gateway transient issue — retry safe
    BUSINESS = "business"    # Integration/config error — do NOT retry blindly
    RAZORPAY = "razorpay"    # Internal — retry after delay


class ErrorReason(str, Enum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    INVALID_OTP = "invalid_otp"
    GATEWAY_ERROR = "gateway_error"
    CARD_DECLINED = "card_declined"
    PAYMENT_CANCELLED = "payment_cancelled"
    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    FRAUD_FLAGGED = "fraud_flagged"
    INVALID_EXPIRY = "invalid_expiry"
    LIMIT_EXCEEDED = "limit_exceeded"


class PaymentMethod(str, Enum):
    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    EMI = "emi"


class CustomerSegment(str, Enum):
    NEW = "new"
    RETURNING = "returning"
    VIP = "vip"
    PREMIUM = "premium"
    ENTERPRISE = "enterprise"


class FailureMode(str, Enum):
    PAYMENT_FAILURE = "payment_failure"
    CHECKOUT_ABANDONMENT = "checkout_abandonment"
    SUBSCRIPTION_FAILURE = "subscription_failure"
    RECEIVABLE_OVERDUE = "receivable_overdue"


class RiskTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RecoveryStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RECOVERED = "recovered"
    ESCALATED = "escalated"
    STOPPED = "stopped"          # Stopping rule triggered
    OPTED_OUT = "opted_out"
    UNRECOVERABLE = "unrecoverable"


# ── Event schemas (mirrors Razorpay webhook shapes) ──────────────────────────

class RazorpayError(BaseModel):
    code: str = ""
    reason: ErrorReason = ErrorReason.GATEWAY_ERROR
    source: ErrorSource = ErrorSource.GATEWAY
    step: str = "payment_initiation"
    description: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class PaymentFailedEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:16]}")
    event_type: str = "payment.failed"
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Payment entity
    payment_id: str = Field(default_factory=lambda: f"pay_{uuid.uuid4().hex[:14]}")
    order_id: str = Field(default_factory=lambda: f"order_{uuid.uuid4().hex[:14]}")
    amount_inr: float = 0.0          # Amount in INR (not paise)
    currency: str = "INR"
    method: PaymentMethod = PaymentMethod.UPI
    bank: Optional[str] = None
    wallet: Optional[str] = None

    # Customer
    customer_id: str = ""
    customer_email: str = ""
    customer_phone: str = ""
    customer_segment: CustomerSegment = CustomerSegment.RETURNING
    customer_ltv_inr: float = 0.0    # Customer Lifetime Value

    # Error details
    error: RazorpayError = Field(default_factory=RazorpayError)

    # Opt-out
    opted_out: bool = False
    previous_attempts: int = 0


class CheckoutAbandonedEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:16]}")
    event_type: str = "checkout.abandoned"
    created_at: datetime = Field(default_factory=datetime.utcnow)

    order_id: str = Field(default_factory=lambda: f"order_{uuid.uuid4().hex[:14]}")
    amount_inr: float = 0.0
    cart_items: list[dict[str, Any]] = Field(default_factory=list)

    # Where they dropped
    abandonment_stage: str = "payment_method"   # address | payment_method | otp | review
    time_spent_seconds: int = 0

    customer_id: str = ""
    customer_email: str = ""
    customer_phone: str = ""
    customer_segment: CustomerSegment = CustomerSegment.RETURNING
    customer_ltv_inr: float = 0.0

    opted_out: bool = False
    previous_attempts: int = 0


class SubscriptionFailureEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:16]}")
    event_type: str = "subscription.charged_halted"
    created_at: datetime = Field(default_factory=datetime.utcnow)

    subscription_id: str = Field(default_factory=lambda: f"sub_{uuid.uuid4().hex[:14]}")
    plan_id: str = ""
    plan_name: str = ""
    plan_tier: str = "basic"         # basic | standard | premium
    charge_amount_inr: float = 0.0
    billing_cycle: str = "monthly"

    mandate_id: str = ""
    mandate_status: str = "active"   # active | paused | cancelled
    failed_charge_count: int = 1
    next_retry_at: Optional[datetime] = None

    customer_id: str = ""
    customer_email: str = ""
    customer_phone: str = ""
    customer_segment: CustomerSegment = CustomerSegment.RETURNING
    customer_ltv_inr: float = 0.0

    error: RazorpayError = Field(default_factory=RazorpayError)
    opted_out: bool = False
    previous_attempts: int = 0


class ReceivableOverdueEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:16]}")
    event_type: str = "receivable.overdue"
    created_at: datetime = Field(default_factory=datetime.utcnow)

    invoice_id: str = Field(default_factory=lambda: f"inv_{uuid.uuid4().hex[:14]}")
    invoice_number: str = ""
    invoice_date: datetime = Field(default_factory=datetime.utcnow)
    due_date: datetime = Field(default_factory=datetime.utcnow)
    amount_inr: float = 0.0
    days_overdue: int = 0
    aging_bucket: str = "0-30"       # 0-30 | 31-60 | 61-90 | 90+

    # B2B customer
    customer_id: str = ""
    company_name: str = ""
    contact_name: str = ""
    contact_email: str = ""
    contact_phone: str = ""
    customer_segment: CustomerSegment = CustomerSegment.ENTERPRISE
    credit_limit_inr: float = 0.0

    # Payment history
    previous_payment_delays: int = 0  # How many times paid late before
    has_active_dispute: bool = False
    promise_to_pay_date: Optional[datetime] = None

    opted_out: bool = False
    previous_attempts: int = 0


# ── Internal agent state ─────────────────────────────────────────────────────

class RiskAssessment(BaseModel):
    risk_score: float = 0.0          # 0–100, higher = more urgent
    risk_tier: RiskTier = RiskTier.MEDIUM
    recommended_action: str = ""
    reasoning: str = ""
    needs_human_review: bool = False
    estimated_recovery_probability: float = 0.0  # 0.0–1.0
    llm_used: bool = False


class RecoveryAttempt(BaseModel):
    attempt_id: str = Field(default_factory=lambda: f"att_{uuid.uuid4().hex[:12]}")
    event_id: str = ""
    attempt_number: int = 1
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    action_taken: str = ""
    channel: str = ""
    result: str = ""                 # success | failed | pending | stopped
    amount_recovered_inr: float = 0.0
    stopping_rule_triggered: Optional[str] = None
    agent_reasoning: str = ""


class AuditEntry(BaseModel):
    audit_id: str = Field(default_factory=lambda: f"aud_{uuid.uuid4().hex[:16]}")
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    event_id: str = ""
    failure_mode: FailureMode = FailureMode.PAYMENT_FAILURE
    agent_name: str = ""
    action_taken: str = ""
    result: str = ""
    amount_at_risk_inr: float = 0.0
    amount_recovered_inr: float = 0.0
    risk_score: float = 0.0
    llm_reasoning: str = ""
    compliance_flags: list[str] = Field(default_factory=list)
    stopping_rule_triggered: Optional[str] = None
    escalated_to_human: bool = False
    human_decision: Optional[str] = None


# ── Batch report ──────────────────────────────────────────────────────────────

class BatchRecoveryReport(BaseModel):
    report_id: str = Field(default_factory=lambda: f"rpt_{uuid.uuid4().hex[:12]}")
    generated_at: datetime = Field(default_factory=datetime.utcnow)
    batch_size: int = 0
    mode: str = "all"

    total_at_risk_inr: float = 0.0
    total_recovered_inr: float = 0.0
    recovery_rate_pct: float = 0.0

    by_failure_mode: dict[str, dict[str, float]] = Field(default_factory=dict)

    total_attempts: int = 0
    stopping_rules_triggered: int = 0
    escalated_to_human: int = 0
    opted_out: int = 0

    agent_performance: dict[str, dict[str, Any]] = Field(default_factory=dict)


# ── Multi-Agent communication schemas ────────────────────────────────────────

class AgentMessage(BaseModel):
    """Message passed between agents in the multi-agent pipeline."""
    from_agent: str = ""
    to_agent: str = ""
    message_type: str = ""           # routing | proposal | verdict | reflection
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class SupervisorDecision(BaseModel):
    """Decision made by the Supervisor agent for routing."""
    decision_id: str = Field(default_factory=lambda: f"dec_{uuid.uuid4().hex[:12]}")
    selected_agent: str = ""         # payment | checkout | subscription | receivables
    reasoning: str = ""
    confidence: float = 0.0          # 0-1
    requires_compliance_check: bool = True
    allow_reflection: bool = False   # Can supervisor re-route after specialist output?
    llm_used: bool = False
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ComplianceVerdict(BaseModel):
    """Verdict from the Compliance Gate agent."""
    verdict_id: str = Field(default_factory=lambda: f"cvd_{uuid.uuid4().hex[:12]}")
    approved: bool = True
    action_allowed: str = ""         # The original proposed action
    action_modified: str = ""        # Modified action (if compliance changed it)
    violations: list[str] = Field(default_factory=list)  # List of rules violated
    reasoning: str = ""
    escalated: bool = False
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class MLModelMetrics(BaseModel):
    """Tracks ML model performance across batches."""
    model_config = ConfigDict(protected_namespaces=())
    model_name: str = ""
    risk_mae: float = 0.0
    action_accuracy: float = 0.0
    training_samples: int = 0
    predictions_made: int = 0
    top_features: list[tuple[str, float]] = Field(default_factory=list)
