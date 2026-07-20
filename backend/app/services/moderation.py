"""
I/O moderation hook points (guardrail Layer 3).

POC scope per the brief: define WHERE moderation happens and pass everything
through; the actual classifier (Llama Guard local or via Groq serverless)
is wired in the pilot phase by replacing the two function bodies — the
pipeline call sites don't change.

Hook points:
  * ``screen_input``  — called on guest text BEFORE it reaches the LLM.
  * ``screen_output`` — called on each speakable chunk BEFORE TTS. This is
    the stop that prevents a successful jailbreak from being spoken on video
    in the lobby, which is why it's the higher-priority screen of the two.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ModerationResult:
    allowed: bool = True
    categories: List[str] = field(default_factory=list)  # classifier labels when wired
    # What to say/do instead when blocked (pilot: drives the handoff script)
    replacement: str = "Let me get a team member to help you with that."


async def screen_input(text: str) -> ModerationResult:
    """Pre-LLM input screen. POC: pass-through; pilot: Llama Guard classify."""
    if not settings.MODERATION_ENABLED:
        return ModerationResult(allowed=True)
    # Pilot: call the classifier here. Until then, enabled==True still passes
    # everything but logs, so the hook's latency position can be observed.
    logger.info("moderation_input_screen", extra={"chars": len(text)})
    return ModerationResult(allowed=True)


async def screen_output(text: str) -> ModerationResult:
    """Pre-TTS output screen. POC: pass-through; pilot: Llama Guard classify."""
    if not settings.MODERATION_ENABLED:
        return ModerationResult(allowed=True)
    logger.info("moderation_output_screen", extra={"chars": len(text)})
    return ModerationResult(allowed=True)
