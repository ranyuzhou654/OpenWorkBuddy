"""Freeze → isolated trial subprocesses → graders → append-only evidence → Markdown report."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib.metadata
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

from .. import prompt, usage
from ..config import ModelSpec
from .cost import calculate
from .grade import grade, verdict
from .report import markdown, summarize
from .schema import Case, Suite, digest, file_hash, snapshot


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def code_version() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[3]
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    files = {str(p.relative_to(root)): file_hash(p) for p in sorted((root / "src").rglob("*.py"))}
    files.update({str(p.relative_to(root)): file_hash(p) for p in sorted((root / "src").rglob("*.md"))})
    for name in ("pyproject.toml", "uv.lock"):
        if (p := root / name).is_file():
            files[name] = file_hash(p)
    return {"git_sha": sha.stdout.strip() or None, "code_sha256": digest(files), "code_files": files}


def archive_source(version: dict, destination: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
        for name, expected in version["code_files"].items():
            if file_hash(root / name) != expected:
                raise ValueError(f"source changed while archiving: {name}")
            archive.write(root / name, name)


def freeze(suite_path: Path, output: Path, development: Path | None = None) -> dict[str, Any]:
    suite = Suite.model_validate_json(suite_path.read_bytes())
    if suite.split == "holdout":
        if development is None:
            raise ValueError("holdout freeze requires the development suite for leakage checks")
        dev = Suite.model_validate_json(development.read_bytes())
        counts = [sum(c.group == group for c in suite.cases) for group in ("office", "research", "context", "workflow")]
        if len(suite.cases) not in (12, 24) or len(set(counts)) != 1:
            raise ValueError("freeze a balanced holdout of 12 or 24 tasks")
        if {c.template_family for c in suite.cases} & {c.template_family for c in dev.cases}:
            raise ValueError("holdout must use different task-template families")
        dev_hashes = set(snapshot(development, dev)["input_hashes"].values())
        if dev_hashes & set(snapshot(suite_path, suite)["input_hashes"].values()):
            raise ValueError("holdout must use different source materials")
    frozen = {**snapshot(suite_path, suite), **code_version(), "frozen_at": time.time()}
    write_json(output, frozen)
    return frozen


def verify_freeze(suite_path: Path, freeze_path: Path) -> tuple[Suite, dict]:
    frozen = json.loads(freeze_path.read_bytes())
    suite = Suite.model_validate_json(suite_path.read_bytes())
    if snapshot(suite_path, suite)["dataset_sha256"] != frozen["dataset_sha256"]:
        raise ValueError("suite/answers/inputs changed after freezing")
    if code_version()["code_sha256"] != frozen["code_sha256"]:
        raise ValueError("code changed after freezing; create a new freeze and run directory")
    return suite, frozen


async def run_trial(case: Case, suite: Suite, frozen: dict, source: Path, output: Path, spec: ModelSpec,
                    api_key: str, arm: str, index: int, price: dict | None = None) -> dict:
    trial_id = f"{case.id}-{arm}-{index}"
    trial = output / secrets.token_hex(12)
    trial.mkdir(mode=0o700)  # never overwrite a failed trial, or silently rerun it
    workspace = trial / "workspace"
    workspace.mkdir()
    input_hashes = {n: frozen["input_hashes"][n] for n in case.inputs}
    for name in case.inputs:
        data = (source / "inputs" / name).read_bytes()
        p = workspace / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if file_hash(p) != input_hashes[name]:
            raise ValueError(f"input changed: {name}")
    provider = {k: v for k, v in dataclasses.asdict(spec.provider).items() if k != "api_key"}
    model = {k: v for k, v in dataclasses.asdict(spec).items() if k != "provider"}
    public = {"arm": arm, "provider": provider, "model": model, "turns": case.turns, "inputs": case.inputs,
              "initial_prompt": json.loads((output / "run.json").read_bytes())["initial_prompt"],
              "allow_clarification": case.allow_clarification, "clarification_reply": case.clarification_reply,
              "limits": suite.limits.model_dump()}
    write_json(trial / "public.json", public)
    base = {"suite_id": suite.id, "split": suite.split, "trial_id": trial_id, "case_id": case.id,
            "group": case.group, "trial_index": index, "arm": arm, "git_sha": frozen["git_sha"],
            "code_sha256": frozen["code_sha256"], "dataset_sha256": frozen["dataset_sha256"],
            "provider_model_version": {"provider": spec.provider.name, "model": spec.id},
            "config_sha256": digest({"provider": provider, "model": model, "limits": public["limits"],
                                     "initial_prompt": public["initial_prompt"],
                                     "tools": ["read", "write", "edit", "create_document", "present", "todo",
                                               "ask_user"]}),
            "environment_id": str(trial), "cache_track": "provider_observed_no_client_flush"}
    usage.append(output / "trials.jsonl", {**base, "type": "trial.started", "status": "running"})
    # Only the selected credential crosses the boundary. No host HOME, Skills, browser, CLI or connector state.
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR",
                                                       "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")}
    env.update(HOME=str(trial / "os-home"), NWB_HOME=str(trial / "home"),
               PYTHONPATH=str(Path(__file__).resolve().parents[2]), NWB_EVAL_API_KEY=api_key)
    (trial / "os-home").mkdir()
    start = time.monotonic()
    with (trial / "stdout.log").open("wb") as stdout, (trial / "stderr.log").open("wb") as stderr:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "nano_workbuddy.eval.worker", str(trial),
                                                    cwd=workspace, env=env, stdout=stdout, stderr=stderr,
                                                    start_new_session=True)
        try:
            await asyncio.wait_for(proc.wait(), suite.limits.seconds)
        except TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            status, failure = "timeout", "budget"
        except asyncio.CancelledError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            raise  # the run's finally report retains trial.started as an interrupted failure
        else:
            status, failure = ("error", "tool_runtime") if proc.returncode else ("ok", None)
    result_path = trial / "worker-result.json"
    system_prompt_hash = None
    if result_path.exists() and status != "timeout":
        result = json.loads(result_path.read_bytes())
        status, failure = result["status"], result["failure_kind"]
        system_prompt_hash = result.get("system_prompt_sha256")
    agent_seconds = time.monotonic() - start
    checks = grade(case, workspace, input_hashes)
    event_paths = sorted((trial / "home" / "usage").glob("*.jsonl"))
    evidence = [e for p in event_paths for e in usage.events(p)]
    calls = [e for e in evidence if e["type"].startswith("model.") or e["type"] == "ledger.corrupt"]
    write_json(trial / "run-diagnostics.json", [e for e in evidence if e not in calls])
    write_json(trial / "calls.json", calls)
    summed = {k: sum(e["usage"].get(k, 0) for e in calls if e["type"] == "model.response")
              for k in ("input", "output", "cache_read", "cache_write")}
    starts = [e for e in calls if e["type"] == "model.started"]
    responses = [e for e in calls if e["type"] == "model.response"]
    complete = (bool(starts) and len(responses) == len(starts) and all(e["usage_complete"] for e in responses)
                and not any(e["type"] == "ledger.corrupt" for e in calls))
    human = [{"id": r.id, "criterion": r.criterion, "ok": None} for r in case.rubric]
    artifacts = [{"path": str(p.relative_to(workspace)), "sha256": file_hash(p)}
                 for p in sorted(workspace.rglob("*")) if p.is_file() and not p.is_symlink()
                 and str(p.relative_to(workspace)) not in input_hashes]
    local_events = usage.events(trial / "events.jsonl")
    blocked = [e for e in local_events if e["type"] == "scope.blocked"]
    checks.append({"id": "scope", "ok": not blocked, "method": "permission_gate", "detail": len(blocked)})
    passed = verdict(checks, human, status)
    record = {**base, "type": "trial.finished", "status": status, "passed": passed,
              "system_prompt_sha256": system_prompt_hash,
              "observed_model_versions": sorted({e["model_version"] for e in responses}),
              "failure_kind": failure or ("model_judgment" if any(not c["ok"] for c in checks) else None),
              "checks": checks, "human_checks": human, "usage": {"input_total": summed["input"] if complete else None,
                  "output_total": summed["output"] if complete else None,
                  "cache_read": summed["cache_read"] if complete else None,
                  "cache_write": summed["cache_write"] if complete else None,
                  "complete": complete, "observed_partial": summed},
              "cost": calculate(calls, price), "model_calls": len(starts),
              "agent_seconds": agent_seconds, "human_wait_seconds": 0,
              "clarifications": sum(e["type"] == "clarification" for e in local_events),
              "required_approvals": 0, "rescue_interventions": 0, "human_rework_minutes": None,
              "trace_path": str(trial / "home" / "sessions"), "artifact_hashes": artifacts,
              "tool_api_cost": {"amount": 0, "reason": "no external tool APIs in this suite"}}
    write_json(trial / "result.json", record)
    usage.append(output / "trials.jsonl", record)
    return record


def records(output: Path) -> list[dict]:
    latest = {}
    for e in usage.events(output / "trials.jsonl"):
        latest[e["trial_id"]] = e
    for e in usage.events(output / "grades.jsonl"):
        latest[e["trial_id"]].update(e["update"])
    for e in usage.events(output / "annotations.jsonl"):
        latest[e["trial_id"]].setdefault("annotations", []).append(e["note"])
    for e in usage.events(output / "reviews.jsonl"):
        row = latest[e["trial_id"]]
        row["human_checks"] = e["checks"]
        row["passed"] = verdict(row["checks"], e["checks"], row["status"])
    # An interrupted parent must not disappear from the denominator.
    return [r if r["type"] == "trial.finished" else {
        **r, "status": "interrupted", "passed": False, "failure_kind": "tool_runtime", "checks": [],
        "human_checks": [], "cost": {"currency": None, "amount": None, "complete": False},
    } for r in latest.values()]


def regrade(output: Path, corrections: Path) -> None:
    """Development-only grader corrections, with a complete versioned audit trail; never reruns the Agent."""
    frozen = json.loads((output / "freeze.json").read_bytes())
    suite = Suite.model_validate(frozen["suite"])
    if suite.split != "development":
        raise ValueError("holdout grading rules are frozen; do not recalibrate on holdout results")
    changes = json.loads(corrections.read_bytes())
    if not changes.get("reason") or not changes.get("overrides"):
        raise ValueError("grader correction needs a reason and explicit per-check overrides")
    from .schema import Check

    version = code_version()
    tag = f"{file_hash(corrections)[:12]}-{version['code_sha256'][:12]}"
    archive = output / f"grader-source-{tag}.zip"
    if not archive.exists():
        archive_source(version, archive)
    write_json(output / f"grading-corrections-{tag}.json", changes)
    for row in records(output):
        if row["case_id"] not in changes["overrides"] or row["type"] != "trial.finished":
            continue
        original = next(c for c in suite.cases if c.id == row["case_id"])
        patch = changes["overrides"][row["case_id"]]
        if not set(patch) <= {c.id for c in original.checks}:
            raise ValueError("corrections may change only existing deterministic checks")
        if any(not set(p) <= {"kind", "path", "expected", "locator"} for p in patch.values()):
            raise ValueError("correction may change only kind/path/expected/locator")
        case = original.model_copy(deep=True)
        case.checks = [Check.model_validate({**c.model_dump(), **patch.get(c.id, {})}) for c in case.checks]
        workspace = Path(row["environment_id"]) / "workspace"
        hashes = {n: frozen["input_hashes"][n] for n in case.inputs}
        checks = grade(case, workspace, hashes)
        checks += [c for c in row["checks"] if c["id"] == "scope"]
        failure = row["failure_kind"]
        if failure == "model_judgment" and all(c["ok"] for c in checks):
            failure = None
        update = {"checks": checks, "passed": verdict(checks, row["human_checks"], row["status"]),
                  "failure_kind": failure, "grading_revision": tag, "grading_reason": changes["reason"]}
        usage.append(output / "grades.jsonl", {"type": "trial.regraded", "trial_id": row["trial_id"],
                                               "grader_code_sha256": version["code_sha256"],
                                               "corrections_sha256": file_hash(corrections), "update": update})
    report(output)


def report(output: Path) -> dict:
    manifest = json.loads((output / "run.json").read_bytes())
    rows = records(output)
    result = {"suite_id": manifest["suite_id"], "split": manifest["split"],
              "planned_trials": manifest["planned_trials"], "observed_trials": len(rows),
              "run_complete": len(rows) == manifest["planned_trials"]
                              and all(r["type"] == "trial.finished" for r in rows),
              **summarize(rows, manifest["repeats"])}
    if not result["run_complete"]:
        result["paired_completion"] = None
    write_json(output / "report.json", result)
    (output / "report.md").write_text(markdown(result, rows), encoding="utf-8")
    return result


def blind_packets(output: Path, destination: Path) -> None:
    """Copy artifacts and grading cards without arm labels; keep the crosswalk with the run owner."""
    frozen = json.loads((output / "freeze.json").read_bytes())
    cases = {c["id"]: c for c in frozen["suite"]["cases"]}
    destination.mkdir(mode=0o700, parents=True)
    mapping = {}
    for row in records(output):
        blind_id = secrets.token_hex(8)
        mapping[blind_id] = row["trial_id"]
        packet = destination / blind_id
        packet.mkdir()
        workspace = Path(row["environment_id"]) / "workspace"
        # Only supplied files and resulting artifacts, never home, public.json, calls or runtime metadata.
        shutil.copytree(workspace, packet / "workspace", symlinks=True)
        write_json(packet / "card.json", {"blind_id": blind_id, "case": cases[row["case_id"]]})
    write_json(output / f"review-map-{secrets.token_hex(4)}.json", mapping)


async def run(suite_path: Path, freeze_path: Path, output: Path, spec: ModelSpec, api_key: str,
              repeats: int = 1, price: dict | None = None) -> dict:
    suite, frozen = verify_freeze(suite_path, freeze_path)
    if suite.split == "holdout" and repeats != 3:
        raise ValueError("holdout tasks require three repetitions")
    output.mkdir(mode=0o700, parents=True)  # no exist_ok: an existing run is evidence, not scratch
    write_json(output / "freeze.json", frozen)
    archive_source(frozen, output / "source.zip")
    # Capture once, including the date. No host project instructions or user memory enter the eval.
    template_root = output / "__empty_project_template__"
    initial = prompt.build(template_root, output / "__empty_home_template__")
    initial["text"] = initial["text"].replace(str(template_root), ".")
    initial["hash"] = hashlib.sha256(initial["text"].encode()).hexdigest()[:16]
    write_json(output / "run.json", {"suite_id": suite.id, "split": suite.split, "repeats": repeats,
                                     "planned_trials": len(suite.cases) * repeats * 2,
                                     "initial_prompt": initial,
                                     "python": sys.version, "platform": sys.platform,
                                     "dependencies": {n: importlib.metadata.version(n)
                                                      for n in ("openai", "anthropic", "pydantic")},
                                     "model": dataclasses.asdict(spec) | {"provider": {
                                         "name": spec.provider.name, "protocol": spec.provider.protocol,
                                         "base_url": spec.provider.base_url}}, "price_snapshot": price})
    try:
        for index in range(1, repeats + 1):
            for n, case in enumerate(suite.cases):
                arms = ("B0", "B1") if (n + index) % 2 else ("B1", "B0")
                for arm in arms:
                    if code_version()["code_sha256"] != frozen["code_sha256"]:
                        raise ValueError("code changed during this run; retaining completed trials and stopping")
                    row = await run_trial(case, suite, frozen, suite_path.parent, output, spec, api_key,
                                          arm, index, price)
                    print(f"{row['trial_id']}: runtime={row['status']} passed={row['passed']} "
                          f"calls={row['model_calls']}", flush=True)
                    report(output)
    finally:
        report(output)
    return report(output)
