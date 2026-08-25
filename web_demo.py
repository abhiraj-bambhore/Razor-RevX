"""
Razorpay AI Revenue Recovery Agent — Web Demo Server.

Runs a lightweight HTTP server serving the interactive demonstration dashboard
and REST API for real-time failure simulation, pipeline visualization, and audit tracking.

Usage:
    python web_demo.py --port 8000
"""
from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn

from dotenv import load_dotenv

load_dotenv()

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.agents.orchestrator import build_recovery_graph, load_config, RecoveryState
from src.audit.audit_trail import AuditTrail
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
    RecoveryAttempt,
    RiskAssessment,
    SubscriptionFailureEvent,
)
from src.data.simulator import generate_batch

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("web_demo")

AUDIT_DB_PATH = "audit/recovery_audit.db"
AUDIT_JSONL_PATH = "audit/recovery_audit.jsonl"
CONFIG_PATH = "config/recovery_config.yaml"

# Global compiled graph and audit trail
audit_trail = AuditTrail(db_path=AUDIT_DB_PATH, jsonl_path=AUDIT_JSONL_PATH)
config_data = load_config(CONFIG_PATH)
recovery_graph = build_recovery_graph().compile()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""
    daemon_threads = True


def json_serializer(obj):
    """Custom JSON serializer for datetime, enum, Pydantic objects."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "value"):
        return obj.value
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


class APIHandler(SimpleHTTPRequestHandler):
    """HTTP Handler for static files and REST API endpoints."""

    def __init__(self, *args, **kwargs):
        web_dir = Path(__file__).parent / "web"
        super().__init__(*args, directory=str(web_dir), **kwargs)

    def do_GET(self):
        if self.path == "/api/stats":
            self._handle_get_stats()
        elif self.path == "/api/audit":
            self._handle_get_audit()
        elif self.path == "/api/config":
            self._handle_get_config()
        elif self.path == "/api/export-csv":
            self._handle_export_csv()
        elif self.path == "/" or self.path == "/index.html":
            self.path = "/index.html"
            super().do_GET()
        else:
            super().do_GET()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception:
            body = {}

        if self.path == "/api/run-event":
            self._handle_run_event(body)
        elif self.path == "/api/run-batch":
            self._handle_run_batch(body)
        elif self.path == "/api/convert-link":
            self._handle_convert_link(body)
        elif self.path == "/api/clear-audit":
            self._handle_clear_audit()
        else:
            self._send_json({"error": "Endpoint not found"}, status=404)

    def _send_json(self, data: dict, status: int = 200):
        response_bytes = json.dumps(data, default=json_serializer, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def _handle_get_stats(self):
        stats = audit_trail.get_summary_stats()
        gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        stats["gemini_active"] = bool(gemini_key)
        self._send_json(stats)

    def _handle_get_audit(self):
        entries = audit_trail.get_all_entries()
        self._send_json({"entries": entries[::-1]})  # Newest first

    def _handle_get_config(self):
        self._send_json(config_data)

    def _handle_export_csv(self):
        entries = audit_trail.get_all_entries()
        import io, csv
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Audit ID", "Timestamp", "Event ID", "Failure Mode", "Agent Name",
            "Action Taken", "Result", "At Risk (INR)", "Recovered (INR)",
            "Risk Score", "LLM Reasoning", "Compliance Flags", "Stopping Rule", "Escalated"
        ])
        for r in entries:
            writer.writerow([
                r.get("audit_id"), r.get("timestamp"), r.get("event_id"),
                r.get("failure_mode"), r.get("agent_name"), r.get("action_taken"),
                r.get("result"), r.get("amount_at_risk_inr"), r.get("amount_recovered_inr"),
                r.get("risk_score"), r.get("llm_reasoning"), r.get("compliance_flags"),
                r.get("stopping_rule_triggered"), r.get("escalated_to_human")
            ])
        csv_bytes = output.getvalue().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=recovery_audit_ledger.csv")
        self.send_header("Content-Length", str(len(csv_bytes)))
        self.end_headers()
        self.wfile.write(csv_bytes)

    def _handle_clear_audit(self):
        audit_trail.clear()
        self._send_json({"message": "Audit trail cleared successfully", "status": "success"})

    def _handle_convert_link(self, body: dict):
        event_id = body.get("event_id")
        amount = float(body.get("amount", 0.0))

        if not event_id:
            self._send_json({"error": "Missing event_id"}, status=400)
            return

        audit_trail.update_result(event_id=event_id, new_result="success", amount_recovered=amount)
        self._send_json({
            "status": "success",
            "message": f"Simulated customer payment conversion for event {event_id}",
            "amount_recovered": amount,
        })

    def _handle_run_batch(self, body: dict):
        batch_size = int(body.get("batch_size", 10))
        mode = body.get("mode", "all")

        events = generate_batch(batch_size=batch_size, mode=mode)
        results = []

        for event in events:
            initial_state: RecoveryState = {
                "event": event,
                "detection": {},
                "risk_assessment": RiskAssessment(),
                "attempt": RecoveryAttempt(),
                "failure_mode": "",
                "config": config_data,
                "audit": audit_trail,
            }
            final_state = recovery_graph.invoke(initial_state)
            attempt = final_state.get("attempt", RecoveryAttempt())
            results.append({
                "event_id": event.event_id,
                "mode": final_state.get("failure_mode"),
                "result": attempt.result,
                "action": attempt.action_taken,
                "recovered_inr": attempt.amount_recovered_inr,
            })

        stats = audit_trail.get_summary_stats()
        self._send_json({
            "status": "completed",
            "batch_size": len(events),
            "mode": mode,
            "results": results,
            "stats": stats,
        })

    def _handle_run_event(self, body: dict):
        """Build a real event object from user input and execute the LangGraph supervisor."""
        mode = body.get("mode", "payment_failure")
        event_id = f"evt_{uuid.uuid4().hex[:12]}"
        now = datetime.utcnow()

        customer_name = body.get("customer_name", "Rahul Sharma")
        customer_email = body.get("customer_email", "rahul.sharma@example.com")
        customer_phone = body.get("customer_phone", "+919876543210")
        amount = float(body.get("amount", 2499.0))
        tier = body.get("customer_tier", "standard")
        opted_out = bool(body.get("customer_opted_out", False))
        attempt_number = int(body.get("attempt_number", 1))

        if mode == "payment_failure":
            reason_str = body.get("failure_reason", "insufficient_funds")
            try:
                reason_enum = ErrorReason(reason_str)
            except ValueError:
                reason_enum = ErrorReason.INSUFFICIENT_FUNDS

            error_obj = RazorpayError(
                code=body.get("error_code", "BAD_REQUEST_ERROR"),
                reason=reason_enum,
                source=ErrorSource.CUSTOMER if reason_str != "gateway_error" else ErrorSource.GATEWAY,
                description=f"Payment failed due to {reason_str.replace('_', ' ')}",
            )

            # Check for fraud flag
            if body.get("fraud_flagged", False):
                error_obj.reason = ErrorReason.FRAUD_FLAGGED

            segment = CustomerSegment.PREMIUM if tier == "premium" else (CustomerSegment.ENTERPRISE if tier == "enterprise" else CustomerSegment.RETURNING)

            event = PaymentFailedEvent(
                event_id=event_id,
                created_at=now,
                payment_id=f"pay_{uuid.uuid4().hex[:12]}",
                order_id=f"order_{uuid.uuid4().hex[:10]}",
                amount_inr=amount,
                currency="INR",
                customer_id=f"cust_{uuid.uuid4().hex[:8]}",
                customer_email=customer_email,
                customer_phone=customer_phone,
                customer_segment=segment,
                error=error_obj,
                previous_attempts=attempt_number - 1,
                opted_out=opted_out,
            )
        elif mode == "checkout_abandonment":
            segment = CustomerSegment.PREMIUM if tier == "premium" else (CustomerSegment.ENTERPRISE if tier == "enterprise" else CustomerSegment.RETURNING)
            event = CheckoutAbandonedEvent(
                event_id=event_id,
                created_at=now,
                checkout_id=f"chk_{uuid.uuid4().hex[:12]}",
                cart_value_inr=amount,
                items_count=int(body.get("items_count", 2)),
                cart_items=[{"name": "Pro SaaS Plan Subscription", "qty": 1, "price": amount}],
                customer_id=f"cust_{uuid.uuid4().hex[:8]}",
                customer_email=customer_email,
                customer_phone=customer_phone,
                customer_segment=segment,
                abandoned_at=now - timedelta(minutes=int(body.get("abandoned_minutes_ago", 30))),
                last_step_completed=body.get("last_step_completed", "payment_method"),
                payment_method_attempted=PaymentMethod.UPI,
                intent_signals={
                    "applied_coupon": True,
                    "time_spent_seconds": 180,
                    "fields_filled": 5,
                },
                opted_out=opted_out,
            )
        elif mode == "subscription_failure":
            segment = CustomerSegment.PREMIUM if tier == "premium" else (CustomerSegment.ENTERPRISE if tier == "enterprise" else CustomerSegment.RETURNING)
            reason_str = body.get("failure_reason", "insufficient_funds")
            try:
                reason_enum = ErrorReason(reason_str)
            except ValueError:
                reason_enum = ErrorReason.INSUFFICIENT_FUNDS

            event = SubscriptionFailureEvent(
                event_id=event_id,
                created_at=now,
                subscription_id=f"sub_{uuid.uuid4().hex[:10]}",
                plan_name=body.get("plan_name", "Enterprise Plan Annual"),
                amount_inr=amount,
                billing_cycle="monthly",
                customer_id=f"cust_{uuid.uuid4().hex[:8]}",
                customer_email=customer_email,
                customer_phone=customer_phone,
                customer_segment=segment,
                mandate_type="eNACH",
                consecutive_failures=attempt_number,
                last_failure_reason=reason_enum,
                grace_period_ends_at=now + timedelta(days=7),
                opted_out=opted_out,
                churn_risk_score=float(body.get("churn_risk", 65.0)),
            )
        elif mode == "receivable_overdue":
            days_overdue = int(body.get("days_overdue", 45))
            event = ReceivableOverdueEvent(
                event_id=event_id,
                created_at=now,
                invoice_id=f"INV-2026-{uuid.uuid4().hex[:4].upper()}",
                invoice_amount_inr=amount,
                due_date=now - timedelta(days=days_overdue),
                days_overdue=days_overdue,
                company_name=customer_name + " Solutions Pvt. Ltd.",
                contact_person=customer_name,
                contact_email=customer_email,
                contact_phone=customer_phone,
                credit_terms="Net 30",
                previous_promises_broken=int(body.get("previous_promises_broken", 0)),
                has_active_dispute=bool(body.get("has_active_dispute", False)),
                opted_out=opted_out,
            )
        else:
            self._send_json({"error": f"Invalid mode: {mode}"}, status=400)
            return

        # Invoke graph
        initial_state: RecoveryState = {
            "event": event,
            "detection": {},
            "risk_assessment": RiskAssessment(),
            "attempt": RecoveryAttempt(),
            "failure_mode": "",
            "config": config_data,
            "audit": audit_trail,
        }

        try:
            final_state = recovery_graph.invoke(initial_state)
            attempt = final_state.get("attempt", RecoveryAttempt())
            risk = final_state.get("risk_assessment", RiskAssessment())
            detection = final_state.get("detection", {})

            # Payment methods pool - Razorpay only
            import random
            gateways = ["Razorpay PG", "Razorpay UPI", "Razorpay Checkout", "Razorpay Mandate", "Razorpay Invoice"]
            selected_gateway = body.get("payment_gateway") or random.choice(gateways)

            # Prepare structured trace for UI visualization
            response_payload = {
                "event_id": event.event_id,
                "timestamp": event.created_at.isoformat(),
                "customer_name": customer_name,
                "customer_email": customer_email,
                "customer_phone": customer_phone,
                "amount_inr": amount,
                "mode": mode,
                "payment_gateway": selected_gateway,
                "detection": {
                    "failure_mode": detection.get("failure_mode", FailureMode.PAYMENT_FAILURE).value if hasattr(detection.get("failure_mode"), "value") else str(detection.get("failure_mode")),
                    "root_cause": str(detection.get("root_cause_category") or detection.get("root_cause") or body.get("failure_reason") or "Insufficient Funds").replace('_', ' ').title(),
                    "urgency": str(detection.get("urgency", "normal")),
                    "intent_score": detection.get("intent_score"),
                    "mandate_eligible": detection.get("mandate_eligible"),
                    "aging_bucket": detection.get("aging_bucket"),
                },
                "risk_assessment": {
                    "risk_score": risk.risk_score,
                    "reasoning": risk.reasoning,
                    "recommended_action": risk.recommended_action,
                    "estimated_recovery_probability": risk.estimated_recovery_probability,
                    "llm_used": risk.llm_used,
                },
                "routing": {
                    "target_agent": final_state.get("failure_mode", "").replace("_failure", "_agent").replace("_abandonment", "_agent"),
                },
                "execution": {
                    "action_taken": attempt.action_taken,
                    "channel": attempt.channel,
                    "result": attempt.result,
                    "amount_recovered_inr": attempt.amount_recovered_inr,
                    "agent_reasoning": attempt.agent_reasoning,
                    "stopping_rule_triggered": attempt.stopping_rule_triggered,
                    "compliance_flags": getattr(attempt, "compliance_flags", []),
                },
                "payment_link_simulated": f"https://rzp.io/i/plink_{uuid.uuid4().hex[:12]}" if "link" in attempt.action_taken or "nudge" in attempt.action_taken or "email" in attempt.action_taken or "retry" in attempt.action_taken else None,
            }

            self._send_json(response_payload)
        except Exception as exc:
            logger.error("Error executing event: %s", exc, exc_info=True)
            self._send_json({"error": str(exc)}, status=500)



def run_server(port: int = 8000):
    web_dir = Path(__file__).parent / "web"
    web_dir.mkdir(exist_ok=True)

    server_address = ("", port)
    httpd = ThreadedHTTPServer(server_address, APIHandler)
    logger.info("Razorpay AI Revenue Recovery Server live at http://localhost:%d", port)
    print(f"\n===========================================================")
    print(f" RAZORPAY AI REVENUE RECOVERY -- INTERACTIVE DEMO DASHBOARD")
    print(f" Access URL: http://localhost:{port}")
    print(f" Status: Ready to receive real-time recovery simulations")
    print(f"===========================================================\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        httpd.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Razorpay AI Revenue Recovery Web Demo Server")
    parser.add_argument("--port", type=int, default=8000, help="Port to run server on (default: 8000)")
    parser.add_argument("--fresh", action="store_true", help="Start with a fresh empty audit database (0 stats)")
    args = parser.parse_args()

    if args.fresh:
        logger.info("Clearing audit trail for fresh live demonstration...")
        audit_trail.clear()

    run_server(port=args.port)
