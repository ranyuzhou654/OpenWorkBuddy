"""Research orchestration checks with fake providers; no real search or model requests."""
import dataclasses
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from conftest import FakeModel, call, reply

from nano_workbuddy import usage
from nano_workbuddy.config import load
from nano_workbuddy.eval.schema import file_hash
from nano_workbuddy.search import Hit, SearchError

SCRIPT = Path(__file__).parents[1] / "evals/run_research_trial.py"
spec = importlib.util.spec_from_file_location("research_trial", SCRIPT)
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


class FakeSearch:
    async def search(self, *args):
        return [Hit("Synthetic source", "https://example.test/official", "Synthetic snippet")]


async def test_search_cap_records_started_requests_without_a_fourth_network_call(tmp_path):
    backend = trial.CountedSearch(FakeSearch(), tmp_path / "search.jsonl")
    for _ in range(3):
        await backend.search("synthetic query", 3, None, "general")
    with pytest.raises(SearchError, match="limit"):
        await backend.search("fourth query", 3, None, "general")
    events = usage.events(backend.ledger)
    assert backend.calls == 3 and sum(e["type"] == "search.started" for e in events) == 3
    assert sum(e["type"] == "search.response" for e in events) == 3


async def test_research_worker_fetches_and_retains_evidence_without_personal_tools(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "project-context.md").write_text("Synthetic project scope")
    chosen = load(tmp_path / "config-source").model()
    provider = dataclasses.asdict(chosen.provider)
    provider.pop("api_key")
    model_data = dataclasses.asdict(chosen)
    model_data.pop("provider")
    (tmp_path / "public.json").write_text(json.dumps({"provider": provider, "model": model_data,
                                                    "task": "Synthetic research task"}))
    model = FakeModel(
        reply(None, call("read", path="project-context.md"), call("web_search", query="synthetic query")),
        reply(None, call("web_fetch", url="https://example.test/official")),
        reply(None, call("create_document", path="research-brief.md", content="# Synthetic brief\nMock fact [S2]"),
              call("export_document", source="research-brief.md", path="research-brief.docx")),
        reply("done"))

    class Client:
        max_retries = 0

        def with_options(self, **kwargs):
            assert kwargs == {"max_retries": 0}
            return self

        async def close(self):
            pass

    model.client = Client()
    monkeypatch.setattr(trial.llm, "connect", lambda spec, key: model.connect(spec))
    monkeypatch.setattr(trial.search, "Tavily", lambda key: FakeSearch())
    factory = httpx.AsyncClient
    transport = httpx.MockTransport(lambda r: httpx.Response(200, headers={"Content-Type": "text/html"},
        text="<title>Synthetic source</title><p>Mock fact from a local test fixture</p>"))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: factory(**kwargs, transport=transport))
    monkeypatch.setenv("NWB_RESEARCH_MODEL_KEY", "fake-model-credential")
    monkeypatch.setenv("NWB_RESEARCH_SEARCH_KEY", "fake-search-credential")
    await trial.work(tmp_path)
    result = json.loads((tmp_path / "worker-result.json").read_text())
    assert result["status"] == "ok" and result["model_calls"] == 4 and result["search_requests"] == 1
    assert "https://example.test/official" in (root / "research-brief.md").read_text()
    names = {t["name"] for t in model.requests[0]["tools"]}
    assert {"web_search", "web_fetch", "create_document", "export_document"} <= names
    assert (root / "research-brief.docx").is_file()
    assert not names & {"feishu", "bash", "recall", "read_record", "skill"}
    logs = "\n".join(p.read_text() for p in (tmp_path / "home/sessions").glob("*/*.jsonl"))
    assert "fake-model-credential" not in logs and "fake-search-credential" not in logs


def test_research_input_reuses_baseline_and_rejects_mutation(tmp_path):
    baseline = tmp_path / "baseline"
    root = baseline / "U2-1/workspace"
    root.mkdir(parents=True)
    context, public = root / "project-context.md", root.parent / "public.json"
    context.write_text("Original project only")
    public.write_text(json.dumps({"task": "Original task only"}))
    (baseline / "freeze.json").write_text(json.dumps({"context_sha256": file_hash(context),
        "public_sha256": file_hash(public), "inputs": {"original.md": "original hash"}}))
    assert trial.research_input(tmp_path / "nonexistent-current-repo", baseline) == (
        "Original project only", "Original task only", {"original.md": "original hash"})
    context.write_text("Later answer leak")
    with pytest.raises(ValueError, match="integrity"):
        trial.research_input(tmp_path, baseline)
