"""Email notifications for claims pipeline events.

Sends SMTP alerts to the operator when:
- Pipeline run completes (daily summary)
- Filing deadlines are urgent (within 3 days)
- New claim drafts are ready for review

Uses only stdlib (smtplib, email.mime) — zero extra dependencies.
Falls back to console logging if SMTP is not configured.
"""

import logging
import smtplib
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import settings
from .models import Claim, ClaimStatus, SessionLocal
from .orchestrator import PipelineResult

logger = logging.getLogger(__name__)


class Notifier:
    """Send email notifications about claims pipeline events."""

    @property
    def _smtp_configured(self) -> bool:
        return bool(settings.smtp_host and settings.notification_email)

    def _send_email(self, subject: str, body: str) -> bool:
        """Send an email via SMTP. Returns True on success."""
        if not self._smtp_configured:
            logger.info("[Notification] %s\n%s", subject, body)
            return False

        msg = MIMEMultipart()
        msg["From"] = settings.smtp_user or settings.company_email
        msg["To"] = settings.notification_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
                server.ehlo()
                if settings.smtp_port != 25:
                    server.starttls()
                if settings.smtp_user and settings.smtp_password:
                    server.login(settings.smtp_user, settings.smtp_password)
                server.send_message(msg)
            logger.info("Notification sent: %s", subject)
            return True
        except Exception as e:
            logger.error("Failed to send notification: %s", e)
            return False

    def notify_pipeline_complete(self, result: PipelineResult) -> None:
        """Send a summary email after a pipeline run."""
        subject = (
            f"[Claims Agent] Pipeline complete — "
            f"{result.drafts_created} new drafts, "
            f"EUR {result.total_claimable_eur:.2f} claimable"
        )
        body = f"""Daily Claims Pipeline Summary
{'=' * 40}

{result.summary()}

---
{settings.company_name}
Vienna Claims Agent — Automated Report
"""
        self._send_email(subject, body)

    def notify_urgent_deadlines(self) -> None:
        """Alert about claims with filing deadlines within 3 days."""
        db = SessionLocal()
        try:
            threshold = date.today() + timedelta(days=3)
            urgent_claims = (
                db.query(Claim)
                .filter(
                    Claim.status.in_([ClaimStatus.DRAFT, ClaimStatus.APPROVED]),
                    Claim.filing_deadline.isnot(None),
                    Claim.filing_deadline <= threshold,
                )
                .all()
            )

            if not urgent_claims:
                return

            lines = []
            for c in urgent_claims:
                s = c.shipment
                days_remaining = (c.filing_deadline - date.today()).days
                urgency = "OVERDUE" if days_remaining < 0 else f"{days_remaining} day(s) left"
                lines.append(
                    f"  Claim #{c.id} | {s.tracking_number} ({s.carrier.value}) | "
                    f"EUR {c.claim_amount:.2f} | Deadline: {c.filing_deadline} ({urgency})"
                )

            subject = f"[Claims Agent] ⚠ {len(urgent_claims)} claim(s) with urgent deadlines"
            body = (
                f"The following claims have filing deadlines within 3 days:\n\n"
                + "\n".join(lines)
                + f"\n\nPlease review and submit these claims promptly.\n\n"
                f"---\n{settings.company_name}\nVienna Claims Agent"
            )
            self._send_email(subject, body)
        finally:
            db.close()

    def notify_new_drafts(self, claims: list[Claim]) -> None:
        """Alert that new claim drafts are ready for review."""
        if not claims:
            return

        total_value = sum(c.claim_amount or 0 for c in claims)
        lines = []
        for c in claims:
            lines.append(
                f"  Claim #{c.id} | Shipment #{c.shipment_id} | EUR {c.claim_amount:.2f}"
            )

        subject = (
            f"[Claims Agent] {len(claims)} new claim draft(s) — "
            f"EUR {total_value:.2f} ready for review"
        )
        body = (
            f"New claim drafts have been created and need your review:\n\n"
            + "\n".join(lines)
            + f"\n\nTotal claimable: EUR {total_value:.2f}\n\n"
            f"Run the claims agent CLI to approve and generate submission emails.\n\n"
            f"---\n{settings.company_name}\nVienna Claims Agent"
        )
        self._send_email(subject, body)
