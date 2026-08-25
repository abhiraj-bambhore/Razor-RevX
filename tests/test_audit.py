"""
Tests for the full audit trail and reporting pipeline.
"""
import json
import os
import tempfile

import pytest

from src.audit.audit_trail import AuditTrail
from src.data.schemas import AuditEntry, FailureMode


class TestAuditImmutability:
    """Verify audit trail is append-only."""

    def test_entries_are_persisted_to_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "audit.db")
            jsonl_path = os.path.join(tmp, "audit.jsonl")
            audit = AuditTrail(db_path=db_path, jsonl_path=jsonl_path)

            audit.log(AuditEntry(
                event_id="evt_persist",
                failure_mode=FailureMode.PAYMENT_FAILURE,
                agent_name="test",
                action_taken="test",
                result="success",
                amount_at_risk_inr=1000,
                amount_recovered_inr=1000,
                risk_score=50,
            ))

            # Verify DB file exists and has data
            assert os.path.exists(db_path)
            entries = audit.get_all_entries()
            assert len(entries) == 1

    def test_entries_are_persisted_to_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "audit.db")
            jsonl_path = os.path.join(tmp, "audit.jsonl")
            audit = AuditTrail(db_path=db_path, jsonl_path=jsonl_path)

            audit.log(AuditEntry(
                event_id="evt_jsonl",
                failure_mode=FailureMode.CHECKOUT_ABANDONMENT,
                agent_name="test",
                action_taken="nudge",
                result="pending",
                amount_at_risk_inr=2000,
                amount_recovered_inr=0,
                risk_score=40,
            ))

            # Verify JSONL
            assert os.path.exists(jsonl_path)
            with open(jsonl_path, "r") as f:
                lines = f.readlines()
            assert len(lines) == 1
            data = json.loads(lines[0])
            assert data["event_id"] == "evt_jsonl"

    def test_multiple_entries_accumulate(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditTrail(
                db_path=os.path.join(tmp, "audit.db"),
                jsonl_path=os.path.join(tmp, "audit.jsonl"),
            )

            for i in range(10):
                audit.log(AuditEntry(
                    event_id=f"evt_{i}",
                    failure_mode=FailureMode.SUBSCRIPTION_FAILURE,
                    agent_name="test",
                    action_taken="test",
                    result="success" if i % 2 == 0 else "failed",
                    amount_at_risk_inr=1000 * (i + 1),
                    amount_recovered_inr=1000 * (i + 1) if i % 2 == 0 else 0,
                    risk_score=50,
                ))

            entries = audit.get_all_entries()
            assert len(entries) == 10

    def test_summary_stats_by_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditTrail(
                db_path=os.path.join(tmp, "audit.db"),
                jsonl_path=os.path.join(tmp, "audit.jsonl"),
            )

            audit.log(AuditEntry(
                event_id="evt_pay",
                failure_mode=FailureMode.PAYMENT_FAILURE,
                agent_name="payment_agent",
                action_taken="retry",
                result="success",
                amount_at_risk_inr=5000,
                amount_recovered_inr=5000,
                risk_score=60,
            ))
            audit.log(AuditEntry(
                event_id="evt_sub",
                failure_mode=FailureMode.SUBSCRIPTION_FAILURE,
                agent_name="subscription_agent",
                action_taken="mandate_retry",
                result="success",
                amount_at_risk_inr=999,
                amount_recovered_inr=999,
                risk_score=55,
            ))

            stats = audit.get_summary_stats()
            assert "payment_failure" in stats["by_mode"]
            assert "subscription_failure" in stats["by_mode"]
            assert stats["by_mode"]["payment_failure"]["recovered_inr"] == 5000

    def test_entries_by_mode_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit = AuditTrail(
                db_path=os.path.join(tmp, "audit.db"),
                jsonl_path=os.path.join(tmp, "audit.jsonl"),
            )

            for mode in [FailureMode.PAYMENT_FAILURE, FailureMode.CHECKOUT_ABANDONMENT, FailureMode.PAYMENT_FAILURE]:
                audit.log(AuditEntry(
                    event_id=f"evt_{mode.value}",
                    failure_mode=mode,
                    agent_name="test",
                    action_taken="test",
                    result="success",
                    amount_at_risk_inr=1000,
                    amount_recovered_inr=1000,
                    risk_score=50,
                ))

            payment_entries = audit.get_entries_by_mode("payment_failure")
            assert len(payment_entries) == 2
            checkout_entries = audit.get_entries_by_mode("checkout_abandonment")
            assert len(checkout_entries) == 1
