"""FedEx carrier integration (Austria/EU)."""

from ..models import CarrierName, ServiceLevel
from .base import CarrierClient, CarrierClaimPolicy, TrackingInfo, TrackingEvent


class FedExClient(CarrierClient):
    AUTH_URL = "https://apis.fedex.com/oauth/token"
    TRACK_URL = "https://apis.fedex.com/track/v1/trackingnumbers"

    @property
    def carrier_name(self) -> CarrierName:
        return CarrierName.FEDEX

    @property
    def claim_policy(self) -> CarrierClaimPolicy:
        return CarrierClaimPolicy(
            carrier=CarrierName.FEDEX,
            allows_late_claims=True,
            eligible_services=[
                ServiceLevel.EXPRESS,
                ServiceLevel.EXPRESS_9,
                ServiceLevel.EXPRESS_12,
            ],
            filing_deadline_days=21,
            deadline_reference="delivery_date",
            who_can_file="sender",
            filing_methods=["email", "fax", "portal"],
            portal_url="https://www.fedex.com/en-at/customer-support/claims.html",
            claim_email="mx-claim@fedex.com",
            claim_phone="+43 800 123 800",
            refund_type="shipping_cost",
            notes=(
                "FedEx Money-Back Guarantee covers time-definite Express services. "
                "IMPORTANT: MBG is SUSPENDED for intercontinental Europe shipments "
                "as of December 2025. Intra-European express may still be eligible. "
                "File within 15 days of invoice or shipment date. "
                "Keep merchandise and packaging until claim is resolved."
            ),
            documentation_required=[
                "FedEx air waybill copy",
                "Ship Manager printout or Ground Pick-Up Record",
                "Delivery receipt",
                "Proof of value documentation",
            ],
        )

    async def _get_token(self) -> str:
        if not settings.fedex_api_key or not settings.fedex_secret_key:
            raise ValueError("FEDEX_API_KEY and FEDEX_SECRET_KEY not configured")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.AUTH_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": settings.fedex_api_key,
                    "client_secret": settings.fedex_secret_key,
                },
            )
            resp.raise_for_status()
            return resp.json()["access_token"]

    async def track_shipment(self, tracking_number: str) -> TrackingInfo:
        token = await self._get_token()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.TRACK_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": tracking_number}}],
                    "includeDetailedScans": True,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = (
            data.get("output", {}).get("completeTrackResults", [{}])[0].get("trackResults", [{}])
        )
        if not results:
            raise ValueError(f"No FedEx shipment found for {tracking_number}")

        result = results[0]
        scan_events = result.get("scanEvents", [])
        events = [
            TrackingEvent(
                timestamp=e.get("date", ""),
                location=e.get("scanLocation", {}).get("city", ""),
                description=e.get("eventDescription", ""),
                status_code=e.get("eventType"),
            )
            for e in scan_events
        ]

        return TrackingInfo(
            tracking_number=tracking_number,
            carrier=CarrierName.FEDEX,
            status=result.get("latestStatusDetail", {}).get("code", "unknown"),
            events=events,
            service_type=result.get("serviceDetail", {}).get("type"),
            raw_response=data,
        )
