"""Rule-based claims agent — no API key required.

Scans shipments, checks carrier-specific eligibility, drafts claims,
generates submission-ready emails, and tracks status — all locally.
"""

from datetime import date, timedelta
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .carriers.base import EligibilityResult
from .carriers.registry import get_carrier_client, get_all_carriers
from .config import settings
from .models import (
    CarrierName,
    Claim,
    ClaimStatus,
    ServiceLevel,
    Shipment,
    ShipmentStatus,
    SessionLocal,
)


@dataclass
class ClaimDraft:
    shipment: Shipment
    claim_reason: str
    claim_amount: float
    eligible: bool
    eligibility_reason: str
    filing_deadline: date | None
    policy_notes: str


class ClaimsAgent:
    """Local rule-based claims filing agent."""

    def scan_late_shipments(
        self,
        carrier: CarrierName | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        unclaimed_only: bool = True,
    ) -> list[Shipment]:
        """Find all late shipments, optionally filtered."""
        db = SessionLocal()
        try:
            query = db.query(Shipment).filter(Shipment.status == ShipmentStatus.DELIVERED_LATE)
            if carrier:
                query = query.filter(Shipment.carrier == carrier)
            if from_date:
                query = query.filter(Shipment.ship_date >= from_date)
            if to_date:
                query = query.filter(Shipment.ship_date <= to_date)
            if unclaimed_only:
                claimed_ids = [c.shipment_id for c in db.query(Claim.shipment_id).all()]
                if claimed_ids:
                    query = query.filter(~Shipment.id.in_(claimed_ids))

            shipments = query.order_by(Shipment.ship_date.desc()).all()
            # Detach from session so they're usable after close
            db.expunge_all()
            return shipments
        finally:
            db.close()

    def check_eligibility(self, shipment: Shipment) -> dict:
        """Check claim eligibility for a single shipment."""
        client = get_carrier_client(shipment.carrier)
        result = client.check_eligibility(
            service_level=shipment.service_level,
            ship_date=shipment.ship_date,
            guaranteed_delivery=shipment.guaranteed_delivery_date,
            actual_delivery=shipment.actual_delivery_date,
            shipping_cost=shipment.declared_value,
        )
        return {
            "shipment": shipment,
            "eligible": result.result == EligibilityResult.ELIGIBLE,
            "result": result.result.value,
            "reason": result.reason,
            "filing_deadline": result.filing_deadline,
            "estimated_refund": result.estimated_refund,
            "recommended_action": result.recommended_action,
            "policy": result.policy,
        }

    def check_all_eligibility(self, shipments: list[Shipment]) -> list[dict]:
        """Check eligibility for a batch of shipments."""
        return [self.check_eligibility(s) for s in shipments]

    def draft_claim(self, shipment: Shipment, eligibility: dict) -> Claim:
        """Create a draft claim in the database."""
        days_late = (
            (shipment.actual_delivery_date - shipment.guaranteed_delivery_date).days
            if shipment.actual_delivery_date and shipment.guaranteed_delivery_date
            else 0
        )
        policy = eligibility["policy"]

        reason = (
            f"Late delivery claim for shipment {shipment.tracking_number}. "
            f"Service: {shipment.service_level.value}. "
            f"Shipped: {shipment.ship_date.isoformat()}. "
            f"Guaranteed delivery: {shipment.guaranteed_delivery_date.isoformat() if shipment.guaranteed_delivery_date else 'N/A'}. "
            f"Actual delivery: {shipment.actual_delivery_date.isoformat() if shipment.actual_delivery_date else 'N/A'}. "
            f"Delay: {days_late} day(s). "
            f"Under the {shipment.carrier.value} {policy.refund_type} guarantee, "
            f"we request a refund of EUR {shipment.declared_value:.2f} "
            f"for failure to meet the contracted delivery time."
        )

        db = SessionLocal()
        try:
            claim = Claim(
                shipment_id=shipment.id,
                status=ClaimStatus.DRAFT,
                claim_amount=shipment.declared_value,
                claim_reason=reason,
                eligible=eligibility["eligible"],
                eligibility_reason=eligibility["reason"],
                carrier_policy_reference=policy.notes,
                filing_deadline=eligibility["filing_deadline"],
            )
            db.add(claim)
            db.commit()
            db.refresh(claim)
            db.expunge(claim)
            return claim
        finally:
            db.close()

    def draft_all_eligible(self, eligibility_results: list[dict]) -> list[Claim]:
        """Draft claims for all eligible shipments."""
        claims = []
        for e in eligibility_results:
            if e["eligible"]:
                claim = self.draft_claim(e["shipment"], e)
                claims.append(claim)
        return claims

    def approve_claim(self, claim_id: int, reviewer: str = "operator") -> Claim:
        """Move a claim from draft to approved."""
        db = SessionLocal()
        try:
            claim = db.query(Claim).get(claim_id)
            if not claim:
                raise ValueError(f"Claim {claim_id} not found")
            if claim.status != ClaimStatus.DRAFT:
                raise ValueError(f"Claim is '{claim.status.value}', expected 'draft'")
            claim.status = ClaimStatus.APPROVED
            claim.reviewed_by = reviewer
            db.commit()
            db.refresh(claim)
            db.expunge(claim)
            return claim
        finally:
            db.close()

    def generate_claim_email(self, claim_id: int) -> str:
        """Generate a submission-ready claim email."""
        db = SessionLocal()
        try:
            claim = db.query(Claim).get(claim_id)
            if not claim:
                raise ValueError(f"Claim {claim_id} not found")
            shipment = claim.shipment
            client = get_carrier_client(shipment.carrier)
            policy = client.claim_policy

            days_late = (
                (shipment.actual_delivery_date - shipment.guaranteed_delivery_date).days
                if shipment.actual_delivery_date and shipment.guaranteed_delivery_date
                else 0
            )

            email = f"""Subject: Late Delivery Claim — {shipment.tracking_number}

Dear {shipment.carrier.value.replace('_', ' ').title()} Claims Department,

We are writing to file a claim for late delivery under your {policy.refund_type.replace('_', ' ')} guarantee.

SHIPMENT DETAILS:
  Tracking Number:      {shipment.tracking_number}
  Waybill Number:       {shipment.waybill_number or shipment.tracking_number}
  Account Number:       {shipment.carrier_account_number or 'N/A'}
  Service Level:        {shipment.service_level.value}
  Ship Date:            {shipment.ship_date.isoformat()}
  Guaranteed Delivery:  {shipment.guaranteed_delivery_date.isoformat() if shipment.guaranteed_delivery_date else 'N/A'}
  Actual Delivery:      {shipment.actual_delivery_date.isoformat() if shipment.actual_delivery_date else 'N/A'}
  Days Late:            {days_late}

CLAIM DETAILS:
  Claim Amount:   EUR {claim.claim_amount:.2f}
  Reference:      {shipment.reference_number or 'N/A'}

We request a refund of EUR {claim.claim_amount:.2f} in accordance with your service guarantee policy for failure to deliver within the contracted timeframe.

Please confirm receipt of this claim and provide a reference number for our records.

Regards,
{settings.company_contact_person}
{settings.company_name}
{settings.company_address}
{settings.company_email}
{settings.company_phone}
"""

            submit_info = f"""
--- SUBMISSION INFO ---
Carrier:    {shipment.carrier.value}
Method:     {', '.join(policy.filing_methods)}
Portal:     {policy.portal_url or 'N/A'}
Email:      {policy.claim_email or 'N/A'}
Phone:      {policy.claim_phone or 'N/A'}
Deadline:   {claim.filing_deadline.isoformat() if claim.filing_deadline else 'N/A'}
Required:   {', '.join(policy.documentation_required) or 'N/A'}
"""
            return email + submit_info
        finally:
            db.close()

    def get_all_claims(self, status: ClaimStatus | None = None) -> list[dict]:
        """List all claims with shipment info."""
        db = SessionLocal()
        try:
            query = db.query(Claim)
            if status:
                query = query.filter(Claim.status == status)
            claims = query.all()
            results = []
            for c in claims:
                s = c.shipment
                results.append(
                    {
                        "claim_id": c.id,
                        "tracking": s.tracking_number,
                        "carrier": s.carrier.value,
                        "status": c.status.value,
                        "amount": c.claim_amount,
                        "deadline": c.filing_deadline,
                        "days_late": s.days_late,
                    }
                )
            return results
        finally:
            db.close()

    def get_summary(self) -> dict:
        """Get a summary of all claims."""
        db = SessionLocal()
        try:
            claims = db.query(Claim).all()
            if not claims:
                return {"total": 0}

            by_status = {}
            by_carrier = {}
            total_claimed = 0.0
            total_refunded = 0.0

            for c in claims:
                by_status[c.status.value] = by_status.get(c.status.value, 0) + 1
                carrier = c.shipment.carrier.value
                if carrier not in by_carrier:
                    by_carrier[carrier] = {"count": 0, "claimed": 0.0}
                by_carrier[carrier]["count"] += 1
                by_carrier[carrier]["claimed"] += c.claim_amount or 0
                total_claimed += c.claim_amount or 0
                if c.refund_amount:
                    total_refunded += c.refund_amount

            return {
                "total": len(claims),
                "total_claimed_eur": round(total_claimed, 2),
                "total_refunded_eur": round(total_refunded, 2),
                "by_status": by_status,
                "by_carrier": by_carrier,
            }
        finally:
            db.close()
