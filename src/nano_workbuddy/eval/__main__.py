"""python -m nano_workbuddy.eval --help"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from .. import usage
from ..config import load, secret
from . import runner


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Agent evaluation (explicit run only calls a model)")
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze", help="freeze dataset, answers and current code hashes")
    freeze.add_argument("suite", type=Path)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.add_argument("--development-suite", type=Path, help="required for holdout leakage checks")
    run = commands.add_parser("run", help="run alternating B0/B1 trials against a configured model")
    run.add_argument("suite", type=Path)
    run.add_argument("--freeze", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--home", type=Path)
    run.add_argument("--repeats", type=int, choices=(1, 3), default=1)
    run.add_argument("--price", type=Path, help="optional dated provider price JSON; otherwise cost=unknown")
    report = commands.add_parser("report", help="rebuild reports from retained append-only evidence")
    report.add_argument("output", type=Path)
    blind = commands.add_parser("blind", help="export reviewer packets with A/B labels removed")
    blind.add_argument("output", type=Path)
    blind.add_argument("--destination", type=Path, required=True)
    regrade = commands.add_parser("regrade", help="append development-only grader corrections; no model calls")
    regrade.add_argument("output", type=Path)
    regrade.add_argument("--corrections", type=Path, required=True)
    review = commands.add_parser("review", help="append human rubric decisions; never edits trial evidence")
    review.add_argument("output", type=Path)
    review.add_argument("--trial-id", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--checks", type=Path, required=True, help='JSON [{"id": ..., "ok": true, "note": ...}]')
    args = parser.parse_args()
    if args.command == "freeze":
        if args.output.exists():
            parser.error("freeze already exists; use a new path")
        frozen = runner.freeze(args.suite.resolve(), args.output.resolve(),
                               args.development_suite.resolve() if args.development_suite else None)
        print(frozen["dataset_sha256"])
    elif args.command == "run":
        cfg = load(args.home)
        spec = cfg.model(args.model)
        price = json.loads(args.price.read_bytes()) if args.price else None
        asyncio.run(runner.run(args.suite.resolve(), args.freeze.resolve(), args.output.resolve(), spec,
                               secret(spec.provider.api_key, cfg.home), args.repeats, price))
    elif args.command == "report":
        print(json.dumps(runner.report(args.output.resolve()), ensure_ascii=False, indent=2))
    elif args.command == "blind":
        runner.blind_packets(args.output.resolve(), args.destination.resolve())
    elif args.command == "regrade":
        runner.regrade(args.output.resolve(), args.corrections.resolve())
    elif args.command == "review":
        output = args.output.resolve()
        rows = runner.records(output)
        row = next(r for r in rows if r["trial_id"] == args.trial_id)
        frozen = json.loads((output / "freeze.json").read_bytes())
        case = next(c for c in frozen["suite"]["cases"] if c["id"] == row["case_id"])
        checks = json.loads(args.checks.read_bytes())
        if (len(checks) != len(case["rubric"]) or {c["id"] for c in checks} != {c["id"] for c in case["rubric"]}
                or any(type(c.get("ok")) is not bool or not c.get("note") for c in checks)):
            parser.error("review must decide every rubric id with a boolean and an evidence note")
        usage.append(output / "reviews.jsonl", {"trial_id": args.trial_id, "reviewer": args.reviewer,
                                                "checks": checks})
        runner.report(output)


if __name__ == "__main__":
    main()
