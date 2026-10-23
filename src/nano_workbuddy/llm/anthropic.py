"""Anthropic Messages protocol: Claude, and providers that offer an Anthropic-compatible endpoint.

The SDK's stream helper assembles the final message (thinking signatures, tool input JSON); we only
forward text/thinking deltas while it streams and translate the final message.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from ..config import ModelSpec
from ..types import Block, Message, Stop, Thinking, ToolCall, text
from . import ContextOverflow, Delta, Response

STOPS: dict[str, Stop] = {"end_turn": "end", "tool_use": "tool_use", "max_tokens": "length",
                          "model_context_window_exceeded": "length", "stop_sequence": "end", "pause_turn": "end",
                          "refusal": "error"}
CACHE = {"type": "ephemeral"}


class AnthropicMessages:
    def __init__(self, spec: ModelSpec, api_key: str):
        self.spec = spec
        self.client = anthropic.AsyncAnthropic(base_url=spec.provider.base_url, api_key=api_key,
                                               max_retries=3)

    async def stream(self, system: str, messages: list[Message], tools: list[dict[str, Any]],
                     tool_choice: str | None = None) -> AsyncIterator[Delta | Response]:
        provider = self.spec.provider.name
        req: dict[str, Any] = {"model": self.spec.id, "max_tokens": self.spec.max_output,
                               "messages": to_anthropic(messages, provider)}
        if system:  # breakpoint 1: tools + the frozen system prompt
            req["system"] = [{"type": "text", "text": system, "cache_control": CACHE}]
        if tools:
            req["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                            for t in tools]
            if tool_choice == "none":
                req["tool_choice"] = {"type": "none"}
        if req["messages"]:  # breakpoint 2: everything so far, so the next request reads it from cache
            req["messages"][-1]["content"][-1]["cache_control"] = CACHE
        if self.spec.params:  # e.g. {"thinking": {"type": "adaptive", "display": "summarized"}}
            req["extra_body"] = self.spec.params
        try:
            async with self.client.messages.stream(**req) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield Delta("text", event.delta.text)
                        elif event.delta.type == "thinking_delta":
                            yield Delta("thinking", event.delta.thinking)
                final = await stream.get_final_message()
        except anthropic.BadRequestError as e:
            msg = str(e).lower()
            if "prompt is too long" in msg or "context window" in msg or "context length" in msg:
                raise ContextOverflow(str(e)) from e
            raise
        yield from_anthropic(final, provider, self.spec.id)


def to_anthropic(messages: list[Message], provider: str) -> list[dict[str, Any]]:
    """Consecutive same-role messages are merged; tool results become tool_result blocks in a user turn."""
    out: list[dict[str, Any]] = []

    def add(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        if m["role"] == "user":
            add("user", [p for b in m["content"] if (p := _part(b))])
        elif m["role"] == "assistant":
            add("assistant", [p for b in m["content"] if (p := _assistant_part(b, provider))])
        else:
            parts = [p for b in m["content"] if (p := _part(b))] or [{"type": "text", "text": "(no output)"}]
            add("user", [{"type": "tool_result", "tool_use_id": m["call_id"], "content": parts,
                          "is_error": m.get("is_error", False)}])
    return out


def _part(b: Block) -> dict[str, Any] | None:
    if b["type"] == "text" and b["text"]:
        return {"type": "text", "text": b["text"]}
    if b["type"] == "image":
        return {"type": "image", "source": {"type": "base64", "media_type": b["mime"], "data": b["data"]}}
    return None


def _assistant_part(b: Block, provider: str) -> dict[str, Any] | None:
    if b["type"] == "tool_call":
        return {"type": "tool_use", "id": b["id"], "name": b["name"], "input": b["args"]}
    if b["type"] == "thinking":
        meta = b.get("meta", {})
        if b["provider"] != provider or b.get("stale") or not ("signature" in meta or "redacted" in meta):
            return None  # replay only unchanged thinking, to the provider that signed it
        if "redacted" in meta:
            return {"type": "redacted_thinking", "data": meta["redacted"]}
        return {"type": "thinking", "thinking": b["text"], "signature": meta["signature"]}
    return _part(b)


def from_anthropic(msg: Any, provider: str, model: str) -> Response:
    content: list[Block] = []
    for b in msg.content:
        if b.type == "text":
            content.append(text(b.text))
        elif b.type == "thinking":
            content.append(Thinking(type="thinking", text=b.thinking, provider=provider,
                                    meta={"signature": b.signature}))
        elif b.type == "redacted_thinking":
            content.append(Thinking(type="thinking", text="", provider=provider, meta={"redacted": b.data}))
        elif b.type == "tool_use":
            call = ToolCall(type="tool_call", id=b.id, name=b.name, args=b.input if isinstance(b.input, dict) else {})
            if not isinstance(b.input, dict):
                call["error"] = "tool input was not a JSON object"
            content.append(call)
    u = msg.usage
    cache_read, cache_write = u.cache_read_input_tokens or 0, u.cache_creation_input_tokens or 0
    usage = {"input": u.input_tokens + cache_read + cache_write, "output": u.output_tokens,
             "cache_read": cache_read, "cache_write": cache_write}
    return Response(content, STOPS.get(msg.stop_reason or "end_turn", "end"), usage,
                    {"provider": provider, "model": model})
