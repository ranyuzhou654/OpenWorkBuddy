"""OpenAI Responses protocol: GPT models, whose reasoning is carried across turns only by this API.

Requests are stateless (store=false): every turn sends the whole history, and reasoning comes back as
encrypted items that we keep in the Thinking block's meta and replay to the same provider (D8).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import openai

from ..config import ModelSpec
from ..types import Block, Message, Stop, Thinking, ToolCall, new_id, text
from . import ContextOverflow, Delta, Response


class OpenAIResponses:
    def __init__(self, spec: ModelSpec, api_key: str):
        self.spec = spec
        self.client = openai.AsyncOpenAI(base_url=spec.provider.base_url, api_key=api_key, max_retries=3)

    async def stream(self, system: str, messages: list[Message], tools: list[dict[str, Any]],
                     tool_choice: str | None = None) -> AsyncIterator[Delta | Response]:
        provider = self.spec.provider.name
        req: dict[str, Any] = {"model": self.spec.id, "instructions": system, "input": to_input(messages, provider),
                               "max_output_tokens": self.spec.max_output, "stream": True, "store": False,
                               "include": ["reasoning.encrypted_content"]}
        if tools:
            req["tools"] = [{"type": "function", "name": t["name"], "description": t["description"],
                             "parameters": t["parameters"], "strict": False} for t in tools]
            if tool_choice:
                req["tool_choice"] = tool_choice
        if self.spec.params:  # e.g. {"reasoning": {"effort": "high", "summary": "auto"}}
            req["extra_body"] = self.spec.params
        final = None
        try:
            async for event in await self.client.responses.create(**req):
                if event.type == "response.output_text.delta":
                    yield Delta("text", event.delta)
                elif event.type == "response.reasoning_summary_text.delta":
                    yield Delta("thinking", event.delta)
                elif event.type in ("response.completed", "response.incomplete", "response.failed"):
                    final = event.response
        except openai.BadRequestError as e:
            if e.code == "context_length_exceeded" or "context window" in str(e).lower():
                raise ContextOverflow(str(e)) from e
            raise
        if final is None:
            raise RuntimeError("the Responses stream ended without a final response")
        if final.status == "failed":
            raise RuntimeError(f"response failed: {final.error.message if final.error else 'unknown error'}")
        yield from_output(final, provider, self.spec.id)


def to_input(messages: list[Message], provider: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "user":
            parts = [_part(b) for b in m["content"] if b["type"] in ("text", "image")]
            items.append({"role": "user", "content": parts})
        elif m["role"] == "assistant":
            for b in m["content"]:
                if b["type"] == "text" and b["text"]:
                    items.append({"role": "assistant", "content": b["text"]})
                elif b["type"] == "tool_call":
                    items.append({"type": "function_call", "call_id": b["id"], "name": b["name"],
                                  "arguments": json.dumps(b["args"], ensure_ascii=False)})
                elif b["type"] == "thinking" and (item := _reasoning(b, provider)):
                    items.append(item)
        else:
            parts = [_part(b, "output") for b in m["content"] if b["type"] in ("text", "image")]
            only_text = all(p["type"] == "input_text" for p in parts)
            items.append({"type": "function_call_output", "call_id": m["call_id"],
                          "output": "".join(p["text"] for p in parts) if only_text else parts})
    return items


def _part(b: Block, where: str = "message") -> dict[str, Any]:
    if b["type"] == "text":
        return {"type": "input_text", "text": b["text"]}
    image = {"type": "input_image", "image_url": f"data:{b['mime']};base64,{b['data']}"}
    return image | ({"detail": "auto"} if where == "message" else {})


def _reasoning(b: Thinking, provider: str) -> dict[str, Any] | None:
    """Encrypted reasoning goes back only to the provider that produced it, and not after history edits."""
    meta = b.get("meta", {})
    if b["provider"] != provider or b.get("stale") or "encrypted" not in meta:
        return None
    summary = [{"type": "summary_text", "text": b["text"]}] if b["text"] else []
    return {"type": "reasoning", "id": meta["id"], "summary": summary, "encrypted_content": meta["encrypted"]}


STOPS: dict[str, Stop] = {"max_output_tokens": "length", "content_filter": "error"}


def from_output(resp: Any, provider: str, model: str) -> Response:
    content: list[Block] = []
    for item in resp.output:
        if item.type == "reasoning":
            summary = "\n\n".join(s.text for s in item.summary or [])
            content.append(Thinking(type="thinking", text=summary, provider=provider,
                                    meta={"id": item.id, "encrypted": item.encrypted_content}))
        elif item.type == "message":
            content += [text(c.text if c.type == "output_text" else f"[refused] {c.refusal}") for c in item.content]
        elif item.type == "function_call":
            call = ToolCall(type="tool_call", id=item.call_id or new_id("call"), name=item.name, args={})
            try:
                args = json.loads(item.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
                call["args"] = args
            except ValueError as e:
                call["error"] = f"arguments are not a valid JSON object ({e}): {item.arguments[:200]}"
            content.append(call)
    stop: Stop = "tool_use" if any(b["type"] == "tool_call" for b in content) else "end"
    if resp.status == "incomplete":
        stop = STOPS.get(resp.incomplete_details.reason if resp.incomplete_details else "", "length")
    u = resp.usage
    usage = {"input": u.input_tokens, "output": u.output_tokens,
             "cache_read": u.input_tokens_details.cached_tokens if u.input_tokens_details else 0} if u else {}
    return Response(content, stop, usage, {"provider": provider, "model": model})
