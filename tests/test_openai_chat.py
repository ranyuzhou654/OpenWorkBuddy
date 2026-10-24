from types import SimpleNamespace as NS

from nano_workbuddy.llm import Delta, Response
from nano_workbuddy.llm.openai_chat import assemble, to_chat


def chunk(content=None, reasoning=None, tool_calls=None, finish=None, usage=None):
    delta = NS(content=content, tool_calls=tool_calls)
    if reasoning:
        delta.reasoning_content = reasoning
    return NS(choices=[NS(delta=delta, finish_reason=finish)] if not usage else [], usage=usage)


def tc(index, id=None, name=None, args=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=args))


async def aiter(items):
    for i in items:
        yield i


async def collect(chunks):
    return [x async for x in assemble(aiter(chunks), "deepseek", "m")]


async def test_assembles_text_thinking_tool_calls_and_usage():
    out = await collect([
        chunk(reasoning="let me "), chunk(reasoning="think"),
        chunk(content="Hi"),
        chunk(tool_calls=[tc(0, "call_1", "read", '{"pa'), tc(1, "call_2", "bash", '{"command"')]),
        chunk(tool_calls=[tc(0, args='th": "a"}'), tc(1, args=': "ls"}')], finish="tool_calls"),
        chunk(usage=NS(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=None,
                       prompt_cache_hit_tokens=80)),
    ])
    deltas = [x for x in out if isinstance(x, Delta)]
    assert [(d.kind, d.text) for d in deltas] == [("thinking", "let me "), ("thinking", "think"), ("text", "Hi")]
    resp = out[-1]
    assert isinstance(resp, Response) and resp.stop == "tool_use"
    assert resp.usage == {"input": 100, "output": 20, "cache_read": 80}
    thinking, txt, c1, c2 = resp.content
    assert thinking == {"type": "thinking", "text": "let me think", "provider": "deepseek",
                        "meta": {"field": "reasoning_content"}}
    assert txt == {"type": "text", "text": "Hi"}
    assert (c1["id"], c1["name"], c1["args"]) == ("call_1", "read", {"path": "a"})
    assert c2["args"] == {"command": "ls"}


async def test_invalid_arguments_are_flagged_not_dropped():
    out = await collect([chunk(tool_calls=[tc(0, "c", "read", '{"path": ')], finish="length")])
    resp = out[-1]
    assert resp.stop == "length"
    assert "error" in resp.content[0] and resp.content[0]["args"] == {}


async def test_stream_without_finish_reason_is_an_error():
    """Complete-looking JSON arguments do not make a cut-off stream trustworthy (F05)."""
    out = await collect([chunk(content="Writing", tool_calls=[tc(0, "c", "write", '{"path": "x", "content": "y"}')])])
    resp = out[-1]
    assert resp.stop == "error" and resp.content[1]["args"] == {"path": "x", "content": "y"}
    out = await collect([chunk(content="plain text, no terminal chunk")])
    assert out[-1].stop == "error"


def test_to_chat_replays_thinking_only_to_its_provider():
    thinking = {"type": "thinking", "text": "hmm", "provider": "deepseek", "meta": {"field": "reasoning_content"}}
    call = {"type": "tool_call", "id": "c1", "name": "read", "args": {"path": "中文"}}
    msgs = [
        {"role": "user", "content": [{"type": "text", "text": "q"}]},
        {"role": "assistant", "content": [thinking, call]},
        {"role": "tool", "call_id": "c1", "content": [{"type": "text", "text": "data"},
                                                     {"type": "image", "mime": "image/png", "data": "AAA"}]},
    ]
    ds = to_chat("sys", msgs, "deepseek")
    assert ds[0] == {"role": "system", "content": "sys"}
    assert ds[2]["reasoning_content"] == "hmm" and ds[2]["content"] is None
    assert ds[2]["tool_calls"][0]["function"]["arguments"] == '{"path": "中文"}'
    assert ds[3] == {"role": "tool", "tool_call_id": "c1", "content": "data"}
    assert ds[4]["role"] == "user" and ds[4]["content"][1]["image_url"]["url"] == "data:image/png;base64,AAA"
    assert "reasoning_content" not in to_chat("sys", msgs, "other")[2]
