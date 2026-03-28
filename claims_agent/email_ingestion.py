"""Email ingestion engine — parse carrier shipping & delivery confirmation emails via IMAP.

Supports two types of carrier emails:
1. Shipping confirmations → create new Shipment records
2. Delivery notifications → update existing Shipment with actual delivery date

Uses only stdlib (imaplib, email) — zero extra dependencies.
"""

import imaplib
import email
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from email.message import Message
from typing import Protocol

from dateutil.parser import parse as parse_date

from .config import settings
from .models import (
    CarrierName,
    ServiceLevel,
    Shipment,
    ShipmentStatus,
    SessionLocal,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ParsedShipment:
    """Extracted data from a shipping confirmation email."""
    tracking_number: str
    carrier: CarrierName
    service_level: ServiceLevel = ServiceLevel.STANDARD
    ship_date: date | None = None
    guaranteed_delivery_date: date | None = None
    sender_name: str = ""
    recipient_name: str = ""
    recipient_address: str = ""
    recipient_city: str = ""
    recipient_country: str = "AT"
    weight_kg: float | None = None
    declared_value: float | None = None
    reference_number: str = ""
    carrier_account_number: str = ""
    waybill_number: str = ""
    description: str = ""


@dataclass
class ParsedDelivery:
    """Extracted data from a delivery notification email."""
    tracking_number: str
    carrier: CarrierName
    delivery_date: date
    delivery_status: str = "delivered"


@dataclass
class IngestionResult:
    """Summary of an email ingestion run."""
    emails_scanned: int = 0
    shipments_created: int = 0
    shipments_updated: int = 0  # delivery notifications
    skipped_duplicate: int = 0
    skipped_unparseable: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Carrier email parsers
# ---------------------------------------------------------------------------

class CarrierEmailParser(Protocol):
    """Protocol for carrier-specific email parsers."""

    def can_parse(self, subject: str, sender: str) -> bool: ...
    def parse_shipment(self, msg: Message) -> ParsedShipment | None: ...
    def parse_delivery(self, msg: Message) -> ParsedDelivery | None: ...


def _get_text_body(msg: Message) -> str:
    """Extract plain text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
            elif ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace")
                    # Basic HTML tag stripping
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text)
                    return text
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def _find_date(text: str, patterns: list[str]) -> date | None:
    """Search text for date patterns and return the first match."""
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                return parse_date(match.group(1), dayfirst=True).date()
            except (ValueError, TypeError):
                continue
    return None


def _find_float(text: str, patterns: list[str]) -> float | None:
    """Search text for a numeric value."""
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            try:
                val = match.group(1).replace(",", ".")
                return float(val)
            except (ValueError, IndexError):
                continue
    return None


def _detect_service_level(text: str) -> ServiceLevel:
    """Detect service level from email text."""
    text_lower = text.lower()
    if any(k in text_lower for k in ["express 9", "express 09", "09:00", "9:00 am"]):
        return ServiceLevel.EXPRESS_9
    if any(k in text_lower for k in ["express 10", "10:00", "10:30"]):
        return ServiceLevel.EXPRESS_10
    if any(k in text_lower for k in ["express 12", "12:00"]):
        return ServiceLevel.EXPRESS_12
    if any(k in text_lower for k in ["same day", "sameday", "same-day"]):
        return ServiceLevel.SAME_DAY
    if any(k in text_lower for k in ["next day", "next-day", "overnight", "next business day"]):
        return ServiceLevel.NEXT_DAY
    if any(k in text_lower for k in ["express", "priority", "eilzustellung"]):
        return ServiceLevel.EXPRESS
    return ServiceLevel.STANDARD


# ---- DHL Parser ----

class DHLEmailParser:
    def can_parse(self, subject: str, sender: str) -> bool:
        sender_l = sender.lower()
        subject_l = subject.lower()
        return (
            "dhl" in sender_l
            or "dhl" in subject_l
            or "versandbestätigung" in subject_l
            or "shipment notification" in subject_l
        )

    def parse_shipment(self, msg: Message) -> ParsedShipment | None:
        body = _get_text_body(msg)
        # DHL tracking: 10+ digits or JD + 18 digits
        tracking_match = re.search(r"\b(JD\d{18}|\d{10,20})\b", body)
        if not tracking_match:
            return None

        ship_date = _find_date(body, [
            r"(?:Ship(?:ment)?\s*Date|Versanddatum)[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
            r"(?:shipped|versendet)\s+(?:on\s+)?(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ])
        guaranteed = _find_date(body, [
            r"(?:Estimated Delivery|Voraussichtliche Zustellung|Expected Delivery)[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
            r"(?:deliver(?:y|ed)?\s+by|Zustellung bis)[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ])

        return ParsedShipment(
            tracking_number=tracking_match.group(1),
            carrier=CarrierName.DHL,
            service_level=_detect_service_level(body),
            ship_date=ship_date or date.today(),
            guaranteed_delivery_date=guaranteed,
            declared_value=_find_float(body, [r"(?:Value|Wert)[:\s]+(?:EUR|€)?\s*(\d+[\.,]?\d*)"]),
            weight_kg=_find_float(body, [r"(?:Weight|Gewicht)[:\s]+(\d+[\.,]?\d*)\s*kg"]),
        )

    def parse_delivery(self, msg: Message) -> ParsedDelivery | None:
        subject = msg.get("Subject", "").lower()
        if not any(k in subject for k in ["delivered", "zugestellt", "delivery confirmation"]):
            return None
        body = _get_text_body(msg)
        tracking_match = re.search(r"\b(JD\d{18}|\d{10,20})\b", body)
        if not tracking_match:
            return None
        delivery_date = _find_date(body, [
            r"(?:Delivered|Zugestellt)\s+(?:on|am)?[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ]) or date.today()

        return ParsedDelivery(
            tracking_number=tracking_match.group(1),
            carrier=CarrierName.DHL,
            delivery_date=delivery_date,
        )


# ---- UPS Parser ----

class UPSEmailParser:
    def can_parse(self, subject: str, sender: str) -> bool:
        sender_l = sender.lower()
        subject_l = subject.lower()
        return "ups" in sender_l or "ups" in subject_l

    def parse_shipment(self, msg: Message) -> ParsedShipment | None:
        body = _get_text_body(msg)
        tracking_match = re.search(r"\b(1Z[A-Z0-9]{16})\b", body)
        if not tracking_match:
            return None

        ship_date = _find_date(body, [
            r"(?:Ship Date|Versanddatum)[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ])
        guaranteed = _find_date(body, [
            r"(?:Scheduled Delivery|Delivery Date|Zustelldatum)[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ])

        return ParsedShipment(
            tracking_number=tracking_match.group(1),
            carrier=CarrierName.UPS,
            service_level=_detect_service_level(body),
            ship_date=ship_date or date.today(),
            guaranteed_delivery_date=guaranteed,
            declared_value=_find_float(body, [r"(?:Value|Wert)[:\s]+(?:EUR|€)?\s*(\d+[\.,]?\d*)"]),
            weight_kg=_find_float(body, [r"(?:Weight|Gewicht)[:\s]+(\d+[\.,]?\d*)\s*(?:kg|lbs)"]),
        )

    def parse_delivery(self, msg: Message) -> ParsedDelivery | None:
        subject = msg.get("Subject", "").lower()
        if not any(k in subject for k in ["delivered", "zugestellt", "delivery"]):
            return None
        body = _get_text_body(msg)
        tracking_match = re.search(r"\b(1Z[A-Z0-9]{16})\b", body)
        if not tracking_match:
            return None
        delivery_date = _find_date(body, [
            r"(?:Delivered|Zugestellt)\s+(?:on|am)?[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ]) or date.today()
        return ParsedDelivery(
            tracking_number=tracking_match.group(1),
            carrier=CarrierName.UPS,
            delivery_date=delivery_date,
        )


# ---- FedEx Parser ----

class FedExEmailParser:
    def can_parse(self, subject: str, sender: str) -> bool:
        sender_l = sender.lower()
        subject_l = subject.lower()
        return "fedex" in sender_l or "fedex" in subject_l

    def parse_shipment(self, msg: Message) -> ParsedShipment | None:
        body = _get_text_body(msg)
        tracking_match = re.search(r"\b(\d{12,22})\b", body)
        if not tracking_match:
            return None

        ship_date = _find_date(body, [
            r"(?:Ship Date|Versanddatum)[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ])
        guaranteed = _find_date(body, [
            r"(?:Estimated Delivery|Delivery by|Zustellung bis)[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ])

        return ParsedShipment(
            tracking_number=tracking_match.group(1),
            carrier=CarrierName.FEDEX,
            service_level=_detect_service_level(body),
            ship_date=ship_date or date.today(),
            guaranteed_delivery_date=guaranteed,
            declared_value=_find_float(body, [r"(?:Value|Wert)[:\s]+(?:EUR|€)?\s*(\d+[\.,]?\d*)"]),
            weight_kg=_find_float(body, [r"(?:Weight|Gewicht)[:\s]+(\d+[\.,]?\d*)\s*(?:kg|lbs)"]),
        )

    def parse_delivery(self, msg: Message) -> ParsedDelivery | None:
        subject = msg.get("Subject", "").lower()
        if not any(k in subject for k in ["delivered", "zugestellt"]):
            return None
        body = _get_text_body(msg)
        tracking_match = re.search(r"\b(\d{12,22})\b", body)
        if not tracking_match:
            return None
        delivery_date = _find_date(body, [
            r"(?:Delivered|Zugestellt)\s+(?:on|am)?[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ]) or date.today()
        return ParsedDelivery(
            tracking_number=tracking_match.group(1),
            carrier=CarrierName.FEDEX,
            delivery_date=delivery_date,
        )


# ---- Austrian Post Parser ----

class AustrianPostEmailParser:
    def can_parse(self, subject: str, sender: str) -> bool:
        sender_l = sender.lower()
        subject_l = subject.lower()
        return (
            "post.at" in sender_l
            or "österreichische post" in subject_l.replace("oe", "ö")
            or "sendungsverfolgung" in subject_l
        )

    def parse_shipment(self, msg: Message) -> ParsedShipment | None:
        body = _get_text_body(msg)
        tracking_match = re.search(r"\b([A-Z]{2}\d{9}[A-Z]{2})\b", body)
        if not tracking_match:
            tracking_match = re.search(r"\b(\d{13,16})\b", body)
        if not tracking_match:
            return None

        ship_date = _find_date(body, [
            r"(?:Aufgabedatum|Versanddatum|Ship Date)[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ])
        guaranteed = _find_date(body, [
            r"(?:Voraussichtliche Zustellung|Zustellung bis|Delivery by)[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ])

        return ParsedShipment(
            tracking_number=tracking_match.group(1),
            carrier=CarrierName.AUSTRIAN_POST,
            service_level=_detect_service_level(body),
            ship_date=ship_date or date.today(),
            guaranteed_delivery_date=guaranteed,
        )

    def parse_delivery(self, msg: Message) -> ParsedDelivery | None:
        subject = msg.get("Subject", "").lower()
        if not any(k in subject for k in ["zugestellt", "delivered", "abgeholt"]):
            return None
        body = _get_text_body(msg)
        tracking_match = re.search(r"\b([A-Z]{2}\d{9}[A-Z]{2}|\d{13,16})\b", body)
        if not tracking_match:
            return None
        delivery_date = _find_date(body, [
            r"(?:Zugestellt|Delivered)\s+(?:am\s+)?(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ]) or date.today()
        return ParsedDelivery(
            tracking_number=tracking_match.group(1),
            carrier=CarrierName.AUSTRIAN_POST,
            delivery_date=delivery_date,
        )


# ---- DPD Parser ----

class DPDEmailParser:
    def can_parse(self, subject: str, sender: str) -> bool:
        sender_l = sender.lower()
        subject_l = subject.lower()
        return "dpd" in sender_l or "dpd" in subject_l or "paketankündigung" in subject_l

    def parse_shipment(self, msg: Message) -> ParsedShipment | None:
        body = _get_text_body(msg)
        tracking_match = re.search(r"\b(\d{14})\b", body)
        if not tracking_match:
            return None

        ship_date = _find_date(body, [
            r"(?:Versanddatum|Ship Date)[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ])
        guaranteed = _find_date(body, [
            r"(?:Voraussichtliche Zustellung|Zustellung|Delivery)[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ])

        return ParsedShipment(
            tracking_number=tracking_match.group(1),
            carrier=CarrierName.DPD,
            service_level=_detect_service_level(body),
            ship_date=ship_date or date.today(),
            guaranteed_delivery_date=guaranteed,
        )

    def parse_delivery(self, msg: Message) -> ParsedDelivery | None:
        subject = msg.get("Subject", "").lower()
        if not any(k in subject for k in ["zugestellt", "delivered"]):
            return None
        body = _get_text_body(msg)
        tracking_match = re.search(r"\b(\d{14})\b", body)
        if not tracking_match:
            return None
        delivery_date = _find_date(body, [
            r"(?:Zugestellt|Delivered)\s+(?:am\s+)?(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ]) or date.today()
        return ParsedDelivery(
            tracking_number=tracking_match.group(1),
            carrier=CarrierName.DPD,
            delivery_date=delivery_date,
        )


# ---- GLS Parser ----

class GLSEmailParser:
    def can_parse(self, subject: str, sender: str) -> bool:
        sender_l = sender.lower()
        subject_l = subject.lower()
        return "gls" in sender_l or "gls" in subject_l or "paketinformation" in subject_l

    def parse_shipment(self, msg: Message) -> ParsedShipment | None:
        body = _get_text_body(msg)
        tracking_match = re.search(r"\b([A-Z]{3}\d{11}|\d{11,14})\b", body)
        if not tracking_match:
            return None

        ship_date = _find_date(body, [
            r"(?:Versanddatum|Ship Date)[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ])
        guaranteed = _find_date(body, [
            r"(?:Voraussichtliche Zustellung|Zustellung|Delivery)[:\s]+(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ])

        return ParsedShipment(
            tracking_number=tracking_match.group(1),
            carrier=CarrierName.GLS,
            service_level=_detect_service_level(body),
            ship_date=ship_date or date.today(),
            guaranteed_delivery_date=guaranteed,
        )

    def parse_delivery(self, msg: Message) -> ParsedDelivery | None:
        subject = msg.get("Subject", "").lower()
        if not any(k in subject for k in ["zugestellt", "delivered"]):
            return None
        body = _get_text_body(msg)
        tracking_match = re.search(r"\b([A-Z]{3}\d{11}|\d{11,14})\b", body)
        if not tracking_match:
            return None
        delivery_date = _find_date(body, [
            r"(?:Zugestellt|Delivered)\s+(?:am\s+)?(\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4})",
        ]) or date.today()
        return ParsedDelivery(
            tracking_number=tracking_match.group(1),
            carrier=CarrierName.GLS,
            delivery_date=delivery_date,
        )


# ---------------------------------------------------------------------------
# All registered parsers
# ---------------------------------------------------------------------------

ALL_PARSERS: list[CarrierEmailParser] = [
    DHLEmailParser(),
    UPSEmailParser(),
    FedExEmailParser(),
    AustrianPostEmailParser(),
    DPDEmailParser(),
    GLSEmailParser(),
]


# ---------------------------------------------------------------------------
# IMAP Email Ingestor
# ---------------------------------------------------------------------------

class EmailIngestor:
    """Connects to an IMAP mailbox and ingests carrier shipping/delivery emails."""

    def __init__(self, parsers: list[CarrierEmailParser] | None = None):
        self.parsers = parsers or ALL_PARSERS
        self._conn: imaplib.IMAP4_SSL | imaplib.IMAP4 | None = None

    def connect(self) -> None:
        """Open IMAP connection."""
        if not settings.imap_host:
            raise ValueError(
                "IMAP not configured. Set IMAP_HOST, IMAP_USER, IMAP_PASSWORD in .env"
            )
        if settings.imap_use_ssl:
            self._conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
        else:
            self._conn = imaplib.IMAP4(settings.imap_host, settings.imap_port)
        self._conn.login(settings.imap_user, settings.imap_password)
        logger.info("Connected to IMAP %s as %s", settings.imap_host, settings.imap_user)

    def disconnect(self) -> None:
        """Close IMAP connection."""
        if self._conn:
            try:
                self._conn.close()
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.disconnect()

    def _ensure_folder(self, folder: str) -> None:
        """Create IMAP folder if it doesn't exist."""
        if self._conn:
            try:
                status, _ = self._conn.select(folder)
                if status != "OK":
                    self._conn.create(folder)
            except imaplib.IMAP4.error:
                try:
                    self._conn.create(folder)
                except imaplib.IMAP4.error:
                    pass
            # Switch back to inbox
            self._conn.select(settings.imap_folder)

    def _fetch_messages(self) -> list[tuple[bytes, Message]]:
        """Fetch all unseen messages from the configured folder."""
        if not self._conn:
            raise RuntimeError("Not connected. Call connect() first.")

        self._conn.select(settings.imap_folder)
        _, msg_ids = self._conn.search(None, "UNSEEN")
        messages = []

        for uid in msg_ids[0].split():
            if not uid:
                continue
            _, msg_data = self._conn.fetch(uid, "(RFC822)")
            if msg_data and msg_data[0]:
                raw = msg_data[0]
                if isinstance(raw, tuple) and len(raw) > 1:
                    msg = email.message_from_bytes(raw[1])
                    messages.append((uid, msg))

        return messages

    def _move_to_processed(self, uid: bytes) -> None:
        """Move a processed email to the Processed folder."""
        if not self._conn:
            return
        processed = settings.imap_processed_folder
        self._ensure_folder(processed)
        try:
            self._conn.select(settings.imap_folder)
            self._conn.copy(uid, processed)
            self._conn.store(uid, "+FLAGS", "\\Deleted")
            self._conn.expunge()
        except imaplib.IMAP4.error as e:
            logger.warning("Could not move email %s to %s: %s", uid, processed, e)

    def _find_parser(self, subject: str, sender: str) -> CarrierEmailParser | None:
        """Find the first parser that can handle this email."""
        for parser in self.parsers:
            if parser.can_parse(subject, sender):
                return parser
        return None

    def _process_shipment(self, parsed: ParsedShipment, message_id: str) -> str:
        """Create or update a Shipment from parsed email data. Returns action taken."""
        db = SessionLocal()
        try:
            # Check for duplicate by tracking number
            existing = (
                db.query(Shipment)
                .filter(Shipment.tracking_number == parsed.tracking_number)
                .first()
            )
            if existing:
                return "duplicate"

            # Check for duplicate by email Message-ID
            if message_id:
                existing_email = (
                    db.query(Shipment)
                    .filter(Shipment.source_email_id == message_id)
                    .first()
                )
                if existing_email:
                    return "duplicate"

            shipment = Shipment(
                tracking_number=parsed.tracking_number,
                carrier=parsed.carrier,
                service_level=parsed.service_level,
                status=ShipmentStatus.IN_TRANSIT,
                ship_date=parsed.ship_date or date.today(),
                guaranteed_delivery_date=parsed.guaranteed_delivery_date,
                sender_name=parsed.sender_name,
                recipient_name=parsed.recipient_name,
                recipient_address=parsed.recipient_address,
                recipient_city=parsed.recipient_city,
                recipient_country=parsed.recipient_country,
                weight_kg=parsed.weight_kg,
                declared_value=parsed.declared_value,
                reference_number=parsed.reference_number,
                carrier_account_number=parsed.carrier_account_number,
                waybill_number=parsed.waybill_number,
                description=parsed.description,
                source="email",
                source_email_id=message_id,
            )
            db.add(shipment)
            db.commit()
            return "created"
        finally:
            db.close()

    def _process_delivery(self, parsed: ParsedDelivery) -> str:
        """Update a Shipment's delivery status. Returns action taken."""
        db = SessionLocal()
        try:
            shipment = (
                db.query(Shipment)
                .filter(Shipment.tracking_number == parsed.tracking_number)
                .first()
            )
            if not shipment:
                return "no_match"

            shipment.actual_delivery_date = parsed.delivery_date
            if shipment.guaranteed_delivery_date and parsed.delivery_date > shipment.guaranteed_delivery_date:
                shipment.status = ShipmentStatus.DELIVERED_LATE
            else:
                shipment.status = ShipmentStatus.DELIVERED

            db.commit()
            return "updated"
        finally:
            db.close()

    def ingest(self) -> IngestionResult:
        """Main ingestion loop — fetch emails, parse, store in DB."""
        result = IngestionResult()
        messages = self._fetch_messages()
        result.emails_scanned = len(messages)

        for uid, msg in messages:
            subject = msg.get("Subject", "")
            sender = msg.get("From", "")
            message_id = msg.get("Message-ID", "")

            parser = self._find_parser(subject, sender)
            if not parser:
                result.skipped_unparseable += 1
                continue

            try:
                # Try as delivery notification first (more specific)
                delivery = parser.parse_delivery(msg)
                if delivery:
                    action = self._process_delivery(delivery)
                    if action == "updated":
                        result.shipments_updated += 1
                        self._move_to_processed(uid)
                    continue

                # Try as shipping confirmation
                shipment = parser.parse_shipment(msg)
                if shipment:
                    action = self._process_shipment(shipment, message_id)
                    if action == "created":
                        result.shipments_created += 1
                        self._move_to_processed(uid)
                    elif action == "duplicate":
                        result.skipped_duplicate += 1
                else:
                    result.skipped_unparseable += 1

            except Exception as e:
                logger.error("Error processing email '%s': %s", subject, e)
                result.errors.append(f"{subject}: {e}")

        return result

    def ingest_from_eml_files(self, eml_dir: str) -> IngestionResult:
        """Parse .eml files from a directory (for testing without IMAP)."""
        import pathlib

        result = IngestionResult()
        eml_path = pathlib.Path(eml_dir)

        if not eml_path.exists():
            result.errors.append(f"Directory not found: {eml_dir}")
            return result

        for eml_file in sorted(eml_path.glob("*.eml")):
            result.emails_scanned += 1
            with open(eml_file, "rb") as f:
                msg = email.message_from_bytes(f.read())

            subject = msg.get("Subject", "")
            sender = msg.get("From", "")
            message_id = msg.get("Message-ID", "") or eml_file.name

            parser = self._find_parser(subject, sender)
            if not parser:
                result.skipped_unparseable += 1
                continue

            try:
                delivery = parser.parse_delivery(msg)
                if delivery:
                    action = self._process_delivery(delivery)
                    if action == "updated":
                        result.shipments_updated += 1
                    continue

                shipment = parser.parse_shipment(msg)
                if shipment:
                    action = self._process_shipment(shipment, message_id)
                    if action == "created":
                        result.shipments_created += 1
                    elif action == "duplicate":
                        result.skipped_duplicate += 1
                else:
                    result.skipped_unparseable += 1
            except Exception as e:
                logger.error("Error processing %s: %s", eml_file.name, e)
                result.errors.append(f"{eml_file.name}: {e}")

        return result
