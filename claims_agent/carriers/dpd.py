"""DPD carrier integration (Austria/EU)."""

from ..models import CarrierName, ServiceLevel
from .base import CarrierClient, CarrierClaimPolicy, TrackingInfo, TrackingEvent


class DPDClient(CarrierClient):
    TRACK_URL = "https://tracking.dpd.de/rest/plc/en_AT"

    @property
    def carrier_name(self) -> CarrierName:
        return CarrierName.DPD

    @property
    def claim_policy(self) -> CarrierClaimPolicy:
        return CarrierClaimPolicy(
            carrier=CarrierName.DPD,
            allows_late_claims=True,
            eligible_services=[ServiceLevel.EXPRESS_10, ServiceLevel.EXPRESS_12],
            filing_deadline_days=21,
            deadline_reference="shipment_date",
            who_can_file="sender",
            filing_methods=["email", "phone"],
            portal_url="https://www.dpd.com/at/en/support/",
            claim_phone="+43 800 373 200",
            refund_type="shipping_cost",
            notes=(
                "Only time-definite services (Express 10:00, 12:00) are eligible. "
                "Standard deliveries are NOT eligible for late delivery claims. "
                "Must file within 21 calendar days of shipment. "
                "Contact your DPD account manager to initiate."
            ),
            documentation_required=[
                "Proof of time-definite service contract",
                "Tracking number",
                "Shipment and delivery documentation",
            ],
        )

    async def track_shipment(self, tracking_number: str) -> TrackingInfo:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.TRACK_URL}/{tracking_number}")
            resp.raise_for_status()
            data = resp.json()

        events = [
            TrackingEvent(
                timestamp=e.get("date", "") + " " + e.get("time", ""),
                location=e.get("location", ""),
                description=e.get("content", {}).get("label", ""),
            )
            for e in data.get("parcellifecycleResponse", {})
            .get("parcelLifeCycleData", {})
            .get("scanInfo", {})
            .get("scan", [])
        ]

        return TrackingInfo(
            tracking_number=tracking_number,
            carrier=CarrierName.DPD,
            status=data.get("statusInfo", {}).get("status", "unknown"),
            events=events,
            raw_response=data,
        )
