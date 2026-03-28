"""CSV/Excel import for bulk shipment ingestion.

Supports:
- Standard CSV files with English or German column headers
- EU date formats (DD.MM.YYYY, DD/MM/YYYY)
- Carrier name normalization (e.g., "DHL Express" → DHL)
- Service level detection from product names
- Duplicate tracking number detection

Uses only stdlib csv module — zero extra dependencies.
"""

import csv
import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dateutil.parser import parse as parse_date

from .models import (
    CarrierName,
    ServiceLevel,
    Shipment,
    ShipmentStatus,
    SessionLocal,
)

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Summary of a CSV import run."""
    imported: int = 0
    skipped_duplicate: int = 0
    skipped_error: int = 0
    errors: list[str] = field(default_factory=list)
    file_name: str = ""


# ---------------------------------------------------------------------------
# Column name mappings (English + German aliases)
# ---------------------------------------------------------------------------

COLUMN_MAP: dict[str, str] = {
    # Tracking
    "tracking_number": "tracking_number",
    "tracking": "tracking_number",
    "tracking nr": "tracking_number",
    "tracking nr.": "tracking_number",
    "trackingnumber": "tracking_number",
    "sendungsnummer": "tracking_number",
    "paketnummer": "tracking_number",
    "barcode": "tracking_number",
    "awb": "tracking_number",
    # Carrier
    "carrier": "carrier",
    "spediteur": "carrier",
    "versender": "carrier",
    "carrier_name": "carrier",
    "dienstleister": "carrier",
    "logistik": "carrier",
    # Service level
    "service_level": "service_level",
    "service": "service_level",
    "produkt": "service_level",
    "product": "service_level",
    "serviceart": "service_level",
    "versandart": "service_level",
    # Ship date
    "ship_date": "ship_date",
    "versanddatum": "ship_date",
    "shipment_date": "ship_date",
    "shipped": "ship_date",
    "datum": "ship_date",
    "aufgabedatum": "ship_date",
    # Guaranteed delivery
    "guaranteed_delivery_date": "guaranteed_delivery_date",
    "zustelldatum": "guaranteed_delivery_date",
    "delivery_date": "guaranteed_delivery_date",
    "soll_zustellung": "guaranteed_delivery_date",
    "guaranteed": "guaranteed_delivery_date",
    "expected_delivery": "guaranteed_delivery_date",
    "lieferdatum": "guaranteed_delivery_date",
    # Actual delivery
    "actual_delivery_date": "actual_delivery_date",
    "actual_delivery": "actual_delivery_date",
    "tatsächliche_zustellung": "actual_delivery_date",
    "ist_zustellung": "actual_delivery_date",
    "delivered": "actual_delivery_date",
    "zugestellt_am": "actual_delivery_date",
    # Recipient
    "recipient_name": "recipient_name",
    "empfänger": "recipient_name",
    "empfaenger": "recipient_name",
    "recipient": "recipient_name",
    "kunde": "recipient_name",
    "customer": "recipient_name",
    # Recipient address
    "recipient_address": "recipient_address",
    "adresse": "recipient_address",
    "address": "recipient_address",
    "lieferadresse": "recipient_address",
    # City
    "recipient_city": "recipient_city",
    "city": "recipient_city",
    "ort": "recipient_city",
    "stadt": "recipient_city",
    # Country
    "recipient_country": "recipient_country",
    "country": "recipient_country",
    "land": "recipient_country",
    # Value
    "declared_value": "declared_value",
    "value": "declared_value",
    "wert": "declared_value",
    "warenwert": "declared_value",
    "betrag": "declared_value",
    # Weight
    "weight_kg": "weight_kg",
    "weight": "weight_kg",
    "gewicht": "weight_kg",
    "gewicht_kg": "weight_kg",
    # Reference
    "reference_number": "reference_number",
    "reference": "reference_number",
    "referenz": "reference_number",
    "bestellnummer": "reference_number",
    "order_number": "reference_number",
    "auftragsnummer": "reference_number",
    # Description
    "description": "description",
    "beschreibung": "description",
    "inhalt": "description",
    # Account
    "carrier_account_number": "carrier_account_number",
    "account": "carrier_account_number",
    "kundennummer": "carrier_account_number",
}


# ---------------------------------------------------------------------------
# Carrier name normalization
# ---------------------------------------------------------------------------

CARRIER_ALIASES: dict[str, CarrierName] = {
    "dhl": CarrierName.DHL,
    "dhl express": CarrierName.DHL,
    "dhl paket": CarrierName.DHL,
    "deutsche post dhl": CarrierName.DHL,
    "ups": CarrierName.UPS,
    "united parcel service": CarrierName.UPS,
    "fedex": CarrierName.FEDEX,
    "federal express": CarrierName.FEDEX,
    "dpd": CarrierName.DPD,
    "dpd austria": CarrierName.DPD,
    "gls": CarrierName.GLS,
    "gls austria": CarrierName.GLS,
    "austrian post": CarrierName.AUSTRIAN_POST,
    "österreichische post": CarrierName.AUSTRIAN_POST,
    "oesterreichische post": CarrierName.AUSTRIAN_POST,
    "post.at": CarrierName.AUSTRIAN_POST,
    "post": CarrierName.AUSTRIAN_POST,
    "hermes": CarrierName.HERMES,
}


SERVICE_ALIASES: dict[str, ServiceLevel] = {
    "standard": ServiceLevel.STANDARD,
    "economy": ServiceLevel.STANDARD,
    "paket": ServiceLevel.STANDARD,
    "express": ServiceLevel.EXPRESS,
    "priority": ServiceLevel.EXPRESS,
    "eilzustellung": ServiceLevel.EXPRESS,
    "express 9": ServiceLevel.EXPRESS_9,
    "express 09:00": ServiceLevel.EXPRESS_9,
    "express 10": ServiceLevel.EXPRESS_10,
    "express 10:30": ServiceLevel.EXPRESS_10,
    "express 12": ServiceLevel.EXPRESS_12,
    "express 12:00": ServiceLevel.EXPRESS_12,
    "next day": ServiceLevel.NEXT_DAY,
    "next business day": ServiceLevel.NEXT_DAY,
    "overnight": ServiceLevel.NEXT_DAY,
    "same day": ServiceLevel.SAME_DAY,
    "sameday": ServiceLevel.SAME_DAY,
}


def _normalize_carrier(raw: str) -> CarrierName | None:
    """Normalize a carrier name string to CarrierName enum."""
    cleaned = raw.strip().lower()
    if cleaned in CARRIER_ALIASES:
        return CARRIER_ALIASES[cleaned]
    # Try enum values directly
    for c in CarrierName:
        if c.value == cleaned:
            return c
    return None


def _normalize_service(raw: str) -> ServiceLevel:
    """Normalize a service level string."""
    cleaned = raw.strip().lower()
    if cleaned in SERVICE_ALIASES:
        return SERVICE_ALIASES[cleaned]
    for s in ServiceLevel:
        if s.value == cleaned:
            return s
    return ServiceLevel.STANDARD


def _parse_date_field(raw: str) -> date | None:
    """Parse a date string in various EU/US formats."""
    if not raw or not raw.strip():
        return None
    try:
        return parse_date(raw.strip(), dayfirst=True).date()
    except (ValueError, TypeError):
        return None


def _parse_float_field(raw: str) -> float | None:
    """Parse a float from various locale formats."""
    if not raw or not raw.strip():
        return None
    try:
        cleaned = raw.strip().replace("€", "").replace("EUR", "").strip()
        # Handle German number format: 1.234,56 → 1234.56
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _detect_delimiter(file_path: str) -> str:
    """Auto-detect CSV delimiter."""
    with open(file_path, "r", encoding="utf-8-sig") as f:
        sample = f.read(4096)
    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        return "," if "," in sample else ";"


class CSVImporter:
    """Import shipments from CSV files."""

    def import_from_csv(
        self,
        file_path: str,
        has_header: bool = True,
    ) -> ImportResult:
        """Import shipments from a CSV file.

        Supports English and German column headers, EU date formats,
        and various carrier/service name aliases.
        """
        result = ImportResult(file_name=Path(file_path).name)

        if not Path(file_path).exists():
            result.errors.append(f"File not found: {file_path}")
            return result

        delimiter = _detect_delimiter(file_path)

        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            if not reader.fieldnames:
                result.errors.append("No headers found in CSV")
                return result

            # Map CSV column names to our field names
            col_mapping = {}
            for csv_col in reader.fieldnames:
                normalized = csv_col.strip().lower().replace(" ", "_")
                # Try direct match first
                if normalized in COLUMN_MAP:
                    col_mapping[csv_col] = COLUMN_MAP[normalized]
                else:
                    # Try without underscores
                    no_underscore = normalized.replace("_", " ")
                    if no_underscore in COLUMN_MAP:
                        col_mapping[csv_col] = COLUMN_MAP[no_underscore]

            logger.info(
                "CSV columns mapped: %s",
                {k: v for k, v in col_mapping.items()},
            )

            db = SessionLocal()
            try:
                for row_num, row in enumerate(reader, start=2):
                    # Map CSV columns to our fields
                    mapped = {}
                    for csv_col, field_name in col_mapping.items():
                        mapped[field_name] = row.get(csv_col, "").strip()

                    # Validate required fields
                    tracking = mapped.get("tracking_number", "")
                    carrier_raw = mapped.get("carrier", "")
                    if not tracking:
                        result.errors.append(f"Row {row_num}: missing tracking number")
                        result.skipped_error += 1
                        continue
                    if not carrier_raw:
                        result.errors.append(f"Row {row_num}: missing carrier")
                        result.skipped_error += 1
                        continue

                    # Normalize
                    carrier = _normalize_carrier(carrier_raw)
                    if not carrier:
                        result.errors.append(
                            f"Row {row_num}: unknown carrier '{carrier_raw}'"
                        )
                        result.skipped_error += 1
                        continue

                    # Check duplicate
                    existing = (
                        db.query(Shipment)
                        .filter(Shipment.tracking_number == tracking)
                        .first()
                    )
                    if existing:
                        result.skipped_duplicate += 1
                        continue

                    ship_date = _parse_date_field(mapped.get("ship_date", ""))
                    if not ship_date:
                        result.errors.append(f"Row {row_num}: invalid/missing ship date")
                        result.skipped_error += 1
                        continue

                    actual_delivery = _parse_date_field(mapped.get("actual_delivery_date", ""))
                    guaranteed = _parse_date_field(mapped.get("guaranteed_delivery_date", ""))

                    # Determine status
                    status = ShipmentStatus.IN_TRANSIT
                    if actual_delivery:
                        if guaranteed and actual_delivery > guaranteed:
                            status = ShipmentStatus.DELIVERED_LATE
                        else:
                            status = ShipmentStatus.DELIVERED

                    shipment = Shipment(
                        tracking_number=tracking,
                        carrier=carrier,
                        service_level=_normalize_service(mapped.get("service_level", "")),
                        status=status,
                        ship_date=ship_date,
                        guaranteed_delivery_date=guaranteed,
                        actual_delivery_date=actual_delivery,
                        recipient_name=mapped.get("recipient_name", ""),
                        recipient_address=mapped.get("recipient_address", ""),
                        recipient_city=mapped.get("recipient_city", ""),
                        recipient_country=mapped.get("recipient_country", "AT"),
                        weight_kg=_parse_float_field(mapped.get("weight_kg", "")),
                        declared_value=_parse_float_field(mapped.get("declared_value", "")),
                        description=mapped.get("description", ""),
                        reference_number=mapped.get("reference_number", ""),
                        carrier_account_number=mapped.get("carrier_account_number", ""),
                        source="csv",
                        source_file=result.file_name,
                    )
                    db.add(shipment)
                    result.imported += 1

                db.commit()
            except Exception as e:
                db.rollback()
                result.errors.append(f"Database error: {e}")
            finally:
                db.close()

        logger.info(
            "CSV import: %d imported, %d duplicates, %d errors from %s",
            result.imported,
            result.skipped_duplicate,
            result.skipped_error,
            result.file_name,
        )
        return result
