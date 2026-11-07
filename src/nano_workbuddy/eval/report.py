"""Keep failures and missing usage in denominators; aggregate repeated trials by task."""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Any


def metrics(rows: list[dict], repeats: int) -> dict[str, Any]:
    tasks = defaultdict(list)
    for row in rows:
        tasks[row["case_id"]].append(row)
    passed = sum(r["passed"] is True for r in rows)
    pending = sum(r["passed"] is None for r in rows)
    stable = sum(len(rs) == repeats and all(r["passed"] is True for r in rs) for rs in tasks.values())
    costs_known = bool(rows) and all(r["cost"]["complete"] for r in rows)
    currencies = {r["cost"]["currency"] for r in rows}
    costs_known &= len(currencies) == 1
    cost = sum(r["cost"]["amount"] for r in rows) if costs_known else None
    return {"passed": passed, "attempts": len(rows), "pending_review": pending,
            "completion_rate": passed / len(rows) if rows and not pending else None,
            "all_repeats_passed": stable, "tasks": len(tasks), "repeats": repeats,
            "pass3": stable / len(tasks) if repeats == 3 and tasks and not pending else None,
            "cost_complete": costs_known, "total_cost": cost,
            "cost_per_attempt": cost / len(rows) if costs_known else None,
            "cost_per_success": cost / passed if costs_known and passed and not pending else None}


def paired_interval(rows: list[dict], samples: int = 2000, seed: int = 20261025) -> dict[str, Any] | None:
    by_task = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        if r["passed"] is None:
            return None
        by_task[r["case_id"]][r["arm"]][r["trial_index"]] = int(r["passed"])
    diffs = []
    for arms in by_task.values():
        b0, b1 = arms["B0"], arms["B1"]
        if not b0 or b0.keys() != b1.keys():
            return None
        diffs.append(sum(b1[i] - b0[i] for i in b0) / len(b0))
    if not diffs:
        return None
    rng = random.Random(seed)
    boot = sorted(sum(rng.choices(diffs, k=len(diffs))) / len(diffs) for _ in range(samples))
    return {"method": "paired task-cluster bootstrap percentile", "tasks": len(diffs),
            "difference_pp": 100 * sum(diffs) / len(diffs),
            "ci95_pp": [100 * boot[int(samples * .025)], 100 * boot[int(samples * .975)]],
            "seed": seed, "samples": samples}


def summarize(rows: list[dict], repeats: int) -> dict[str, Any]:
    arms = {arm: metrics([r for r in rows if r["arm"] == arm], repeats) for arm in ("B0", "B1")}
    groups = {g: {a: metrics([r for r in rows if r["arm"] == a and r["group"] == g], repeats)
                   for a in ("B0", "B1")} for g in sorted({r["group"] for r in rows})}
    return {"arms": arms, "groups": groups, "paired_completion": paired_interval(rows)}


def markdown(report: dict, rows: list[dict]) -> str:
    lines = ["# Local Agent evaluation", "", f"Split: **{report['split']}**. Suite: `{report['suite_id']}`.",
             f"Observed: {report['observed_trials']}/{report['planned_trials']} planned trials. "
             f"Run complete: {report['run_complete']}.",
             "", "Development runs are calibration data, not holdout or benchmark scores.",
             "Unknown cost is not zero. Pending human reviews are not passes.", "",
             "| Arm | Confirmed passes / attempts | Pending review | Tasks passing all repeats | Cost/success |",
             "| --- | --- | --- | --- | --- |"]
    for arm, m in report["arms"].items():
        lines.append(f"| {arm} | {m['passed']}/{m['attempts']} | {m['pending_review']} | "
                     f"{m['all_repeats_passed']}/{m['tasks']} ({m['repeats']} repeat) | "
                     f"{m['cost_per_success'] if m['cost_complete'] else 'unknown'} |")
    lines += ["", "## Trials", "", "| Trial | Arm | Runtime | Automatic checks | Human review | Passed |",
              "| --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        auto = all(c["ok"] for c in r["checks"])
        pending = sum(c["ok"] is None for c in r["human_checks"])
        lines.append(f"| {r['trial_id']} | {r['arm']} | {r['status']} | {auto} | "
                     f"{pending} pending | {r['passed']} |")
    lines += ["", "Raw trials, calls, inputs and artifacts are retained beside this report.",
              "Scope: local materials and file tools; no live search, Feishu, browser or scheduler acceptance.", ""]
    return "\n".join(lines)
