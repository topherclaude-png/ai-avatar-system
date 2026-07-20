"""
Guardrail transcript logging (Layer 5 — POC required).

Every turn is appended to a per-session JSONL file with flags:

  * ``injection_attempt`` — user text matched a prompt-injection heuristic
  * ``refusal``           — assistant text matched a refusal/redirect script
  * ``moderation_hit``    — an I/O screen blocked content (pilot, once wired)
  * ``tool_calls``        — tools the LLM executed this turn

This is the tuning loop for tightening the scope prompt against real guest
behavior, and the evidence trail if an interaction is ever disputed. The
flags are cheap heuristics by design — they exist to make eyeball review of
thousands of turns fast, not to be a classifier (that's Layer 3).

DB persistence of plain messages already exists (websocket._persist_message);
this file adds the flagged, grep-able view without schema changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Prompt-injection heuristics — deliberately broad; false positives are fine
# (they just flag a row for human review), false negatives are the cost of
# keeping this regex-cheap. Tuned from protocol steps 7–8 in the POC brief.
_INJECTION_PATTERNS = [
    r"ignore\s+(your|all|any|previous|prior|the)\s+\w*\s*(instructions|rules|prompt)",
    r"disregard\s+(your|all|any|previous|prior|the)",
    r"(reveal|show|repeat|print|tell me)\b.{0,40}\b(system prompt|instructions|rules)",
    r"\bsystem prompt\b",
    r"pretend\s+(you'?re|you are|to be)",
    r"you\s+are\s+now\s+",
    r"\b(jailbreak|developer mode|dan mode)\b",
    r"act\s+as\s+(if|an?\s)",
    r"new\s+persona",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION_PATTERNS), re.IGNORECASE)

# Refusal/redirect script markers — keep in sync with prompts/you42_kiosk.md.
_REFUSAL_PATTERNS = [
    r"let me get a team member",
    r"i can'?t adjust pricing",
    r"i'?m just here to help with you42",
]
_REFUSAL_RE = re.compile("|".join(f"(?:{p})" for p in _REFUSAL_PATTERNS), re.IGNORECASE)


def detect_injection_attempt(text: str) -> bool:
    return bool(_INJECTION_RE.search(text or ""))


def detect_refusal(text: str) -> bool:
    return bool(_REFUSAL_RE.search(text or ""))


def _transcript_path(session_id: str) -> Optional[Path]:
    if not settings.TRANSCRIPT_LOG_DIR:
        return None
    # Session ids are server-generated UUIDs, but sanitize anyway — this
    # value lands in a filesystem path.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)
    return Path(settings.TRANSCRIPT_LOG_DIR) / f"{safe}.jsonl"


async def log_turn(
    session_id: str,
    role: str,
    content: str,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    moderation_hit: bool = False,
) -> None:
    """
    Append one turn to the session transcript. Best-effort: a logging
    failure must never break the chat pipeline.
    """
    path = _transcript_path(session_id)
    if path is None:
        return

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "role": role,
        "content": content,
        "flags": {
            "injection_attempt": role == "user" and detect_injection_attempt(content),
            "refusal": role == "assistant" and detect_refusal(content),
            "moderation_hit": moderation_hit,
            "tool_calls": tool_calls or [],
        },
    }

    def _append() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    try:
        await asyncio.to_thread(_append)
    except Exception as e:
        logger.warning(f"Transcript log failed for {session_id}: {e}")

    if entry["flags"]["injection_attempt"]:
        logger.warning(f"Injection attempt flagged [{session_id}]")
