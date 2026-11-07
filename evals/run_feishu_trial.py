"""One scoped U3 read-only trial. Model cannot select other documents, scopes or writes."""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from nano_workbuddy import artifacts, config, llm, policy, prompt, usage
from nano_workbuddy.budgets import BudgetExceeded, Limits
from nano_workbuddy.connectors.feishu import Feishu, FeishuArgs, feishu
from nano_workbuddy.connectors.feishu_ops import flags
from nano_workbuddy.eval.runner import archive_source, code_version, write_json
from nano_workbuddy.eval.schema import file_hash
from nano_workbuddy.eval.worker import EvalWorkspace
from nano_workbuddy.tools import BUILTIN, Registry, ToolError


def permitted(args: FeishuArgs, documents: list[dict]) -> bool:
    if args.operation is not None or args.mode != "execute" or args.command[:2] != ["docs", "+fetch"]:
        return False
    values = flags(args.command)
    return any(values == flags(d["command"]) for d in documents)


class MeetingWorkspace(EvalWorkspace):
    def feishu(self):
        return self.connector

    async def toolset(self, spec, root, scheduled=False):
        async def scoped(args, ctx):
            if not permitted(args, self.documents):
                raise ToolError("Only the exact frozen docs +fetch commands in this task are available. "
                                "No other documents, sections, calendar, search or writes are allowed.")
            usage.append(self.trial / "reads.jsonl", {"type": "document.started", "command": args.command})
            result = await feishu.fn(args, ctx)
            usage.append(self.trial / "reads.jsonl", {"type": "document.result", "is_error": result.is_error})
            return result

        tools = [t for t in BUILTIN if t.name in ("read", "create_document", "export_document", "present", "todo")]
        limited = dataclasses.replace(feishu, fn=scoped, description=
            "Read the two frozen Feishu docs +fetch commands supplied in this task. "
            "Other documents, parameters, searches, calendar and remote writes are unavailable.")
        return Registry([*tools, limited], services={"feishu": self.connector}), {
            "meeting_trial": "本次只读飞书学习材料，使用任务中给出的两个精确命令（含版本和范围）。"
            "没有日历、搜索、shell、glob、额外问答或远端写入；不要读取可选 AGENTS.md。"
            "正文先生成 meeting-prep.md，再 export_document 导出 meeting-prep.docx，不重复成文。"}


async def work(trial):
    public = json.loads((trial / "public.json").read_text())
    spec = config.ModelSpec(provider=config.Provider(**public["provider"], api_key="env:NWB_U3_MODEL_KEY"),
                            **public["model"])
    model = llm.connect(spec, os.environ.pop("NWB_U3_MODEL_KEY"))
    model.client = model.client.with_options(max_retries=0)
    calls = 0

    class Bounded:
        def __init__(self, measured):
            self.measured, self.spec = measured, measured.spec

        async def stream(self, *args, **kwargs):
            nonlocal calls
            if calls >= 12:
                raise BudgetExceeded("U3 total model-call limit (12)")
            calls += 1
            async for item in self.measured.stream(*args, **kwargs):
                yield item

    rules = [("*", "*", "deny"), ("read", "{root}/*", "allow"), ("read", "{spill}/*", "allow"),
             ("write", "{root}/*", "allow"), ("todo", "*", "allow")]
    rules += [("feishu", f"docs +fetch doc={d['url']}", "allow") for d in public["documents"]]
    cfg = config.Config(trial / "home", spec.name, {spec.name: spec}, max_turns=12,
                        scratch_dir=trial / "scratch", budget=Limits(model_calls=12, tool_calls=20, wall_seconds=180),
                        rules=tuple(policy.Rule(*r) for r in rules))
    ws = MeetingWorkspace(cfg, connect=lambda _: model)
    ws.connector, ws.documents = Feishu(public["cli"]), public["documents"]
    ws.arm, ws.trial, ws.bound = "B1", trial, Bounded
    log = ws.new_session(trial / "workspace", initial_prompt=prompt.build(trial / "workspace", cfg.home))
    result = {"session_id": log.id, "status": "error"}
    try:
        result["status"] = await ws.submit(log, public["task"])
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    finally:
        result["model_calls"] = calls
        write_json(trial / "worker-result.json", result)
        for session in ws._logs.values():
            session.close()
        if ws._knowledge:
            ws._knowledge.db.close()
        ws.index.db.close()
        await model.client.close()


async def run(output, scope):
    approved = json.loads(scope.read_text())
    if len(approved["documents"]) != 2 or approved["id"] not in ("U3-1", "U3-2"):
        raise ValueError("expected one U3-1 or U3-2 with two frozen documents")
    case_id = approved["id"]
    cfg, repo = config.load(), Path(__file__).resolve().parents[1]
    spec = cfg.model()
    if (spec.provider.name, spec.id) != ("opendesign", "deepseek-v4.1-flash"):
        raise ValueError("unexpected provider/model; keep the approved connection")
    key = config.secret(spec.provider.api_key, cfg.home)
    if not key:
        raise ValueError("model key missing")
    output.mkdir(mode=0o700)
    trial = output / case_id
    (trial / "workspace").mkdir(parents=True, mode=0o700)
    public = {**approved, "cli": cfg.feishu.cli,
              "provider": {k: v for k, v in dataclasses.asdict(spec.provider).items() if k != "api_key"},
              "model": {k: v for k, v in dataclasses.asdict(spec).items() if k != "provider"}}
    write_json(trial / "public.json", public)
    version = code_version()
    write_json(output / "freeze.json", {**version, "scope_sha256": file_hash(scope),
        "public_sha256": file_hash(trial / "public.json"), "runner_sha256": file_hash(Path(__file__)),
        "authorization": f"User authorized {case_id} using two supplied Feishu links; "
                         "12 requests / 180s; no calendar or writes"})
    archive_source(version, output / "source.zip")
    shutil.copyfile(__file__, output / "runner.py")
    shutil.copyfile(scope, output / "scope.json")
    env = {k: v for k, v in os.environ.items() if k in (
        "PATH", "HOME", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")}
    env.update(PYTHONPATH=str(repo / "src"), NWB_HOME=str(trial / "home"), NWB_U3_MODEL_KEY=key,
               LARKSUITE_CLI_NO_UPDATE_NOTIFIER="1", LARKSUITE_CLI_NO_SKILLS_NOTIFIER="1")
    started = time.monotonic()
    with (trial / "stdout.log").open("wb") as out, (trial / "stderr.log").open("wb") as err:
        proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).resolve()), "_worker",
                    str(trial), cwd=trial / "workspace", env=env, stdout=out, stderr=err, start_new_session=True)
        try:
            await asyncio.wait_for(proc.wait(), 180)
            process_status = "finished" if proc.returncode == 0 else "worker_error"
        except (TimeoutError, asyncio.CancelledError) as exc:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            process_status = "interrupted" if isinstance(exc, asyncio.CancelledError) else "timeout"
    result_path = trial / "worker-result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else {"status": process_status}
    events = [e for f in (trial / "home/usage").glob("*.jsonl") for e in usage.events(f)]
    write_json(trial / "calls.json", events)
    begin, end = ([e for e in events if e["type"] == k] for k in ("model.started", "model.response"))
    record = {**result, "id": case_id, "process_status": process_status,
        "model_calls": len(begin), "model_responses": len(end),
        "observed_models": sorted({e.get("model_version", "unknown") for e in end}),
        "usage": {k: sum(e["usage"].get(k, 0) for e in end) for k in ("input", "output", "cache_read")},
        "usage_complete": len(begin) == len(end) and all(e.get("usage_complete") for e in end),
        "elapsed_seconds": time.monotonic() - started,
        "artifact_checks": [{"path": p.name, "sha256": file_hash(p), "checks": artifacts.check(p)[0]}
                            for p in sorted((trial / "workspace").glob("meeting-prep.*"))],
        "cost": None, "factual_acceptance": "pending", "human_acceptance": "pending"}
    write_json(trial / "review-card.json", record)
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("mode", choices=("run", "_worker"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--scope", type=Path)
    args = parser.parse_args()
    if args.mode == "run" and args.scope is None:
        parser.error("run needs --scope")
    asyncio.run(work(args.output.resolve()) if args.mode == "_worker" else
                run(args.output.resolve(), args.scope.resolve()))
