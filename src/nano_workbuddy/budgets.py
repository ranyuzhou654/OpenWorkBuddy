"""Small durable run limits. All logical model requests (including summaries) share one counter.

These counters include failed attempts. SDK-internal HTTP retries are reported separately and cannot
be inferred as additional billed requests. Wall time includes approval waits and restart downtime.
"""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class Limits:
    model_calls: int = 60
    tool_calls: int = 200
    wall_seconds: int = 900
    background_concurrency: int = 2

    def __post_init__(self):
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value < 1:
                raise ValueError(f"budget {field.name} must be a positive integer")


class BudgetExceeded(Exception):
    pass


class RunBudget:
    def __init__(self, path: Path, run_id: str, defaults: Limits, started_at: float, overrides: dict,
                 legacy_tool_starts: int = 0):
        from . import usage

        self.path, self.run_id = path, run_id
        events = [e for e in usage.events(path) if e.get("run_id") == run_id]
        frozen = next((e for e in events if e["type"] == "budget.frozen"), None)
        values = dataclasses.asdict(defaults) | {k: v for k, v in overrides.items()
                                                if k in ("model_calls", "tool_calls", "wall_seconds")}
        self.limits = Limits(**(frozen["limits"] if frozen else values))
        self.deadline = started_at + self.limits.wall_seconds
        # Legacy runs have only the model ledger; those starts still consume the model budget.
        self.models = sum(e["type"] == "model.started" for e in events)
        counted_tools = sum(e["type"] == "budget.tool" for e in events)
        legacy_tools = frozen.get("legacy_tool_calls", 0) if frozen else max(0, legacy_tool_starts - counted_tools)
        self.tools = counted_tools + legacy_tools
        if not frozen:
            self.event("budget.frozen", limits=dataclasses.asdict(self.limits), deadline=self.deadline,
                       legacy_tool_calls=legacy_tools)

    def event(self, kind: str, **data) -> None:
        from . import usage

        usage.append(self.path, {"type": kind, "run_id": self.run_id, **data})

    def model(self) -> None:
        self.check_model()
        self.models += 1  # MeasuredModel appends model.started before entering the provider

    def check_model(self) -> None:
        self.check_wall()
        if self.models >= self.limits.model_calls:
            raise BudgetExceeded("model_calls")

    def tool(self, name: str) -> None:
        self.check_wall()
        if self.tools >= self.limits.tool_calls:
            raise BudgetExceeded("tool_calls")
        self.event("budget.tool", tool=name)
        self.tools += 1

    def check_wall(self) -> None:
        if time.time() >= self.deadline:
            raise BudgetExceeded("wall_seconds")

    def remaining(self) -> dict[str, int]:
        return {"model_calls": max(0, self.limits.model_calls - self.models),
                "wall_seconds": max(0, int(self.deadline - time.time()))}

    def finishing(self) -> bool:
        left = self.remaining()
        return (left["model_calls"] <= min(3, max(1, self.limits.model_calls // 4))
                or left["wall_seconds"] <= min(45, max(1, self.limits.wall_seconds // 4)))
