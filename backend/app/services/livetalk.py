"""
LiveTalking client — drives a continuous-stream avatar (rendering engine v2).

When LIVETALKING_URL is configured, the kiosk brain stops producing
per-sentence MP4 chunks and instead forwards speakable text to a LiveTalking
server (https://github.com/lipku/LiveTalking), which renders a CONTINUOUS
WebRTC stream: real-time MuseTalk lip-sync while speaking, natural idle
motion while silent. The browser holds the WebRTC session directly with
LiveTalking (/offer) and reports its sessionid to us over the chat WS.

API used (all POST, JSON):
  /human           {sessionid, type:'echo', text, interrupt?}  → speak text
  /interrupt_talk  {sessionid}                                 → barge-in
  /is_speaking     {sessionid}                                 → bool
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def enabled() -> bool:
    return bool(settings.LIVETALKING_URL)


def _base() -> str:
    return settings.LIVETALKING_URL.rstrip("/")


async def speak(sessionid: str, text: str, interrupt: bool = False) -> bool:
    """
    Queue `text` for the avatar to speak. Returns True on success; failures
    are logged, never raised — a rendering hiccup must not kill the chat turn
    (the guest still sees the streamed text reply).
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{_base()}/human",
                json={
                    "sessionid": sessionid,
                    "type": "echo",
                    "text": text,
                    "interrupt": interrupt,
                },
            )
            r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"LiveTalking speak failed [{sessionid}]: {e}")
        return False


async def speak_audio(sessionid: str, wav_bytes: bytes) -> bool:
    """
    Push pre-synthesized speech audio for lip-sync (POST /humanaudio,
    multipart). Used for the CLONED voice: our Chatterbox synthesizes with
    the guest-facing voice profile and LiveTalking only does the lip-sync
    (it resamples internally, any WAV rate is fine).
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            r = await client.post(
                f"{_base()}/humanaudio",
                data={"sessionid": sessionid},
                files={"file": ("speech.wav", wav_bytes, "audio/wav")},
            )
            r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"LiveTalking speak_audio failed [{sessionid}]: {e}")
        return False


async def interrupt(sessionid: str) -> bool:
    """Barge-in: flush anything queued or being spoken."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{_base()}/interrupt_talk", json={"sessionid": sessionid})
            r.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"LiveTalking interrupt failed [{sessionid}]: {e}")
        return False


async def is_speaking(sessionid: str) -> Optional[bool]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(f"{_base()}/is_speaking", json={"sessionid": sessionid})
            r.raise_for_status()
            return bool(r.json().get("data"))
    except Exception as e:
        logger.warning(f"LiveTalking is_speaking failed [{sessionid}]: {e}")
        return None
