"""U3 harness uses native source/export tools and only the frozen document reads."""
import dataclasses
import importlib.util
import json
from pathlib import Path

import pytest
from conftest import FakeModel, call, reply
from test_connectors import lark as lark

from nano_workbuddy.config import load
from nano_workbuddy.connectors.feishu import FeishuArgs

spec = importlib.util.spec_from_file_location("feishu_trial", Path(__file__).parents[1] / "evals/run_feishu_trial.py")
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


def document(token):
    url = f"https://example.feishu.cn/docx/{token}"
    return {"url": url, "command": ["docs", "+fetch", "--doc", url, "--scope", "section",
                                    "--start-block-id", "b1", "--revision-id", "7", "--doc-format", "markdown"]}


@pytest.mark.parametrize("mutation", ["document", "revision", "scope", "calendar", "write"])
def test_only_frozen_resources_and_read_parameters_are_permitted(mutation):
    d = document("d1")
    assert trial.permitted(FeishuArgs(command=d["command"]), [d])
    command = list(d["command"])
    if mutation == "write":
        args = FeishuArgs(operation={"kind": "create_document", "title": "Forbidden", "content": "NO"})
    else:
        if mutation == "document":
            command[3] = "https://example.feishu.cn/docx/other"
        elif mutation == "revision":
            command[9] = "8"
        elif mutation == "scope":
            command[5] = "full"
        else:
            command = ["calendar", "+agenda"]
        args = FeishuArgs(command=command)
    assert not trial.permitted(args, [d])


async def test_native_feishu_read_to_two_formats_without_real_services(tmp_path, monkeypatch, lark):
    root = tmp_path / "workspace"
    root.mkdir()
    chosen = load(tmp_path / "source-config").model()
    provider = dataclasses.asdict(chosen.provider)
    provider.pop("api_key")
    model_data = dataclasses.asdict(chosen)
    model_data.pop("provider")
    docs = [document("d1"), document("d2")]
    (tmp_path / "public.json").write_text(json.dumps({"provider": provider, "model": model_data,
        "documents": docs, "cli": str(lark), "task": "Synthetic U3 task"}))
    model = FakeModel(reply(None, *(call("feishu", command=d["command"]) for d in docs)),
        reply(None, call("create_document", path="meeting-prep.md", content="# Synthetic meeting\nFacts [S1][S2]"),
              call("export_document", source="meeting-prep.md", path="meeting-prep.docx")), reply("done"))

    class Client:
        max_retries = 0

        def with_options(self, **kwargs):
            assert kwargs == {"max_retries": 0}
            return self

        async def close(self):
            pass

    model.client = Client()
    monkeypatch.setattr(trial.llm, "connect", lambda spec, key: model.connect(spec))
    monkeypatch.setenv("NWB_U3_MODEL_KEY", "fake-secret-not-in-log")
    await trial.work(tmp_path)
    record = json.loads((tmp_path / "worker-result.json").read_text())
    assert record["status"] == "ok" and record["model_calls"] == 3
    assert (root / "meeting-prep.docx").is_file()
    assert (root / "meeting-prep.md").read_text().count("## 参考文献") == 1
    calls = [json.loads(s) for s in (root / ".lark-calls.jsonl").read_text().splitlines()]
    assert len(calls) == 2 and all(c[-1] == "--as=user" for c in calls)
    names = {t["name"] for t in model.requests[0]["tools"]}
    assert not names & {"bash", "web_fetch", "web_search", "recall", "read_record", "ask_user"}
    logs = "\n".join(p.read_text() for p in (tmp_path / "home/sessions").glob("*/*.jsonl"))
    assert "fake-secret-not-in-log" not in logs and '"state": "inspected"' in logs
