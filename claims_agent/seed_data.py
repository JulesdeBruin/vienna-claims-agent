"""Seed the database with sample shipment data for testing."""

from datetime import date

from .models import (
    CarrierName,
    ServiceLevel,
    Shipment,
    ShipmentStatus,
    SessionLocal,
    init_db,
)


SAMPLE_SHIPMENTS = [
    # DHL Express — late, eligible
    {
        "tracking_number": "1234567890",
        "carrier": CarrierName.DHL,
        "service_level": ServiceLevel.EXPRESS_12,
        "status": ShipmentStatus.DELIVERED_LATE,
        "sender_name": "Acme Logistics GmbH",
        "sender_address": "Mariahilfer Straße 100, 1060 Wien",
        "recipient_name": "TechCorp München",
        "recipient_address": "Leopoldstraße 50, 80802 München",
        "recipient_city": "München",
        "recipient_country": "DE",
        "ship_date": date(2026, 3, 20),
        "guaranteed_delivery_date": date(2026, 3, 21),
        "actual_delivery_date": date(2026, 3, 24),
        "weight_kg": 2.5,
        "declared_value": 45.00,
        "description": "Electronic components",
        "reference_number": "ORD-2026-4401",
        "carrier_account_number": "DHL-AT-55501",
    },
    # UPS — late, eligible (GSR)
    {
        "tracking_number": "1Z999AA10123456784",
        "carrier": CarrierName.UPS,
        "service_level": ServiceLevel.EXPRESS,
        "status": ShipmentStatus.DELIVERED_LATE,
        "sender_name": "Acme Logistics GmbH",
        "sender_address": "Mariahilfer Straße 100, 1060 Wien",
        "recipient_name": "PharmaCo AG",
        "recipient_address": "Bahnhofstraße 12, 8001 Zürich",
        "recipient_city": "Zürich",
        "recipient_country": "CH",
        "ship_date": date(2026, 3, 22),
        "guaranteed_delivery_date": date(2026, 3, 23),
        "actual_delivery_date": date(2026, 3, 26),
        "weight_kg": 0.8,
        "declared_value": 32.50,
        "description": "Lab samples",
        "reference_number": "ORD-2026-4412",
        "carrier_account_number": "UPS-AT-77201",
    },
    # FedEx Express — late, eligible
    {
        "tracking_number": "794644790138",
        "carrier": CarrierName.FEDEX,
        "service_level": ServiceLevel.EXPRESS,
        "status": ShipmentStatus.DELIVERED_LATE,
        "sender_name": "Acme Logistics GmbH",
        "sender_address": "Mariahilfer Straße 100, 1060 Wien",
        "recipient_name": "GlobalParts Ltd",
        "recipient_address": "23 King Street, London EC2V 8EH",
        "recipient_city": "London",
        "recipient_country": "GB",
        "ship_date": date(2026, 3, 18),
        "guaranteed_delivery_date": date(2026, 3, 19),
        "actual_delivery_date": date(2026, 3, 22),
        "weight_kg": 5.0,
        "declared_value": 78.00,
        "description": "Mechanical parts",
        "reference_number": "ORD-2026-4398",
        "carrier_account_number": "FEDEX-AT-33100",
    },
    # DPD Standard — late but INELIGIBLE (standard service)
    {
        "tracking_number": "01234567890123",
        "carrier": CarrierName.DPD,
        "service_level": ServiceLevel.STANDARD,
        "status": ShipmentStatus.DELIVERED_LATE,
        "sender_name": "Acme Logistics GmbH",
        "sender_address": "Mariahilfer Straße 100, 1060 Wien",
        "recipient_name": "Moda Fashion s.r.o.",
        "recipient_address": "Wenceslas Square 5, 110 00 Praha",
        "recipient_city": "Praha",
        "recipient_country": "CZ",
        "ship_date": date(2026, 3, 19),
        "guaranteed_delivery_date": date(2026, 3, 22),
        "actual_delivery_date": date(2026, 3, 25),
        "weight_kg": 3.2,
        "declared_value": 18.50,
        "description": "Clothing samples",
        "reference_number": "ORD-2026-4405",
    },
    # GLS Express — late, eligible
    {
        "tracking_number": "GLS12345678901",
        "carrier": CarrierName.GLS,
        "service_level": ServiceLevel.NEXT_DAY,
        "status": ShipmentStatus.DELIVERED_LATE,
        "sender_name": "Acme Logistics GmbH",
        "sender_address": "Mariahilfer Straße 100, 1060 Wien",
        "recipient_name": "AutoTeile Handel GmbH",
        "recipient_address": "Industriestraße 22, 4020 Linz",
        "recipient_city": "Linz",
        "recipient_country": "AT",
        "ship_date": date(2026, 3, 25),
        "guaranteed_delivery_date": date(2026, 3, 26),
        "actual_delivery_date": date(2026, 3, 28),
        "weight_kg": 12.0,
        "declared_value": 55.00,
        "description": "Auto spare parts",
        "reference_number": "ORD-2026-4420",
        "carrier_account_number": "GLS-AT-10050",
    },
    # Austrian Post — standard, late, INELIGIBLE
    {
        "tracking_number": "AT123456789",
        "carrier": CarrierName.AUSTRIAN_POST,
        "service_level": ServiceLevel.STANDARD,
        "status": ShipmentStatus.DELIVERED_LATE,
        "sender_name": "Acme Logistics GmbH",
        "sender_address": "Mariahilfer Straße 100, 1060 Wien",
        "recipient_name": "Wiener Handels GmbH",
        "recipient_address": "Kärntner Ring 10, 1010 Wien",
        "recipient_city": "Wien",
        "recipient_country": "AT",
        "ship_date": date(2026, 3, 15),
        "guaranteed_delivery_date": date(2026, 3, 17),
        "actual_delivery_date": date(2026, 3, 21),
        "weight_kg": 1.5,
        "declared_value": 8.50,
        "description": "Documents",
        "reference_number": "ORD-2026-4390",
    },
    # DHL Express 9:00 — late, eligible, URGENT (approaching deadline)
    {
        "tracking_number": "9876543210",
        "carrier": CarrierName.DHL,
        "service_level": ServiceLevel.EXPRESS_9,
        "status": ShipmentStatus.DELIVERED_LATE,
        "sender_name": "Acme Logistics GmbH",
        "sender_address": "Mariahilfer Straße 100, 1060 Wien",
        "recipient_name": "BioMed Italia S.p.A.",
        "recipient_address": "Via Roma 15, 20121 Milano",
        "recipient_city": "Milano",
        "recipient_country": "IT",
        "ship_date": date(2026, 3, 16),
        "guaranteed_delivery_date": date(2026, 3, 17),
        "actual_delivery_date": date(2026, 3, 19),
        "weight_kg": 0.3,
        "declared_value": 62.00,
        "description": "Medical samples - temperature controlled",
        "reference_number": "ORD-2026-4395",
        "carrier_account_number": "DHL-AT-55501",
        "waybill_number": "WB-DHL-9876543210",
    },
]


def seed():
    """Insert sample shipments into the database."""
    init_db()
    db = SessionLocal()
    try:
        existing = db.query(Shipment).count()
        if existing > 0:
            print(f"Database already has {existing} shipments. Skipping seed.")
            return

        for data in SAMPLE_SHIPMENTS:
            db.add(Shipment(**data))

        db.commit()
        print(f"Seeded {len(SAMPLE_SHIPMENTS)} sample shipments.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
