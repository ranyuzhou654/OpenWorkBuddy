"""Derive an auditable R-D summary; never amend raw trials or infer human acceptance."""
from __future__ import annotations

import argparse
import collections
import json
import shutil
from pathlib import Path

from nano_workbuddy import usage
from nano_workbuddy.eval.runner import write_json
from nano_workbuddy.eval.schema import file_hash


def summarize(source: Path, destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    rows, starts, responses = [], [], []
    totals = collections.Counter()
    for trial in sorted(source.iterdir()):
        if not (trial / "review-card.json").is_file():
            continue
        record = json.loads((trial / "review-card.json").read_text())
        events = json.loads((trial / "calls.json").read_text())
        begin = [e for e in events if e["type"] == "model.started"]
        end = [e for e in events if e["type"] == "model.response"]
        starts += begin
        responses += end
        totals.update({k: sum(e["usage"].get(k, 0) for e in end)
                       for k in ("input", "output", "cache_read", "cache_write")})
        target = destination / trial.name
        target.mkdir(exist_ok=True)
        for name in ("public.json", "worker-result.json", "review-card.json", "calls.json",
                     "artifact-checks.json", "occurrences.json", "accepted-inputs.json"):
            if (trial / name).exists():
                shutil.copyfile(trial / name, target / name)
        home = Path(record["final_home"])
        logs = [json.loads(line) for f in sorted((home / "sessions").glob("*/*.jsonl"))
                for line in f.read_text().splitlines()]
        final_metadata = []
        for e in logs:
            artifact = e.get("data", {}).get("meta", {}).get("artifact")
            if artifact:
                path = Path(artifact["path"])
                if (path.exists() and file_hash(path) == artifact["sha256"]
                        and not any(a["sha256"] == artifact["sha256"] for a in final_metadata)):
                    final_metadata.append(artifact)
        write_json(target / "final-artifact-metadata.json", final_metadata)
        shutil.copytree(home / "sessions", target / "sessions", dirs_exist_ok=True)
        for name in ("backup.json", "restore.json"):
            if (trial / name).exists():
                shutil.copyfile(trial / name, target / name)
        outputs = target / "outputs"
        outputs.mkdir(exist_ok=True)
        for entry in record["artifact_checks"]:
            path = trial / "workspace" / entry["path"]
            if file_hash(path) != entry["sha256"]:
                raise ValueError(f"output changed after trial: {path}")
            shutil.copyfile(path, outputs / path.name)
        # Corrections are diagnostic annotations, never retroactive passing grades.
        scopes = record.get("record_scopes", [])
        confounded = record["id"].startswith("U6") and (
            not scopes or any(s.get("session_id") is not None for s in scopes))
        row = {**record, "evidence_path": str(target),
               "interpretation": "harness_configuration_confounded" if confounded else "pending_human_review",
               "harness_issue": "scratch_dir == project root imposed session-only recall" if confounded else None,
               "elapsed_scope": "execution through automatic checks; excludes later human review",
               "completion_elapsed_seconds": None,
               "calls_per_run": dict(collections.Counter(e["run_id"] for e in begin)),
               "tool_errors": sum(e.get("type") == "tool_result"
                                  and bool(e.get("data", {}).get("is_error")) for e in logs),
               "usage_complete": len(begin) == len(end) and all(e.get("usage_complete") for e in end),
               "scope_blocks": sum(e["type"] == "scope.blocked" for e in usage.events(trial / "events.jsonl"))}
        write_json(target / "annotated-card.json", row)
        rows.append(row)
    summary = {
        "scope": "Six local trials using real repository materials; historical replay, not natural longitudinal usage",
        "provider": "opendesign", "requested_model": "deepseek-v4.1-flash",
        "observed_models": sorted({e["model_version"] for e in responses}),
        "trials": len(rows), "model_requests": len(starts), "model_responses": len(responses),
        "sdk_retries": sorted({e.get("sdk_max_retries") for e in starts}),
        "runtime_status_counts": dict(collections.Counter(r["status"] for r in rows)),
        "harness_confounded_trials_retained": [r["id"] for r in rows if r["harness_issue"]],
        "usage": dict(totals), "usage_complete": all(r["usage_complete"] for r in rows),
        "cache_note": "input includes cache_read; do not add cache_read to input again",
        "cost": {"amount": None, "complete": False, "reason": "subscription billing unknown"},
        "sum_trial_seconds": sum(r["elapsed_seconds"] for r in rows),
        "human_acceptance_elapsed_seconds": None,
        "human_reviews_completed": 0, "task_completion_rate": None,
        "actual_external_writes": 0, "scope_blocks": sum(r["scope_blocks"] for r in rows),
        "remaining_unexecuted_slots": ["U2-1", "U2-2", "U3-1", "U3-2", "U4-1", "U4-2"],
        "natural_restart_sleep_observed": False,
        "trials_summary": rows,
        "raw_source": str(source),
        "source_archive_sha256": file_hash(source / "source.zip"),
        "executed_runner_sha256": file_hash(source / "runner.py"),
        "all_paid_trials_use_original_executed_runner": True,
        "product_code_sha256": json.loads((source / "freeze.json").read_text())["code_sha256"],
    }
    write_json(destination / "summary.json", summary)
    shutil.copyfile(source / "freeze.json", destination / "freeze.json")
    shutil.copyfile(source / "runner.py", destination / "executed-runner.py")
    shutil.copyfile(source / "trials.jsonl", destination / "raw-trials.jsonl")
    (destination / "evidence-sha256.json").write_text(json.dumps({
        str(p.relative_to(destination)): file_hash(p) for p in sorted(destination.rglob("*"))
        if p.is_file() and p.name != "evidence-sha256.json"}, ensure_ascii=False, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    result = summarize(args.source.resolve(), args.destination.resolve())
    print(json.dumps({k: result[k] for k in (
        "trials", "model_requests", "runtime_status_counts", "usage", "usage_complete",
        "harness_confounded_trials_retained", "sum_trial_seconds")}, ensure_ascii=False, indent=2))
