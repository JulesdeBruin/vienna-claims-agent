"""Austrian Post (Österreichische Post) carrier integration."""

from ..models import CarrierName, ServiceLevel
from .base import CarrierClient, CarrierClaimPolicy, TrackingInfo


class AustrianPostClient(CarrierClient):
    TRACK_URL = "https://www.post.at/sendungsverfolgung"

    @property
    def carrier_name(self) -> CarrierName:
        return CarrierName.AUSTRIAN_POST

    @property
    def claim_policy(self) -> CarrierClaimPolicy:
        return CarrierClaimPolicy(
            carrier=CarrierName.AUSTRIAN_POST,
            allows_late_claims=True,
            eligible_services=[ServiceLevel.EXPRESS],  # Post Express only
            filing_deadline_days=30,  # Not formally specified; using conservative estimate
            deadline_reference="delivery_date",
            who_can_file="sender",
            filing_methods=["portal", "phone"],
            portal_url="https://www.rtr.at/TKP/was_wir_tun/post/postal_services.en.html",
            claim_phone="+43 1 58058 888",
            refund_type="shipping_cost",
            notes=(
                "Only Post Express is eligible for late delivery refunds. "
                "Standard parcels: NO late delivery compensation. "
                "The legal contract is between sender and Austrian Post, not the recipient. "
                "For disputes, use RTR (Austrian Regulatory Authority) conciliation service. "
                "RTR contact: +43 1 58058 888 (Mon-Fri 8am-5pm)."
            ),
            documentation_required=[
                "Tracking number",
                "Proof of Post Express booking",
                "Shipment receipt",
            ],
        )

    async def track_shipment(self, tracking_number: str) -> TrackingInfo:
        """Austrian Post tracking. Note: no official public API, uses web scraping approach."""
        # Austrian Post does not offer a public REST API for tracking.
        # In production, integrate via their business portal or use a third-party
        # tracking aggregator like aftership.com or 17track.net.
        raise NotImplementedError(
            "Austrian Post does not provide a public tracking API. "
            "Use the business portal at post.at or integrate via a tracking aggregator."
        )
