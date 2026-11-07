"""The Anthropic adapter against a local server that speaks the Messages streaming protocol,
so the real SDK parses real SSE events."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nano_workbuddy.config import ModelSpec, Provider
from nano_workbuddy.llm import ContextOverflow, Delta, Response
from nano_workbuddy.llm.anthropic import AnthropicMessages

EVENTS = [
    {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test", "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 90,
                  "cache_creation_input_tokens": 5}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "let me think"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-123"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hello"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "content_block_start", "index": 2,
     "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {}}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"path": "a.'}},
    {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": 'txt"}'}},
    {"type": "content_block_stop", "index": 2},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
     "usage": {"output_tokens": 42}},
    {"type": "message_stop"},
]


@pytest.fixture
def server():
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            bodies.append(body)
            if "too long" in json.dumps(body):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"type": "error", "error": {
                    "type": "invalid_request_error", "message": "prompt is too long: 300000 tokens > 200000 maximum"}
                }).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for e in EVENTS:
                self.wfile.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", bodies
    srv.shutdown()


@pytest.fixture
def model(server):
    provider = Provider("claude", "anthropic", server[0], "secret:claude")
    return AnthropicMessages(ModelSpec("c", provider, "claude-test", 200_000, 8_000), "test-key")


TOOLS = [{"name": "read", "description": "Read a file", "parameters": {"type": "object", "properties": {}}}]


async def test_stream_is_translated_to_neutral_blocks(model, server):
    items = [x async for x in model.stream("SYS", [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                         TOOLS)]
    deltas = [(d.kind, d.text) for d in items if isinstance(d, Delta)]
    assert deltas == [("thinking", "let me think"), ("text", "Hello")]
    resp = items[-1]
    assert isinstance(resp, Response) and resp.stop == "tool_use"
    thinking, hello, tool = resp.content
    assert thinking == {"type": "thinking", "text": "let me think", "provider": "claude",
                        "meta": {"signature": "sig-123"}}
    assert hello == {"type": "text", "text": "Hello"}
    assert (tool["id"], tool["name"], tool["args"]) == ("toolu_1", "read", {"path": "a.txt"})
    assert resp.usage == {"input": 105, "output": 42, "cache_read": 90, "cache_write": 5}


@pytest.mark.parametrize("tail", [1, 2])
async def test_missing_terminal_event_never_authorizes_tools(model, monkeypatch, tail):
    monkeypatch.setattr(__import__(__name__), "EVENTS", EVENTS[:-tail])
    response = [x async for x in model.stream("SYS", [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                               TOOLS)][-1]
    assert response.stop == "error"
    assert response.meta["usage_complete"] is False
    from nano_workbuddy.loop import FailCalls, next_step
    from nano_workbuddy.types import Entry

    step = next_step([Entry("a", None, "assistant", {"content": response.content, "stop": response.stop})])
    assert isinstance(step, FailCalls)


@pytest.mark.parametrize("reason", [None, "future_reason", "pause_turn", "refusal"])
async def test_nonfinal_or_unknown_stop_reason_is_blocked(model, monkeypatch, reason):
    events = copy.deepcopy(EVENTS)
    events[-2]["delta"]["stop_reason"] = reason
    monkeypatch.setattr(__import__(__name__), "EVENTS", events)
    response = [x async for x in model.stream("SYS", [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
                                               TOOLS)][-1]
    assert response.stop == "error"


async def test_request_shape_caching_and_replay(model, server):
    history = [
        {"role": "user", "content": [{"type": "text", "text": "read a.txt"}]},
        {"role": "assistant", "content": [
            {"type": "thinking", "text": "plan", "provider": "claude", "meta": {"signature": "sig-1"}},
            {"type": "thinking", "text": "other", "provider": "deepseek", "meta": {"field": "reasoning_content"}},
            {"type": "tool_call", "id": "t1", "name": "read", "args": {"path": "a.txt"}},
            {"type": "tool_call", "id": "t2", "name": "read", "args": {"path": "b.txt"}}]},
        {"role": "tool", "call_id": "t1", "content": [{"type": "text", "text": "A"}], "is_error": False},
        {"role": "tool", "call_id": "t2", "content": [], "is_error": True},
        {"role": "user", "content": [{"type": "text", "text": "<system-notice>n</system-notice>"}]},
    ]
    _ = [x async for x in model.stream("SYS", history, TOOLS, tool_choice="none")]
    body = server[1][-1]
    assert body["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert body["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
    assert body["tool_choice"] == {"type": "none"}
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]  # tool results merged
    assistant = body["messages"][1]["content"]
    assert assistant[0] == {"type": "thinking", "thinking": "plan", "signature": "sig-1"}  # own thinking replayed
    assert [b["type"] for b in assistant] == ["thinking", "tool_use", "tool_use"]  # other provider's dropped
    results = body["messages"][2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result", "text"]
    assert results[1]["content"] == [{"type": "text", "text": "(no output)"}] and results[1]["is_error"]
    assert results[-1]["cache_control"] == {"type": "ephemeral"}  # second breakpoint: end of history


async def test_prompt_too_long_becomes_context_overflow(model):
    with pytest.raises(ContextOverflow):
        _ = [x async for x in model.stream("SYS", [{"role": "user", "content": [{"type": "text", "text": "too long"}]}],
                                           [])]
