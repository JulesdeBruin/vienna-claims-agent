"""UPS carrier integration (Austria/EU)."""

from ..models import CarrierName, ServiceLevel
from .base import CarrierClient, CarrierClaimPolicy, TrackingInfo, TrackingEvent


class UPSClient(CarrierClient):
    AUTH_URL = "https://onlinetools.ups.com/security/v1/oauth/token"
    TRACK_URL = "https://onlinetools.ups.com/api/track/v1/details"

    @property
    def carrier_name(self) -> CarrierName:
        return CarrierName.UPS

    @property
    def claim_policy(self) -> CarrierClaimPolicy:
        return CarrierClaimPolicy(
            carrier=CarrierName.UPS,
            allows_late_claims=True,
            eligible_services=[
                ServiceLevel.EXPRESS,
                ServiceLevel.EXPRESS_9,
                ServiceLevel.EXPRESS_10,
                ServiceLevel.EXPRESS_12,
                ServiceLevel.NEXT_DAY,
                ServiceLevel.STANDARD,  # UPS GSR covers most services
            ],
            filing_deadline_days=15,
            deadline_reference="delivery_date",
            who_can_file="sender",
            filing_methods=["portal"],
            portal_url="https://www.ups.com/at/en/support/file-a-claim",
            claim_phone="+43 1 50 15 96 002",
            refund_type="shipping_cost",
            notes=(
                "UPS Guaranteed Service Refund (GSR) covers most small parcel services. "
                "Full refund of shipping charges even if late by 60 seconds. "
                "UPS does NOT voluntarily issue refunds — you MUST request them. "
                "File within 15 calendar days of scheduled delivery date. "
                "Excludes: severe weather, incorrect addresses, large package surcharge."
            ),
            documentation_required=[
                "Tracking number",
                "Scheduled delivery date",
                "Package value",
                "Proof of shipment (invoice or receipt)",
            ],
        )

    async def _get_token(self) -> str:
        if not settings.ups_client_id or not settings.ups_client_secret:
            raise ValueError("UPS_CLIENT_ID and UPS_CLIENT_SECRET not configured")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                self.AUTH_URL,
                data={"grant_type": "client_credentials"},
                auth=(settings.ups_client_id, settings.ups_client_secret),
            )
            resp.raise_for_status()
            return resp.json()["access_token"]

    async def track_shipment(self, tracking_number: str) -> TrackingInfo:
        token = await self._get_token()

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.TRACK_URL}/{tracking_number}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "transId": "claims-agent",
                    "transactionSrc": "vienna-claims-agent",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        package = data.get("trackResponse", {}).get("shipment", [{}])[0].get("package", [{}])[0]
        activities = package.get("activity", [])

        events = [
            TrackingEvent(
                timestamp=a.get("date", "") + " " + a.get("time", ""),
                location=a.get("location", {}).get("address", {}).get("city", ""),
                description=a.get("status", {}).get("description", ""),
                status_code=a.get("status", {}).get("code"),
            )
            for a in activities
        ]

        return TrackingInfo(
            tracking_number=tracking_number,
            carrier=CarrierName.UPS,
            status=package.get("currentStatus", {}).get("code", "unknown"),
            events=events,
            raw_response=data,
        )
