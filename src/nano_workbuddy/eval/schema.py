"""Frozen case cards and trial contracts. Answers are never sent to the worker."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Limits(Strict):
    model_calls: int = Field(12, ge=1)
    seconds: int = Field(180, ge=1)
    max_turns: int = Field(12, ge=1)
    sdk_retries: Literal[0] = 0


class Check(Strict):
    id: str
    kind: Literal["contains", "not_contains", "numbers", "json", "unchanged", "structure"]
    path: str
    expected: Any = None
    locator: str = ""  # source page/paragraph/cell used to establish the answer


class Rubric(Strict):
    id: str
    criterion: str


class Case(Strict):
    id: str
    group: Literal["office", "research", "context", "workflow"]
    template_family: str
    inputs: list[str]
    turns: list[str] = Field(min_length=1)
    checks: list[Check] = Field(min_length=1)
    rubric: list[Rubric] = Field(default_factory=list)
    allow_clarification: bool = False
    clarification_reply: str | None = None

    @model_validator(mode="after")
    def paths_are_relative(self):
        for name in [*self.inputs, *(c.path for c in self.checks)]:
            p = Path(name)
            if p.is_absolute() or ".." in p.parts or not p.parts:
                raise ValueError(f"case path must stay relative: {name}")
        ids = [c.id for c in self.checks] + [r.id for r in self.rubric]
        if len(ids) != len(set(ids)):
            raise ValueError("check/rubric ids must be unique")
        return self


class Suite(Strict):
    id: str
    split: Literal["development", "holdout"]
    description: str
    limits: Limits = Field(default_factory=Limits)
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({c.id for c in self.cases}) != len(self.cases):
            raise ValueError("case ids must be unique")
        return self


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_suite(path: Path) -> Suite:
    return Suite.model_validate_json(path.read_bytes())


def snapshot(path: Path, suite: Suite) -> dict[str, Any]:
    paths = sorted({p for c in suite.cases for p in c.inputs})
    hashes = {}
    root = (path.parent / "inputs").resolve()
    for name in paths:
        f = (root / name).resolve()
        if not f.is_relative_to(root) or not f.is_file():
            raise ValueError(f"missing/out-of-scope input: {name}")
        hashes[name] = file_hash(f)
    payload = {"suite": suite.model_dump(), "input_hashes": hashes}
    return {**payload, "dataset_sha256": digest(payload)}
