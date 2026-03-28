"""Tracking Agent — uses BrowserBase to browse carrier tracking pages,
then uses OpenRouter LLM to extract delivery status from the page content.

This is the multi-agent component: the orchestrator LLM calls this tool,
which uses a headless browser (BrowserBase) to load real tracking pages,
then another LLM call to parse the unstructured page content.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import date

import httpx

from .config import settings
from .models import CarrierName, Shipment, ShipmentStatus, SessionLocal

logger = logging.getLogger(__name__)


@dataclass
class TrackingResult:
    tracking_number: str
    carrier: str
    status: str  # "in_transit", "delivered", "delivered_late", "unknown"
    delivery_date: str | None = None
    location: str | None = None
    signed_by: str | None = None
    source_url: str = ""
    raw_page_snippet: str = ""


# Per-carrier tracking URLs
TRACKING_URLS: dict[CarrierName, str] = {
    CarrierName.DHL: "https://www.dhl.com/at-en/home/tracking/tracking-parcel.html?submit=1&tracking-id={tracking}",
    CarrierName.UPS: "https://www.ups.com/track?tracknum={tracking}&loc=en_AT",
    CarrierName.FEDEX: "https://www.fedex.com/fedextrack/?trknbr={tracking}",
    CarrierName.GLS: "https://gls-group.com/AT/en/parcel-tracking?match={tracking}",
    CarrierName.DPD: "https://tracking.dpd.de/status/en_DE/parcel/{tracking}",
    CarrierName.AUSTRIAN_POST: "https://www.post.at/sv/sendungssuche?snr={tracking}",
}


class TrackingAgent:
    """Uses BrowserBase headless browser + LLM to check carrier tracking pages."""

    def __init__(self):
        self.api_key = settings.openrouter_api_key
        self.base_url = settings.openrouter_base_url
        self.model = settings.llm_model
        self.browserbase_key = settings.browserbase_api_key

    async def _fetch_tracking_page(
        self, tracking_number: str, carrier: CarrierName
    ) -> tuple[str, str]:
        """Use BrowserBase to load the carrier tracking page and extract text.

        Falls back to httpx if BrowserBase is not configured.
        """
        url_template = TRACKING_URLS.get(carrier)
        if not url_template:
            return f"No tracking URL configured for {carrier.value}", ""

        url = url_template.format(tracking=tracking_number)

        # Try BrowserBase first
        if self.browserbase_key:
            try:
                return await self._fetch_with_browserbase(url), url
            except Exception as e:
                logger.warning("BrowserBase failed, falling back to httpx: %s", e)

        # Fallback: direct httpx (won't work for JS-heavy pages)
        return await self._fetch_with_httpx(url), url

    async def _fetch_with_browserbase(self, url: str) -> str:
        """Load a page using BrowserBase headless browser."""
        from browserbase import Browserbase
        from playwright.async_api import async_playwright

        bb = Browserbase(api_key=self.browserbase_key)
        session = bb.sessions.create(
            project_id=settings.browserbase_project_id,
        )

        logger.info("BrowserBase session: %s — loading %s", session.id, url)

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(session.connect_url)
            context = browser.contexts[0]
            page = context.pages[0]

            await page.goto(url, wait_until="networkidle", timeout=30000)
            # Wait a bit for JS tracking widgets to load
            await page.wait_for_timeout(3000)

            # Extract text content
            text = await page.inner_text("body")

            await page.close()
            await browser.close()

        logger.info(
            "BrowserBase session complete. Replay: https://browserbase.com/sessions/%s",
            session.id,
        )

        # Clean up excessive whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        return text[:3000]

    async def _fetch_with_httpx(self, url: str) -> str:
        """Fallback: fetch with httpx (no JS rendering)."""
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        ) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                # Strip HTML tags
                text = re.sub(r"<script[^>]*>.*?</script>", "", response.text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text)
                return text[:3000]
            except Exception as e:
                return f"Error fetching {url}: {e}"

    async def _extract_with_llm(
        self, page_text: str, tracking_number: str, carrier: str
    ) -> dict:
        """Use OpenRouter LLM to extract structured tracking data from page text."""
        prompt = f"""Extract the delivery status from this {carrier} tracking page for shipment {tracking_number}.

Return ONLY a JSON object with these fields:
- "status": one of "in_transit", "delivered", "exception", "unknown"
- "delivery_date": date in YYYY-MM-DD format if delivered, null otherwise
- "location": last known location or delivery location
- "signed_by": name of person who signed, null if not available

Page content:
{page_text[:2000]}

Return ONLY valid JSON, nothing else."""

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0,
                    },
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                # Sometimes model wraps it in markdown code blocks
                if "```" in content:
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                return json.loads(content.strip())
        except (httpx.RequestError, json.JSONDecodeError, KeyError, IndexError) as e:
            logger.warning("LLM extraction failed: %s", e)
            return self._fallback_extract(page_text)

    def _fallback_extract(self, text: str) -> dict:
        """Fallback extraction using regex when LLM is unavailable."""
        text_lower = text.lower()
        status = "unknown"
        delivery_date = None

        if any(w in text_lower for w in ["delivered", "zugestellt", "delivery completed"]):
            status = "delivered"
        elif any(w in text_lower for w in ["in transit", "unterwegs", "on the way", "shipped"]):
            status = "in_transit"
        elif any(w in text_lower for w in ["exception", "failed", "returned"]):
            status = "exception"

        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if not date_match:
            date_match = re.search(r"(\d{1,2}[./]\d{1,2}[./]\d{2,4})", text)
        if date_match:
            try:
                from dateutil.parser import parse as parse_date
                delivery_date = parse_date(date_match.group(1), dayfirst=True).date().isoformat()
            except (ValueError, TypeError):
                pass

        return {"status": status, "delivery_date": delivery_date, "location": None, "signed_by": None}

    def _update_shipment(self, tracking_number: str, extracted: dict) -> None:
        """Update the Shipment record in the database."""
        db = SessionLocal()
        try:
            shipment = (
                db.query(Shipment)
                .filter(Shipment.tracking_number == tracking_number)
                .first()
            )
            if not shipment:
                return

            if extracted.get("delivery_date"):
                try:
                    delivery = date.fromisoformat(extracted["delivery_date"])
                    shipment.actual_delivery_date = delivery
                    if shipment.guaranteed_delivery_date and delivery > shipment.guaranteed_delivery_date:
                        shipment.status = ShipmentStatus.DELIVERED_LATE
                    else:
                        shipment.status = ShipmentStatus.DELIVERED
                except ValueError:
                    pass
            elif extracted.get("status") == "in_transit":
                shipment.status = ShipmentStatus.IN_TRANSIT

            db.commit()
        finally:
            db.close()

    async def check_status(
        self, tracking_number: str, carrier: CarrierName
    ) -> TrackingResult:
        """Full tracking check: BrowserBase fetch → LLM extraction → DB update."""
        logger.info("Checking tracking for %s on %s", tracking_number, carrier.value)

        # Step 1: Fetch tracking page via BrowserBase
        page_text, url = await self._fetch_tracking_page(tracking_number, carrier)

        # Step 2: Extract structured data with LLM
        extracted = await self._extract_with_llm(page_text, tracking_number, carrier.value)

        # Step 3: Update database
        self._update_shipment(tracking_number, extracted)

        # Step 4: Determine final status
        status = extracted.get("status", "unknown")
        if status == "delivered":
            db = SessionLocal()
            try:
                s = db.query(Shipment).filter(Shipment.tracking_number == tracking_number).first()
                if s and s.status == ShipmentStatus.DELIVERED_LATE:
                    status = "delivered_late"
            finally:
                db.close()

        return TrackingResult(
            tracking_number=tracking_number,
            carrier=carrier.value,
            status=status,
            delivery_date=extracted.get("delivery_date"),
            location=extracted.get("location"),
            signed_by=extracted.get("signed_by"),
            source_url=url,
            raw_page_snippet=page_text[:200] if page_text else "",
        )
