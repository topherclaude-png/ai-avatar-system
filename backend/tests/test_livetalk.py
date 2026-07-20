"""
Tests for the LiveTalking (engine v2) integration: the client wrapper, the
consumer switch (sentences → /human instead of TTS+MP4 chunks), interrupt
fan-out, and sessionid attachment.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.services import livetalk


class _FakeResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"data": True}


class _FakeClient:
    """Records every POST; stands in for httpx.AsyncClient."""

    calls: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        _FakeClient.calls.append((url, json))
        return _FakeResponse()


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.calls = []
    monkeypatch.setattr(livetalk.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(settings, "LIVETALKING_URL", "http://lt:8010")
    return _FakeClient.calls


def test_enabled_toggles_on_setting(monkeypatch):
    monkeypatch.setattr(settings, "LIVETALKING_URL", "")
    assert not livetalk.enabled()
    monkeypatch.setattr(settings, "LIVETALKING_URL", "http://lt:8010")
    assert livetalk.enabled()


async def test_speak_posts_echo(fake_http):
    ok = await livetalk.speak("sid-1", "Hello there.", interrupt=True)
    assert ok
    url, payload = fake_http[0]
    assert url == "http://lt:8010/human"
    assert payload == {
        "sessionid": "sid-1",
        "type": "echo",
        "text": "Hello there.",
        "interrupt": True,
    }


async def test_interrupt_posts(fake_http):
    assert await livetalk.interrupt("sid-1")
    url, payload = fake_http[0]
    assert url == "http://lt:8010/interrupt_talk"
    assert payload == {"sessionid": "sid-1"}


async def test_speak_failure_returns_false(monkeypatch):
    monkeypatch.setattr(settings, "LIVETALKING_URL", "http://lt:8010")

    class Boom(_FakeClient):
        async def post(self, url, json=None):
            raise RuntimeError("down")

    monkeypatch.setattr(livetalk.httpx, "AsyncClient", Boom)
    assert (await livetalk.speak("sid", "x")) is False  # never raises


# ── consumer switch ──────────────────────────────────────────────────────────


async def test_consumer_forwards_sentences_to_livetalk(monkeypatch):
    from app import websocket as wsmod

    monkeypatch.setattr(settings, "LIVETALKING_URL", "http://lt:8010")

    manager = wsmod.ConnectionManager()
    session_id = "sess-lt"
    manager.active_connections[session_id] = object()
    manager.session_data[session_id] = {
        "livetalk_sessionid": "lt-42",
        "avatar_image_local": "/nonexistent.jpg",  # must NOT be used in v2 path
        "language": "en",
        "connected_at": datetime.now(timezone.utc),
    }

    spoken = []

    async def fake_speak(sid, text, interrupt=False):
        spoken.append((sid, text))
        return True

    monkeypatch.setattr(wsmod.livetalk, "speak", fake_speak)

    sent = []

    async def fake_send(sid, message):
        sent.append(message)

    monkeypatch.setattr(manager, "send_message", fake_send)

    queue: asyncio.Queue = asyncio.Queue()
    for s in ["First sentence.", "Second sentence.", None]:
        queue.put_nowait(s)

    await manager._animate_from_queue(session_id, queue)

    assert spoken == [("lt-42", "First sentence."), ("lt-42", "Second sentence.")]
    # No chunked-pipeline events in engine v2.
    assert not any(m["type"].startswith("video_chunk") for m in sent)


async def test_consumer_uses_chunk_pipeline_without_lt_session(monkeypatch):
    """LIVETALKING_URL set but no sessionid reported → classic pipeline path
    (drains silently here because there's no avatar image)."""
    from app import websocket as wsmod

    monkeypatch.setattr(settings, "LIVETALKING_URL", "http://lt:8010")

    manager = wsmod.ConnectionManager()
    session_id = "sess-nolt"
    manager.active_connections[session_id] = object()
    manager.session_data[session_id] = {
        "livetalk_sessionid": None,
        "avatar_image_local": None,
        "language": "en",
    }

    called = []

    async def fake_speak(sid, text, interrupt=False):
        called.append(text)
        return True

    monkeypatch.setattr(wsmod.livetalk, "speak", fake_speak)

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait("Hello.")
    queue.put_nowait(None)
    await manager._animate_from_queue(session_id, queue)
    assert called == []  # nothing forwarded


# ── interrupt fan-out + sessionid attach ─────────────────────────────────────


async def test_interrupt_flushes_livetalk(monkeypatch):
    from app import websocket as wsmod

    monkeypatch.setattr(settings, "LIVETALKING_URL", "http://lt:8010")

    manager = wsmod.ConnectionManager()
    session_id = "sess-int"
    manager.active_connections[session_id] = object()
    manager.session_data[session_id] = {"livetalk_sessionid": "lt-9"}

    async def fake_send(sid, message):
        pass

    monkeypatch.setattr(manager, "send_message", fake_send)

    flushed = []

    async def fake_interrupt(sid):
        flushed.append(sid)
        return True

    monkeypatch.setattr(wsmod.livetalk, "interrupt", fake_interrupt)

    async def long_turn():
        await asyncio.sleep(30)

    manager._active_turns[session_id] = asyncio.create_task(long_turn())
    assert await manager.interrupt_active_turn(session_id) is True
    await asyncio.sleep(0)  # let the fire-and-forget interrupt task run
    assert flushed == ["lt-9"]


async def test_set_livetalk_session():
    from app import websocket as wsmod

    manager = wsmod.ConnectionManager()
    manager.session_data["s1"] = {"livetalk_sessionid": None}
    await manager.set_livetalk_session("s1", "lt-77")
    assert manager.session_data["s1"]["livetalk_sessionid"] == "lt-77"
