"""Development quantification: offline fixtures and bounded scheduled development trials.

No production defaults change. Every A/B switch is local to QuantWorkspace below.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import dataclasses
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from nano_workbuddy import config, llm, policy, prompt, skills, usage
from nano_workbuddy.budgets import BudgetExceeded, Limits
from nano_workbuddy.context import request_tokens, to_messages
from nano_workbuddy.eval.quantification import (
    aggregate_retrieval,
    ledger_metrics,
    ranked_metrics,
    repeated_reads,
    request_chars,
    retrieval_fixture,
    save,
    write_corpus,
)
from nano_workbuddy.eval.quantification_cases import (
    EXPERIMENT_VERSION,
    compact_history,
    contract_errors,
    pilot_cases,
)
from nano_workbuddy.eval.runner import archive_source, code_version
from nano_workbuddy.eval.schema import digest, file_hash
from nano_workbuddy.knowledge import Knowledge
from nano_workbuddy.log import SessionLog
from nano_workbuddy.runtime import Workspace
from nano_workbuddy.tools import CORE, Registry
from nano_workbuddy.types import ASSISTANT, TOOL_RESULT, USER, new_id, text, text_of

REPO = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
TOOL_PROFILE = {"id": "scoped-local-grep-v2", "shared": ["read", "write", "edit", "grep"],
                "history_B_extra": ["recall", "read_record"]}


def trial_tools(case, arm):
    names = [*TOOL_PROFILE["shared"]]
    if case["track"] == "history" and arm == "B":
        names.extend(TOOL_PROFILE["history_B_extra"])
    available = {t.name: t for t in CORE}
    return [available[name] for name in names]


def trial_rules(case, arm):
    # Directory access lets grep search '.', but candidates still require explicit child read rules.
    rules = [policy.Rule("*", "*", "deny"), policy.Rule("read", "{root}", "allow"),
             policy.Rule("read", "{spill}", "allow"), policy.Rule("read", "{spill}/*", "allow"),
             policy.Rule("read", "{root}/result.json", "allow"),
             policy.Rule("write", "{root}/result.json", "allow"),
             policy.Rule("edit", "{root}/result.json", "allow")]
    rules.extend(policy.Rule("read", "{root}/" + name, "allow") for name in case["inputs"])
    if case["track"] == "history" and arm == "B":
        rules.append(policy.Rule("recall", "", "allow"))
    return tuple(rules)


def source_snapshot(destination, extra_paths=()):
    version = code_version()
    version["code_files"][str(SCRIPT.relative_to(REPO))] = file_hash(SCRIPT)
    version["code_files"].update({str(p.relative_to(REPO)): file_hash(p) for p in extra_paths})
    version["code_sha256"] = digest(version["code_files"])
    archive_source(version, destination)
    return version


def verify_source(version):
    if any(file_hash(REPO / name) != expected for name, expected in version["code_files"].items()):
        raise ValueError("source changed since freeze; use a new run directory")


def offline_retrieval(output, sizes=(1000, 10000, 50000)):
    output.mkdir(parents=True, exist_ok=False)
    records, queries = retrieval_fixture()
    save(output / "fixture.json", {"records": records, "queries": queries,
        "note": "Synthetic fixed-template queries; frozen-test is not a generalization holdout."})
    source = source_snapshot(output / "source.zip")
    save(output / "freeze.json", {"source": source, "fixture_sha256": file_hash(output / "fixture.json"),
                                  "sizes": sizes, "model_calls": 0, "python": platform.python_version()})
    results = []
    for size in sizes:
        verify_source(source)
        root = output / str(size)
        root.mkdir()
        store = root / "store"
        rows = write_corpus(root / "corpus", records, size, store)
        db = Knowledge(root / "knowledge.db", store)
        start = time.perf_counter()
        indexed = 0
        while (n := db.sync(rows)):
            indexed += n
        build_seconds = time.perf_counter() - start
        count = db.db.execute("select count(*) from items").fetchone()[0]
        if count != size:
            raise ValueError(f"expected {size} indexed records, got {count}")
        # Close/reopen once, without attempting to evict OS or SQLite filesystem caches.
        db.db.close()
        db = Knowledge(root / "knowledge.db", store)
        measured = []
        for q in queries:
            start = time.perf_counter()
            hits = db.search(q["query"], limit=10, **q["scope"])
            first_ms = 1000 * (time.perf_counter() - start)
            repeat = []
            for _ in range(5):
                start = time.perf_counter()
                db.search(q["query"], limit=10, **q["scope"])
                repeat.append(1000 * (time.perf_counter() - start))
            ids = [h["record_id"] for h in hits]
            unauthorized = db.read_record(q["forbidden_id"], 0, 100, **q["scope"]) is not None
            measured.append({**q, **ranked_metrics(ids, q["relevant"]), "returned": ids,
                "first_ms": first_ms, "repeat_ms": repeat, "unauthorized_read": unauthorized,
                "scope_violations": sum(h["session_id"] not in q["scope"]["session_ids"] or
                    (q["scope"]["project_id"] is not None and h["project_id"] != q["scope"]["project_id"]) or
                    (q["scope"]["session_id"] is not None and h["session_id"] != q["scope"]["session_id"])
                    for h in hits)})
        # Actual incremental JSONL ingestion, after all retrieval scores are fixed.
        path = Path(rows[0]["file_path"])
        from nano_workbuddy.types import Entry
        with path.open("a", encoding="utf-8") as stream:
            for i in range(100):
                e = Entry(f"increment-{i}", None, USER, {"content": [text(f"增量更新标记{i:04}")]})
                stream.write(json.dumps(e.to_json(), ensure_ascii=False) + "\n")
        start = time.perf_counter()
        added = db.sync(rows)
        increment_seconds = time.perf_counter() - start
        incremental_hit = bool(db.search("增量更新标记0099", session_ids=["s0"], project_id="p0"))
        db.db.execute("pragma wal_checkpoint(truncate)")
        database_bytes = (root / "knowledge.db").stat().st_size
        db.db.close()
        summary = {"size": size, "indexed_entries": indexed, "indexed_records": count,
            "build_seconds": build_seconds, "increment_entries": added,
            "increment_seconds": increment_seconds, "increment_visible": incremental_hit,
            "database_bytes": database_bytes, "all": aggregate_retrieval(measured),
            "by_split": {s: aggregate_retrieval([r for r in measured if r["split"] == s])
                         for s in ("development", "frozen-test")},
            "by_category": {c: aggregate_retrieval([r for r in measured if r["category"] == c])
                            for c in sorted({r["category"] for r in measured})}}
        save(root / "queries.json", measured)
        save(root / "summary.json", summary)
        results.append(summary)
    save(output / "summary.json", {"model_calls": 0, "results": results,
        "latency_note": "first-in-process query per text, not OS-cold; five warm repeats per query",
        "scope_note": "Knowledge uses fixture-authorized scopes; host scope selection separately tested"})
    return results


async def close_workspace(ws):
    await ws.mcp.close()
    for log in ws._logs.values():
        log.close()
    if ws._knowledge:
        ws._knowledge.db.close()
    ws.index.db.close()


class QuantWorkspace(Workspace):
    async def toolset(self, spec, root, scheduled=False):
        return self.tools, {"evaluation_tools": "Local file tools include read, write, edit and grep. "
            "grep can search declared input files or '.' in mode='content' for matching lines; use read "
            "for surrounding context if needed. Choose tools according to the task; no tool is mandatory. "
            "Only declared input files, result.json and this session's spill are readable."}

    def refresh_context(self, log, run_id=None):
        if self.case_id == "MEM-FRESH" and self.arm == "A":
            return  # session-start snapshot baseline; only this mechanism is disabled
        if self.case_id == "MEM-REUSE" and self.arm == "A":
            return  # initial prompt has no memory and no subsequent injection
        super().refresh_context(log, run_id)

    def make_run(self, *args, **kwargs):
        run = super().make_run(*args, **kwargs)
        run.context_governance = not (self.track == "context" and self.arm == "A")
        before = run.model.before_call

        def reserve():
            if self.calls >= 12:
                raise BudgetExceeded("whole-trial model limit")
            if time.monotonic() >= self.deadline:
                raise BudgetExceeded("whole-trial wall limit")
            if before:
                before()
            self.calls += 1

        run.model.before_call = reserve
        # Wrap the real refresh callback to record a same-history, pre-fit checkpoint.
        refresh = run.refresh_context

        def checkpoint():
            if refresh:
                refresh()
            from nano_workbuddy.loop import sync_notices
            sync_notices(run)
            self.active_run = run
            system, messages = to_messages(run.log.path())
            checkpoint_id = new_id("cp")
            usage.append(self.trial / "checkpoints.jsonl", {"type": "before_fit", "id": checkpoint_id,
                "session_id": run.log.id, "run_id": run.run_id,
                "chars": request_chars(system, messages, run.schemas()),
                "estimated_tokens": request_tokens(system, messages, run.schemas()),
                "log_ids": [e.id for e in run.log.path()]})
            self.checkpoint_id = checkpoint_id

        run.refresh_context = checkpoint
        gate = run.gate

        async def instrument(tool, call):
            blocked = await gate(tool, call)
            if blocked:
                usage.append(self.trial / "tools.jsonl", {"type": "blocked", "name": tool.name,
                                                           "args": call["args"]})
                return blocked
            if tool.name == "read":
                p = Path(call["args"]["path"])
                p = (p if p.is_absolute() else run.ctx.root / p).resolve()
                if p.is_file():
                    raw = p.read_bytes()
                    offset, limit = call["args"].get("offset", 1), call["args"].get("limit", 2000)
                    lines = raw.decode(errors="replace").splitlines()[offset - 1:offset - 1 + limit]
                    usage.append(self.trial / "tools.jsonl", {"type": "read", "path": str(p),
                        "sha256": hashlib.sha256(raw).hexdigest(), "offset": offset, "limit": limit,
                        "chars": len("\n".join(line[:2000] for line in lines))})
            usage.append(self.trial / "tools.jsonl", {"type": "allowed", "name": tool.name,
                                                       "args": call["args"]})
            return None

        run.gate = instrument
        return run


def initial_prompt(root, home, no_memory=False):
    # Both arms use exactly the same base text. Only the declared memory ablation changes context.
    base = prompt.build(root, home)["base_text"]
    base = base.split("<environment>")[0].strip() + "\nUse relative workspace paths. Session date: 2026-10-29."
    current = "" if no_memory else prompt.context(root, home)
    return {"base_text": base, "context": current, "text": base + ("\n\n" + current if current else "")}


def seed_history(ws, root, case):
    ids = []
    if case.get("history"):
        old = ws.new_session(root, initial_prompt=initial_prompt(root, ws.cfg.home))
        if case["id"] == "HIST-VERSION":
            old.append(USER, {"content": [text("南桥交付项目草案：交付日期2026-11-04，预算61000元。") ]})
        entry = old.append(USER, {"content": [text(case["history"])]})
        from nano_workbuddy.eval.quantification import record_id
        ids.append(record_id(old.id, "message", entry.id))
        if case["id"] == "HIST-VERSION":
            other = root.parent / "other-project"
            other.mkdir()
            log = ws.new_session(other, initial_prompt=initial_prompt(other, ws.cfg.home))
            log.append(USER, {"content": [text("南桥交付项目最终确认：交付日期2026-12-29，预算99000元。") ]})
    return ids


def deterministic_model(case, arm):
    # Only used in offline harness checks. Never interpret these scripted outputs as model quality.
    sys.path.insert(0, str(REPO / "tests"))
    from conftest import FakeModel, call, reply
    script = []
    base = case.get("base_case_id", case["id"])
    if base == "CTX-SPILL":
        script.append(reply(None, call("read", path="material.txt")))
        if arm == "B":
            script.append(reply(None, call("grep", path="material.txt", pattern="正式决定", mode="content")))
    elif base == "CTX-COMPACT":
        script.append(reply(None, call("read", path="input.json")))
    elif case["id"] == "MEM-REUSE" and arm == "A":
        script.append(reply(None, call("read", path="original-notes.txt")))
    elif case["track"] == "history":
        script.append(reply(None, call("read", path="previous-notes.txt")))
    if "gold" in case:
        gold = copy.deepcopy(case["gold"])
    elif base == "CTX-SPILL":
        import re
        body = case["inputs"]["material.txt"]
        gold = {key: re.search(rf"{key}=([^；。\n]+)", body)[1] for key in ("supplier", "budget", "deadline")}
        gold["budget"] = int(gold["budget"])
    else:
        import re
        owners = re.findall(r"owner(?:=| 改为)([^；，。\s]+)", "\n".join(e["text"] for e in case["seed_history"]))
        data = json.loads(case["inputs"]["input.json"])
        gold = {"owner": owners[-1], "total": data["quantity"] * data["unit_price"], "protected_file": "input.json"}
    if case["id"] == "MEM-FRESH" and arm == "A":
        gold = {"locale": "en-US", "project_rule": "RULE_V1"}
    if case["track"] == "history":
        gold["evidence"] = "previous-notes.txt"
    script.extend([reply(None, call("write", path="result.json", content=json.dumps(gold))), reply("done")])
    return FakeModel(*script, summary=f"owner={gold.get('owner', '林杉')}；"
                     "protected_file=input.json 不得修改；总价=quantity*unit_price。")


async def worker(trial):
    public = json.loads((trial / "public.json").read_text())
    case, arm = public["case"], public["arm"]
    root, home = trial / "workspace", trial / "home"
    root.mkdir()
    home.mkdir()
    # Only trial-local files/skills/MCP; no discovery from shared user directories.
    skills.SHARED = trial / "empty-shared-skills"
    for name, body in case["inputs"].items():
        (root / name).write_text(body)
    if case["id"] in ("MEM-FRESH", "MEM-REUSE"):
        (home / "USER.md").write_text("preferred_locale=en-US\n" if case["id"] == "MEM-FRESH" else
                                      "preferred_locale=ja-JP\noutput_format=bullet_list\n")
    if case["id"] == "MEM-FRESH":
        (root / "AGENTS.md").write_text("project_rule=RULE_V1\n")
    provider = config.Provider(**public["provider"])
    spec = config.ModelSpec(provider=provider, **public["model"])
    if public["fake"]:
        fake_case = case if "base_case_id" in case else next(c for c in pilot_cases() if c["id"] == case["id"])
        model = deterministic_model(fake_case, arm).connect(spec)
    else:
        model = llm.connect(spec, os.environ.pop("NWB_QUANT_KEY"))
        model.client = model.client.with_options(max_retries=0, timeout=60)

    class Capture:
        client = getattr(model, "client", None)

        def __init__(self, requested):
            self.spec = requested

        async def stream(self, system, messages, tools, tool_choice=None):
            kind = usage.PURPOSE.get()
            raw_chars = raw_tokens = None
            if kind == "agent":
                entries = copy.deepcopy(ws.active_run.log.path())
                entries = [e for e in entries if e.type not in ("prune", "compaction")]
                for e in entries:
                    exposure = e.data.get("meta", {}).get("exposure") if e.type == TOOL_RESULT else None
                    if exposure and Path(exposure["full_output"]).is_file():
                        e.data["content"] = [text(Path(exposure["full_output"]).read_text())]
                raw_system, raw_messages = to_messages(entries)
                raw_chars = request_chars(raw_system, raw_messages, tools)
                raw_tokens = request_tokens(raw_system, raw_messages, tools)
            usage.append(trial / "requests.jsonl", {"type": "request", "purpose": kind,
                "checkpoint_id": ws.checkpoint_id, "system": system, "messages": messages,
                "tools": tools, "chars": request_chars(system, messages, tools),
                "estimated_tokens": request_tokens(system, messages, tools),
                "ungoverned_same_history_chars": raw_chars, "ungoverned_estimated_tokens": raw_tokens})
            async for item in model.stream(system, messages, tools, tool_choice):
                yield item
    tools = trial_tools(case, arm)
    if public["tool_names"] != [t.name for t in tools]:
        raise ValueError("tool manifest does not match the frozen profile")
    if "tool_schemas" in public and public["tool_schemas"] != [t.schema() for t in tools]:
        raise ValueError("tool schemas differ from freeze")
    cfg = config.Config(home, spec.name, {spec.name: spec}, max_turns=12, rules=trial_rules(case, arm),
                        plugins_enabled=(), scratch_dir=trial / "scratch",
                        budget=Limits(model_calls=12, tool_calls=36, wall_seconds=180))
    ws = QuantWorkspace(cfg, tools=Registry(tools), connect=lambda requested: Capture(requested))
    ws.case_id, ws.track, ws.arm, ws.trial = case["id"], case["track"], arm, trial
    ws.calls, ws.deadline, ws.checkpoint_id = 0, time.monotonic() + 175, None
    valid_evidence = seed_history(ws, root, case)
    log = ws.new_session(root, initial_prompt=initial_prompt(root, home, case["id"] == "MEM-REUSE" and arm == "A"))
    if "seed_history" in case:
        for entry in case["seed_history"]:
            log.append(entry["role"], {"content": [text(entry["text"])]})
    elif case["id"] == "CTX-COMPACT":
        for role, value in compact_history():
            log.append(role, {"content": [text(value)]})
    if case["id"] == "MEM-FRESH":
        log.append(USER, {"content": [text("历史保存偏好 preferred_locale=en-US，项目约定 RULE_V1。") ]})
        log.append(ASSISTANT, {"content": [text("已记录旧约定。") ]})
        (home / "USER.md").write_text("preferred_locale=ja-JP\n")
        (root / "AGENTS.md").write_text("project_rule=RULE_V2\n")
    initial_raw = log.file.read_bytes()
    initial_inputs = {name: file_hash(root / name) for name in case["inputs"]}
    result = {"case_id": case["id"], "track": case["track"], "arm": arm,
              "dataset_version": case["dataset_version"], "tool_names": [t.name for t in tools],
              "mode": "deterministic" if public["fake"] else "real-model", "checks": [], "status": "error",
              "valid_evidence_ids": valid_evidence, "session_id": log.id, "cost": None}
    started = time.monotonic()
    try:
        async with asyncio.timeout(175):
            if case["track"] == "history":
                await ws.sync_records(list(ws._logs))
            result["status"] = await ws.submit(log, case["task"])
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    finally:
        result["elapsed_seconds"] = time.monotonic() - started
        result["calls_reserved"] = ws.calls
        result["history_append_only"] = log.file.read_bytes().startswith(initial_raw)
        result["inputs_unchanged"] = all(file_hash(root / name) == h for name, h in initial_inputs.items())
        result["governance_events"] = {kind: sum(e.type == kind for e in log.path())
                                        for kind in ("prune", "compaction", "context_state")}
        result["tool_errors"] = sum(e.type == TOOL_RESULT and e.data.get("is_error", False) for e in log.path())
        result["spilled_results"] = sum(e.type == TOOL_RESULT and bool(e.data.get("meta", {}).get("exposure"))
                                        for e in log.path())
        save(trial / "worker-result.json", result)
        await close_workspace(ws)
        if not public["fake"]:
            await model.client.close()



def score_trial(trial, case, worker_result):
    result = dict(worker_result)
    actual, parse_error = None, None
    try:
        actual = json.loads((trial / "workspace/result.json").read_text())
        if not isinstance(actual, dict):
            actual = None
            parse_error = "not_an_object"
    except (OSError, ValueError) as exc:
        parse_error = type(exc).__name__
    checks = [{"id": key, "ok": actual is not None and actual.get(key) == value}
              for key, value in case["gold"].items()]
    format_errors = contract_errors(actual, case["output_contract"])
    checks.append({"id": "exact_schema", "ok": not format_errors})
    if case["track"] == "history":
        evidence = actual.get("evidence") if actual else None
        # Attribution must correspond to an actually successful read, not just a plausible-looking ID.
        logs = [SessionLog.open(p) for p in (trial / "home/sessions").rglob("*.jsonl")]
        results = [e for log in logs for e in log.path() if e.type == TOOL_RESULT and not e.data.get("is_error")]
        calls = {c["id"]: c for log in logs for e in log.path() if e.type == ASSISTANT for c in e.tool_calls}

        def source_supports(entry):
            call = calls.get(entry.data["call_id"], {})
            name, args = call.get("name"), call.get("args", {})
            body = text_of(entry.data["content"])
            path = Path(args.get("path", "."))
            target = (path if path.is_absolute() else trial / "workspace" / path).resolve()
            source = (trial / "workspace/previous-notes.txt").resolve()
            if case["history"] not in body:
                return False
            if name == "read":
                return target == source
            if name == "grep":
                return any(case["history"] in line and line.startswith("previous-notes.txt:")
                           for line in body.splitlines()) and target in (source, source.parent)
            return False

        source_read = evidence == "previous-notes.txt" and any(source_supports(e) for e in results)
        historical_read = evidence in result.get("valid_evidence_ids", []) and any(
            case["history"] in text_of(e.data["content"]) and (
                (e.data.get("name") == "read_record" and
                 calls.get(e.data["call_id"], {}).get("args", {}).get("ref") == evidence) or
                (e.data.get("name") == "recall" and evidence in text_of(e.data["content"])))
            for e in results)
        checks.append({"id": "evidence_read_and_supports_answer", "ok": source_read or historical_read})
    checks.extend([{"id": "runtime_ok", "ok": result.get("status") == "ok"},
                   {"id": "inputs_unchanged", "ok": result.get("inputs_unchanged", False)},
                   {"id": "append_only", "ok": result.get("history_append_only", False)}])
    metrics = ledger_metrics(sorted((trial / "home/usage").glob("*.jsonl")))
    requests = [r for r in usage.events(trial / "requests.jsonl") if r["purpose"] == "agent"]
    raw = sum(r["ungoverned_same_history_chars"] for r in requests)
    shown = sum(r["chars"] for r in requests)
    tools = usage.events(trial / "tools.jsonl")
    result.update(checks=checks, actual=actual, parse_error=parse_error, passed=all(c["ok"] for c in checks),
        format_valid=not format_errors, format_errors=format_errors,
        factual_fields_correct=sum(c["ok"] for c in checks[:len(case["gold"])]),
        factual_fields_required=len(case["gold"]),
        metrics=metrics, reads=repeated_reads([t for t in tools if t["type"] == "read"]),
        blocked_tools=sum(t["type"] == "blocked" for t in tools),
        recall_calls=sum(t["type"] == "allowed" and t["name"] == "recall" for t in tools),
        grep_calls=sum(t["type"] == "allowed" and t["name"] == "grep" for t in tools),
        read_record_calls=sum(t["type"] == "allowed" and t["name"] == "read_record" for t in tools),
        same_history_projection={"observed_agent_requests": len(requests), "raw_chars": raw,
                                 "shown_chars": shown, "reduction": 1 - shown / raw if raw else None},
        quality_scope="Automatic exact checks of synthetic JSON tasks; not human business acceptance")
    save(trial / "result.json", result)
    return result


def pilot(output, model_name=None, source_home=None, fake=False):
    output.mkdir(parents=True, exist_ok=False)
    cases = pilot_cases()
    if fake:
        original = config.ModelSpec("deterministic", config.Provider("fake", "openai-chat", "http://invalid", ""),
                                    "fake", 100000, 2048)
        secret = ""
    else:
        source = config.load(source_home)
        original = source.model(model_name)
        # Resolve without logging or persisting the secret. No network preflight is spent.
        secret = config.secret(original.provider.api_key, source.home)
        if not secret:
            raise ValueError("selected model key is unavailable")
    version = source_snapshot(output / "source.zip")
    provider = dataclasses.asdict(dataclasses.replace(original.provider, api_key="env:NWB_QUANT_KEY"))
    model = dataclasses.asdict(original)
    model.pop("provider")
    model["max_output"] = 2048
    freeze = {"source": version, "cases": cases, "dataset_sha256": digest(cases),
              "dataset_version": EXPERIMENT_VERSION, "tool_profile": TOOL_PROFILE,
              "grep_backend": "ripgrep" if shutil.which("rg") else "python",
              "provider": provider, "model": model, "split": "development", "fake": fake,
              "authorization": ("Offline deterministic tools/format calibration; no remote model calls" if fake else
                  "Development only; this new run requires user authorization; bounds are not an authorization grant"),
              "limits": {"trials": 12, "per_trial_requests": 12, "total_requests": 144,
                         "per_trial_seconds": 180, "sdk_retries": 0},
              "python": platform.python_version(), "created_at": time.time()}
    schedule = []
    for index, case in enumerate(cases):
        arms = ("A", "B") if index % 2 == 0 else ("B", "A")
        for arm in arms:
            schedule.append({"case_id": case["id"], "arm": arm})
    freeze["schedule"] = schedule
    return scheduled_trials(output, freeze, secret, report_pilot)


def scheduled_trials(output, freeze, secret, report):
    """Execute the frozen order once; each child retains the existing 12-call/180s limit."""
    if len(freeze["schedule"]) != freeze["limits"]["trials"]:
        raise ValueError("schedule length differs from trial limit")
    save(output / "freeze.json", freeze)
    cases = {case["id"]: case for case in freeze["cases"]}
    results = []
    for item in freeze["schedule"]:
        verify_source(freeze["source"])
        if sum(r["metrics"]["started_calls"] for r in results) + 12 > freeze["limits"]["total_requests"]:
            raise BudgetExceeded("insufficient global budget for another bounded trial")
        case, arm = cases[item["case_id"]], item["arm"]
        trial = output / uuid.uuid4().hex[:16]
        trial.mkdir()
        tools = trial_tools(case, arm)
        if "tool_schemas" in freeze and freeze["tool_schemas"] != [t.schema() for t in tools]:
            raise ValueError("tool schemas changed since freeze")
        public_case = {k: v for k, v in case.items() if k != "gold"}
        save(trial / "public.json", {"case": public_case, "arm": arm,
            "model": {**freeze["model"], "context_window": case["window"]},
            "provider": freeze["provider"], "fake": freeze["fake"], "limit": freeze["limits"],
            "tool_names": [t.name for t in tools], "tool_schemas": [t.schema() for t in tools]})
        usage.append(output / "trials.jsonl", {"type": "started", **item, "directory": trial.name})
        env = {**os.environ, "NWB_QUANT_KEY": secret}
        t0 = time.monotonic()
        with (trial / "stdout.txt").open("w") as out, (trial / "stderr.txt").open("w") as err:
            try:
                proc = subprocess.run([sys.executable, str(SCRIPT), "_worker", str(trial)],
                                      env=env, stdout=out, stderr=err, timeout=180)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                exit_code = "timeout"
        worker_file = trial / "worker-result.json"
        result = json.loads(worker_file.read_text()) if worker_file.exists() else {
            "case_id": case["id"], "track": case["track"], "arm": arm, "status": "error",
            "mode": "deterministic" if freeze["fake"] else "real-model", "error_type": "WorkerInterrupted"}
        result.update(item, worker_exit=exit_code, parent_elapsed_seconds=time.monotonic() - t0, directory=trial.name)
        if exit_code != 0:
            result["status"] = "error"
        result = score_trial(trial, case, result)
        results.append(result)
        usage.append(output / "trials.jsonl", {"type": "finished", **result})
        report(output, results)
        print(json.dumps({**item, "passed": result["passed"], "calls": result["metrics"]["started_calls"],
                          "status": result["status"]}), flush=True)
        if sum(r["metrics"]["started_calls"] for r in results) > freeze["limits"]["total_requests"]:
            raise RuntimeError("global request authorization exceeded")
    return results


def report_pilot(output, results):
    pairs = []
    for case_id in dict.fromkeys(r["case_id"] for r in results):
        arms = {r["arm"]: r for r in results if r["case_id"] == case_id}
        pair = {"case_id": case_id, "arms": {a: {"passed": r["passed"], "metrics": r["metrics"],
            "reads": r["reads"], "same_history_projection": r["same_history_projection"],
            "format_valid": r["format_valid"], "factual_fields_correct": r["factual_fields_correct"],
            "factual_fields_required": r["factual_fields_required"], "grep_calls": r["grep_calls"]}
            for a, r in arms.items()}}
        if set(arms) == {"A", "B"}:
            a, b = arms["A"], arms["B"]
            pair["both_passed"] = a["passed"] and b["passed"]
            ai, bi = a["metrics"]["actual_input_tokens"], b["metrics"]["actual_input_tokens"]
            pair["actual_input_reduction"] = 1 - bi / ai if (pair["both_passed"] and ai and bi is not None
                and a["mode"] == "real-model" and b["mode"] == "real-model") else None
            pair["note"] = "Only complete-usage, both-passing real-model pairs compare actual input savings."
        pairs.append(pair)
    summary = {"split": "development", "trials": len(results), "planned_trials": 12,
        "dataset_version": EXPERIMENT_VERSION, "tool_profile": TOOL_PROFILE,
        "remote_model_requests": sum(r["metrics"]["started_calls"] for r in results if r["mode"] == "real-model"),
        "mode": results[0]["mode"] if results else None,
        "started_calls": sum(r["metrics"]["started_calls"] for r in results),
        "responses": sum(r["metrics"]["responses"] for r in results),
        "observed_usage": {k: sum(r["metrics"]["observed_usage"][k] for r in results)
                           for k in ("input", "output", "cache_read")},
        "usage_complete": bool(results) and all(r["metrics"]["usage_complete"] or
                    r["metrics"]["started_calls"] == 0 for r in results),
        "pairs": pairs, "cost": None,
        "scope": "Six synthetic development cases, one repeat; no holdout/generalization/business-success claim"}
    save(output / "summary.json", summary)
    lines = ["# Phase-one development calibration", "", summary["scope"], "",
        f"Mode: {summary['mode']}; trials {len(results)}/12; requests {summary['started_calls']}/144. Cost unknown.",
        f"Dataset: {EXPERIMENT_VERSION}; remote model requests: {summary['remote_model_requests']}.",
        "", "| Case | Arm | Facts | Format | Pass | Requests | grep | Actual input tokens | Projection reduction |",
        "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        reduction = r["same_history_projection"]["reduction"]
        shown = f"{reduction:.2%}" if reduction is not None else "n/a"
        lines.append(f"| {r['case_id']} | {r['arm']} | {r['factual_fields_correct']}/{r['factual_fields_required']} | "
            f"{r['format_valid']} | {r['passed']} | {r['metrics']['started_calls']} | {r['grep_calls']} | "
            f"{r['metrics']['actual_input_tokens']} | {shown} |")
    lines += ["", "Projection reduction compares full-history vs actual serialization on the same observed history;",
              "it excludes summary requests. Task token totals include summaries and re-reads.",
              "Deterministic usage is fabricated by the fixture and is never a provider cost/quality measurement.",
              "A pre-provider context rejection can have zero requests; it is still a failed task."]
    (output / "report.md").write_text("\n".join(lines) + "\n")


async def offline_memory(output):
    from nano_workbuddy.memory import memory_tool
    from nano_workbuddy.tools import ToolCtx
    from nano_workbuddy.tools.registry import execute
    output.mkdir(parents=True, exist_ok=False)
    home, root = output / "home", output / "workspace"
    home.mkdir()
    root.mkdir()
    ctx = ToolCtx(root, "offline", output / "spill")
    tool = memory_tool(home)
    checks = []
    for action, file, old, value in [
        ("add", "user", "", "locale=en-US"), ("replace", "user", "en-US", "ja-JP"),
        ("add", "project", "", "rule=PROJECT_ONLY"), ("remove", "user", "locale=ja-JP", ""),
    ]:
        call = {"type": "tool_call", "name": "memory", "id": new_id("call"),
                "args": {"action": action, "file": file, "old_text": old, "text": value}}
        r = await execute(tool, call, ctx)
        checks.append({"action": action, "file": file, "ok": not r.is_error})
    checks.extend([{"id": "removed_from_current_injection", "ok": "locale=" not in prompt.context(root, home)},
        {"id": "project_rule_kept", "ok": "PROJECT_ONLY" in prompt.context(root, home)},
        {"id": "user_file_not_project_file", "ok": "PROJECT_ONLY" not in (home / "USER.md").read_text()}])
    save(output / "result.json", {"checks": checks, "passed": all(c["ok"] for c in checks), "model_calls": 0})


async def offline_context(output):
    from nano_workbuddy import compaction
    from nano_workbuddy.context import spill
    from nano_workbuddy.tools import Result
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(REPO / "tests"))
    from conftest import FakeModel
    model = FakeModel(summary="owner=林杉；protected_file=input.json；旧记录是背景。")
    cfg = dataclasses.replace(config.load(output / "home"), plugins_enabled=())
    ws = Workspace(cfg, connect=model.connect, tools=Registry([t for t in CORE if t.name == "read"]))
    root = output / "workspace"
    root.mkdir()
    log = ws.new_session(root, initial_prompt=initial_prompt(root, cfg.home))
    run = ws.make_run(log, cfg.default_model, None, lambda _: None)
    results = []
    raw = "HEAD\n" + "背景资料" * 10000 + "\nTAIL"
    result = spill(Result.ok(raw), output / "spill", "offline-call")
    results.append({"mechanism": "spill", "before_chars": len(raw), "after_chars": len(text_of(result.content)),
                    "full_copy_matches": Path(result.meta["exposure"]["full_output"]).read_text() == raw})
    for i in range(4):
        cid = f"offline-call-{i}"
        log.append(ASSISTANT, {"content": [{"type": "tool_call", "id": cid, "name": "read",
                                          "args": {"path": "synthetic.txt"}}]})
        log.append(TOOL_RESULT, {"call_id": cid, "name": "read", "content": [text("旧工具输出" * 1000)],
                                "is_error": False})
    log.append(USER, {"content": [text("继续处理，owner=林杉；protected_file=input.json。") ]})

    def size():
        system, messages = to_messages(log.path())
        return request_chars(system, messages, run.schemas())

    before = size()
    pruned = compaction.prune(run, keep=500)
    results.append({"mechanism": "prune", "before_chars": before, "after_chars": size(), "triggered": pruned})
    for role, body in compact_history():
        log.append(role, {"content": [text(body)]})
    before, original = size(), log.file.read_bytes()
    compacted = await compaction.compact(run, keep=500)
    results.append({"mechanism": "compact", "before_chars": before, "after_chars": size(),
                    "triggered": compacted is not None, "raw_log_preserved": log.file.read_bytes().startswith(original),
                    "summary_source": "deterministic fixture, not quality evidence"})
    for r in results:
        r["reduction"] = 1 - r["after_chars"] / r["before_chars"]
    save(output / "result.json", {"model_calls_remote": 0, "measurements": results,
        "scope": "Structural mechanism checks; summary text is scripted; not real-model quality or token savings"})
    await close_workspace(ws)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    offline = sub.add_parser("offline")
    offline.add_argument("output", type=Path)
    real = sub.add_parser("pilot")
    real.add_argument("output", type=Path)
    real.add_argument("--model", default="ds-flash")
    real.add_argument("--home", type=Path)
    real.add_argument("--fake", action="store_true")
    worker_parser = sub.add_parser("_worker")
    worker_parser.add_argument("trial", type=Path)
    args = parser.parse_args()
    if args.command == "offline":
        args.output.mkdir(parents=True, exist_ok=False)
        offline_retrieval(args.output / "retrieval")
        asyncio.run(offline_memory(args.output / "memory"))
        asyncio.run(offline_context(args.output / "context"))
    elif args.command == "pilot":
        pilot(args.output.resolve(), args.model, args.home, args.fake)
    else:
        asyncio.run(worker(args.trial.resolve()))


if __name__ == "__main__":
    main()
