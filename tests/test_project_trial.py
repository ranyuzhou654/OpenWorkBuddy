"""Orchestration regressions only; these are not R-D user trials."""
import dataclasses
import importlib.util
import json
import re
from pathlib import Path

import pytest
from conftest import FakeModel, call, reply

from nano_workbuddy.config import load

SCRIPT = Path(__file__).resolve().parents[1] / "evals/run_project_trial.py"
spec = importlib.util.spec_from_file_location("project_trial", SCRIPT)
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


class Client:
    max_retries = 0

    def with_options(self, **kwargs):
        assert kwargs == {"max_retries": 0}
        return self

    async def close(self):
        pass


def setup(tmp_path, monkeypatch, case, model):
    directory = tmp_path / "trial"
    root = directory / "workspace"
    root.mkdir(parents=True)
    (root / "period-one.json").write_text('{"stage":"R-B","tests":340}')
    (root / "period-two.json").write_text('{"stage":"R-C","tests":372}')
    cfg = load(tmp_path / "config-source")
    chosen = cfg.model()
    provider = dataclasses.asdict(chosen.provider)
    provider.pop("api_key")
    model_data = dataclasses.asdict(chosen)
    model_data.pop("provider")
    (directory / "public.json").write_text(json.dumps(
        {"case": case, "provider": provider, "model": model_data}))
    model.client = Client()
    monkeypatch.setenv("NWB_TRIAL_API_KEY", "fake-not-a-credential")
    monkeypatch.setattr(trial.llm, "connect", lambda spec, _: model.connect(spec))
    return directory


@pytest.mark.parametrize("same", [False, True])
async def test_schedule_reopen_records_exactly_two_consumed_occurrences(tmp_path, monkeypatch, same):
    first = [
        reply(None, call("glob", pattern="progress.md"), call("read", path="current.json")),
        reply(None, call("create_document", path="progress.md", content="# R-B\n340 tests")),
        reply(None, call("task_result", outcome="changed", summary="first", evidence=["progress.md"])),
        reply("done")]
    second = [reply(None, call("read", path="current.json"), call("read", path="progress.md"))]
    if not same:
        second += [reply(None, call("create_document", path="progress.md",
                                   content="# R-C\n372 tests", overwrite=True))]
    second += [reply(None, call("task_result", outcome="unchanged" if same else "changed",
                                summary="compared current with previous",
                                evidence=["current.json" if same else "progress.md"])), reply("done")]
    case = {"id": "U5-test", "mode": "schedule", "same_period": same,
            "inputs": ["period-one.json", "period-two.json"], "turn": "Compare progress"}
    directory = setup(tmp_path, monkeypatch, case, FakeModel(*first, *second))
    await trial.work(directory)
    result = json.loads((directory / "worker-result.json").read_text())
    assert result["status"] == "ok", result
    assert all(c["ok"] for c in result["workflow_checks"]), result
    assert result["model_calls"] == len(first + second)
    occurrences = json.loads((directory / "occurrences.json").read_text())
    assert len({o["request_id"] for o in occurrences}) == 2
    logs = [json.loads(line) for p in (directory / "home/sessions").glob("*/*.jsonl")
            for line in p.read_text().splitlines()]
    assert not any(e.get("data", {}).get("is_error") for e in logs if e.get("type") == "tool_result")


async def test_restore_removes_original_home_then_reads_history_in_new_home(tmp_path, monkeypatch):
    def read_hit(messages):
        raw = json.dumps(messages, ensure_ascii=False)
        assert "Scope: 当前项目" in raw
        # Do not accidentally 'verify retrieval' by reading the new user's own question.
        hits = re.findall(r"history-note\.md[^\n]*?record=(rec_[0-9a-f]{24})", raw)
        assert hits, raw[-1800:]
        return reply(None, call("read_record", ref=hits[-1], start=0, end=1000))

    model = FakeModel(
        reply(None, call("read", path="current.json")),
        reply(None, call("create_document", path="history-note.md", content="# R-B\nR-B has 340 tests")),
        reply("done"),
        reply(None, call("recall", query="R-B")),
        read_hit,
        reply(None, call("create_document", path="recovered.md", content="# R-B\n340 tests recovered")),
        reply("done"))
    case = {"id": "U6-test", "mode": "restore", "inputs": ["period-one.json", "period-two.json"],
            "turn": "Record R-B", "followup": "Recall old R-B"}
    directory = setup(tmp_path, monkeypatch, case, model)
    await trial.work(directory)
    result = json.loads((directory / "worker-result.json").read_text())
    assert result["status"] == "ok", result
    assert all(c["ok"] for c in result["workflow_checks"]), result
    assert all(s["label"] == "当前项目" for s in result["record_scopes"])
    assert result["record_scopes"][-1]["session_ids"] == result["session_ids"]
    assert not (directory / "home").exists()
    assert (directory / "restored-home/index.db").exists()
    assert (directory / "workspace/recovered.md").exists()
    assert not (directory / "workspace/history-note.md").exists()
    logs = list((directory / "restored-home/sessions").glob("*/*.jsonl"))
    assert any('"read_record"' in p.read_text() for p in logs)
    assert any("R-B has 340 tests" in json.dumps(r["messages"], ensure_ascii=False)
               and "Characters 0-" in json.dumps(r["messages"], ensure_ascii=False) for r in model.requests)


async def test_total_call_limit_is_retained_across_reopened_workspaces(tmp_path, monkeypatch):
    # The first scheduled phase uses 8 calls; the second must stop at 4, not get a fresh quota of 12.
    responses = [reply(None, call("read", path="current.json")) for _ in range(5)]
    responses += [reply(None, call("create_document", path="progress.md", content="# R-B\n340 tests")),
                  reply(None, call("task_result", outcome="changed", summary="first", evidence=["progress.md"])),
                  reply("done")]
    responses += [reply(None, call("read", path="current.json")) for _ in range(6)]
    model = FakeModel(*responses)
    case = {"id": "U5-limit", "mode": "schedule", "same_period": False,
            "inputs": ["period-one.json", "period-two.json"], "turn": "Compare"}
    directory = setup(tmp_path, monkeypatch, case, model)
    await trial.work(directory)
    result = json.loads((directory / "worker-result.json").read_text())
    assert result["status"] == "error"
    assert result["model_calls"] == 12
    assert len(model.requests) == 12
    outcomes = json.loads((directory / "occurrences.json").read_text())
    assert outcomes[0]["outcome"] == "failed"


def test_preflight_rejects_changed_frozen_input(tmp_path):
    path = tmp_path / "inputs"
    path.mkdir()
    source = path / "a.json"
    source.write_text("{}")
    manifest = {"visual_qa": "inspected", "inputs": {"inputs/a.json": {"sha256": trial.sha(source)}}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    source.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="input changed"):
        trial.verify(tmp_path)
