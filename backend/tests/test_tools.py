"""
Tests for the You42 tool-calling layer:

  * `get_events` — the server-side `start_date >= now` filter is the load-
    bearing guarantee (expired events must be unreachable no matter what
    window the model requests), so it gets the most cases.
  * `show_payment_qr` — stub payment URL payload.
  * `LLMService.stream_with_tools` — the streamed agentic loop against a
    fake OpenAI client: tool-call delta accumulation across chunks, executor
    dispatch, result feedback, follow-up round, and the non-OpenAI fallback.
  * `_llm_producer` — forwards `tool_call` events to the client and keeps
    tool scaffolding out of the spoken-text pipeline.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import tools as tools_mod
from app.services.llm import LLMService
from app.services.tools import execute_tool, get_events, show_payment_qr

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)

EVENTS = [
    {"id": "past", "title": "Past Event", "start_date": "2026-06-01T19:00:00+00:00", "price": 5},
    {"id": "soon", "title": "Soon Event", "start_date": "2026-08-01T19:00:00+00:00", "price": 15},
    {"id": "later", "title": "Later Event", "start_date": "2026-10-01T19:00:00+00:00", "price": 20},
    {"id": "undated", "title": "No Date"},
]


@pytest.fixture
def events_file(tmp_path, monkeypatch):
    path = tmp_path / "events.json"
    path.write_text(json.dumps(EVENTS), encoding="utf-8")
    monkeypatch.setattr(settings, "EVENTS_FILE", str(path))
    return path


# ── get_events: the date filter ──────────────────────────────────────────────


def test_past_events_are_excluded(events_file):
    result = get_events(_now=NOW)
    ids = [e["id"] for e in result["events"]]
    assert ids == ["soon", "later"]
    assert result["count"] == 2


def test_requested_past_window_is_clamped_to_now(events_file):
    # The model asking for January cannot resurface expired events.
    result = get_events(start_date="2026-01-01", _now=NOW)
    ids = [e["id"] for e in result["events"]]
    assert "past" not in ids
    assert ids == ["soon", "later"]


def test_end_date_narrows_window(events_file):
    result = get_events(end_date="2026-09-01", _now=NOW)
    ids = [e["id"] for e in result["events"]]
    assert ids == ["soon"]


def test_future_start_date_is_respected(events_file):
    result = get_events(start_date="2026-09-01", _now=NOW)
    ids = [e["id"] for e in result["events"]]
    assert ids == ["later"]


def test_undated_events_never_surface(events_file):
    ids = [e["id"] for e in get_events(_now=NOW)["events"]]
    assert "undated" not in ids


def test_missing_events_file_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "EVENTS_FILE", str(tmp_path / "nope.json"))
    result = get_events(_now=NOW)
    assert result["events"] == []
    assert result["count"] == 0


def test_garbage_date_inputs_dont_crash(events_file):
    result = get_events(start_date="not-a-date", end_date="also-no", _now=NOW)
    # Unparseable window → same as no window (still now-clamped).
    assert [e["id"] for e in result["events"]] == ["soon", "later"]


# ── show_payment_qr ──────────────────────────────────────────────────────────


def test_payment_qr_payload(monkeypatch):
    monkeypatch.setattr(settings, "PAYMENT_URL_STUB", "https://pay.test/checkout")
    result = show_payment_qr(event_id="soon", price=30.0)
    assert result["payment_url"] == "https://pay.test/checkout?event=soon&amount=30.0"
    assert result["display"] == "qr_overlay"
    assert result["event_id"] == "soon"


async def test_execute_tool_dispatch(events_file):
    result = await execute_tool("get_events", {})
    assert "events" in result

    result = await execute_tool("show_payment_qr", {"event_id": "soon", "price": 15})
    assert "payment_url" in result

    result = await execute_tool("show_payment_qr", {"event_id": "soon"})
    assert "error" in result  # missing price

    result = await execute_tool("does_not_exist", {})
    assert "error" in result


async def test_execute_tool_swallows_handler_exceptions(events_file, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(tools_mod, "get_events", boom)
    result = await execute_tool("get_events", {})
    assert result == {"error": "Tool get_events failed"}


# ── stream_with_tools: the agentic loop ──────────────────────────────────────


def _chunk(content=None, tool_calls=None, finish_reason=None):
    """Build an object shaped like an OpenAI streaming chunk."""
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])


def _tc_delta(index, id=None, name=None, arguments=None):
    fn = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=id, function=fn)


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _make_service(rounds):
    """
    LLMService with a fake OpenAI client that serves `rounds` (a list of
    chunk lists) in order, recording every request payload.
    """
    svc = LLMService.__new__(LLMService)  # skip __init__ — no real clients
    svc.provider = "openai"
    svc.model = "test-model"
    svc.temperature = 0.5
    svc.max_tokens = 100

    calls = []

    async def fake_create(**kwargs):
        calls.append(kwargs)
        return _FakeStream(rounds[len(calls) - 1])

    svc.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))
    )
    return svc, calls


async def test_tool_loop_executes_and_continues():
    rounds = [
        # Round 1: model streams a tool call, arguments split across chunks.
        [
            _chunk(tool_calls=[_tc_delta(0, id="call_1", name="get_events", arguments='{"end_')]),
            _chunk(tool_calls=[_tc_delta(0, arguments='date": "2026-09-01"}')]),
            _chunk(finish_reason="tool_calls"),
        ],
        # Round 2: model answers in text.
        [
            _chunk(content="One event "),
            _chunk(content="coming up."),
            _chunk(finish_reason="stop"),
        ],
    ]
    svc, calls = _make_service(rounds)

    executed = []

    async def executor(name, arguments):
        executed.append((name, arguments))
        return {"events": [{"id": "soon"}], "count": 1}

    events = []
    async for ev in svc.stream_with_tools(
        [{"role": "user", "content": "any events?"}],
        system_prompt="sys",
        tools=[{"type": "function", "function": {"name": "get_events"}}],
        executor=executor,
    ):
        events.append(ev)

    # Arguments were accumulated across deltas and parsed once complete.
    assert executed == [("get_events", {"end_date": "2026-09-01"})]

    tool_events = [e for e in events if e["type"] == "tool_call"]
    assert len(tool_events) == 1
    assert tool_events[0]["result"]["count"] == 1

    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert text == "One event coming up."

    # Round 2 request carried the tool scaffolding: assistant tool_calls
    # message + tool result message, after system + user.
    msgs = calls[1]["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[-2]["role"] == "assistant"
    assert msgs[-2]["tool_calls"][0]["function"]["name"] == "get_events"
    assert msgs[-1]["role"] == "tool"
    assert json.loads(msgs[-1]["content"])["count"] == 1


async def test_caller_messages_never_mutated():
    rounds = [
        [
            _chunk(tool_calls=[_tc_delta(0, id="c1", name="get_events", arguments="{}")]),
            _chunk(finish_reason="tool_calls"),
        ],
        [_chunk(content="Done."), _chunk(finish_reason="stop")],
    ]
    svc, _ = _make_service(rounds)

    async def executor(name, arguments):
        return {"ok": True}

    original = [{"role": "user", "content": "hi"}]
    async for _ in svc.stream_with_tools(
        original, tools=[{"x": 1}], executor=executor
    ):
        pass
    assert original == [{"role": "user", "content": "hi"}]


async def test_tool_round_cap_forces_toolless_final_call():
    # Model requests a tool every single round — the service must stop
    # offering tools after _MAX_TOOL_ROUNDS and force a text answer.
    from app.services.llm import _MAX_TOOL_ROUNDS

    tool_round = [
        _chunk(tool_calls=[_tc_delta(0, id="c", name="get_events", arguments="{}")]),
        _chunk(finish_reason="tool_calls"),
    ]
    final_round = [_chunk(content="Giving up on tools."), _chunk(finish_reason="stop")]
    rounds = [tool_round] * _MAX_TOOL_ROUNDS + [final_round]
    svc, calls = _make_service(rounds)

    async def executor(name, arguments):
        return {"ok": True}

    events = [
        e
        async for e in svc.stream_with_tools(
            [{"role": "user", "content": "hi"}], tools=[{"x": 1}], executor=executor
        )
    ]

    assert len(calls) == _MAX_TOOL_ROUNDS + 1
    assert all("tools" in c for c in calls[:-1])
    assert "tools" not in calls[-1]  # final call is tool-free
    assert [e["type"] for e in events].count("tool_call") == _MAX_TOOL_ROUNDS


async def test_non_openai_provider_falls_back_to_plain_stream(monkeypatch):
    svc = LLMService.__new__(LLMService)
    svc.provider = "anthropic"

    async def fake_plain(messages, system_prompt=None):
        for tok in ["a", "b"]:
            yield tok

    monkeypatch.setattr(svc, "stream_response", fake_plain)

    events = [
        e
        async for e in svc.stream_with_tools(
            [{"role": "user", "content": "hi"}], tools=[{"x": 1}], executor=None
        )
    ]
    assert events == [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]


# ── WS producer wiring ───────────────────────────────────────────────────────


async def test_llm_producer_forwards_tool_call_events(monkeypatch):
    import asyncio

    from app import websocket as wsmod

    manager = wsmod.ConnectionManager()
    session_id = "sess-tools"
    manager.active_connections[session_id] = object()  # just needs to be present

    sent = []

    async def fake_send(sid, message):
        sent.append(message)

    monkeypatch.setattr(manager, "send_message", fake_send)
    monkeypatch.setattr(wsmod.settings, "TOOLS_ENABLED", True)

    async def fake_stream_with_tools(messages, system_prompt=None, tools=None, executor=None):
        yield {"type": "text", "text": "Here is the QR code. "}
        yield {
            "type": "tool_call",
            "name": "show_payment_qr",
            "arguments": {"event_id": "soon", "price": 30},
            "result": {"payment_url": "https://pay.test/x", "display": "qr_overlay"},
        }
        yield {"type": "text", "text": "Scan to pay!"}

    monkeypatch.setattr(wsmod.llm_service, "stream_with_tools", fake_stream_with_tools)

    queue: asyncio.Queue = asyncio.Queue()
    full_text = await manager._llm_producer(
        session_id, [{"role": "user", "content": "two tickets"}], None, queue
    )

    tool_msgs = [m for m in sent if m["type"] == "tool_call"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["name"] == "show_payment_qr"
    assert tool_msgs[0]["result"]["payment_url"] == "https://pay.test/x"

    # Spoken text is untouched by the tool event.
    assert full_text == "Here is the QR code. Scan to pay!"

    # Queue got the speakable chunks then the end sentinel; no tool payloads.
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    assert items[-1] is None
    assert all(i is None or isinstance(i, str) for i in items)
