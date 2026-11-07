"""Metrics and synthetic retrieval fixtures for bounded, development-only experiments."""
from __future__ import annotations

import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

from .. import artifacts, usage
from ..types import Entry, text


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    at = (len(ordered) - 1) * p
    lo, hi = math.floor(at), math.ceil(at)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (at - lo)


def ranked_metrics(ids: list[str], relevant: list[str]) -> dict:
    # Relevant IDs represent evidence units, not duplicated renderings of the same document.
    unique = list(dict.fromkeys(ids))
    gold = set(relevant)
    return {
        "recall5": len(set(unique[:5]) & gold) / len(gold) if gold else None,
        "recall10": len(set(unique[:10]) & gold) / len(gold) if gold else None,
        "hit5": bool(set(unique[:5]) & gold) if gold else None,
        "mrr10": next((1 / (i + 1) for i, rid in enumerate(unique[:10]) if rid in gold), 0) if gold else None,
        "false_return": bool(unique) if not gold else None,
    }


def aggregate_retrieval(rows: list[dict]) -> dict:
    positive = [r for r in rows if r["relevant"]]
    negative = [r for r in rows if not r["relevant"]]
    def mean(key):
        return statistics.mean(r[key] for r in positive) if positive else None
    return {"queries": len(rows), "answerable_queries": len(positive), "no_answer_queries": len(negative),
            **{key: mean(key) for key in ("recall5", "recall10", "hit5", "mrr10")},
            "false_returns": sum(r["false_return"] for r in negative),
            "false_return_rate": sum(r["false_return"] for r in negative) / len(negative) if negative else None,
            "scope_violations": sum(r["scope_violations"] for r in rows),
            "unauthorized_read_probes": len(rows),
            "unauthorized_read_successes": sum(r["unauthorized_read"] for r in rows),
            "first_in_process_ms_p50": percentile([r["first_ms"] for r in rows], .5),
            "first_in_process_ms_p95": percentile([r["first_ms"] for r in rows], .95),
            "repeat_ms_p50": percentile([v for r in rows for v in r["repeat_ms"]], .5),
            "repeat_ms_p95": percentile([v for r in rows for v in r["repeat_ms"]], .95)}


def ledger_metrics(paths: list[Path]) -> dict:
    events = [e for p in paths for e in usage.events(p)]
    starts = {e["call_id"]: e for e in events if e["type"] == "model.started"}
    responses = {e["call_id"]: e for e in events if e["type"] == "model.response"}
    complete = bool(starts) and starts.keys() == responses.keys() and all(
        r.get("usage_complete", False) for r in responses.values())
    totals = {k: sum(r["usage"].get(k, 0) for r in responses.values())
              for k in ("input", "output", "cache_read")}
    return {"started_calls": len(starts), "responses": len(responses), "usage_complete": complete,
            "observed_usage": totals, "actual_input_tokens": totals["input"] if complete else None,
            "summary_calls": sum(e.get("purpose") == "compaction" for e in starts.values()),
            "observed_models": sorted({r.get("model_version", "unknown") for r in responses.values()}),
            "cost": None}


def repeated_reads(reads: list[dict]) -> dict:
    seen: set[tuple] = set()
    count, chars = 0, 0
    for r in reads:
        key = (r["path"], r["sha256"], r["offset"], r["limit"])
        if key in seen:
            count += 1
            chars += r["chars"]
        seen.add(key)
    return {"reads": len(reads), "read_chars": sum(r["chars"] for r in reads),
            "exact_repeat_reads": count, "exact_repeat_chars": chars,
            "definition": "same resolved path, content hash and requested line range; overlapping ranges not counted"}


def request_chars(system: str, messages: list, tools: list) -> int:
    # One reproducible serialization in both snapshots; this is not a tokenizer/billing count.
    return len(json.dumps({"system": system, "messages": messages, "tools": tools},
                          ensure_ascii=False, sort_keys=True))


def record_id(session: str, kind: str, ref: str) -> str:
    return "rec_" + hashlib.sha256(f"{session}:{kind}:{ref}".encode()).hexdigest()[:24]


def retrieval_fixture() -> tuple[list[dict], list[dict]]:
    records, queries = [], []
    categories = ("chinese", "short", "mixed", "compound", "paraphrase", "versions", "same_filename",
                  "selected_cross_project", "projectless_session", "no_answer_or_excluded")
    for category_index, category in enumerate(categories):
        for j in range(12):
            token = f"专题{category_index:02}{j:02}松岚"
            key = f"NW{category_index:02}{j:02}"
            stem = f"{token} delivery-{key} 预算72000元 交付日期2026-11-18 验收"
            variants = [
                ("old", "p0", "s0", stem.replace("72000", "61000") + " 草案", 1),
                ("new", "p0", "s0", stem + " 最终确认", 2),
                ("other", "p1", "s1", stem.replace("72000", "99000") + " 最终确认", 3),
                ("excluded", "p2", "s2", stem + f" 排除专属{key}", 4),
            ]
            by_variant = {}
            for name, project, session, body, at in variants:
                kind = "file" if category == "same_filename" else "source" if category == "mixed" else "message"
                ref = f"{key}-{name}.md"
                identity = (f"{ref}@S1" if kind == "source" else
                            f"{ref}:{hashlib.sha256(body.encode()).hexdigest()}" if kind == "file" else ref)
                rid = record_id(session, kind, identity)
                records.append(dict(id=rid, title=f"{token}-交付说明.md", body=body, kind=kind, ref=ref,
                                    session_id=session, project_id=project, at=at))
                by_variant[name] = rid
            query, relevant = token, [by_variant["new"]]
            if category == "chinese":
                relevant = [by_variant["old"], by_variant["new"]]
            elif category == "short":
                query, relevant = f"{token} 验收", [by_variant["old"], by_variant["new"]]
            elif category == "mixed":
                query, relevant = f"delivery-{key} 最终确认", [by_variant["new"]]
            elif category == "compound":
                query = f"{token} 预算72000 最终确认"
            elif category == "paraphrase":
                query = f"{token} 截止时间"  # Intentional lexical gap; do not insert answer aliases.
            elif category in ("versions", "same_filename"):
                query = f"{token} 最终确认"
            elif category == "selected_cross_project":
                query, relevant = f"{token} 预算99000", [by_variant["other"]]
            elif category == "projectless_session":
                query = f"{token} 最终确认"
            elif category == "no_answer_or_excluded":
                query, relevant = (f"排除专属{key}" if j % 2 else f"不存在记录{key}"), []
            sessions = ["s0", "s1"] if category == "selected_cross_project" else ["s0"]
            scope = dict(session_ids=sessions, project_id=None if len(sessions) > 1 else "p0",
                         session_id="s0" if category == "projectless_session" else None, access_scope="local")
            queries.append(dict(id=f"Q-{category_index:02}-{j:02}", category=category, query=query,
                                split="development" if j < 4 else "frozen-test", relevant=relevant,
                                scope=scope, forbidden_id=by_variant["excluded"]))
    return records, queries


def write_corpus(root: Path, records: list[dict], size: int, store: Path) -> list[dict]:
    if size < len(records):
        raise ValueError("corpus must fit all labelled records")
    root.mkdir(parents=True, exist_ok=False)
    groups: dict[str, list] = {}
    for r in records:
        groups.setdefault(r["session_id"], []).append(r)
    filler = groups.setdefault("s0", [])
    filler.extend(dict(id=f"filler-{i}", title="日常归档", body=f"例行记录{i:07}，设备维护与库存盘点。",
                       kind="message", ref=f"filler-{i}", session_id="s0", project_id="p0", at=0)
                  for i in range(size - len(records)))
    rows = []
    for sid, items in groups.items():
        file = root / f"{sid}.jsonl"
        with file.open("x", encoding="utf-8") as stream:
            for r in items:
                if r["kind"] == "message":
                    entry = Entry(r["ref"], None, "user", {"content": [text(r["body"])]}, ts=r["at"])
                elif r["kind"] == "source":
                    entry = Entry(r["ref"], None, "tool_result", {"content": [text(r["body"])],
                        "meta": {"sources": [{"n": 1, "url": r["ref"], "title": r["title"],
                                             "snippet": r["body"]}]}}, ts=r["at"])
                else:
                    digest = artifacts.keep_bytes(store, r["body"].encode())
                    entry = Entry(r["ref"], None, "tool_result", {"content": [text("delivered")],
                        "meta": {"artifact": {"sha256": digest, "title": r["title"],
                                              "name": r["ref"], "path": r["ref"]}}}, ts=r["at"])
                stream.write(json.dumps(entry.to_json(), ensure_ascii=False) + "\n")
        rows.append(dict(id=sid, project_id=items[0]["project_id"], title="合成历史", file_path=str(file)))
    return rows
