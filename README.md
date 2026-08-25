# 🤖 Razorpay AI Revenue Recovery Agent

A **production-grade, stateful multi-agent system** that detects revenue at risk across 4 failure modes, diagnoses root causes, selects the optimal intervention, executes it within compliance guardrails, and surfaces a measurable audit trail of money recovered — across a full batch run.

## Architecture

```
Detection Layer → LangGraph Orchestrator → Specialized Agents → Action Layer → Audit Trail
```

## 4 Failure Modes Covered

| Mode | Signal | Recovery Strategy |
|---|---|---|
| **Payment Failure** | `payment.failed` webhook | Root-cause → retry / recovery link / escalate |
| **Checkout Abandonment** | `ondismiss` + no capture | Time-decayed nudge → recovery link |
| **Subscription Degradation** | `subscription.charged_halted` | Mandate retry → plan downgrade offer |
| **B2B Overdue Receivables** | Invoice age > SLA | Dunning sequence → PTP tracker → legal flag |

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env: add GEMINI_API_KEY (optional, fallback works without it)

# 3. Run full recovery batch
python run_recovery.py --batch-size 100 --mode all --report

# 4. Run specific mode
python run_recovery.py --batch-size 50 --mode payment --report
python run_recovery.py --batch-size 50 --mode checkout --report
python run_recovery.py --batch-size 50 --mode subscription --report
python run_recovery.py --batch-size 50 --mode receivables --report

# 5. Run tests
pytest tests/ -v
```

## Output

- **Console**: Real-time agent decisions with reasoning
- **Audit DB**: `audit/recovery_audit.db` — immutable SQLite log
- **Report**: `audit/recovery_report_<timestamp>.json` — INR recovered metrics

## Key Design Decisions

- **Root-cause-aware recovery**: `insufficient_funds` ≠ `gateway_error` — each gets the right intervention
- **Stopping rules enforced at graph level**: max 3 attempts, 48h cooldown, opt-out check
- **HITL escalation**: Cases >₹50,000 pause for human review before acting
- **Full audit trail**: Every AI decision + reasoning stored in SQLite, exportable as JSONL
- **Promise-to-pay tracker**: B2B cases record PTP dates with follow-up scheduling

## Configuration

Edit `config/recovery_config.yaml` to tune dunning schedules, stopping rules, and escalation thresholds — no code changes needed.
