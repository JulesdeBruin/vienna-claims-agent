"""FastAPI backend for the Vienna Claims Agent — workflow-driven dashboard.

Workflow: Add Shipments → Track via BrowserBase → Monitor Daily → Auto-Draft Claims
"""

import asyncio
import json
import logging
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..agent import ClaimsAgent
from ..config import settings
from ..models import (
    CarrierName,
    Claim,
    ClaimStatus,
    ServiceLevel,
    Shipment,
    ShipmentStatus,
    SessionLocal,
    init_db,
)
from ..orchestrator import Orchestrator
from ..tools import execute_tool, _serialize_shipment

logger = logging.getLogger(__name__)

app = FastAPI(title="Vienna Claims Agent", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.on_event("startup")
async def startup():
    init_db()
    db = SessionLocal()
    try:
        if db.query(Shipment).count() == 0:
            from ..seed_data import seed
            seed()
    finally:
        db.close()


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ---------------------------------------------------------------------------
# Shipments API
# ---------------------------------------------------------------------------

class AddShipmentRequest(BaseModel):
    tracking_number: str
    carrier: str
    service_level: str = "standard"
    ship_date: str | None = None
    guaranteed_delivery_date: str | None = None
    recipient_name: str = ""
    declared_value: float | None = None


@app.get("/api/shipments")
async def list_shipments(status: str | None = None):
    """List all shipments with optional status filter."""
    db = SessionLocal()
    try:
        query = db.query(Shipment).order_by(Shipment.created_at.desc())
        if status:
            try:
                query = query.filter(Shipment.status == ShipmentStatus(status))
            except ValueError:
                pass
        shipments = query.all()
        result = []
        for s in shipments:
            result.append({
                "id": s.id,
                "tracking_number": s.tracking_number,
                "carrier": s.carrier.value,
                "service_level": s.service_level.value,
                "status": s.status.value,
                "ship_date": s.ship_date.isoformat() if s.ship_date else None,
                "guaranteed_delivery": s.guaranteed_delivery_date.isoformat() if s.guaranteed_delivery_date else None,
                "actual_delivery": s.actual_delivery_date.isoformat() if s.actual_delivery_date else None,
                "days_late": s.days_late,
                "declared_value": s.declared_value,
                "recipient_name": s.recipient_name,
                "recipient_city": s.recipient_city,
                "source": getattr(s, "source", "manual") or "manual",
                "monitoring": s.status.value in ("in_transit", "delivered"),
            })
        return {"shipments": result, "total": len(result)}
    finally:
        db.close()


@app.post("/api/shipments")
async def add_shipment(req: AddShipmentRequest):
    """Manually add a shipment by tracking number."""
    db = SessionLocal()
    try:
        # Check duplicate
        existing = db.query(Shipment).filter(
            Shipment.tracking_number == req.tracking_number
        ).first()
        if existing:
            return {"error": "Shipment already exists", "shipment_id": existing.id}

        # Parse carrier
        try:
            carrier = CarrierName(req.carrier.lower())
        except ValueError:
            return {"error": f"Unknown carrier: {req.carrier}"}

        # Parse service level
        try:
            service = ServiceLevel(req.service_level.lower())
        except ValueError:
            service = ServiceLevel.STANDARD

        ship_date = date.fromisoformat(req.ship_date) if req.ship_date else date.today()
        guaranteed = date.fromisoformat(req.guaranteed_delivery_date) if req.guaranteed_delivery_date else None

        shipment = Shipment(
            tracking_number=req.tracking_number,
            carrier=carrier,
            service_level=service,
            status=ShipmentStatus.IN_TRANSIT,
            ship_date=ship_date,
            guaranteed_delivery_date=guaranteed,
            recipient_name=req.recipient_name,
            declared_value=req.declared_value,
            source="manual",
        )
        db.add(shipment)
        db.commit()
        db.refresh(shipment)
        return {"success": True, "shipment_id": shipment.id, "tracking_number": shipment.tracking_number}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Email Ingestion API
# ---------------------------------------------------------------------------

class ImapConnectRequest(BaseModel):
    host: str
    port: int = 993
    user: str
    password: str
    use_ssl: bool = True


@app.post("/api/ingest/email/connect")
async def connect_and_scan_email(req: ImapConnectRequest):
    """Connect to IMAP mailbox and scan for carrier emails."""
    from ..email_ingestion import EmailIngestor
    import imaplib

    try:
        ingestor = EmailIngestor()
        # Override settings with provided credentials
        if req.use_ssl:
            conn = imaplib.IMAP4_SSL(req.host, req.port)
        else:
            conn = imaplib.IMAP4(req.host, req.port)
        conn.login(req.user, req.password)

        # Store credentials in settings for future scans
        settings.imap_host = req.host
        settings.imap_port = req.port
        settings.imap_user = req.user
        settings.imap_password = req.password
        settings.imap_use_ssl = req.use_ssl

        # Count emails
        conn.select("INBOX")
        _, msg_ids = conn.search(None, "ALL")
        total_emails = len(msg_ids[0].split()) if msg_ids[0] else 0

        # Now do the actual ingestion
        conn.close()
        conn.logout()

        ingestor.connect()
        result = ingestor.ingest()
        ingestor.disconnect()

        return {
            "success": True,
            "connected": True,
            "total_emails_in_inbox": total_emails,
            "emails_scanned": result.emails_scanned,
            "shipments_created": result.shipments_created,
            "shipments_updated": result.shipments_updated,
            "skipped_duplicate": result.skipped_duplicate,
            "skipped_unparseable": result.skipped_unparseable,
            "errors": result.errors,
        }
    except imaplib.IMAP4.error as e:
        return {"success": False, "error": f"IMAP login failed: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/ingest/email/scan")
async def scan_email():
    """Scan the connected mailbox for new emails."""
    from ..email_ingestion import EmailIngestor

    if not settings.imap_host:
        return {"error": "No mailbox connected. Connect first."}

    try:
        ingestor = EmailIngestor()
        ingestor.connect()
        result = ingestor.ingest()
        ingestor.disconnect()
        return {
            "success": True,
            "emails_scanned": result.emails_scanned,
            "shipments_created": result.shipments_created,
            "shipments_updated": result.shipments_updated,
            "skipped_duplicate": result.skipped_duplicate,
            "errors": result.errors,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/ingest/eml")
async def scan_eml_files():
    """Scan test_emails directory for .eml files."""
    from ..email_ingestion import EmailIngestor

    ingestor = EmailIngestor()
    result = ingestor.ingest_from_eml_files("test_emails")
    return {
        "success": True,
        "emails_scanned": result.emails_scanned,
        "shipments_created": result.shipments_created,
        "shipments_updated": result.shipments_updated,
        "skipped_duplicate": result.skipped_duplicate,
        "errors": result.errors,
    }


@app.post("/api/ingest/csv")
async def upload_csv(file: UploadFile = File(...)):
    """Upload and import a CSV file."""
    from ..importer import CSVImporter
    import tempfile

    # Save uploaded file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="wb") as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    importer = CSVImporter()
    result = importer.import_from_csv(tmp_path)

    # Clean up
    Path(tmp_path).unlink(missing_ok=True)

    return {
        "success": True,
        "filename": file.filename,
        "imported": result.imported,
        "skipped_duplicate": result.skipped_duplicate,
        "skipped_error": result.skipped_error,
        "errors": result.errors[:10],
    }


# ---------------------------------------------------------------------------
# Tracking API (BrowserBase)
# ---------------------------------------------------------------------------

@app.post("/api/track/{shipment_id}")
async def track_shipment(shipment_id: int):
    """Check tracking status for a shipment via BrowserBase."""
    db = SessionLocal()
    try:
        shipment = db.query(Shipment).get(shipment_id)
        if not shipment:
            return {"error": "Shipment not found"}

        carrier = shipment.carrier
        tracking = shipment.tracking_number
    finally:
        db.close()

    # Execute tracking
    result_json = execute_tool("check_tracking", {
        "tracking_number": tracking,
        "carrier": carrier.value,
    })
    result = json.loads(result_json)

    # Reload shipment to get updated data
    db = SessionLocal()
    try:
        shipment = db.query(Shipment).get(shipment_id)
        return {
            "tracking_result": result,
            "shipment": {
                "id": shipment.id,
                "status": shipment.status.value,
                "actual_delivery": shipment.actual_delivery_date.isoformat() if shipment.actual_delivery_date else None,
                "days_late": shipment.days_late,
            },
        }
    finally:
        db.close()


@app.post("/api/track-all")
async def track_all_shipments():
    """Check tracking for all in-transit and delivered shipments. Returns SSE stream."""
    db = SessionLocal()
    try:
        shipments = (
            db.query(Shipment)
            .filter(Shipment.status.in_([ShipmentStatus.IN_TRANSIT]))
            .all()
        )
        to_track = [(s.id, s.tracking_number, s.carrier.value) for s in shipments]
    finally:
        db.close()

    async def stream():
        for sid, tracking, carrier in to_track:
            yield {
                "event": "tracking",
                "data": json.dumps({"shipment_id": sid, "tracking": tracking, "carrier": carrier, "status": "checking"}),
            }
            try:
                result_json = execute_tool("check_tracking", {
                    "tracking_number": tracking,
                    "carrier": carrier,
                })
                result = json.loads(result_json)
                yield {
                    "event": "tracking",
                    "data": json.dumps({"shipment_id": sid, "tracking": tracking, "carrier": carrier, "status": "done", "result": result}),
                }
            except Exception as e:
                yield {
                    "event": "tracking",
                    "data": json.dumps({"shipment_id": sid, "tracking": tracking, "status": "error", "error": str(e)}),
                }

        # After tracking, auto-flag late deliveries
        agent = ClaimsAgent()
        orchestrator = Orchestrator(agent=agent)
        newly_late, total_late = orchestrator.step_flag_late()
        yield {
            "event": "flagged",
            "data": json.dumps({"newly_late": newly_late, "total_late": total_late}),
        }

        # Auto-draft claims for eligible
        eligibility = orchestrator.step_check_eligibility()
        eligible = [r for r in eligibility if r["eligible"]]
        if eligible:
            claims = orchestrator.step_draft_claims(eligibility)
            yield {
                "event": "claims_drafted",
                "data": json.dumps({
                    "count": len(claims),
                    "total_eur": sum(c.claim_amount or 0 for c in claims),
                }),
            }

        yield {"event": "done", "data": json.dumps({"message": "Tracking complete"})}

    return EventSourceResponse(stream())


# ---------------------------------------------------------------------------
# Claims API
# ---------------------------------------------------------------------------

@app.get("/api/claims")
async def list_claims(status: str | None = None):
    agent = ClaimsAgent()
    claim_status = None
    if status:
        try:
            claim_status = ClaimStatus(status)
        except ValueError:
            pass
    claims = agent.get_all_claims(status=claim_status)
    for c in claims:
        if c.get("deadline"):
            c["deadline"] = c["deadline"].isoformat()
    return {"claims": claims}


@app.get("/api/claims/summary")
async def claims_summary():
    agent = ClaimsAgent()
    return agent.get_summary()


@app.post("/api/claims/{claim_id}/approve")
async def approve_claim(claim_id: int):
    agent = ClaimsAgent()
    try:
        claim = agent.approve_claim(claim_id, "dashboard")
        return {"success": True, "claim_id": claim.id, "status": claim.status.value}
    except ValueError as e:
        return {"error": str(e)}


@app.post("/api/claims/{claim_id}/email")
async def generate_email(claim_id: int):
    agent = ClaimsAgent()
    try:
        email = agent.generate_claim_email(claim_id)
        return {"claim_id": claim_id, "email": email}
    except ValueError as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Pipeline API
# ---------------------------------------------------------------------------

@app.post("/api/pipeline/run")
async def run_pipeline():
    """Run the full pipeline with SSE progress."""
    queue: asyncio.Queue = asyncio.Queue()

    def on_event(step: str, message: str):
        queue.put_nowait({"step": step, "message": message})

    async def stream():
        agent = ClaimsAgent()
        orchestrator = Orchestrator(agent=agent, on_event=on_event)
        loop = asyncio.get_event_loop()

        async def run():
            result = await loop.run_in_executor(
                None, lambda: orchestrator.run_pipeline(skip_ingestion=True, auto_draft=True)
            )
            await queue.put({"step": "done", "message": result.summary()})
            await queue.put(None)

        task = asyncio.create_task(run())
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                if event is None:
                    break
                yield {"event": "pipeline", "data": json.dumps(event)}
            except asyncio.TimeoutError:
                yield {"event": "keepalive", "data": "{}"}
        await task

    return EventSourceResponse(stream())


# ---------------------------------------------------------------------------
# Carriers API
# ---------------------------------------------------------------------------

@app.get("/api/carriers")
async def carrier_policies():
    result = execute_tool("get_carrier_policies", {})
    return json.loads(result)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    from ..llm_agent import OllamaAgent
    agent = OllamaAgent()
    ollama_status = await agent.health_check()
    db = SessionLocal()
    try:
        shipment_count = db.query(Shipment).count()
        claim_count = db.query(Claim).count()
        in_transit = db.query(Shipment).filter(Shipment.status == ShipmentStatus.IN_TRANSIT).count()
        late = db.query(Shipment).filter(Shipment.status == ShipmentStatus.DELIVERED_LATE).count()
    finally:
        db.close()
    return {
        "status": "ok",
        "ollama": ollama_status,
        "database": {
            "shipments": shipment_count,
            "claims": claim_count,
            "in_transit": in_transit,
            "late": late,
        },
    }
