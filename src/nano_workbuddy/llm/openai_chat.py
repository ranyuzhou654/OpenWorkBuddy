"""OpenAI Chat Completions protocol: DeepSeek, Qwen, Kimi, GLM, OpenRouter, Ollama, vLLM, ..."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

import openai

from ..config import ModelSpec, secret
from ..types import Block, Message, Stop, Thinking, ToolCall, new_id, text
from . import ContextOverflow, Delta, Response

REASONING_FIELD = "reasoning_content"  # DeepSeek, Kimi, Qwen, GLM all use this name
STOPS: dict[str, Stop] = {"stop": "end", "tool_calls": "tool_use", "length": "length", "content_filter": "error"}


class OpenAIChat:
    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self.client = openai.AsyncOpenAI(base_url=spec.provider.base_url, api_key=secret(spec.provider.api_key),
                                         max_retries=3)

    async def stream(self, system: str, messages: list[Message], tools: list[dict[str, Any]],
                     tool_choice: str | None = None) -> AsyncIterator[Delta | Response]:
        req: dict[str, Any] = {
            "model": self.spec.id,
            "messages": to_chat(system, messages, self.spec.provider.name),
            "max_tokens": self.spec.max_output,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            req["tools"] = [{"type": "function", "function": t} for t in tools]
            if tool_choice:
                req["tool_choice"] = tool_choice
        if self.spec.params:
            req["extra_body"] = self.spec.params
        try:
            chunks = await self.client.chat.completions.create(**req)
        except openai.BadRequestError as e:
            msg = str(e).lower()
            if e.code == "context_length_exceeded" or "context length" in msg or "maximum context" in msg:
                raise ContextOverflow(str(e)) from e
            raise
        async for item in assemble(chunks, self.spec.provider.name, self.spec.id):
            yield item


def to_chat(system: str, messages: list[Message], provider: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    images: list[dict[str, Any]] = []  # images from tool results; chat tool messages are text-only

    def flush_images() -> None:
        if images:
            out.append({"role": "user", "content": [{"type": "text", "text": "Images from the tool results above:"},
                                                    *images]})
            images.clear()

    for m in messages:
        if m["role"] != "tool":
            flush_images()
        match m["role"]:
            case "user":
                out.append({"role": "user", "content": _user_content(m["content"])})
            case "assistant":
                msg: dict[str, Any] = {"role": "assistant",
                                       "content": "".join(b["text"] for b in m["content"] if b["type"] == "text")}
                calls = [b for b in m["content"] if b["type"] == "tool_call"]
                if calls:
                    msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"],
                                                       "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                                         for c in calls]
                    msg["content"] = msg["content"] or None
                # D8: thinking goes back only to the provider that produced it (DeepSeek requires it with tools)
                for b in m["content"]:
                    if b["type"] == "thinking" and b["provider"] == provider and "field" in b.get("meta", {}):
                        msg[b["meta"]["field"]] = b["text"]
                out.append(msg)
            case "tool":
                out.append({"role": "tool", "tool_call_id": m["call_id"],
                            "content": "".join(b["text"] for b in m["content"] if b["type"] == "text")})
                images.extend(_image_part(b) for b in m["content"] if b["type"] == "image")
    flush_images()
    return out


def _image_part(b: Block) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": f"data:{b['mime']};base64,{b['data']}"}}


def _user_content(blocks: list[Block]) -> str | list[dict[str, Any]]:
    if all(b["type"] == "text" for b in blocks):
        return "".join(b["text"] for b in blocks)
    return [{"type": "text", "text": b["text"]} if b["type"] == "text" else _image_part(b)
            for b in blocks if b["type"] in ("text", "image")]


async def assemble(chunks: AsyncIterable[Any], provider: str, model: str) -> AsyncIterator[Delta | Response]:
    """Fold streamed chunks into deltas plus one final Response. Tool calls are only emitted complete."""
    texts: list[str] = []
    thinking: list[str] = []
    calls: dict[int, dict[str, str]] = {}
    stop: Stop = "end"
    usage: dict[str, int] = {}
    async for chunk in chunks:
        if chunk.usage:
            usage = _usage(chunk.usage)
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        if r := getattr(delta, REASONING_FIELD, None):
            thinking.append(r)
            yield Delta("thinking", r)
        if delta.content:
            texts.append(delta.content)
            yield Delta("text", delta.content)
        for tc in delta.tool_calls or []:
            slot = calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
            slot["id"] = tc.id or slot["id"]
            if tc.function:
                slot["name"] += tc.function.name or ""
                slot["args"] += tc.function.arguments or ""
        if choice.finish_reason:
            stop = STOPS.get(choice.finish_reason, "end")

    content: list[Block] = []
    if thinking:
        content.append(Thinking(type="thinking", text="".join(thinking), provider=provider,
                                meta={"field": REASONING_FIELD}))
    if texts:
        content.append(text("".join(texts)))
    for _, slot in sorted(calls.items()):
        call = ToolCall(type="tool_call", id=slot["id"] or new_id("call"), name=slot["name"], args={})
        try:
            args = json.loads(slot["args"] or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
            call["args"] = args
        except ValueError as e:
            call["error"] = f"arguments are not a valid JSON object ({e}): {slot['args'][:200]}"
        content.append(call)
    if calls and stop == "end":
        stop = "tool_use"
    yield Response(content, stop, usage, {"provider": provider, "model": model})


def _usage(u: Any) -> dict[str, int]:
    details = getattr(u, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) or getattr(u, "prompt_cache_hit_tokens", None) or 0
    return {"input": u.prompt_tokens or 0, "output": u.completion_tokens or 0, "cache_read": cached}
