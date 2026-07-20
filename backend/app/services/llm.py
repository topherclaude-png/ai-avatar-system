"""
LLM facade for the chat pipeline.

Provider-agnostic interface that currently supports Anthropic (default) and
OpenAI. The Anthropic path takes advantage of:

  * **Prompt caching** — the system prompt is wrapped in a content block
    with `cache_control={"type": "ephemeral"}`. Cached reads cost ~10% of
    fresh input, which dominates per-token cost for chatty avatars that
    share a system prompt across many turns. Workspace-isolated as of
    Anthropic's Feb 2026 change.
  * **Extended thinking (opt-in)** — when callers pass `thinking=True`
    we set `thinking={"type": "enabled", "budget_tokens": ...}` so the
    model reasons internally before answering. Reserved for hard turns;
    using it on every turn would multiply token cost.

Exceptions are re-raised as `LLMError` subclasses so the WebSocket pipeline
can distinguish rate-limit / auth / network failures and surface
appropriate user-facing messages.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Optional

import anthropic
import openai

from app.config import settings

logger = logging.getLogger(__name__)

# Hard cap on tool-execution rounds within one turn, so a model that keeps
# requesting tools can't loop forever. After the cap we make one final call
# with tools disabled to force a spoken answer.
_MAX_TOOL_ROUNDS = 4

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful AI assistant in a real-time avatar conversation system. "
    "Keep replies concise and conversational so they can be spoken aloud."
)

# Extended-thinking budget. Claude 4.x Opus supports up to 128k thinking
# tokens; for an interactive avatar we want responses fast, so we cap the
# budget low. Increase for research/agentic use cases.
_DEFAULT_THINKING_BUDGET = 4096


class LLMError(Exception):
    """Base class — chat pipeline catches this and emits a typed WS error."""


class LLMRateLimited(LLMError):
    """Provider returned 429."""


class LLMAuthError(LLMError):
    """Provider returned 401/403 — usually a misconfigured API key."""


class LLMUnavailable(LLMError):
    """Network failure, timeout, or 5xx from the provider."""


def _cacheable_system(system_prompt: Optional[str]) -> list[dict]:
    """
    Build a system block list with prompt-cache marking applied to the
    (long-lived) system prompt. The SDK accepts either a plain string OR
    a list of blocks; blocks are needed to attach `cache_control` per-block.
    """
    text = system_prompt or DEFAULT_SYSTEM_PROMPT
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _map_anthropic_exception(exc: Exception) -> LLMError:
    """Translate Anthropic SDK exceptions into the typed LLMError hierarchy."""
    if isinstance(exc, anthropic.RateLimitError):
        return LLMRateLimited(str(exc))
    if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return LLMAuthError(str(exc))
    if isinstance(
        exc,
        (anthropic.APITimeoutError, anthropic.APIConnectionError, anthropic.InternalServerError),
    ):
        return LLMUnavailable(str(exc))
    if isinstance(exc, anthropic.BadRequestError):
        # 400 from Anthropic is usually our bug, not theirs — surface verbatim.
        return LLMError(f"Invalid request to Anthropic: {exc}")
    return LLMError(str(exc))


def _map_openai_exception(exc: Exception) -> LLMError:
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimited(str(exc))
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return LLMAuthError(str(exc))
    if isinstance(
        exc, (openai.APITimeoutError, openai.APIConnectionError, openai.InternalServerError)
    ):
        return LLMUnavailable(str(exc))
    return LLMError(str(exc))


class LLMService:
    """LLM Service for AI responses."""

    def __init__(self):
        self.provider = settings.LLM_PROVIDER
        self.model = settings.LLM_MODEL
        self.temperature = settings.LLM_TEMPERATURE
        self.max_tokens = settings.LLM_MAX_TOKENS

        if self.provider == "anthropic":
            self.client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        elif self.provider == "ollama":
            # Ollama (and vLLM / LM Studio / OpenRouter) speak the OpenAI
            # wire protocol — reuse the OpenAI client against their base URL.
            # Fully local and free; no API key required (the client insists
            # on a non-empty string, so we pass a placeholder).
            base_url = settings.OPENAI_BASE_URL or "http://localhost:11434/v1"
            self.client = openai.AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY or "ollama",
                base_url=base_url,
            )
            self.provider = "openai"  # downstream code paths are identical
            logger.info(f"LLM provider 'ollama' → OpenAI-compatible client at {base_url}")
        elif self.provider == "openai":
            self.client = openai.AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY,
                base_url=settings.OPENAI_BASE_URL,  # None → api.openai.com
            )

    # ── non-streaming ────────────────────────────────────────────────────────

    async def generate_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
        thinking: bool = False,
    ) -> str:
        if self.provider == "anthropic":
            return await self._generate_anthropic(messages, system_prompt, thinking)
        if self.provider == "openai":
            return await self._generate_openai(messages, system_prompt)
        raise LLMError(f"Unsupported LLM provider: {self.provider}")

    async def _generate_anthropic(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str],
        thinking: bool,
    ) -> str:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": _cacheable_system(system_prompt),
            "messages": messages,
        }
        # Extended thinking — model "thinks" privately before answering. The
        # thinking tokens still count against output budget so we widen
        # max_tokens to cover both. Per Anthropic docs, temperature must be 1
        # when extended thinking is enabled.
        if thinking:
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": _DEFAULT_THINKING_BUDGET}
            kwargs["max_tokens"] = max(self.max_tokens + _DEFAULT_THINKING_BUDGET, self.max_tokens)
            kwargs["temperature"] = 1.0

        try:
            response = await self.client.messages.create(**kwargs)
        except Exception as e:
            mapped = _map_anthropic_exception(e)
            logger.error(
                "anthropic_call_failed",
                extra={"error_type": type(e).__name__, "mapped": type(mapped).__name__},
            )
            raise mapped from e

        # Find the first text block (skip thinking blocks if any).
        for block in response.content or []:
            if getattr(block, "type", None) == "text" and hasattr(block, "text"):
                self._log_usage(response.usage, thinking)
                return block.text

        raise LLMError("Anthropic response contained no text block")

    async def _generate_openai(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> str:
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}] + messages

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as e:
            mapped = _map_openai_exception(e)
            logger.error("openai_call_failed", extra={"error_type": type(e).__name__})
            raise mapped from e

        return response.choices[0].message.content or ""

    # ── streaming ────────────────────────────────────────────────────────────

    async def stream_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        if self.provider == "anthropic":
            async for chunk in self._stream_anthropic(messages, system_prompt):
                yield chunk
        elif self.provider == "openai":
            async for chunk in self._stream_openai(messages, system_prompt):
                yield chunk
        else:
            raise LLMError(f"Unsupported LLM provider: {self.provider}")

    async def _stream_anthropic(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        try:
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=_cacheable_system(system_prompt),
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        except Exception as e:
            mapped = _map_anthropic_exception(e)
            logger.error("anthropic_stream_failed", extra={"error_type": type(e).__name__})
            raise mapped from e

    async def _stream_openai(
        self,
        messages: List[Dict[str, str]],
        system_prompt: Optional[str] = None,
    ) -> AsyncGenerator[str, None]:
        if system_prompt:
            messages = [{"role": "system", "content": system_prompt}] + messages

        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                stream=True,
            )
            async for chunk in stream:
                content = chunk.choices[0].delta.content
                if content:
                    yield content
        except Exception as e:
            mapped = _map_openai_exception(e)
            logger.error("openai_stream_failed", extra={"error_type": type(e).__name__})
            raise mapped from e

    # ── streaming with tool calling (OpenAI-compatible path) ────────────────

    async def stream_with_tools(
        self,
        messages: List[Dict[str, Any]],
        system_prompt: Optional[str] = None,
        tools: Optional[List[dict]] = None,
        executor: Optional[Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Stream a turn with function calling. Yields typed events:

          {"type": "text", "text": <token>}          — spoken-reply token
          {"type": "tool_call", "name": ..., "arguments": {...}, "result": {...}}
                                                     — after each executed tool

        The agentic loop runs entirely here: when the model requests tools we
        execute them via `executor`, append the results, and re-call — up to
        _MAX_TOOL_ROUNDS, then one forced tool-free call. The caller's
        `messages` list is never mutated (tool/assistant scaffolding stays
        internal; the caller owns its own history format).

        Only the OpenAI-compatible provider supports this; on the Anthropic
        provider (or with no tools/executor) it degrades to plain streaming.
        """
        if self.provider != "openai" or not tools or executor is None:
            if tools and self.provider != "openai":
                logger.warning(
                    f"Tool calling requested but provider is '{self.provider}' — "
                    "streaming without tools (set LLM_PROVIDER=openai/ollama)."
                )
            async for token in self.stream_response(messages, system_prompt):
                yield {"type": "text", "text": token}
            return

        # Work on a private copy — tool scaffolding must not leak into the
        # session's conversation history.
        convo: List[Dict[str, Any]] = list(messages)
        if system_prompt:
            convo = [{"role": "system", "content": system_prompt}] + convo

        for round_no in range(_MAX_TOOL_ROUNDS + 1):
            # Final round: drop tools to force a plain spoken answer.
            offer_tools = tools if round_no < _MAX_TOOL_ROUNDS else None

            kwargs: dict = {
                "model": self.model,
                "messages": convo,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "stream": True,
            }
            if offer_tools:
                kwargs["tools"] = offer_tools

            content = ""
            # index → {"id", "name", "arguments"} accumulated from deltas
            pending: Dict[int, Dict[str, str]] = {}
            finish_reason: Optional[str] = None

            try:
                stream = await self.client.chat.completions.create(**kwargs)
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta

                    if delta and delta.content:
                        content += delta.content
                        yield {"type": "text", "text": delta.content}

                    if delta and delta.tool_calls:
                        for tc in delta.tool_calls:
                            slot = pending.setdefault(
                                tc.index, {"id": "", "name": "", "arguments": ""}
                            )
                            if tc.id:
                                slot["id"] = tc.id
                            if tc.function and tc.function.name:
                                slot["name"] = tc.function.name
                            if tc.function and tc.function.arguments:
                                slot["arguments"] += tc.function.arguments

                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
            except Exception as e:
                mapped = _map_openai_exception(e)
                logger.error("openai_tool_stream_failed", extra={"error_type": type(e).__name__})
                raise mapped from e

            if finish_reason != "tool_calls" or not pending:
                return  # plain answer — turn complete

            # Model requested tools: record its request, execute each, append
            # results, and loop for the follow-up call.
            calls = [pending[i] for i in sorted(pending)]
            convo.append(
                {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {
                            "id": c["id"] or f"call_{i}",
                            "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"] or "{}"},
                        }
                        for i, c in enumerate(calls)
                    ],
                }
            )
            for i, call in enumerate(calls):
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                    if not isinstance(arguments, dict):
                        arguments = {}
                except json.JSONDecodeError:
                    arguments = {}
                result = await executor(call["name"], arguments)
                logger.info(
                    "tool_executed",
                    extra={"tool": call["name"], "round": round_no},
                )
                yield {
                    "type": "tool_call",
                    "name": call["name"],
                    "arguments": arguments,
                    "result": result,
                }
                convo.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"] or f"call_{i}",
                        "content": json.dumps(result),
                    }
                )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _log_usage(self, usage, thinking: bool) -> None:
        if usage is None:
            return
        try:
            logger.info(
                "llm_usage",
                extra={
                    "in_tokens": getattr(usage, "input_tokens", 0),
                    "out_tokens": getattr(usage, "output_tokens", 0),
                    "cache_create_tokens": getattr(usage, "cache_creation_input_tokens", 0),
                    "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0),
                    "thinking": thinking,
                },
            )
        except Exception:
            pass


# Global instance
llm_service = LLMService()
