"""GLS carrier integration (Austria)."""

from ..models import CarrierName, ServiceLevel
from .base import CarrierClient, CarrierClaimPolicy, TrackingInfo, TrackingEvent


class GLSClient(CarrierClient):
    TRACK_URL = "https://gls-group.com/app/service/open/rest/AT/en/rstt001"

    @property
    def carrier_name(self) -> CarrierName:
        return CarrierName.GLS

    @property
    def claim_policy(self) -> CarrierClaimPolicy:
        return CarrierClaimPolicy(
            carrier=CarrierName.GLS,
            allows_late_claims=True,
            eligible_services=[
                ServiceLevel.EXPRESS_9,
                ServiceLevel.EXPRESS_10,
                ServiceLevel.EXPRESS_12,
                ServiceLevel.NEXT_DAY,
            ],
            filing_deadline_days=90,
            deadline_reference="shipment_date",
            who_can_file="sender",
            filing_methods=["email", "phone"],
            portal_url="https://gls-group.com/AT/en/",
            claim_phone="+43 5 0852 3030",
            refund_type="shipping_cost",
            notes=(
                "GLS Express guarantees next working day delivery nationwide in Austria. "
                "Claims limitation period is 1 year, but should file within 90 days. "
                "Express time-definite services (09h/12h/17h) eligible for refund if SLA missed."
            ),
            documentation_required=[
                "GLS parcel number",
                "Proof of express service booking",
                "Delivery SLA documentation",
            ],
        )

    async def track_shipment(self, tracking_number: str) -> TrackingInfo:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                self.TRACK_URL,
                params={"match": tracking_number, "type": "MYGLS", "caller": "witt002"},
            )
            resp.raise_for_status()
            data = resp.json()

        tudata = data.get("tuStatus", [{}])
        if not tudata:
            raise ValueError(f"No GLS shipment found for {tracking_number}")

        history = tudata[0].get("history", [])
        events = [
            TrackingEvent(
                timestamp=e.get("date", "") + " " + e.get("time", ""),
                location=e.get("address", {}).get("city", ""),
                description=e.get("evtDscr", ""),
            )
            for e in history
        ]

        return TrackingInfo(
            tracking_number=tracking_number,
            carrier=CarrierName.GLS,
            status=tudata[0].get("progressBar", {}).get("statusInfo", "unknown"),
            events=events,
            raw_response=data,
        )
