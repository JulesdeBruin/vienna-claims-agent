#!/usr/bin/env python3
"""Entry point for the Vienna Claims Agent.

Usage:
    python run.py                 # Interactive CLI (default)
    python run.py web             # Web dashboard at http://localhost:8000
    python run.py pipeline        # Run pipeline once and exit
    python run.py daemon          # Start daily scheduler
    python run.py import FILE     # Import shipments from CSV
    python run.py --seed          # Seed sample data for testing
"""

import argparse
import logging
import sys

from claims_agent.config import settings
from claims_agent.models import init_db


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_interactive(args):
    """Launch the interactive CLI."""
    if args.seed:
        from claims_agent.seed_data import seed
        seed()
    from claims_agent.cli import main
    main()


def cmd_pipeline(args):
    """Run the pipeline once and exit."""
    from claims_agent.agent import ClaimsAgent
    from claims_agent.config import settings
    from claims_agent.email_ingestion import EmailIngestor
    from claims_agent.notifications import Notifier
    from claims_agent.orchestrator import Orchestrator

    ingestor = EmailIngestor() if settings.imap_host else None
    orchestrator = Orchestrator(agent=ClaimsAgent(), ingestor=ingestor)

    result = orchestrator.run_pipeline(
        eml_dir=args.eml_dir,
        auto_draft=not args.no_draft,
    )

    print(result.summary())

    notifier = Notifier()
    notifier.notify_pipeline_complete(result)
    notifier.notify_urgent_deadlines()

    sys.exit(0 if not result.errors else 1)


def cmd_daemon(args):
    """Start the daily scheduler."""
    from claims_agent.agent import ClaimsAgent
    from claims_agent.config import settings
    from claims_agent.email_ingestion import EmailIngestor
    from claims_agent.notifications import Notifier
    from claims_agent.orchestrator import Orchestrator
    from claims_agent.scheduler import Scheduler

    ingestor = EmailIngestor() if settings.imap_host else None
    orchestrator = Orchestrator(agent=ClaimsAgent(), ingestor=ingestor)
    notifier = Notifier()

    def on_complete(result):
        notifier.notify_pipeline_complete(result)
        notifier.notify_urgent_deadlines()

    scheduler = Scheduler(orchestrator=orchestrator, on_complete=on_complete)
    scheduler.start(hour=args.hour, minute=args.minute)


def cmd_web(args):
    """Start the web dashboard."""
    import uvicorn

    if args.seed:
        from claims_agent.seed_data import seed
        seed()

    print(f"\n🦞 Vienna Claims Agent — Web Dashboard")
    print(f"   http://localhost:{args.port}\n")

    uvicorn.run(
        "claims_agent.web.app:app",
        host=args.host or settings.web_host,
        port=args.port or settings.web_port,
        reload=False,
        log_level="info",
    )


def cmd_import(args):
    """Import shipments from a CSV file."""
    from claims_agent.importer import CSVImporter

    importer = CSVImporter()
    result = importer.import_from_csv(args.file)

    print(f"Imported: {result.imported}")
    print(f"Duplicates skipped: {result.skipped_duplicate}")
    print(f"Errors: {result.skipped_error}")
    for e in result.errors:
        print(f"  {e}")

    sys.exit(0 if not result.errors else 1)


def main():
    parser = argparse.ArgumentParser(
        description="Vienna Claims Agent — Automated late delivery claims for Austrian/EU carriers",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )
    parser.add_argument(
        "--seed", action="store_true", help="Seed sample data for testing"
    )

    subparsers = parser.add_subparsers(dest="command")

    # Pipeline
    pipe_parser = subparsers.add_parser("pipeline", help="Run pipeline once and exit")
    pipe_parser.add_argument("--eml-dir", help="Ingest .eml files from this directory")
    pipe_parser.add_argument(
        "--no-draft", action="store_true", help="Skip auto-drafting claims"
    )

    # Daemon
    daemon_parser = subparsers.add_parser("daemon", help="Start daily scheduler")
    daemon_parser.add_argument(
        "--hour", type=int, default=None, help="Hour to run (0-23, default from .env)"
    )
    daemon_parser.add_argument(
        "--minute", type=int, default=None, help="Minute to run (0-59, default from .env)"
    )

    # Web dashboard
    web_parser = subparsers.add_parser("web", help="Start web dashboard")
    web_parser.add_argument("--host", default=None, help="Host (default from .env)")
    web_parser.add_argument("--port", type=int, default=None, help="Port (default from .env)")

    # Import
    import_parser = subparsers.add_parser("import", help="Import shipments from CSV")
    import_parser.add_argument("file", help="Path to CSV file")

    args = parser.parse_args()
    setup_logging(args.verbose)
    init_db()

    match args.command:
        case "web":
            cmd_web(args)
        case "pipeline":
            cmd_pipeline(args)
        case "daemon":
            cmd_daemon(args)
        case "import":
            cmd_import(args)
        case _:
            cmd_interactive(args)


if __name__ == "__main__":
    main()
