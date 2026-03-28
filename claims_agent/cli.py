"""Interactive CLI for the Vienna Claims Agent — no API key required."""

import logging
import os
from datetime import date

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table

from .agent import ClaimsAgent
from .carriers.registry import get_all_carriers
from .config import settings
from .email_ingestion import EmailIngestor
from .importer import CSVImporter
from .models import CarrierName, ClaimStatus, init_db
from .notifications import Notifier
from .orchestrator import Orchestrator

console = Console()
agent = ClaimsAgent()


def _build_orchestrator(verbose: bool = True) -> Orchestrator:
    """Build an orchestrator with optional console event logging."""

    def on_event(step: str, message: str):
        if verbose:
            console.print(f"  [dim][{step}][/dim] {message}")

    ingestor = EmailIngestor() if settings.imap_host else None
    return Orchestrator(agent=agent, ingestor=ingestor, on_event=on_event)


def show_welcome():
    console.print(
        Panel(
            "[bold]Vienna Claims Agent[/bold]\n"
            "Automated late delivery claims for Austrian/EU carriers\n\n"
            "[dim]No API key required — runs 100% locally[/dim]",
            border_style="blue",
        )
    )


def show_menu():
    console.print()
    console.print("[bold cyan]─── Claims ───[/bold cyan]")
    console.print("  [bold]1[/bold]  Scan late shipments")
    console.print("  [bold]2[/bold]  Check eligibility & draft claims")
    console.print("  [bold]3[/bold]  View draft claims")
    console.print("  [bold]4[/bold]  Approve & generate claim emails")
    console.print("[bold cyan]─── Pipeline ───[/bold cyan]")
    console.print("  [bold]5[/bold]  Run full pipeline (email → flag → check → draft)")
    console.print("  [bold]6[/bold]  Import shipments from CSV")
    console.print("  [bold]7[/bold]  Start daily scheduler (daemon)")
    console.print("  [bold]8[/bold]  Test IMAP connection")
    console.print("[bold cyan]─── Info ───[/bold cyan]")
    console.print("  [bold]9[/bold]  View carrier policies")
    console.print("  [bold]10[/bold] Claims summary dashboard")
    console.print("  [bold]q[/bold]  Quit")
    console.print()


def cmd_scan():
    """Scan for late shipments."""
    shipments = agent.scan_late_shipments()
    if not shipments:
        console.print("[yellow]No unclaimed late shipments found.[/yellow]")
        return

    table = Table(title=f"Late Shipments ({len(shipments)} found)")
    table.add_column("ID", style="dim")
    table.add_column("Tracking")
    table.add_column("Carrier")
    table.add_column("Service")
    table.add_column("Days Late", justify="right")
    table.add_column("Value (EUR)", justify="right")
    table.add_column("Recipient")
    table.add_column("Source")

    for s in shipments:
        days = s.days_late or 0
        late_style = "red bold" if days >= 3 else "yellow"
        table.add_row(
            str(s.id),
            s.tracking_number,
            s.carrier.value,
            s.service_level.value,
            f"[{late_style}]{days}[/{late_style}]",
            f"{s.declared_value:.2f}" if s.declared_value else "—",
            s.recipient_name or "—",
            getattr(s, "source", "manual") or "manual",
        )

    console.print(table)


def cmd_check_and_draft():
    """Check eligibility for all late shipments and draft claims."""
    shipments = agent.scan_late_shipments()
    if not shipments:
        console.print("[yellow]No unclaimed late shipments to process.[/yellow]")
        return

    console.print(f"\n[bold]Checking eligibility for {len(shipments)} shipments...[/bold]\n")
    results = agent.check_all_eligibility(shipments)

    table = Table(title="Eligibility Results")
    table.add_column("Tracking")
    table.add_column("Carrier")
    table.add_column("Service")
    table.add_column("Eligible?")
    table.add_column("Reason")
    table.add_column("Deadline")

    eligible_count = 0
    for r in results:
        s = r["shipment"]
        if r["eligible"]:
            eligible_count += 1
            status = "[green bold]YES[/green bold]"
        else:
            status = "[red]NO[/red]"

        table.add_row(
            s.tracking_number,
            s.carrier.value,
            s.service_level.value,
            status,
            r["reason"][:60] + "..." if len(r["reason"]) > 60 else r["reason"],
            r["filing_deadline"].isoformat() if r["filing_deadline"] else "—",
        )

    console.print(table)

    if eligible_count == 0:
        console.print("\n[yellow]No eligible shipments for claims.[/yellow]")
        return

    if Confirm.ask(f"\nDraft claims for {eligible_count} eligible shipments?"):
        claims = agent.draft_all_eligible(results)
        console.print(f"\n[green]{len(claims)} claim draft(s) created.[/green]")
        for c in claims:
            console.print(f"  Claim #{c.id} — Shipment #{c.shipment_id} — EUR {c.claim_amount:.2f}")


def cmd_view_drafts():
    """View all draft claims."""
    claims = agent.get_all_claims(status=ClaimStatus.DRAFT)
    if not claims:
        console.print("[yellow]No draft claims.[/yellow]")
        return

    table = Table(title=f"Draft Claims ({len(claims)})")
    table.add_column("Claim ID")
    table.add_column("Tracking")
    table.add_column("Carrier")
    table.add_column("Amount (EUR)", justify="right")
    table.add_column("Days Late", justify="right")
    table.add_column("Deadline")

    for c in claims:
        deadline_str = c["deadline"].isoformat() if c["deadline"] else "—"
        if c["deadline"] and (c["deadline"] - date.today()).days <= 3:
            deadline_str = f"[red bold]{deadline_str} URGENT[/red bold]"

        table.add_row(
            str(c["claim_id"]),
            c["tracking"],
            c["carrier"],
            f"{c['amount']:.2f}",
            str(c["days_late"] or "—"),
            deadline_str,
        )

    console.print(table)


def cmd_approve_and_generate():
    """Approve drafts and generate claim emails."""
    claims = agent.get_all_claims(status=ClaimStatus.DRAFT)
    if not claims:
        console.print("[yellow]No draft claims to approve.[/yellow]")
        return

    cmd_view_drafts()
    console.print()

    choice = Prompt.ask(
        "Approve which claim? (ID, 'all', or 'skip')",
        default="all",
    )

    if choice.lower() == "skip":
        return

    ids_to_approve = []
    if choice.lower() == "all":
        ids_to_approve = [c["claim_id"] for c in claims]
    else:
        try:
            ids_to_approve = [int(choice)]
        except ValueError:
            console.print("[red]Invalid input.[/red]")
            return

    reviewer = Prompt.ask("Reviewer name", default="operator")

    for cid in ids_to_approve:
        try:
            agent.approve_claim(cid, reviewer)
            console.print(f"[green]Claim #{cid} approved.[/green]")
        except ValueError as e:
            console.print(f"[red]Claim #{cid}: {e}[/red]")
            continue

        if Confirm.ask(f"Generate claim email for #{cid}?", default=True):
            claim_email = agent.generate_claim_email(cid)
            console.print(Panel(claim_email, title=f"Claim Email — #{cid}", border_style="green"))

            out_dir = "claim_emails"
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, f"claim_{cid}.txt")
            with open(path, "w") as f:
                f.write(claim_email)
            console.print(f"[dim]Saved to {path}[/dim]")


def cmd_run_pipeline():
    """Run the full orchestrator pipeline."""
    console.print("[bold]Running full claims pipeline...[/bold]\n")

    orchestrator = _build_orchestrator()

    # Ask about ingestion source
    if settings.imap_host:
        source = Prompt.ask(
            "Ingestion source",
            choices=["email", "eml_files", "skip"],
            default="email",
        )
    else:
        source = Prompt.ask(
            "Ingestion source (IMAP not configured)",
            choices=["eml_files", "skip"],
            default="skip",
        )

    eml_dir = None
    skip_ingestion = False

    if source == "eml_files":
        eml_dir = Prompt.ask("Path to .eml files directory", default="test_emails")
    elif source == "skip":
        skip_ingestion = True

    auto_draft = Confirm.ask("Auto-draft claims for eligible shipments?", default=True)

    result = orchestrator.run_pipeline(
        skip_ingestion=skip_ingestion,
        eml_dir=eml_dir,
        auto_draft=auto_draft,
    )

    console.print(
        Panel(
            result.summary(),
            title="Pipeline Result",
            border_style="green" if not result.errors else "yellow",
        )
    )

    # Send notifications
    notifier = Notifier()
    notifier.notify_pipeline_complete(result)
    notifier.notify_urgent_deadlines()


def cmd_import_csv():
    """Import shipments from a CSV file."""
    file_path = Prompt.ask("CSV file path")

    if not os.path.exists(file_path):
        console.print(f"[red]File not found: {file_path}[/red]")
        return

    # Preview first few lines
    with open(file_path, "r", encoding="utf-8-sig") as f:
        lines = f.readlines()[:6]
    console.print(Panel("\n".join(lines), title="CSV Preview (first 5 rows)", border_style="dim"))

    if not Confirm.ask("Import this file?"):
        return

    importer = CSVImporter()
    result = importer.import_from_csv(file_path)

    console.print(f"\n[green]Imported: {result.imported}[/green]")
    console.print(f"[yellow]Duplicates skipped: {result.skipped_duplicate}[/yellow]")
    if result.errors:
        console.print(f"[red]Errors: {len(result.errors)}[/red]")
        for e in result.errors[:10]:
            console.print(f"  [dim]{e}[/dim]")


def cmd_start_scheduler():
    """Start the daily scheduler."""
    from .scheduler import Scheduler

    console.print(
        f"[bold]Starting daily scheduler at "
        f"{settings.schedule_hour:02d}:{settings.schedule_minute:02d} local time[/bold]"
    )
    console.print("[dim]Press Ctrl+C to stop[/dim]\n")

    orchestrator = _build_orchestrator()
    notifier = Notifier()

    def on_complete(result):
        notifier.notify_pipeline_complete(result)
        notifier.notify_urgent_deadlines()

    scheduler = Scheduler(orchestrator=orchestrator, on_complete=on_complete)
    scheduler.start()


def cmd_test_imap():
    """Test the IMAP connection."""
    if not settings.imap_host:
        console.print("[red]IMAP not configured. Set IMAP_HOST, IMAP_USER, IMAP_PASSWORD in .env[/red]")
        return

    console.print(f"Connecting to {settings.imap_host}:{settings.imap_port}...")
    try:
        ingestor = EmailIngestor()
        ingestor.connect()
        console.print("[green]Connection successful![/green]")
        console.print(f"[dim]User: {settings.imap_user}[/dim]")
        console.print(f"[dim]Folder: {settings.imap_folder}[/dim]")
        ingestor.disconnect()
    except Exception as e:
        console.print(f"[red]Connection failed: {e}[/red]")


def cmd_carrier_policies():
    """Display all carrier policies."""
    carriers = get_all_carriers()

    for name, client in carriers.items():
        policy = client.claim_policy
        eligible = "[green]Yes[/green]" if policy.allows_late_claims else "[red]No[/red]"
        services = ", ".join(s.value for s in policy.eligible_services) or "None"

        console.print(
            Panel(
                f"[bold]Late claims allowed:[/bold] {eligible}\n"
                f"[bold]Eligible services:[/bold] {services}\n"
                f"[bold]Filing deadline:[/bold] {policy.filing_deadline_days} days from {policy.deadline_reference.replace('_', ' ')}\n"
                f"[bold]Who can file:[/bold] {policy.who_can_file}\n"
                f"[bold]Methods:[/bold] {', '.join(policy.filing_methods)}\n"
                f"[bold]Portal:[/bold] {policy.portal_url or '—'}\n"
                f"[bold]Email:[/bold] {policy.claim_email or '—'}\n"
                f"[bold]Phone:[/bold] {policy.claim_phone or '—'}\n"
                f"[bold]Refund type:[/bold] {policy.refund_type}\n"
                f"[bold]Docs required:[/bold] {', '.join(policy.documentation_required) or '—'}\n\n"
                f"[dim]{policy.notes}[/dim]",
                title=f"{name.value.replace('_', ' ').title()}",
                border_style="cyan",
            )
        )


def cmd_summary():
    """Show claims dashboard."""
    summary = agent.get_summary()
    if summary.get("total", 0) == 0:
        console.print("[yellow]No claims yet. Run the pipeline first.[/yellow]")
        return

    console.print(
        Panel(
            f"[bold]Total claims:[/bold] {summary['total']}\n"
            f"[bold]Total claimed:[/bold] EUR {summary['total_claimed_eur']:.2f}\n"
            f"[bold]Total refunded:[/bold] EUR {summary['total_refunded_eur']:.2f}",
            title="Claims Dashboard",
            border_style="green",
        )
    )

    if summary.get("by_status"):
        table = Table(title="By Status")
        table.add_column("Status")
        table.add_column("Count", justify="right")
        for status, count in summary["by_status"].items():
            table.add_row(status, str(count))
        console.print(table)

    if summary.get("by_carrier"):
        table = Table(title="By Carrier")
        table.add_column("Carrier")
        table.add_column("Claims", justify="right")
        table.add_column("Claimed (EUR)", justify="right")
        for carrier, data in summary["by_carrier"].items():
            table.add_row(carrier, str(data["count"]), f"{data['claimed']:.2f}")
        console.print(table)


def main():
    show_welcome()
    init_db()
    console.print("[dim]Database ready.[/dim]")

    while True:
        show_menu()
        try:
            choice = Prompt.ask("[bold]Choose[/bold]", default="5")
        except (KeyboardInterrupt, EOFError):
            console.print("\nGoodbye!")
            break

        try:
            match choice.strip().lower():
                case "1":
                    cmd_scan()
                case "2":
                    cmd_check_and_draft()
                case "3":
                    cmd_view_drafts()
                case "4":
                    cmd_approve_and_generate()
                case "5":
                    cmd_run_pipeline()
                case "6":
                    cmd_import_csv()
                case "7":
                    cmd_start_scheduler()
                case "8":
                    cmd_test_imap()
                case "9":
                    cmd_carrier_policies()
                case "10":
                    cmd_summary()
                case "q" | "quit" | "exit":
                    console.print("Goodbye!")
                    break
                case _:
                    console.print("[red]Unknown command.[/red]")
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")


if __name__ == "__main__":
    main()
