"""
Kiosk system-prompt loader.

The You42 kiosk runs a file-based system prompt (versioned in the repo at
prompts/you42_kiosk.md) rather than per-avatar DB metadata, so guardrail
edits are code-reviewed and deployable without touching avatar records.

Date grounding: the literal token ``{now}`` in the prompt file is replaced
with the current date/time at session start. Without this the model cannot
reason about "already happened" — it's a hard requirement from the POC
brief, not a nicety. Replacement uses ``str.replace`` (not ``str.format``)
so the ``{{KB_*}}`` placeholder blocks pass through untouched.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def load_kiosk_prompt(now: Optional[datetime] = None) -> Optional[str]:
    """
    Return the kiosk system prompt with ``{now}`` resolved, or None when no
    prompt file is configured (SYSTEM_PROMPT_FILE empty) or readable —
    callers fall back to the repo's default per-avatar behavior.
    """
    if not settings.SYSTEM_PROMPT_FILE:
        return None
    path = Path(settings.SYSTEM_PROMPT_FILE)
    if not path.exists():
        logger.warning(f"SYSTEM_PROMPT_FILE not found: {path}")
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"Could not read system prompt file {path}: {e}")
        return None

    stamp = (now or datetime.now(timezone.utc)).strftime("%A, %B %d, %Y, %H:%M %Z")
    return text.replace("{now}", stamp)
