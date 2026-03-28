"""Database models for shipments and claims."""

from datetime import datetime, date
from enum import Enum

from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    DateTime,
    Date,
    Text,
    Boolean,
    ForeignKey,
    Enum as SQLEnum,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from .config import settings

Base = declarative_base()


class CarrierName(str, Enum):
    AUSTRIAN_POST = "austrian_post"
    DHL = "dhl"
    DPD = "dpd"
    GLS = "gls"
    FEDEX = "fedex"
    UPS = "ups"
    HERMES = "hermes"


class ServiceLevel(str, Enum):
    STANDARD = "standard"
    EXPRESS = "express"
    EXPRESS_9 = "express_9"
    EXPRESS_10 = "express_10"
    EXPRESS_12 = "express_12"
    NEXT_DAY = "next_day"
    SAME_DAY = "same_day"


class ShipmentStatus(str, Enum):
    IN_TRANSIT = "in_transit"
    DELIVERED = "delivered"
    DELIVERED_LATE = "delivered_late"
    LOST = "lost"
    RETURNED = "returned"


class ClaimStatus(str, Enum):
    IDENTIFIED = "identified"  # Late delivery detected
    ELIGIBLE = "eligible"  # Meets carrier claim criteria
    INELIGIBLE = "ineligible"  # Does not meet criteria
    DRAFT = "draft"  # Claim drafted, pending review
    APPROVED = "approved"  # Approved by human reviewer
    SUBMITTED = "submitted"  # Sent to carrier
    ACCEPTED = "accepted"  # Carrier accepted the claim
    DENIED = "denied"  # Carrier denied the claim
    REFUNDED = "refunded"  # Refund received


class Shipment(Base):
    __tablename__ = "shipments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tracking_number = Column(String(100), unique=True, nullable=False, index=True)
    carrier = Column(SQLEnum(CarrierName), nullable=False, index=True)
    service_level = Column(SQLEnum(ServiceLevel), nullable=False)
    status = Column(SQLEnum(ShipmentStatus), default=ShipmentStatus.IN_TRANSIT)

    # Addresses
    sender_name = Column(String(200))
    sender_address = Column(Text)
    recipient_name = Column(String(200))
    recipient_address = Column(Text)
    recipient_city = Column(String(100))
    recipient_country = Column(String(10), default="AT")

    # Dates
    ship_date = Column(Date, nullable=False)
    guaranteed_delivery_date = Column(Date)
    actual_delivery_date = Column(Date)

    # Package details
    weight_kg = Column(Float)
    declared_value = Column(Float)
    currency = Column(String(3), default="EUR")
    description = Column(Text)
    reference_number = Column(String(100))  # Internal order/reference

    # Carrier-specific
    carrier_account_number = Column(String(100))
    waybill_number = Column(String(100))

    # Ingestion source
    source = Column(String(20), default="manual")  # manual | csv | email
    source_email_id = Column(String(500))  # Email Message-ID for dedup
    source_file = Column(String(500))  # CSV filename for audit trail

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    claims = relationship("Claim", back_populates="shipment")

    @property
    def days_late(self) -> int | None:
        if self.guaranteed_delivery_date and self.actual_delivery_date:
            delta = self.actual_delivery_date - self.guaranteed_delivery_date
            return max(0, delta.days)
        return None

    @property
    def is_late(self) -> bool:
        return (self.days_late or 0) > 0

    def __repr__(self) -> str:
        return f"<Shipment {self.tracking_number} ({self.carrier.value})>"


class Claim(Base):
    __tablename__ = "claims"

    id = Column(Integer, primary_key=True, autoincrement=True)
    shipment_id = Column(Integer, ForeignKey("shipments.id"), nullable=False)
    status = Column(SQLEnum(ClaimStatus), default=ClaimStatus.IDENTIFIED)

    # Claim details
    claim_type = Column(String(50), default="late_delivery")
    claim_amount = Column(Float)  # Requested refund amount
    currency = Column(String(3), default="EUR")
    claim_reason = Column(Text)  # AI-generated claim narrative
    carrier_reference = Column(String(100))  # Carrier's claim ID after submission

    # Eligibility
    eligible = Column(Boolean)
    eligibility_reason = Column(Text)
    carrier_policy_reference = Column(Text)

    # Filing details
    filing_deadline = Column(Date)
    filed_date = Column(Date)
    filed_via = Column(String(50))  # portal, email, phone, api
    filed_to_email = Column(String(200))
    claim_form_path = Column(String(500))  # Path to generated claim document

    # Resolution
    resolution_date = Column(Date)
    resolution_notes = Column(Text)
    refund_amount = Column(Float)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    reviewed_by = Column(String(100))

    # Relationships
    shipment = relationship("Shipment", back_populates="claims")

    def __repr__(self) -> str:
        return f"<Claim {self.id} for Shipment {self.shipment_id} ({self.status.value})>"


# Database setup
engine = create_engine(settings.database_url, echo=False)
SessionLocal = sessionmaker(bind=engine)


def init_db():
    """Create all tables."""
    Base.metadata.create_all(engine)


def migrate_db():
    """Add new columns to existing tables (idempotent)."""
    from sqlalchemy import text, inspect

    inspector = inspect(engine)
    existing_columns = {c["name"] for c in inspector.get_columns("shipments")}

    new_columns = {
        "source": "VARCHAR(20) DEFAULT 'manual'",
        "source_email_id": "VARCHAR(500)",
        "source_file": "VARCHAR(500)",
    }
    with engine.connect() as conn:
        for col_name, col_def in new_columns.items():
            if col_name not in existing_columns:
                conn.execute(text(f"ALTER TABLE shipments ADD COLUMN {col_name} {col_def}"))
        conn.commit()


def get_db():
    """Get a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
