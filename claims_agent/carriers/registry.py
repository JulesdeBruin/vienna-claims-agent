"""Carrier registry — maps carrier names to client instances."""

from ..models import CarrierName
from .base import CarrierClient
from .austrian_post import AustrianPostClient
from .dhl import DHLClient
from .dpd import DPDClient
from .fedex import FedExClient
from .gls import GLSClient
from .ups import UPSClient

_CLIENTS: dict[CarrierName, CarrierClient] = {
    CarrierName.AUSTRIAN_POST: AustrianPostClient(),
    CarrierName.DHL: DHLClient(),
    CarrierName.DPD: DPDClient(),
    CarrierName.FEDEX: FedExClient(),
    CarrierName.GLS: GLSClient(),
    CarrierName.UPS: UPSClient(),
}


def get_carrier_client(carrier: CarrierName) -> CarrierClient:
    client = _CLIENTS.get(carrier)
    if not client:
        raise ValueError(f"No client registered for carrier: {carrier}")
    return client


def get_all_carriers() -> dict[CarrierName, CarrierClient]:
    return dict(_CLIENTS)
