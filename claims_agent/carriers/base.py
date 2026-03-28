"""Base carrier interface and shared types."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum

from ..models import CarrierName, ServiceLevel


class EligibilityResult(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    NEEDS_REVIEW = "needs_review"


@dataclass
class TrackingEvent:
    timestamp: str
    location: str
    description: str
    status_code: str | None = None


@dataclass
class TrackingInfo:
    tracking_number: str
    carrier: CarrierName
    status: str
    estimated_delivery: date | None = None
    actual_delivery: date | None = None
    ship_date: date | None = None
    events: list[TrackingEvent] = field(default_factory=list)
    service_type: str | None = None
    raw_response: dict | None = None


@dataclass
class CarrierClaimPolicy:
    """Carrier-specific claim policy details."""

    carrier: CarrierName
    allows_late_claims: bool
    eligible_services: list[ServiceLevel]
    filing_deadline_days: int  # Days from delivery/shipment to file
    deadline_reference: str  # "shipment_date" or "delivery_date"
    who_can_file: str  # "sender", "recipient", "both"
    filing_methods: list[str]  # ["portal", "email", "phone", "fax"]
    portal_url: str | None = None
    claim_email: str | None = None
    claim_phone: str | None = None
    refund_type: str = "shipping_cost"  # "shipping_cost", "premium_only", "full_value"
    notes: str = ""
    documentation_required: list[str] = field(default_factory=list)


@dataclass
class ClaimEligibility:
    """Result of checking whether a shipment is eligible for a claim."""

    result: EligibilityResult
    reason: str
    policy: CarrierClaimPolicy
    filing_deadline: date | None = None
    estimated_refund: float | None = None
    recommended_action: str = ""


class CarrierClient(ABC):
    """Base class for carrier integrations."""

    @property
    @abstractmethod
    def carrier_name(self) -> CarrierName:
        ...

    @property
    @abstractmethod
    def claim_policy(self) -> CarrierClaimPolicy:
        ...

    @abstractmethod
    async def track_shipment(self, tracking_number: str) -> TrackingInfo:
        """Fetch tracking info from the carrier API."""
        ...

    def check_eligibility(
        self,
        service_level: ServiceLevel,
        ship_date: date,
        guaranteed_delivery: date | None,
        actual_delivery: date | None,
        shipping_cost: float | None = None,
    ) -> ClaimEligibility:
        """Check if a late shipment is eligible for a claim."""
        policy = self.claim_policy

        # Basic checks
        if not policy.allows_late_claims:
            return ClaimEligibility(
                result=EligibilityResult.INELIGIBLE,
                reason=f"{self.carrier_name.value} does not accept late delivery claims.",
                policy=policy,
            )

        if service_level not in policy.eligible_services:
            return ClaimEligibility(
                result=EligibilityResult.INELIGIBLE,
                reason=(
                    f"Service level '{service_level.value}' is not eligible for late delivery "
                    f"claims. Eligible services: "
                    f"{', '.join(s.value for s in policy.eligible_services)}"
                ),
                policy=policy,
            )

        if not guaranteed_delivery or not actual_delivery:
            return ClaimEligibility(
                result=EligibilityResult.NEEDS_REVIEW,
                reason="Missing guaranteed or actual delivery date. Manual review needed.",
                policy=policy,
            )

        if actual_delivery <= guaranteed_delivery:
            return ClaimEligibility(
                result=EligibilityResult.INELIGIBLE,
                reason="Package was delivered on time or early.",
                policy=policy,
            )

        # Check filing deadline
        ref_date = ship_date if policy.deadline_reference == "shipment_date" else actual_delivery
        deadline = ref_date + timedelta(days=policy.filing_deadline_days)
        today = date.today()

        if today > deadline:
            return ClaimEligibility(
                result=EligibilityResult.INELIGIBLE,
                reason=f"Filing deadline passed ({deadline.isoformat()}). "
                f"Carrier requires filing within {policy.filing_deadline_days} days of "
                f"{policy.deadline_reference.replace('_', ' ')}.",
                policy=policy,
                filing_deadline=deadline,
            )

        days_late = (actual_delivery - guaranteed_delivery).days

        return ClaimEligibility(
            result=EligibilityResult.ELIGIBLE,
            reason=f"Package was {days_late} day(s) late. Eligible for claim under "
            f"{self.carrier_name.value} {policy.refund_type} refund policy.",
            policy=policy,
            filing_deadline=deadline,
            estimated_refund=shipping_cost,
            recommended_action=(
                f"File claim via {', '.join(policy.filing_methods)} "
                f"before {deadline.isoformat()}"
            ),
        )
