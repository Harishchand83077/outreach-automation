"""
main.py — Entry point for the Funding Outreach Automation System.

System Architecture:
─────────────────────────────────────────────────────────────────────
Phase 1: Load leads from Excel → Store in SQLite DB
Phase 2: Run parallel LangGraph workflows for each lead:
         - Research insights → Human approval → Email draft → Human approval → Send
Phase 3: Background workers (concurrent with Phase 2):
         - Reply Monitor: Checks inbox every N minutes
         - Decision Engine: Classifies replies and takes actions

Key Design Decisions:
- asyncio.gather() for parallel lead processing
- asyncio.Semaphore for rate limiting (max 5 concurrent)
- LangGraph workflow ends after email send
- Reply handling is fully decoupled from lead workflow
─────────────────────────────────────────────────────────────────────

Usage:
    python main.py leads.xlsx
    python main.py leads.xlsx --skip-monitor   # Skip background workers
    python main.py --status                    # Show DB status summary
"""

import asyncio
import sys
import argparse
from pathlib import Path

# Windows: use UTF-8 for stdout/stderr so emojis and Unicode log messages don't crash (cp1252)
if sys.platform == "win32":
    try:
        import io
        if hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "buffer"):
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

from config.config import config
from database import init_db, upsert_lead, print_leads_summary
from excel_loader import load_leads_from_excel
from runner import run_all_leads
from reply_monitor import reply_monitor_loop
from decision_engine import decision_engine_loop
from logger import get_logger, set_log_level

logger = get_logger("main")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Funding Outreach Automation System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py leads.xlsx              # Full run with all workers
  python main.py leads.xlsx --no-monitor # Skip reply monitoring
  python main.py --status                # Show current lead statuses
  python main.py --workers-only          # Only run background workers
        """,
    )
    parser.add_argument(
        "excel_file",
        nargs="?",
        default=None,
        help="Path to Excel file with leads (Name, Email, Company columns)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current status of all leads in DB and exit",
    )
    parser.add_argument(
        "--no-monitor",
        action="store_true",
        help="Skip reply monitoring background workers",
    )
    parser.add_argument(
        "--workers-only",
        action="store_true",
        help="Only run background workers (reply monitor + decision engine)",
    )
    parser.add_argument(
        "--log-level",
        default=config.LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Set log level (default: INFO)",
    )
    return parser.parse_args()


async def load_and_store_leads(excel_path: str) -> list:
    """
    Load leads from Excel and store them in the DB.

    Returns:
        List of lead dicts for workflow processing.
    """
    logger.info("Loading leads from: %s", excel_path)
    leads = load_leads_from_excel(excel_path)

    logger.info("Storing %d leads in database...", len(leads))
    for lead in leads:
        await upsert_lead(
            name=lead["name"],
            email=lead["email"],
            company=lead["company"],
        )

    return leads


async def run_outreach_phase(leads: list) -> None:
    """
    Phase 1-4: Run all LangGraph workflows in parallel.
    """
    logger.info("=" * 70)
    logger.info("PHASE 1-4: PARALLEL OUTREACH WORKFLOWS")
    logger.info("=" * 70)
    logger.info("Processing %d leads with max %d concurrent", len(leads), config.MAX_CONCURRENT_LEADS)

    results = await run_all_leads(leads)

    # Summary report
    sent = sum(1 for r in results if r.get("status") == "EMAIL_SENT")
    failed = sum(1 for r in results if r.get("status") in ("ERROR", "EMAIL_FAILED"))

    logger.info("=" * 70)
    logger.info("OUTREACH COMPLETE: %d sent, %d failed, %d total", sent, failed, len(results))
    logger.info("=" * 70)


async def run_background_workers() -> None:
    """
    Phase 5-6: Run reply monitor and decision engine as background tasks.
    Both run indefinitely until the process is killed.
    """
    logger.info("=" * 70)
    logger.info("PHASE 5-6: STARTING BACKGROUND WORKERS")
    logger.info("=" * 70)
    logger.info("Reply monitor: every %d minutes", config.REPLY_CHECK_INTERVAL_MINUTES)
    logger.info("Decision engine: every %d minutes (offset 30s)", config.REPLY_CHECK_INTERVAL_MINUTES)
    logger.info("Press Ctrl+C to stop background workers")

    # Run both workers concurrently — they loop forever
    await asyncio.gather(
        reply_monitor_loop(),
        decision_engine_loop(),
    )


async def main():
    """
    Main async entry point.
    Orchestrates all phases of the outreach system.
    """
    args = parse_args()
    set_log_level(args.log_level)

    logger.info("=" * 70)
    logger.info("FUNDING OUTREACH AUTOMATION SYSTEM")
    logger.info("=" * 70)

    # Validate required config
    try:
        config.validate()
    except EnvironmentError as e:
        logger.error("Configuration error:\n%s", str(e))
        sys.exit(1)

    # Initialize database
    await init_db()

    # ── Mode: Status only ──
    if args.status:
        await print_leads_summary()
        return

    # ── Mode: Workers only ──
    if args.workers_only:
        logger.info("Running in WORKERS-ONLY mode")
        try:
            await run_background_workers()
        except KeyboardInterrupt:
            logger.info("\nBackground workers stopped by user")
        return

    # ── Mode: Full run (needs Excel file) ──
    if not args.excel_file:
        logger.error(
            "Excel file required. Usage: python main.py leads.xlsx\n"
            "   Or: python main.py --help"
        )
        sys.exit(1)

    excel_path = args.excel_file
    if not Path(excel_path).exists():
        logger.error("File not found: %s", excel_path)
        sys.exit(1)

    try:
        # Phase 1: Load and store leads
        leads = await load_and_store_leads(excel_path)

        # Phase 2-4: Parallel LangGraph workflows
        await run_outreach_phase(leads)

        # Phase 5-6: Background workers (unless skipped)
        if not args.no_monitor:
            logger.info("\nOutreach complete! Starting reply monitoring...")
            logger.info("(Run with --no-monitor to skip this phase)\n")
            try:
                await run_background_workers()
            except KeyboardInterrupt:
                logger.info("\nBackground workers stopped by user")
        else:
            logger.info("Reply monitoring skipped (--no-monitor flag)")
            logger.info("Run 'python main.py --workers-only' to start monitoring later")

    except KeyboardInterrupt:
        logger.info("\nSystem stopped by user")
    except Exception as e:
        logger.error("Fatal error: %s", str(e), exc_info=True)
        sys.exit(1)

    # Final status
    logger.info("\nFinal lead status:")
    await print_leads_summary()


if __name__ == "__main__":
    asyncio.run(main())
