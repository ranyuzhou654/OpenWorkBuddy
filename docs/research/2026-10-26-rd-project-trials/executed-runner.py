"""R-D repository-material replay; real Workspace/Scheduler/backup, no external tools.

preflight DATASET is read-only. run DATASET NEW_OUTPUT runs the six frozen local cases.
Every trial gets a fresh child process, <=12 total model requests across phases and 180s total.
Workspace reconstruction is an in-process restart, not an OS sleep or multi-week usage claim.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from nano_workbuddy import artifacts, config, llm, policy, prompt, schedule, scheduler, usage
from nano_workbuddy.budgets import Limits
from nano_workbuddy.eval.runner import archive_source, code_version, write_json
from nano_workbuddy.eval.worker import BudgetExceeded, EvalWorkspace
from nano_workbuddy.maintenance import backup, restore
from nano_workbuddy.runs import RunManager
from nano_workbuddy.tools import BUILTIN, Registry, ToolError


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(dataset):
    manifest = json.loads((dataset / "manifest.json").read_text())
    if manifest.get("visual_qa") != "inspected":
        raise ValueError("final input PDF/DOCX visual review must be recorded")
    for rel, item in manifest["inputs"].items():
        path = (dataset / rel).resolve()
        if not path.is_relative_to(dataset) or sha(path) != item["sha256"]:
            raise ValueError(f"input changed after preparation: {rel}")
    cases = json.loads((dataset / "cases.json").read_text())
    if sha(dataset / "cases.json") != manifest.get("cases_sha256"):
        raise ValueError("case cards changed after preparation")
    if {c["id"] for c in cases} != {"U1-1", "U1-2", "U5-1", "U5-2", "U6-1", "U6-2"}:
        raise ValueError("this runner supports only the six local replay cases")
    return manifest, cases


class ProjectWorkspace(EvalWorkspace):
    async def toolset(self, spec, root, scheduled=False):
        tools = [t for t in BUILTIN if t.name in (
            "read", "create_document", "present", "recall", "read_record", "todo", "ask_user")]
        notes = {"trial": "仅使用声明的项目资料及本项目历史；无目录枚举、shell、网络或连接器。"}
        if scheduled:
            tools.append(scheduler.task_result)
            notes["scheduled"] = scheduler.SCHEDULED_NOTICE
        return Registry(tools), notes


async def close_workspace(ws):
    for log in ws._logs.values():
        log.close()
    if ws._knowledge:
        ws._knowledge.db.close()
    ws.index.db.close()


async def work(trial):
    import asyncio

    public = json.loads((trial / "public.json").read_text())
    case, root, home = public["case"], trial / "workspace", trial / "home"
    provider = config.Provider(**public["provider"], api_key="env:NWB_TRIAL_API_KEY")
    spec = config.ModelSpec(provider=provider, **public["model"])
    model = llm.connect(spec, os.environ.pop("NWB_TRIAL_API_KEY"))
    model.client = model.client.with_options(max_retries=0)
    # Reuse a single counter across both phases and every reconstructed Workspace.
    calls = 0

    class Bounded:
        def __init__(self, measured):
            self.measured, self.spec = measured, measured.spec

        async def stream(self, *args, **kwargs):
            nonlocal calls
            if calls >= 12:
                raise BudgetExceeded("total R-D trial model-call budget exhausted")
            calls += 1
            async for item in self.measured.stream(*args, **kwargs):
                yield item

    def open_ws(target):
        cfg = config.load(target)
        ws = ProjectWorkspace(cfg, connect=lambda _: model)
        ws.arm, ws.trial, ws.bound = "B1", trial, Bounded
        return ws

    home.mkdir()
    # A sanitized configuration is required for offline backup. Never copy the user's config or secrets.
    import tomlkit

    raw = {"default_model": spec.name, "max_turns": 12, "scratch_dir": str(root),
           "providers": {provider.name: {**public["provider"], "api_key": "env:NWB_TRIAL_API_KEY"}},
           "models": {spec.name: {k: v for k, v in public["model"].items() if k != "name" and v is not None}
                      | {"provider": provider.name}},
           "budget": dataclasses.asdict(Limits(model_calls=12, wall_seconds=180)),
           "rules": [dataclasses.asdict(r) for r in (
               policy.Rule("*", "*", "deny"), policy.Rule("read", "{root}", "allow"),
               policy.Rule("read", "{root}/*", "allow"), policy.Rule("read", "{spill}/*", "allow"),
               policy.Rule("write", "{root}/*", "allow"), policy.Rule("recall", "*", "allow"),
               policy.Rule("todo", "*", "allow"), policy.Rule("ask_user", "*", "allow"),
               policy.Rule("task_result", "*", "allow"))]}
    # Provider.name is a dataclass field, not a config.toml setting.
    raw["providers"][provider.name].pop("name", None)
    (home / "config.toml").write_text(tomlkit.dumps(raw))
    ws, runs = open_ws(home), None
    result = {"status": "error", "phases": [], "session_ids": [], "workflow_checks": []}

    async def question(q):
        usage.append(trial / "events.jsonl", {"type": "clarification", "question": q})
        raise ToolError("本轮没有额外答复。请明确标出缺失材料，不得补造事实。")

    async def notify(*args):
        usage.append(trial / "events.jsonl", {"type": "local_notification", "content": args})

    try:
        inventory = "可读输入：\n" + "\n".join(case["inputs"]) + "\n\n"
        if case["mode"] == "brief":
            log = ws.new_session(root, initial_prompt=prompt.build(root, home))
            result["session_ids"].append(log.id)
            result["phases"].append(await ws.submit(log, inventory + case["turn"], question=question))
        elif case["mode"] == "schedule":
            shutil.copyfile(root / "period-one.json", root / "current.json")
            runs = RunManager(ws)
            sched = scheduler.Scheduler(runs, notify=notify)
            row = scheduler.create(ws, case["id"], case["turn"], root,
                                   schedule.Rule("once", "00:00", "UTC", day="2099-01-01"))
            result["session_ids"].append(row["session_id"])
            for phase in range(2):
                if phase:
                    await runs.shutdown()
                    await close_workspace(ws)
                    ws = open_ws(home)
                    runs = RunManager(ws)
                    sched = scheduler.Scheduler(runs, notify=notify)
                    source = "period-one.json" if case["same_period"] else "period-two.json"
                    shutil.copyfile(root / source, root / "current.json")
                    usage.append(trial / "events.jsonl", {"type": "workspace.reopened", "phase": phase + 1})
                request = await sched.run_now(row["id"])
                while runs.live:
                    # Missing inputs must fail visibly instead of waiting for a human forever.
                    for live in list(runs.live.values()):
                        for qid in list(live.questions):
                            usage.append(trial / "events.jsonl", {"type": "clarification", "question_id": qid})
                            runs.answer(qid, "请记录缺失信息，本轮没有补充答复。")
                        for aid in list(live.approvals):
                            usage.append(trial / "events.jsonl", {"type": "approval_denied", "approval_id": aid})
                            runs.resolve(aid, "reject", "本地试用未授权额外操作")
                    await asyncio.sleep(.05)
                await sched.tick()
                run = ws.index.run_for_request(request)
                result["phases"].append(run["status"] if run else "not_started")
            occurrences = ws.index.occurrences(row["id"])
            write_json(trial / "occurrences.json", occurrences)
            outcomes = list(reversed([r["outcome"] for r in occurrences]))
            expected = ["changed", "unchanged" if case["same_period"] else "changed"]
            result["workflow_checks"].append({"check": "period_outcomes", "ok": outcomes == expected,
                                               "observed": outcomes, "expected": expected})
            accepted = ws.index._all("select * from inputs where session_id=?", row["session_id"])
            write_json(trial / "accepted-inputs.json", accepted)
            result["workflow_checks"].append({"check": "exactly_two_consumed_inputs",
                                               "ok": len(accepted) == 2 and all(r["status"] == "consumed"
                                                                              for r in accepted)})
        else:
            shutil.copyfile(root / "period-one.json", root / "current.json")
            log = ws.new_session(root, initial_prompt=prompt.build(root, home))
            result["session_ids"].append(log.id)
            result["phases"].append(await ws.submit(log, case["turn"], question=question))
            await ws.sync_records([log.id])
            old_hashes = artifacts.files_of(log)
            # Remove the current note: a later answer must use recorded history/blob evidence.
            (root / "history-note.md").unlink(missing_ok=True)
            shutil.copyfile(root / "period-two.json", root / "current.json")
            await close_workspace(ws)
            if case["mode"] == "restore":
                write_json(trial / "backup.json", backup(home, trial / "backup.tar.gz"))
                home.rename(trial / "original-home-unavailable")
                target = trial / "restored-home"
                write_json(trial / "restore.json", restore(trial / "backup.tar.gz", target))
                result["workflow_checks"].append({"check": "original_home_absent", "ok": not home.exists()})
                home = target
            ws = open_ws(home)
            original = ws.open_session(log.id)
            preserved = all(artifacts.blob(ws.store, h).is_file()
                            and sha(artifacts.blob(ws.store, h)) == h for h in old_hashes)
            result["workflow_checks"].append({"check": "prior_blobs_accessible",
                                               "ok": bool(old_hashes) and preserved})
            log = ws.new_session(root, initial_prompt=prompt.build(root, home))
            result["session_ids"].append(log.id)
            await ws.sync_records([original.id])
            result["phases"].append(await ws.submit(log, case["followup"], question=question))
        result["status"] = "ok" if result["phases"] and all(s == "ok" for s in result["phases"]) else "error"
    except Exception as exc:
        result.update(status="error", error_type=type(exc).__name__)
    finally:
        result.update(model_calls=calls, final_home=str(home))
        write_json(trial / "worker-result.json", result)
        if runs:
            await runs.shutdown()
        await close_workspace(ws)
        await model.client.close()


async def run(dataset, output, selected):
    import asyncio

    manifest, cases = verify(dataset)
    cfg = config.load()
    spec = cfg.model()
    if (spec.provider.name, spec.id) != (manifest["provider"], manifest["model"]):
        raise ValueError("configured provider/model differs from the selected trial scope")
    output.mkdir(mode=0o700)  # existing trials are never silently overwritten or rerun
    version = code_version()
    write_json(output / "freeze.json", {"manifest": manifest, "cases": cases, **version,
                                        "runner_sha256": sha(Path(__file__)), "authorization":
                                        "User selected current repository material for the six local trials"})
    archive_source(version, output / "source.zip")
    shutil.copyfile(__file__, output / "runner.py")
    key = config.secret(spec.provider.api_key, cfg.home)
    for case in cases:
        if selected and case["id"] not in selected:
            continue
        if code_version()["code_sha256"] != version["code_sha256"]:
            raise ValueError("product code changed; keep completed trials and start a new run")
        trial = output / case["id"]
        root = trial / "workspace"
        root.mkdir(parents=True, mode=0o700)
        (trial / "os-home").mkdir()
        for name in case["inputs"]:
            shutil.copyfile(dataset / "inputs" / name, root / name)
        provider = {k: v for k, v in dataclasses.asdict(spec.provider).items() if k != "api_key"}
        model = {k: v for k, v in dataclasses.asdict(spec).items() if k != "provider"}
        write_json(trial / "public.json", {"case": case, "provider": provider, "model": model})
        usage.append(output / "trials.jsonl", {"type": "trial.started", "id": case["id"]})
        env = {k: v for k, v in os.environ.items() if k in (
            "PATH", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")}
        env.update(HOME=str(trial / "os-home"), NWB_HOME=str(trial / "home"),
                   PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), NWB_TRIAL_API_KEY=key)
        started = time.monotonic()
        with (trial / "stdout.log").open("wb") as stdout, (trial / "stderr.log").open("wb") as stderr:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).resolve()), "_worker", str(trial),
                cwd=root, env=env, stdout=stdout, stderr=stderr, start_new_session=True)
            try:
                await asyncio.wait_for(proc.wait(), 180)
                status = "finished" if proc.returncode == 0 else "worker_error"
            except (TimeoutError, asyncio.CancelledError) as exc:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
                status = "timeout"
                if isinstance(exc, asyncio.CancelledError):
                    usage.append(output / "trials.jsonl", {"type": "trial.interrupted", "id": case["id"]})
                    raise
        result_file = trial / "worker-result.json"
        result = json.loads(result_file.read_text()) if result_file.exists() else {"status": status}
        final_home = Path(result.get("final_home", trial / "home"))
        events = [e for p in sorted((final_home / "usage").glob("*.jsonl")) for e in usage.events(p)]
        write_json(trial / "calls.json", events)
        local = usage.events(trial / "events.jsonl")
        checks = [{"path": str(p.relative_to(root)), "checks": artifacts.check(p)[0], "sha256": sha(p)}
                  for p in sorted(root.glob("*")) if p.is_file() and p.name not in (*case["inputs"], "current.json")]
        write_json(trial / "artifact-checks.json", checks)
        record = {"type": "trial.finished", "id": case["id"], "process_status": status, **result,
                  "completion": None, "factual_errors": None, "human_review": "pending",
                  "artifact_checks": checks, "clarifications": sum(e["type"] == "clarification" for e in local),
                  "necessary_approvals": 0, "denied_extra_approvals":
                  sum(e["type"] == "approval_denied" for e in local),
                  "rescue_count": 0, "elapsed_seconds": time.monotonic() - started,
                  "human_wait_seconds": 0,
                  "cost": None, "cost_reason": "subscription billing unknown",
                  "material_mode": "repository_history_replay"}
        record["usage"] = {k: sum(e["usage"].get(k, 0) for e in events if e["type"] == "model.response")
                           for k in ("input", "output", "cache_read", "cache_write")}
        record["model_calls"] = sum(e["type"] == "model.started" for e in events)
        write_json(trial / "review-card.json", record)
        usage.append(output / "trials.jsonl", record)
        print(f'{case["id"]}: runtime={record["status"]}, calls={record["model_calls"]}, human=pending', flush=True)


def main():
    import asyncio

    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("mode", choices=("preflight", "run", "_worker"))
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--case", action="append")
    args = parser.parse_args()
    if args.mode == "_worker":
        asyncio.run(work(args.dataset.resolve()))
    elif args.mode == "preflight":
        manifest, cases = verify(args.dataset.resolve())
        print(json.dumps({"inputs": len(manifest["inputs"]), "cases": [c["id"] for c in cases],
                          "provider_calls": 0, "human_review": "pending"}, ensure_ascii=False))
    else:
        if args.output is None:
            parser.error("run requires a new output directory")
        asyncio.run(run(args.dataset.resolve(), args.output.resolve(), args.case))


if __name__ == "__main__":
    main()
