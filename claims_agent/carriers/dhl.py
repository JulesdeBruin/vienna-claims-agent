"""DHL carrier integration (Austria/EU)."""

from ..models import CarrierName, ServiceLevel
from .base import CarrierClient, CarrierClaimPolicy, TrackingInfo, TrackingEvent


class DHLClient(CarrierClient):
    BASE_URL = "https://api-eu.dhl.com/track/shipments"

    @property
    def carrier_name(self) -> CarrierName:
        return CarrierName.DHL

    @property
    def claim_policy(self) -> CarrierClaimPolicy:
        return CarrierClaimPolicy(
            carrier=CarrierName.DHL,
            allows_late_claims=True,
            eligible_services=[ServiceLevel.EXPRESS_9, ServiceLevel.EXPRESS_12, ServiceLevel.EXPRESS],
            filing_deadline_days=14,
            deadline_reference="shipment_date",
            who_can_file="sender",
            filing_methods=["portal", "phone", "email"],
            portal_url="https://mydhl.express.dhl/at/en/home.html",
            claim_email=None,
            claim_phone="+43 820 820 820",
            refund_type="premium_only",
            notes=(
                "Money-back guarantee covers DHL EXPRESS 9:00 and 12:00 services only. "
                "Refund is for the premium paid, not full shipping cost. "
                "Must notify DHL within 14 calendar days of shipment date. "
                "DHL responds within 30 calendar days."
            ),
            documentation_required=[
                "Account number",
                "Waybill number",
                "Shipment date",
                "Receiver information",
                "Proof of guaranteed delivery time",
            ],
        )

    async def track_shipment(self, tracking_number: str) -> TrackingInfo:
        if not settings.dhl_api_key:
            raise ValueError("DHL_API_KEY not configured")

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.BASE_URL,
                params={"trackingNumber": tracking_number},
                headers={"DHL-API-Key": settings.dhl_api_key},
            )
            resp.raise_for_status()
            data = resp.json()

        shipments = data.get("shipments", [])
        if not shipments:
            raise ValueError(f"No DHL shipment found for {tracking_number}")

        s = shipments[0]
        events = [
            TrackingEvent(
                timestamp=e.get("timestamp", ""),
                location=e.get("location", {}).get("address", {}).get("addressLocality", ""),
                description=e.get("description", ""),
                status_code=e.get("statusCode"),
            )
            for e in s.get("events", [])
        ]

        return TrackingInfo(
            tracking_number=tracking_number,
            carrier=CarrierName.DHL,
            status=s.get("status", {}).get("statusCode", "unknown"),
            estimated_delivery=s.get("estimatedTimeOfDelivery"),
            events=events,
            service_type=s.get("service"),
            raw_response=data,
        )
