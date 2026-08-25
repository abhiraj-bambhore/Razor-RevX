"""
Batch recovery report generator.
Produces end-of-run metrics: total at risk, recovered, rate by category, etc.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

from src.audit.audit_trail import AuditTrail
from src.data.schemas import BatchRecoveryReport

logger = logging.getLogger(__name__)


def generate_recovery_report(
    audit: AuditTrail,
    batch_size: int,
    mode: str,
    save_path: str = "audit/",
    console: Optional[Console] = None,
) -> BatchRecoveryReport:
    """
    Generate a comprehensive batch recovery report from audit trail data.
    Prints rich console output AND saves to JSON.
    """
    if console is None:
        _console = Console(force_terminal=True, highlight=False)
    else:
        _console = console

    stats = audit.get_summary_stats()
    totals = stats["totals"]

    recovery_rate = (
        round(totals["recovered_inr"] / totals["at_risk_inr"] * 100, 2)
        if totals["at_risk_inr"] > 0
        else 0.0
    )

    report = BatchRecoveryReport(
        batch_size=batch_size,
        mode=mode,
        total_at_risk_inr=totals["at_risk_inr"],
        total_recovered_inr=totals["recovered_inr"],
        recovery_rate_pct=recovery_rate,
        by_failure_mode=stats.get("by_mode", {}),
        total_attempts=totals["entries"],
        stopping_rules_triggered=totals["stopping_rules_hit"],
        escalated_to_human=totals["escalated"],
        opted_out=totals["stopped"],
        agent_performance=stats.get("by_agent", {}),
    )

    # ── Print rich console report ────────────────────────────────────────

    _console.print()
    _console.print(
        Panel.fit(
            "[bold cyan]REVENUE RECOVERY AGENT -- BATCH REPORT[/bold cyan]",
            border_style="cyan",
        )
    )
    _console.print()

    # Summary table
    summary_table = Table(
        title="Recovery Summary",
        box=box.ROUNDED,
        border_style="green",
        show_header=True,
        header_style="bold magenta",
    )
    summary_table.add_column("Metric", style="cyan", justify="left", min_width=30)
    summary_table.add_column("Value", style="white", justify="right", min_width=20)

    summary_table.add_row("Batch Size", str(batch_size))
    summary_table.add_row("Mode", mode)
    summary_table.add_row("Total Events Processed", str(totals["entries"]))
    summary_table.add_row("", "")
    summary_table.add_row(
        "Total Revenue at Risk",
        f"INR {totals['at_risk_inr']:,.2f}",
    )
    summary_table.add_row(
        "[bold green]Total Revenue Recovered[/bold green]",
        f"[bold green]INR {totals['recovered_inr']:,.2f}[/bold green]",
    )
    summary_table.add_row(
        "[bold]Recovery Rate[/bold]",
        f"[bold yellow]{recovery_rate:.1f}%[/bold yellow]",
    )
    summary_table.add_row("", "")
    summary_table.add_row("Successful Recoveries", str(totals["successful"]))
    summary_table.add_row("Failed Attempts", str(totals["failed"]))
    summary_table.add_row("Pending", str(totals["pending"]))
    summary_table.add_row("Stopping Rules Triggered", str(totals["stopping_rules_hit"]))
    summary_table.add_row("Escalated to Human", str(totals["escalated"]))

    _console.print(summary_table)
    _console.print()

    # By failure mode
    if stats.get("by_mode"):
        mode_table = Table(
            title="Recovery by Failure Mode",
            box=box.ROUNDED,
            border_style="blue",
            show_header=True,
            header_style="bold cyan",
        )
        mode_table.add_column("Failure Mode", style="white", min_width=25)
        mode_table.add_column("Events", justify="right")
        mode_table.add_column("At Risk (INR)", justify="right")
        mode_table.add_column("Recovered (INR)", justify="right", style="green")
        mode_table.add_column("Rate %", justify="right", style="yellow")

        for mode_name, data in stats["by_mode"].items():
            mode_table.add_row(
                mode_name.replace("_", " ").title(),
                str(data["entries"]),
                f"{data['at_risk_inr']:,.2f}",
                f"{data['recovered_inr']:,.2f}",
                f"{data.get('recovery_rate', 0):.1f}%",
            )

        _console.print(mode_table)
        _console.print()

    # By agent
    if stats.get("by_agent"):
        agent_table = Table(
            title="Agent Performance",
            box=box.ROUNDED,
            border_style="magenta",
            show_header=True,
            header_style="bold green",
        )
        agent_table.add_column("Agent", style="white", min_width=25)
        agent_table.add_column("Actions", justify="right")
        agent_table.add_column("Recovered (INR)", justify="right", style="green")
        agent_table.add_column("Successes", justify="right", style="cyan")

        for agent_name, data in stats["by_agent"].items():
            agent_table.add_row(
                agent_name,
                str(data["actions"]),
                f"{data['recovered_inr']:,.2f}",
                str(data["successful"]),
            )

        _console.print(agent_table)
        _console.print()

    # ── Save to JSON ─────────────────────────────────────────────────────

    Path(save_path).mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    report_file = Path(save_path) / f"recovery_report_{timestamp}.json"

    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report.model_dump(mode="json"), f, indent=2, default=str)

    _console.print(f"[dim]Report saved: {report_file}[/dim]")
    _console.print(f"[dim]Audit DB: {audit.db_path}[/dim]")
    _console.print(f"[dim]Audit JSONL: {audit.jsonl_path}[/dim]")
    _console.print()

    return report
