"""Tool definitions for the Ollama agent — wraps ClaimsAgent methods as callable tools.

Each tool is defined in Ollama's function-calling schema and has an executor
that calls the underlying ClaimsAgent/Orchestrator methods.
"""

import json
import logging
from datetime import date

from .agent import ClaimsAgent
from .carriers.registry import get_all_carriers
from .models import CarrierName, ClaimStatus, SessionLocal, Shipment

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _serialize_shipment(s: Shipment) -> dict:
    return {
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
        "currency": s.currency or "EUR",
        "recipient_name": s.recipient_name,
        "recipient_city": s.recipient_city,
        "reference_number": s.reference_number,
    }


def _serialize_claim(c: dict) -> dict:
    """Serialize a claim dict (from get_all_claims)."""
    result = dict(c)
    if isinstance(result.get("deadline"), date):
        result["deadline"] = result["deadline"].isoformat()
    return result


def _parse_carrier(raw: str | None) -> CarrierName | None:
    if not raw:
        return None
    try:
        return CarrierName(raw.lower().strip())
    except ValueError:
        return None


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Tool definitions (Ollama function-calling schema)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "scan_late_shipments",
            "description": "Find all late shipments that haven't been claimed yet. Returns tracking numbers, carriers, days late, and amounts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "carrier": {
                        "type": "string",
                        "description": "Filter by carrier name",
                        "enum": ["dhl", "ups", "fedex", "dpd", "gls", "austrian_post"],
                    },
                    "from_date": {
                        "type": "string",
                        "description": "Start date filter (YYYY-MM-DD)",
                    },
                    "to_date": {
                        "type": "string",
                        "description": "End date filter (YYYY-MM-DD)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_eligibility",
            "description": "Check which late shipments are eligible for refund claims based on each carrier's policy. Returns eligible/ineligible status with reasons and filing deadlines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "carrier": {
                        "type": "string",
                        "description": "Only check shipments from this carrier",
                        "enum": ["dhl", "ups", "fedex", "dpd", "gls", "austrian_post"],
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "draft_claims",
            "description": "Automatically draft refund claims for all eligible late shipments. First checks eligibility, then creates claim drafts with amounts and deadlines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "carrier": {
                        "type": "string",
                        "description": "Only draft claims for this carrier",
                        "enum": ["dhl", "ups", "fedex", "dpd", "gls", "austrian_post"],
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_all_claims",
            "description": "List all claims. Optionally filter by status: draft, approved, submitted, accepted, denied, refunded.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by claim status",
                        "enum": ["draft", "approved", "submitted", "accepted", "denied", "refunded"],
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_summary",
            "description": "Get a summary dashboard of all claims: totals, amounts, breakdowns by status and carrier.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "approve_claim",
            "description": "Approve a draft claim for submission. Requires the claim ID number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "claim_id": {
                        "type": "integer",
                        "description": "The claim ID to approve",
                    },
                    "reviewer": {
                        "type": "string",
                        "description": "Name of the person approving (default: operator)",
                    },
                },
                "required": ["claim_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_claim_email",
            "description": "Generate a submission-ready claim email for a specific claim. The claim must be approved first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "claim_id": {
                        "type": "integer",
                        "description": "The claim ID to generate an email for",
                    },
                },
                "required": ["claim_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_carrier_policies",
            "description": "View carrier claim policies: which services are eligible, filing deadlines, refund types, and how to submit claims.",
            "parameters": {
                "type": "object",
                "properties": {
                    "carrier": {
                        "type": "string",
                        "description": "View policy for a specific carrier only",
                        "enum": ["dhl", "ups", "fedex", "dpd", "gls", "austrian_post"],
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_pipeline",
            "description": "Run the full claims pipeline: ingest emails, flag late deliveries, check eligibility, and draft claims automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skip_ingestion": {
                        "type": "boolean",
                        "description": "Skip email ingestion step (default: true)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_tracking",
            "description": "Check the live delivery status of a shipment by scraping the carrier's tracking website. Updates the shipment record with the latest status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tracking_number": {
                        "type": "string",
                        "description": "The tracking number to check",
                    },
                    "carrier": {
                        "type": "string",
                        "description": "The carrier name",
                        "enum": ["dhl", "ups", "fedex", "dpd", "gls", "austrian_post"],
                    },
                },
                "required": ["tracking_number", "carrier"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------

def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool by name and return the JSON result string."""
    agent = ClaimsAgent()

    try:
        match name:
            case "scan_late_shipments":
                carrier = _parse_carrier(arguments.get("carrier"))
                from_date = _parse_date(arguments.get("from_date"))
                to_date = _parse_date(arguments.get("to_date"))
                shipments = agent.scan_late_shipments(
                    carrier=carrier, from_date=from_date, to_date=to_date
                )
                data = [_serialize_shipment(s) for s in shipments]
                return json.dumps({"count": len(data), "shipments": data})

            case "check_eligibility":
                carrier = _parse_carrier(arguments.get("carrier"))
                shipments = agent.scan_late_shipments(carrier=carrier)
                if not shipments:
                    return json.dumps({"message": "No unclaimed late shipments found.", "eligible": [], "ineligible": []})
                results = agent.check_all_eligibility(shipments)
                eligible = []
                ineligible = []
                for r in results:
                    s = r["shipment"]
                    entry = {
                        "tracking_number": s.tracking_number,
                        "carrier": s.carrier.value,
                        "service_level": s.service_level.value,
                        "days_late": s.days_late,
                        "amount_eur": s.declared_value,
                        "eligible": r["eligible"],
                        "reason": r["reason"],
                        "filing_deadline": r["filing_deadline"].isoformat() if r["filing_deadline"] else None,
                    }
                    if r["eligible"]:
                        eligible.append(entry)
                    else:
                        ineligible.append(entry)
                return json.dumps({"eligible": eligible, "ineligible": ineligible})

            case "draft_claims":
                carrier = _parse_carrier(arguments.get("carrier"))
                shipments = agent.scan_late_shipments(carrier=carrier)
                if not shipments:
                    return json.dumps({"message": "No unclaimed late shipments to draft.", "claims": []})
                results = agent.check_all_eligibility(shipments)
                claims = agent.draft_all_eligible(results)
                data = []
                for c in claims:
                    data.append({
                        "claim_id": c.id,
                        "shipment_id": c.shipment_id,
                        "amount_eur": c.claim_amount,
                        "status": c.status.value,
                        "filing_deadline": c.filing_deadline.isoformat() if c.filing_deadline else None,
                    })
                total = sum(c.claim_amount or 0 for c in claims)
                return json.dumps({"claims_drafted": len(data), "total_eur": total, "claims": data})

            case "get_all_claims":
                status_str = arguments.get("status")
                status = None
                if status_str:
                    try:
                        status = ClaimStatus(status_str)
                    except ValueError:
                        pass
                claims = agent.get_all_claims(status=status)
                data = [_serialize_claim(c) for c in claims]
                return json.dumps({"count": len(data), "claims": data})

            case "get_summary":
                summary = agent.get_summary()
                return json.dumps(summary)

            case "approve_claim":
                claim_id = int(arguments.get("claim_id", 0))
                reviewer = arguments.get("reviewer", "operator")
                claim = agent.approve_claim(claim_id, reviewer)
                return json.dumps({
                    "success": True,
                    "claim_id": claim.id,
                    "status": claim.status.value,
                    "reviewed_by": claim.reviewed_by,
                })

            case "generate_claim_email":
                claim_id = int(arguments.get("claim_id", 0))
                email_text = agent.generate_claim_email(claim_id)
                return json.dumps({"claim_id": claim_id, "email": email_text})

            case "get_carrier_policies":
                carrier_filter = _parse_carrier(arguments.get("carrier"))
                carriers = get_all_carriers()
                data = []
                for cname, client in carriers.items():
                    if carrier_filter and cname != carrier_filter:
                        continue
                    p = client.claim_policy
                    data.append({
                        "carrier": cname.value,
                        "allows_late_claims": p.allows_late_claims,
                        "eligible_services": [s.value for s in p.eligible_services],
                        "filing_deadline_days": p.filing_deadline_days,
                        "deadline_reference": p.deadline_reference,
                        "who_can_file": p.who_can_file,
                        "filing_methods": p.filing_methods,
                        "portal_url": p.portal_url,
                        "claim_email": p.claim_email,
                        "claim_phone": p.claim_phone,
                        "refund_type": p.refund_type,
                        "notes": p.notes,
                        "documentation_required": p.documentation_required,
                    })
                return json.dumps({"carriers": data})

            case "run_pipeline":
                from .orchestrator import Orchestrator
                skip = arguments.get("skip_ingestion", True)
                orchestrator = Orchestrator(agent=agent)
                result = orchestrator.run_pipeline(skip_ingestion=skip, auto_draft=True)
                return json.dumps({
                    "emails_scanned": result.emails_scanned,
                    "shipments_created": result.shipments_created,
                    "newly_late": result.newly_late,
                    "eligible": result.eligible_count,
                    "drafts_created": result.drafts_created,
                    "total_claimable_eur": result.total_claimable_eur,
                    "errors": result.errors,
                })

            case "check_tracking":
                import asyncio
                from .tracking_agent import TrackingAgent

                tracking_number = arguments.get("tracking_number", "")
                carrier = _parse_carrier(arguments.get("carrier"))
                if not tracking_number or not carrier:
                    return json.dumps({"error": "tracking_number and carrier are required"})

                tracker = TrackingAgent()
                # Run async in sync context
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            result = pool.submit(
                                asyncio.run, tracker.check_status(tracking_number, carrier)
                            ).result()
                    else:
                        result = asyncio.run(tracker.check_status(tracking_number, carrier))
                except RuntimeError:
                    result = asyncio.run(tracker.check_status(tracking_number, carrier))

                return json.dumps({
                    "tracking_number": result.tracking_number,
                    "carrier": result.carrier,
                    "status": result.status,
                    "delivery_date": result.delivery_date,
                    "location": result.location,
                    "signed_by": result.signed_by,
                    "source_url": result.source_url,
                    "raw_snippet": result.raw_page_snippet,
                })

            case _:
                return json.dumps({"error": f"Unknown tool: {name}"})

    except Exception as e:
        logger.error("Tool %s failed: %s", name, e)
        return json.dumps({"error": str(e)})
