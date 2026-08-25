"""
Immutable SQLite + JSONL audit trail.
Every agent action, decision, and outcome is recorded here.
Records are append-only — no UPDATE or DELETE operations.
"""
from __future__ import annotations

import json
import os
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.data.schemas import AuditEntry, FailureMode

logger = logging.getLogger(__name__)


class AuditTrail:
    """Immutable audit trail backed by SQLite + JSONL."""

    def __init__(self, db_path: str = "audit/recovery_audit.db", jsonl_path: str = "audit/recovery_audit.jsonl"):
        self.db_path = db_path
        self.jsonl_path = jsonl_path

        # Ensure audit directory exists
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(jsonl_path).parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _init_db(self):
        """Create the audit table if it doesn't exist."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                audit_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                event_id TEXT NOT NULL,
                failure_mode TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                action_taken TEXT NOT NULL,
                result TEXT NOT NULL,
                amount_at_risk_inr REAL NOT NULL,
                amount_recovered_inr REAL NOT NULL,
                risk_score REAL NOT NULL,
                llm_reasoning TEXT,
                compliance_flags TEXT,
                stopping_rule_triggered TEXT,
                escalated_to_human INTEGER DEFAULT 0,
                human_decision TEXT
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_event_id ON audit_log(event_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_failure_mode ON audit_log(failure_mode)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_result ON audit_log(result)
        """)
        conn.commit()
        conn.close()
        logger.info("Audit DB initialized: %s", self.db_path)

    def log(self, entry: AuditEntry):
        """Write a single audit entry to both SQLite and JSONL. Append-only."""
        # SQLite
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO audit_log (
                audit_id, timestamp, event_id, failure_mode, agent_name,
                action_taken, result, amount_at_risk_inr, amount_recovered_inr,
                risk_score, llm_reasoning, compliance_flags,
                stopping_rule_triggered, escalated_to_human, human_decision
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.audit_id,
                entry.timestamp.isoformat(),
                entry.event_id,
                entry.failure_mode.value,
                entry.agent_name,
                entry.action_taken,
                entry.result,
                entry.amount_at_risk_inr,
                entry.amount_recovered_inr,
                entry.risk_score,
                entry.llm_reasoning,
                json.dumps(entry.compliance_flags),
                entry.stopping_rule_triggered,
                1 if entry.escalated_to_human else 0,
                entry.human_decision,
            ),
        )
        conn.commit()
        conn.close()

        # JSONL (for easy export / streaming)
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")

    def get_attempts_for_event(self, event_id: str) -> int:
        """Count how many attempts exist for a given event_id."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event_id = ?",
            (event_id,),
        )
        count = cursor.fetchone()[0]
        conn.close()
        return count

    def get_all_entries(self) -> list[dict]:
        """Retrieve all audit entries (for reporting)."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_log ORDER BY timestamp")
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def get_entries_by_mode(self, failure_mode: str) -> list[dict]:
        """Retrieve audit entries filtered by failure mode."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM audit_log WHERE failure_mode = ? ORDER BY timestamp",
            (failure_mode,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return rows

    def get_summary_stats(self) -> dict:
        """Get summary statistics for reporting."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        stats = {}

        # Total at risk vs recovered
        cursor.execute("""
            SELECT
                COUNT(*) as total_entries,
                COALESCE(SUM(amount_at_risk_inr), 0) as total_at_risk,
                COALESCE(SUM(amount_recovered_inr), 0) as total_recovered,
                COUNT(CASE WHEN result = 'success' THEN 1 END) as successful,
                COUNT(CASE WHEN result = 'failed' THEN 1 END) as failed,
                COUNT(CASE WHEN result = 'pending' THEN 1 END) as pending,
                COUNT(CASE WHEN result = 'stopped' THEN 1 END) as stopped,
                COUNT(CASE WHEN escalated_to_human = 1 THEN 1 END) as escalated,
                COUNT(CASE WHEN stopping_rule_triggered IS NOT NULL THEN 1 END) as stopping_rules_hit
            FROM audit_log
        """)
        row = cursor.fetchone()
        stats["totals"] = {
            "entries": row[0],
            "at_risk_inr": row[1],
            "recovered_inr": row[2],
            "successful": row[3],
            "failed": row[4],
            "pending": row[5],
            "stopped": row[6],
            "escalated": row[7],
            "stopping_rules_hit": row[8],
        }

        # By failure mode
        cursor.execute("""
            SELECT
                failure_mode,
                COUNT(*) as entries,
                COALESCE(SUM(amount_at_risk_inr), 0) as at_risk,
                COALESCE(SUM(amount_recovered_inr), 0) as recovered,
                COUNT(CASE WHEN result = 'success' THEN 1 END) as successful
            FROM audit_log
            GROUP BY failure_mode
        """)
        stats["by_mode"] = {}
        for row in cursor.fetchall():
            mode = row[0]
            stats["by_mode"][mode] = {
                "entries": row[1],
                "at_risk_inr": row[2],
                "recovered_inr": row[3],
                "successful": row[4],
                "recovery_rate": round(row[3] / row[2] * 100, 1) if row[2] > 0 else 0,
            }

        # By agent
        cursor.execute("""
            SELECT
                agent_name,
                COUNT(*) as actions,
                COALESCE(SUM(amount_recovered_inr), 0) as recovered,
                COUNT(CASE WHEN result = 'success' THEN 1 END) as successful
            FROM audit_log
            GROUP BY agent_name
        """)
        stats["by_agent"] = {}
        for row in cursor.fetchall():
            stats["by_agent"][row[0]] = {
                "actions": row[1],
                "recovered_inr": row[2],
                "successful": row[3],
            }

        conn.close()
        return stats

    def update_result(self, event_id: str, new_result: str, amount_recovered: float):
        """Update result and amount_recovered for an event (e.g. customer converted via link)."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE audit_log 
            SET result = ?, amount_recovered_inr = ? 
            WHERE event_id = ?
            """,
            (new_result, amount_recovered, event_id),
        )
        conn.commit()
        conn.close()
        logger.info("Updated audit log: event_id=%s -> result=%s, recovered=INR %.2f", event_id, new_result, amount_recovered)

    def clear(self):
        """Clear all audit data. Only for testing — never in production."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM audit_log")
        conn.commit()
        conn.close()

        # Clear JSONL
        if os.path.exists(self.jsonl_path):
            open(self.jsonl_path, "w").close()

        logger.warning("Audit trail cleared (test mode only)")
