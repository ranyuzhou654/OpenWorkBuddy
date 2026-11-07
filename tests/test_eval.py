import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from nano_workbuddy.config import ModelSpec, Provider
from nano_workbuddy.eval import runner
from nano_workbuddy.eval.cost import calculate
from nano_workbuddy.eval.grade import grade, verdict
from nano_workbuddy.eval.report import paired_interval, summarize
from nano_workbuddy.eval.schema import Case, Check, Limits, Suite, file_hash


def case(**kwargs):
    return Case(id="D1", group="office", template_family="test", inputs=["source.txt"], turns=["write answer.json"],
                checks=[{"id": "answer", "kind": "json", "path": "answer.json", "expected": {"amount": 42}}],
                **kwargs)


def dataset(tmp_path, **kwargs):
    root = tmp_path / "data"
    root.mkdir()
    (root / "inputs").mkdir()
    (root / "inputs" / "source.txt").write_text("amount: 42")
    suite = Suite(id="test-dev", split="development", description="test", cases=[case(**kwargs)])
    path = root / "suite.json"
    path.write_text(suite.model_dump_json())
    return path, suite


def test_freeze_rejects_modified_answers_and_inputs(tmp_path):
    path, suite = dataset(tmp_path)
    freeze = tmp_path / "freeze.json"
    runner.freeze(path, freeze)
    runner.verify_freeze(path, freeze)
    (path.parent / "inputs" / "source.txt").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        runner.verify_freeze(path, freeze)
    (path.parent / "inputs" / "source.txt").write_text("amount: 42")
    suite.cases[0].checks[0].expected = {"amount": 43}
    path.write_text(suite.model_dump_json())
    with pytest.raises(ValueError, match="changed"):
        runner.verify_freeze(path, freeze)


def test_grader_checks_facts_separately_from_structure_and_preserves_pending(tmp_path):
    (tmp_path / "source.txt").write_text("original")
    (tmp_path / "answer.json").write_text('{"amount": 41}')
    checks = grade(case(), tmp_path, {"source.txt": file_hash(tmp_path / "source.txt")})
    assert checks[0]["ok"] is False and checks[1]["ok"] is True
    assert verdict(checks, [{"ok": True}], "ok") is False
    assert verdict([{"ok": True}], [{"ok": None}], "ok") is None
    assert verdict([{"ok": True}], [], "error") is False
    with pytest.raises(ValueError):
        case().model_validate({**case().model_dump(), "inputs": ["../answers.json"]})


def test_numeric_grader_ignores_thousands_format_but_not_wrong_amounts(tmp_path):
    c = case()
    c.checks = [Check(id="money", kind="numbers", path="report.md", expected=[2160, 2289.6, 63.6])]
    (tmp_path / "report.md").write_text("含税 2,289.60 元，不含税2,160, 差额63.60. Done.")
    assert grade(c, tmp_path, {})[0]["ok"] is True
    (tmp_path / "report.md").write_text("含税22,289.60元，不含税21,600元，差额163.60元。")
    assert grade(c, tmp_path, {})[0]["ok"] is False


def test_cost_never_double_counts_cache_or_reasoning_and_keeps_missing_unknown():
    start = dict(type="model.started", call_id="a", provider="p", model="m", sdk_max_retries=0)
    response = dict(type="model.response", call_id="a", usage_complete=True,
                    usage=dict(input=100, output=20, cache_read=80))
    price = dict(provider="p", model="m", currency="CNY", as_of="2026-10-25", source="test only",
                 per_million=dict(input=2, output=4, cache_read=1))
    assert calculate([start, response], price)["amount"] == pytest.approx(.0002)
    assert calculate([start, response], None)["amount"] is None
    assert calculate([start], price)["amount"] is None
    assert not calculate([start | {"sdk_max_retries": 3}, response], price)["complete"]
    assert not calculate([start, response | {"usage_complete": False}], price)["complete"]
    assert not calculate([start, response | {"usage": dict(input=10, output=1, cache_read=80)}], price)["complete"]


def test_repeated_trial_statistics_keep_failures_and_cluster_by_task():
    rows = [dict(case_id=c, group="office", arm=a, trial_index=i, passed=(c == "A" or a == "B1"),
                 cost={"complete": True, "amount": 1, "currency": "CNY"})
            for c in ("A", "B") for a in ("B0", "B1") for i in (1, 2, 3)]
    result = summarize(rows, 3)
    assert result["arms"]["B0"]["completion_rate"] == .5
    assert result["arms"]["B0"]["pass3"] == .5
    assert result["arms"]["B0"]["cost_per_success"] == 2
    assert paired_interval(rows)["difference_pp"] == 50
    assert paired_interval(rows)["tasks"] == 2
    rows[0]["passed"] = None
    assert summarize(rows, 3)["arms"]["B0"]["completion_rate"] is None
    assert paired_interval(rows) is None


@pytest.mark.parametrize("call_limit", [1, 12])
async def test_runner_uses_real_subprocesses_isolates_answers_and_keeps_evidence(tmp_path, call_limit):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            assert "checks" not in json.dumps(body)
            tool_turn = len([m for m in body["messages"] if m["role"] == "tool"]) == 0
            delta = {"tool_calls": [{"index": 0, "id": "c", "type": "function", "function": {
                "name": "write", "arguments": json.dumps({"path": "answer.json", "content": '{"amount":42}'})}}]
            } if tool_turn else {"content": "done"}
            chunk = dict(id="r", model="test-wire", object="chat.completion.chunk", created=0,
                         choices=[dict(index=0, delta=delta, finish_reason="tool_calls" if tool_turn else "stop")],
                         usage={"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("x-request-id", "req-test")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode())

        def log_message(self, *args):
            pass

    path, suite = dataset(tmp_path, rubric=[dict(id="manual", criterion="independent review")])
    suite.limits = Limits(model_calls=call_limit)
    path.write_text(suite.model_dump_json())
    frozen = tmp_path / "freeze.json"
    runner.freeze(path, frozen)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    provider = Provider("local-test", "openai-chat", f"http://127.0.0.1:{server.server_port}", "env:FAKE")
    spec = ModelSpec("test", provider, "test-model", 100_000, 1000)
    output = tmp_path / "results"
    try:
        result = await runner.run(path, frozen, output, spec, "not-a-real-key")
    finally:
        server.shutdown()
    assert result["observed_trials"] == 2 and len(requests) == (2 if call_limit == 1 else 4)
    for record in runner.records(output):
        assert record["status"] == ("error" if call_limit == 1 else "ok")
        assert record["passed"] is (False if call_limit == 1 else None)
        assert all(c["ok"] for c in record["checks"])
        assert record["usage"]["input_total"] == (100 if call_limit == 1 else 200)
        assert record["model_calls"] == (1 if call_limit == 1 else 2)
        if call_limit == 1:
            assert record["failure_kind"] == "budget"
        assert record["cost"]["amount"] is None
        assert record["config_sha256"] == runner.records(output)[0]["config_sha256"]
        trial = Path(record["environment_id"])
        assert "checks" not in json.loads((trial / "public.json").read_text())
        assert json.loads((trial / "public.json").read_text())["inputs"] == ["source.txt"]
        events = json.loads((trial / "calls.json").read_text())
        assert events[1]["request_id"] == "req-test"
        assert record["arm"] not in str(trial / "workspace")
    assert len({r["system_prompt_sha256"] for r in runner.records(output)}) == 1
    assert len({r["config_sha256"] for r in runner.records(output)}) == 1
    assert (output / "source.zip").is_file()
    # Correct a development grader without changing or rerunning its original Agent evidence.
    old_requests = len(requests)
    original = (output / "trials.jsonl").read_bytes()
    corrections = tmp_path / "corrections.json"
    corrections.write_text(json.dumps({"reason": "test grader correction", "overrides": {
        "D1": {"answer": {"expected": {"amount": 41}}}}}))
    runner.regrade(output, corrections)
    assert len(requests) == old_requests and (output / "trials.jsonl").read_bytes() == original
    assert all(r["passed"] is False for r in runner.records(output))
    runner.blind_packets(output, tmp_path / "packets")
    assert len(list((tmp_path / "packets").iterdir())) == 2
    assert "B0" not in "".join(str(p) for p in (tmp_path / "packets").rglob("*"))
    assert "public.json" not in "".join(str(p) for p in (tmp_path / "packets").rglob("*"))
    with pytest.raises(FileExistsError):
        await runner.run(path, frozen, output, spec, "not-a-real-key")


async def test_timeout_kills_worker_and_keeps_unknown_usage_in_denominator(tmp_path):
    release = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(self.rfile.read(int(self.headers["Content-Length"])))
            release.wait(10)  # a provider that accepted the request but never finished

        def log_message(self, *args):
            pass

    path, suite = dataset(tmp_path)
    suite.limits = Limits(seconds=2)
    path.write_text(suite.model_dump_json())
    frozen = tmp_path / "freeze.json"
    runner.freeze(path, frozen)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    spec = ModelSpec("test", Provider("local", "openai-chat", f"http://127.0.0.1:{server.server_port}", "env:FAKE"),
                     "test-model", 100_000, 1000)
    output = tmp_path / "timeout-results"
    try:
        result = await runner.run(path, frozen, output, spec, "not-a-real-key")
    finally:
        release.set()
        server.shutdown()
    assert len(requests) == 2 and result["observed_trials"] == 2
    for row in runner.records(output):
        assert row["status"] == "timeout" and row["passed"] is False
        assert row["usage"]["input_total"] is None and not row["cost"]["complete"]
        trial = Path(row["environment_id"])
        assert not (trial / "worker-result.json").exists()  # SIGKILL skipped the worker's finally block
        events = json.loads((trial / "calls.json").read_text())
        assert [e["type"] for e in events] == ["model.started"]
