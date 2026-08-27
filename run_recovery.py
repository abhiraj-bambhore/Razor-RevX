"""
Razorpay AI Revenue Recovery Agent -- CLI Entry Point.

Usage:
    python run_recovery.py --batch-size 100 --mode all --report
    python run_recovery.py --batch-size 50 --mode payment --report
    python run_recovery.py --batch-size 30 --mode checkout
    python run_recovery.py --batch-size 20 --mode subscription --report
    python run_recovery.py --batch-size 25 --mode receivables --report
"""
from __future__ import annotations

import argparse
import logging
import sys
import os
import io

# Force UTF-8 output on Windows to handle unicode characters
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    os.environ["PYTHONIOENCODING"] = "utf-8"

from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel

# Create console once at module level for consistent encoding
console = Console(force_terminal=True, highlight=False)


def main():
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Razorpay AI Revenue Recovery Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_recovery.py --batch-size 100 --mode all --report
  python run_recovery.py --batch-size 50 --mode payment
  python run_recovery.py --batch-size 30 --mode checkout --report
  python run_recovery.py --batch-size 20 --mode subscription --report
  python run_recovery.py --batch-size 25 --mode receivables --report
        """,
    )
    parser.add_argument(
        "--batch-size", type=int, default=50,
        help="Number of events to generate and process (default: 50)",
    )
    parser.add_argument(
        "--mode",
        choices=["all", "payment", "checkout", "subscription", "receivables"],
        default="all",
        help="Recovery mode: all (mixed), or specific failure type",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Generate and print the batch recovery report after processing",
    )
    parser.add_argument(
        "--config", type=str, default="config/recovery_config.yaml",
        help="Path to recovery config YAML (default: config/recovery_config.yaml)",
    )
    parser.add_argument(
        "--audit-db", type=str, default="audit/recovery_audit.db",
        help="Path to SQLite audit DB (default: audit/recovery_audit.db)",
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Clear audit trail before running (for testing only)",
    )

    args = parser.parse_args()

    # ── Logging setup ────────────────────────────────────────────────────
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )
    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)

    # ── Banner ───────────────────────────────────────────────────────────
    console.print()
    console.print(Panel.fit(
        "[bold cyan]RAZORPAY AI REVENUE RECOVERY AGENT[/bold cyan]\n"
        "[dim]Multi-Agent Hierarchical Architecture | Supervisor + Specialists + Compliance Gate[/dim]\n"
        f"[dim]Batch: {args.batch_size} events | Mode: {args.mode} | Config: {args.config}[/dim]",
        border_style="cyan",
    ))
    console.print()

    # ── Check Gemini ─────────────────────────────────────────────────────
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key:
        console.print("[green]>> Tier 1: Gemini LLM active (contextual AI reasoning)[/green]")
    else:
        console.print("[yellow]>> Tier 1 offline: No GEMINI_API_KEY[/yellow]")
        console.print("[green]>> Tier 2: ML model active (GradientBoosting + RandomForest)[/green]")
        console.print("[dim]>> Tier 3: Heuristic rules on standby (absolute last resort)[/dim]")
    console.print()

    # ── Generate events ──────────────────────────────────────────────────
    from src.data.simulator import generate_batch

    console.print(f"[cyan]>> Generating {args.batch_size} {args.mode} events...[/cyan]")
    events = generate_batch(batch_size=args.batch_size, mode=args.mode)
    console.print(f"[green]>> Generated {len(events)} events[/green]")

    # Quick stats
    from collections import Counter
    type_counts = Counter(type(e).__name__ for e in events)
    for etype, count in sorted(type_counts.items()):
        console.print(f"   {etype}: {count}")
    console.print()

    # ── Clean audit if requested ─────────────────────────────────────────
    if args.clean:
        from src.audit.audit_trail import AuditTrail
        audit = AuditTrail(db_path=args.audit_db)
        audit.clear()
        console.print("[yellow]>> Audit trail cleared[/yellow]")
        console.print()

    # ── Run recovery ─────────────────────────────────────────────────────
    from src.agents.orchestrator import run_recovery_batch

    console.print("[bold cyan]>> Starting multi-agent recovery workflow...[/bold cyan]")
    console.print("[dim]   Supervisor → Detect → Score Risk → Specialist → Review → Compliance → Execute[/dim]")
    console.print()

    results, audit = run_recovery_batch(
        events=events,
        config_path=args.config,
        audit_db_path=args.audit_db,
    )

    # ── Summary ──────────────────────────────────────────────────────────
    console.print()
    console.print("[bold green]>> Recovery batch complete[/bold green]")
    console.print()

    result_counts = Counter(r.result for r in results)
    total_recovered = sum(r.amount_recovered_inr for r in results)

    console.print(f"   Events processed: {len(results)}")
    for status, count in sorted(result_counts.items()):
        label = {"success": "[green]RECOVERED[/green]", "failed": "[red]FAILED[/red]",
                 "pending": "[yellow]PENDING[/yellow]", "stopped": "[dim]STOPPED[/dim]",
                 "escalated": "[cyan]ESCALATED[/cyan]"}.get(status, status.upper())
        console.print(f"   {label}: {count}")
    console.print(f"   Total recovered: [bold green]INR {total_recovered:,.2f}[/bold green]")
    console.print()

    # ── Report ───────────────────────────────────────────────────────────
    if args.report:
        from src.audit.recovery_report import generate_recovery_report
        generate_recovery_report(
            audit=audit,
            batch_size=args.batch_size,
            mode=args.mode,
            save_path="audit/",
            console=console,
        )


if __name__ == "__main__":
    main()
