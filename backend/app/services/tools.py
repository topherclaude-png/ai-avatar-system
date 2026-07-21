"""
LLM tool-calling layer for the You42 kiosk avatar.

Two tools, per the POC brief:

  * ``get_events(start_date?, end_date?)`` — upcoming events. Backed by a
    local JSON table for the POC (an Eventbrite-synced cache in pilot). The
    ``start_date >= now`` filter is applied HERE, server-side, on every call
    — never delegated to the prompt — so the model physically cannot see or
    resurrect expired events.
  * ``show_payment_qr(event_id, price)`` — returns a payment URL the
    frontend renders as a QR overlay next to the avatar video. The URL is a
    configurable stub in the POC (no checkout integration this phase).

Definitions use the OpenAI function-calling wire format, which every
endpoint in the POC matrix (vLLM, OpenRouter, Groq) speaks.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

TOOL_DEFINITIONS: List[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_events",
            "description": (
                "Look up upcoming events at You42. Always call this before "
                "discussing any event — never rely on memory. Only events "
                "returned by this tool exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "Earliest event date to include, ISO 8601 (YYYY-MM-DD). Defaults to today.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "Latest event date to include, ISO 8601 (YYYY-MM-DD). Optional.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_payment_qr",
            "description": (
                "Display a payment QR code on screen so the guest can buy "
                "tickets with their phone. Call this once the guest confirms "
                "which event and how many tickets they want."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "ID of the event, from a get_events result.",
                    },
                    "price": {
                        "type": "number",
                        "description": "Total price in USD for the requested tickets.",
                    },
                },
                "required": ["event_id", "price"],
            },
        },
    },
]


def _parse_iso_date(raw: Any) -> Optional[datetime]:
    """Parse an ISO date/datetime string into an aware UTC datetime, else None."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_events_table() -> List[dict]:
    """Read the local events table. Missing/corrupt file → empty list, never an error."""
    path = Path(settings.EVENTS_FILE)
    if not path.exists():
        logger.warning(f"Events file not found: {path}")
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Could not read events file {path}: {e}")
        return []


def get_events(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    _now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    Return upcoming events, mandatorily filtered to ``start_date >= now``.

    The caller-supplied window can only NARROW the result further — a
    requested start in the past is clamped to now, so expired events are
    unreachable regardless of what the model asks for. ``_now`` exists for
    tests only.
    """
    now = _now or datetime.now(timezone.utc)

    window_start = _parse_iso_date(start_date)
    if window_start is None or window_start < now:
        window_start = now  # clamp: the past is never queryable
    window_end = _parse_iso_date(end_date)
    # A date-only end (no time component) means "through the END of that
    # day". Parsed as midnight it EXCLUDES same-day events — and with
    # timezone offsets even excludes them in UTC terms (observed live: an
    # Aug 7 19:00-05:00 event is Aug 8 in UTC; asking for Aug 7 → "no
    # events"). Extend by one day to make the window inclusive.
    if window_end is not None and isinstance(end_date, str) and "T" not in end_date:
        window_end += timedelta(days=1)

    events = []
    for ev in _load_events_table():
        starts = _parse_iso_date(ev.get("start_date"))
        if starts is None or starts < window_start:
            continue
        if window_end is not None and starts > window_end:
            continue
        events.append(
            {
                "id": ev.get("id"),
                "title": ev.get("title"),
                "start_date": ev.get("start_date"),
                "venue": ev.get("venue"),
                "price": ev.get("price"),
                "description": ev.get("description"),
            }
        )

    events.sort(key=lambda e: e.get("start_date") or "")
    return {"events": events, "count": len(events), "as_of": now.isoformat()}


def show_payment_qr(event_id: str, price: float) -> Dict[str, Any]:
    """
    Return the payload the frontend needs to render the QR overlay.

    POC: ``payment_url`` is a stub built from PAYMENT_URL_STUB — no Stripe/
    Eventbrite checkout this phase. The frontend QRs whatever URL comes back,
    so swapping in a real checkout link later is backend-only.
    """
    return {
        "payment_url": f"{settings.PAYMENT_URL_STUB.rstrip('/')}?event={event_id}&amount={price}",
        "event_id": event_id,
        "price": price,
        "display": "qr_overlay",
    }


async def execute_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """
    Dispatch a tool call by name. Always returns a JSON-serializable dict —
    errors come back as ``{"error": ...}`` so the model can recover
    conversationally instead of the turn crashing.
    """
    try:
        if name == "get_events":
            return get_events(
                start_date=arguments.get("start_date"),
                end_date=arguments.get("end_date"),
            )
        if name == "show_payment_qr":
            event_id = arguments.get("event_id")
            price = arguments.get("price")
            if not event_id or price is None:
                return {"error": "show_payment_qr requires event_id and price"}
            return show_payment_qr(event_id=str(event_id), price=float(price))
        return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}")
        return {"error": f"Tool {name} failed"}
