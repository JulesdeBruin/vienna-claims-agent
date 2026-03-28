"""Orchestrator — chains the full claims pipeline matching the workflow diagram.

Pipeline steps:
1. Ingest emails (shipping confirmations + delivery notifications)
2. Flag late deliveries (compare actual vs guaranteed dates)
3. Check eligibility (service level + carrier policy rules)
4. Draft claims (auto-generate for all eligible shipments)

Each step can be run independently or chained via run_pipeline().
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from .agent import ClaimsAgent
from .email_ingestion import EmailIngestor, IngestionResult
from .models import (
    Claim,
    ClaimStatus,
    Shipment,
    ShipmentStatus,
    SessionLocal,
)

logger = logging.getLogger(__name__)

# Type alias for pipeline event callbacks
PipelineCallback = Callable[[str, str], None]  # (step_name, message)


@dataclass
class PipelineResult:
    """Summary of a full pipeline run."""

    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None

    # Step 1: Ingestion
    emails_scanned: int = 0
    shipments_created: int = 0
    shipments_updated: int = 0

    # Step 2: Flag late
    newly_late: int = 0
    total_late: int = 0

    # Step 3: Eligibility
    eligible_count: int = 0
    ineligible_count: int = 0

    # Step 4: Claims
    drafts_created: int = 0
    total_claimable_eur: float = 0.0

    # Errors
    errors: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return 0.0

    def summary(self) -> str:
        lines = [
            f"Pipeline completed in {self.duration_seconds:.1f}s",
            f"  Emails scanned:     {self.emails_scanned}",
            f"  Shipments created:  {self.shipments_created}",
            f"  Shipments updated:  {self.shipments_updated}",
            f"  Newly flagged late: {self.newly_late}",
            f"  Eligible for claim: {self.eligible_count}",
            f"  Ineligible:         {self.ineligible_count}",
            f"  Claims drafted:     {self.drafts_created}",
            f"  Total claimable:    EUR {self.total_claimable_eur:.2f}",
        ]
        if self.errors:
            lines.append(f"  Errors:             {len(self.errors)}")
            for e in self.errors[:5]:
                lines.append(f"    - {e}")
        return "\n".join(lines)


class Orchestrator:
    """Chains all workflow steps into a single pipeline."""

    def __init__(
        self,
        agent: ClaimsAgent | None = None,
        ingestor: EmailIngestor | None = None,
        on_event: PipelineCallback | None = None,
    ):
        self.agent = agent or ClaimsAgent()
        self.ingestor = ingestor
        self._on_event = on_event or (lambda step, msg: None)

    def _emit(self, step: str, message: str) -> None:
        """Emit a pipeline event for logging/notifications."""
        logger.info("[%s] %s", step, message)
        self._on_event(step, message)

    # ------------------------------------------------------------------
    # Step 1: Ingest emails
    # ------------------------------------------------------------------
    def step_ingest_emails(self) -> IngestionResult:
        """Parse new emails from IMAP and create/update Shipments."""
        self._emit("ingest", "Starting email ingestion...")

        if not self.ingestor:
            self._emit("ingest", "No email ingestor configured — skipping.")
            return IngestionResult()

        try:
            self.ingestor.connect()
            result = self.ingestor.ingest()
            self.ingestor.disconnect()
        except Exception as e:
            logger.error("Email ingestion failed: %s", e)
            result = IngestionResult()
            result.errors.append(str(e))

        self._emit(
            "ingest",
            f"Done: {result.shipments_created} created, "
            f"{result.shipments_updated} updated, "
            f"{result.skipped_duplicate} duplicates skipped",
        )
        return result

    def step_ingest_eml_files(self, eml_dir: str) -> IngestionResult:
        """Parse .eml files from a directory (testing/offline mode)."""
        self._emit("ingest", f"Ingesting .eml files from {eml_dir}...")
        ingestor = self.ingestor or EmailIngestor()
        result = ingestor.ingest_from_eml_files(eml_dir)
        self._emit(
            "ingest",
            f"Done: {result.shipments_created} created, {result.shipments_updated} updated",
        )
        return result

    # ------------------------------------------------------------------
    # Step 2: Flag late deliveries
    # ------------------------------------------------------------------
    def step_flag_late(self) -> tuple[int, int]:
        """Compare actual vs guaranteed dates, set DELIVERED_LATE status.

        Returns (newly_late, total_late).
        """
        self._emit("flag", "Checking for late deliveries...")
        db = SessionLocal()
        try:
            # Find delivered shipments where actual > guaranteed
            delivered = (
                db.query(Shipment)
                .filter(
                    Shipment.status == ShipmentStatus.DELIVERED,
                    Shipment.actual_delivery_date.isnot(None),
                    Shipment.guaranteed_delivery_date.isnot(None),
                )
                .all()
            )

            newly_late = 0
            for s in delivered:
                if s.actual_delivery_date > s.guaranteed_delivery_date:
                    s.status = ShipmentStatus.DELIVERED_LATE
                    newly_late += 1
                    self._emit("flag", f"  {s.tracking_number}: {s.days_late} day(s) late")

            db.commit()

            total_late = (
                db.query(Shipment)
                .filter(Shipment.status == ShipmentStatus.DELIVERED_LATE)
                .count()
            )

            self._emit("flag", f"Done: {newly_late} newly flagged, {total_late} total late")
            return newly_late, total_late
        finally:
            db.close()

    # ------------------------------------------------------------------
    # Step 3: Check eligibility
    # ------------------------------------------------------------------
    def step_check_eligibility(self) -> list[dict]:
        """Check eligibility for all unclaimed late shipments.

        Returns list of eligibility result dicts.
        """
        self._emit("eligibility", "Checking carrier policy eligibility...")
        shipments = self.agent.scan_late_shipments(unclaimed_only=True)
        if not shipments:
            self._emit("eligibility", "No unclaimed late shipments to check.")
            return []

        results = self.agent.check_all_eligibility(shipments)

        eligible = [r for r in results if r["eligible"]]
        ineligible = [r for r in results if not r["eligible"]]

        for r in eligible:
            s = r["shipment"]
            self._emit(
                "eligibility",
                f"  ✓ {s.tracking_number} ({s.carrier.value} {s.service_level.value}) — "
                f"{s.days_late}d late, EUR {s.declared_value:.2f}",
            )

        for r in ineligible:
            s = r["shipment"]
            self._emit(
                "eligibility",
                f"  ✗ {s.tracking_number} ({s.carrier.value}): {r['reason'][:60]}",
            )

        self._emit(
            "eligibility",
            f"Done: {len(eligible)} eligible, {len(ineligible)} ineligible",
        )
        return results

    # ------------------------------------------------------------------
    # Step 4: Draft claims
    # ------------------------------------------------------------------
    def step_draft_claims(self, eligibility_results: list[dict]) -> list[Claim]:
        """Auto-draft claims for all eligible shipments.

        Returns list of created Claim objects.
        """
        self._emit("draft", "Drafting claims for eligible shipments...")
        claims = self.agent.draft_all_eligible(eligibility_results)

        total_value = sum(c.claim_amount or 0 for c in claims)
        for c in claims:
            self._emit(
                "draft",
                f"  Claim #{c.id} — Shipment #{c.shipment_id} — EUR {c.claim_amount:.2f}",
            )

        self._emit(
            "draft",
            f"Done: {len(claims)} claim draft(s), total EUR {total_value:.2f}",
        )
        return claims

    # ------------------------------------------------------------------
    # Full pipeline
    # ------------------------------------------------------------------
    def run_pipeline(
        self,
        skip_ingestion: bool = False,
        eml_dir: str | None = None,
        auto_draft: bool = True,
    ) -> PipelineResult:
        """Execute the full pipeline: ingest → flag → check → draft.

        Args:
            skip_ingestion: Skip email ingestion step.
            eml_dir: If set, ingest from .eml files instead of IMAP.
            auto_draft: Automatically draft claims for eligible shipments.

        Returns:
            PipelineResult with summary statistics.
        """
        result = PipelineResult()
        self._emit("pipeline", "=" * 50)
        self._emit("pipeline", f"Starting pipeline at {result.started_at.isoformat()}")
        self._emit("pipeline", "=" * 50)

        # Step 1: Ingest
        if not skip_ingestion:
            if eml_dir:
                ingestion = self.step_ingest_eml_files(eml_dir)
            else:
                ingestion = self.step_ingest_emails()

            result.emails_scanned = ingestion.emails_scanned
            result.shipments_created = ingestion.shipments_created
            result.shipments_updated = ingestion.shipments_updated
            result.errors.extend(ingestion.errors)

        # Step 2: Flag late
        newly_late, total_late = self.step_flag_late()
        result.newly_late = newly_late
        result.total_late = total_late

        # Step 3: Check eligibility
        eligibility_results = self.step_check_eligibility()
        eligible = [r for r in eligibility_results if r["eligible"]]
        ineligible = [r for r in eligibility_results if not r["eligible"]]
        result.eligible_count = len(eligible)
        result.ineligible_count = len(ineligible)

        # Step 4: Draft claims
        if auto_draft and eligible:
            claims = self.step_draft_claims(eligibility_results)
            result.drafts_created = len(claims)
            result.total_claimable_eur = sum(c.claim_amount or 0 for c in claims)
        elif eligible:
            self._emit("draft", f"Skipping auto-draft. {len(eligible)} eligible shipments awaiting manual review.")

        result.finished_at = datetime.utcnow()
        self._emit("pipeline", "=" * 50)
        self._emit("pipeline", result.summary())
        self._emit("pipeline", "=" * 50)

        return result
