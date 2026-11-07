"""The Responses adapter against a local server that speaks its streaming protocol, so the real SDK parses it."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nano_workbuddy.config import ModelSpec, Provider
from nano_workbuddy.llm import ContextOverflow, Delta, Response
from nano_workbuddy.llm.openai_responses import OpenAIResponses, to_input
from nano_workbuddy.types import Message, text

FINAL = {
    "id": "resp_1", "object": "response", "created_at": 0, "model": "gpt-test", "status": "completed",
    "error": None, "incomplete_details": None, "parallel_tool_calls": True, "tool_choice": "auto", "tools": [],
    "output": [
        {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "plan"}],
         "encrypted_content": "ENC=="},
        {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": "Hello", "annotations": []}]},
        {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "read", "arguments": '{"path": "a.txt"}',
         "status": "completed"},
    ],
    "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
              "input_tokens_details": {"cached_tokens": 80}, "output_tokens_details": {"reasoning_tokens": 5}},
}
EVENTS = [
    {"type": "response.reasoning_summary_text.delta", "delta": "plan", "item_id": "rs_1", "output_index": 0,
     "summary_index": 0, "sequence_number": 1},
    {"type": "response.output_text.delta", "delta": "Hello", "item_id": "msg_1", "output_index": 1,
     "content_index": 0, "logprobs": [], "sequence_number": 2},
    {"type": "response.completed", "response": FINAL, "sequence_number": 3},
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
                self.wfile.write(json.dumps({"error": {"message": "Your input exceeds the context window",
                                                       "type": "invalid_request_error",
                                                       "code": "context_length_exceeded"}}).encode())
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
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1", bodies
    srv.shutdown()


def model(url):
    spec = ModelSpec("gpt", Provider("openai", "openai-responses", url, "env:X"), "gpt-test", 400_000, 32_000,
                     {"reasoning": {"effort": "low", "summary": "auto"}})
    return OpenAIResponses(spec, "sk-test")


async def test_stream_and_final_response(server):
    url, bodies = server
    tools = [{"name": "read", "description": "Read", "parameters": {"type": "object", "properties": {}}}]
    items = [x async for x in model(url).stream("SYS", [Message(role="user", content=[text("hi")])], tools)]
    assert [(d.kind, d.text) for d in items if isinstance(d, Delta)] == [("thinking", "plan"), ("text", "Hello")]
    final = items[-1]
    assert isinstance(final, Response) and final.stop == "tool_use"
    thinking, hello, call = final.content
    assert thinking == {"type": "thinking", "text": "plan", "provider": "openai",
                        "meta": {"id": "rs_1", "encrypted": "ENC=="}}
    assert hello == text("Hello") and call["id"] == "call_1" and call["args"] == {"path": "a.txt"}
    assert final.usage == {"input": 100, "output": 20, "cache_read": 80}

    body = bodies[0]
    assert body["instructions"] == "SYS" and body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"] and body["reasoning"]["effort"] == "low"
    assert body["tools"][0] == {"type": "function", "name": "read", "description": "Read",
                                "parameters": {"type": "object", "properties": {}}, "strict": False}


def test_history_round_trip():
    reasoning = {"type": "thinking", "text": "plan", "provider": "openai", "meta": {"id": "rs_1", "encrypted": "E"}}
    call = {"type": "tool_call", "id": "call_1", "name": "read", "args": {"path": "a"}}
    image = {"type": "image", "mime": "image/png", "data": "AAA"}
    msgs = [Message(role="user", content=[text("hi")]),
            Message(role="assistant", content=[reasoning, text("ok"), call]),
            Message(role="tool", content=[text("file"), image], call_id="call_1"),
            Message(role="assistant", content=[{**reasoning, "stale": True}, {**reasoning, "provider": "other"}])]
    items = to_input(msgs, "openai")
    assert items[0] == {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    assert items[1] == {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "plan"}],
                        "encrypted_content": "E"}
    assert items[2] == {"role": "assistant", "content": "ok"}
    assert items[3] == {"type": "function_call", "call_id": "call_1", "name": "read", "arguments": '{"path": "a"}'}
    assert items[4]["output"][1] == {"type": "input_image", "image_url": "data:image/png;base64,AAA"}
    assert len(items) == 5  # stale reasoning and another provider's reasoning are not replayed


async def test_context_overflow(server):
    url, _ = server
    with pytest.raises(ContextOverflow):
        async for _ in model(url).stream("SYS", [Message(role="user", content=[text("too long")])], []):
            pass


@pytest.mark.parametrize(("status", "reason", "expected"), [
    ("incomplete", "max_output_tokens", "length"),
    ("incomplete", "content_filter", "error"),
    ("incomplete", "future_reason", "error"),
    ("failed", None, "error"),
    ("future_status", None, "error"),
    ("refusal", None, "error"),
])
async def test_abnormal_terminal_responses_keep_usage_but_never_authorize_tools(server, monkeypatch,
                                                                               status, reason, expected):
    final = copy.deepcopy(FINAL)
    final["status"] = status
    if status == "incomplete":
        final["incomplete_details"] = {"reason": reason}
    elif status == "failed":
        final["error"] = {"code": "server_error", "message": "failed"}
    elif status == "refusal":
        final["status"] = "completed"
        final["output"][1]["content"] = [{"type": "refusal", "refusal": "refused"}]
    terminal = "response.failed" if status == "failed" else (
        "response.incomplete" if status == "incomplete" else "response.completed")
    monkeypatch.setattr(__import__(__name__), "EVENTS", [{"type": terminal, "response": final, "sequence_number": 1}])
    messages = [Message(role="user", content=[text("hi")])]
    response = [item async for item in model(server[0]).stream("SYS", messages, [])][-1]
    assert response.stop == expected
    assert response.usage == {"input": 100, "output": 20, "cache_read": 80}
    assert response.meta["raw_usage"]["output_tokens_details"]["reasoning_tokens"] == 5


async def test_missing_final_response_raises_without_returning_tool_calls(server, monkeypatch):
    monkeypatch.setattr(__import__(__name__), "EVENTS", EVENTS[:-1])
    with pytest.raises(RuntimeError, match="without a final response"):
        async for _ in model(server[0]).stream("SYS", [Message(role="user", content=[text("hi")])], []):
            pass
