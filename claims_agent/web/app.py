"""FastAPI backend for the Vienna Claims Agent web dashboard.

Provides:
- SSE-streaming agent chat endpoint
- REST endpoints for claims, carriers, shipments
- Pipeline execution with SSE progress streaming
- Static file serving for the frontend
"""

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from ..agent import ClaimsAgent
from ..config import settings
from ..llm_agent import OllamaAgent, AgentEvent
from ..models import ClaimStatus, SessionLocal, Shipment, Claim, init_db
from ..orchestrator import Orchestrator
from ..tools import execute_tool

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Vienna Claims Agent",
    description="AI-powered late delivery claims for Austrian/EU carriers",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Shared agent instance (keeps conversation context)
_agent_instance: OllamaAgent | None = None


def _get_agent() -> OllamaAgent:
    global _agent_instance
    if _agent_instance is None:
        _agent_instance = OllamaAgent()
    return _agent_instance


@app.on_event("startup")
async def startup():
    init_db()
    # Seed if empty
    db = SessionLocal()
    try:
        if db.query(Shipment).count() == 0:
            from ..seed_data import seed
            seed()
            logger.info("Seeded sample data.")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


# ---------------------------------------------------------------------------
# Agent Chat (SSE streaming)
# ---------------------------------------------------------------------------

@app.get("/api/chat")
async def chat(message: str = Query(..., description="User message")):
    """Chat with the AI agent. Streams events via SSE."""
    agent = _get_agent()

    async def event_stream():
        async for event in agent.chat(message):
            yield {
                "event": event.type,
                "data": json.dumps(event.data, default=str),
            }

    return EventSourceResponse(event_stream())


@app.post("/api/chat/reset")
async def reset_chat():
    """Reset the agent's conversation history."""
    agent = _get_agent()
    agent.reset()
    return {"status": "ok", "message": "Conversation reset."}


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    agent = _get_agent()
    ollama_status = await agent.health_check()
    db = SessionLocal()
    try:
        shipment_count = db.query(Shipment).count()
        claim_count = db.query(Claim).count()
    finally:
        db.close()
    return {
        "status": "ok",
        "ollama": ollama_status,
        "database": {
            "shipments": shipment_count,
            "claims": claim_count,
        },
    }


# ---------------------------------------------------------------------------
# Claims REST API
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
    # Serialize dates
    for c in claims:
        if c.get("deadline"):
            c["deadline"] = c["deadline"].isoformat()
    return {"claims": claims}


@app.get("/api/claims/summary")
async def claims_summary():
    agent = ClaimsAgent()
    return agent.get_summary()


# ---------------------------------------------------------------------------
# Shipments REST API
# ---------------------------------------------------------------------------

@app.get("/api/shipments/late")
async def late_shipments():
    agent = ClaimsAgent()
    shipments = agent.scan_late_shipments()
    from ..tools import _serialize_shipment
    return {"shipments": [_serialize_shipment(s) for s in shipments]}


# ---------------------------------------------------------------------------
# Carrier Policies
# ---------------------------------------------------------------------------

@app.get("/api/carriers")
async def carrier_policies():
    result = execute_tool("get_carrier_policies", {})
    return json.loads(result)


# ---------------------------------------------------------------------------
# Pipeline execution (SSE streaming)
# ---------------------------------------------------------------------------

@app.post("/api/pipeline/run")
async def run_pipeline():
    """Run the full claims pipeline with SSE progress streaming."""
    queue: asyncio.Queue = asyncio.Queue()

    def on_event(step: str, message: str):
        queue.put_nowait({"step": step, "message": message})

    async def pipeline_stream():
        agent = ClaimsAgent()
        orchestrator = Orchestrator(agent=agent, on_event=on_event)

        # Run pipeline in a thread to not block
        loop = asyncio.get_event_loop()

        async def run():
            result = await loop.run_in_executor(
                None, lambda: orchestrator.run_pipeline(skip_ingestion=True, auto_draft=True)
            )
            await queue.put({"step": "done", "message": result.summary()})
            await queue.put(None)  # Signal completion

        task = asyncio.create_task(run())

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30.0)
                if event is None:
                    break
                yield {
                    "event": "pipeline",
                    "data": json.dumps(event),
                }
            except asyncio.TimeoutError:
                yield {"event": "keepalive", "data": "{}"}

        await task

    return EventSourceResponse(pipeline_stream())


# ---------------------------------------------------------------------------
# Tracking check
# ---------------------------------------------------------------------------

@app.post("/api/track/{tracking_number}")
async def track_shipment(tracking_number: str, carrier: str = Query(...)):
    """Check live tracking status on carrier website."""
    result = execute_tool("check_tracking", {
        "tracking_number": tracking_number,
        "carrier": carrier,
    })
    return json.loads(result)
