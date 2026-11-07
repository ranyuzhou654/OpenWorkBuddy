"""Deterministic checks; a file opening successfully never overrides an incorrect fact."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from .. import artifacts, documents
from .schema import Case, file_hash


def read_text(path: Path) -> str:
    return documents.load(path).text() if documents.is_document(path) else path.read_text(encoding="utf-8")


def grade(case: Case, workspace: Path, inputs: dict[str, str]) -> list[dict[str, Any]]:
    out = []
    for c in case.checks:
        try:
            p = (workspace / c.path).resolve()
            if not p.is_relative_to(workspace.resolve()) or not p.is_file():
                raise ValueError("missing or out-of-scope file")
            if c.kind == "unchanged":
                ok = file_hash(p) == inputs[c.path]
            elif c.kind == "structure":
                checks, _ = artifacts.check(p)
                ok = bool(checks) and all(x["ok"] for x in checks)
            elif c.kind == "json":
                ok = json.loads(p.read_text()) == c.expected
            elif c.kind == "numbers":
                # Parse complete numeric tokens: 2,160 and 2160 compare equal; 21600 does not.
                body = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", read_text(p))
                found = {Decimal(m.group().replace(",", "")) for m in re.finditer(
                    r"(?<![\d.])-?\d+(?:\.\d+)?(?!\d|\.\d)", body)}
                ok = all(Decimal(str(n)) in found for n in c.expected)
            else:
                body = read_text(p)
                values = c.expected if isinstance(c.expected, list) else [c.expected]
                ok = all(v in body for v in values) if c.kind == "contains" else all(v not in body for v in values)
            out.append({"id": c.id, "ok": ok, "method": c.kind, "detail": c.path})
        except Exception as exc:
            out.append({"id": c.id, "ok": False, "method": c.kind,
                        "detail": f"{c.path}: {type(exc).__name__}: {exc}"})
    for name, expected in inputs.items():
        p = workspace / name
        ok = p.is_file() and not p.is_symlink() and file_hash(p) == expected
        out.append({"id": f"input_integrity:{name}", "ok": ok, "method": "sha256", "detail": name})
    return out


def verdict(checks: list[dict], rubric: list[dict], status: str) -> bool | None:
    if status != "ok" or any(c["ok"] is False for c in checks + rubric):
        return False
    return None if any(c["ok"] is None for c in checks + rubric) else True
