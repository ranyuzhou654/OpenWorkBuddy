"""When scheduled tasks run (M3): pure functions over a schedule's rule, no I/O.

A rule is a time of day in an IANA time zone plus a repeat: once (a date), daily, or weekly on chosen
weekdays. Occurrences are computed in that zone and stored as UTC instants, so daylight-saving changes and
a machine whose zone changes later never move a task by an hour. Each occurrence has a stable identity
(its planned instant), which is what keeps a restart from firing it twice.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KINDS = ("once", "daily", "weekly")
CATCH_UP = ("once", "skip", "ask")  # after missed occurrences: run one, skip them, or ask the user
GRACE = 15 * 60  # seconds: an occurrence found due later than this was missed (machine asleep, server down)


def local_zone() -> str:
    """The machine's IANA zone name (macOS and Linux keep it in the /etc/localtime link), else UTC."""
    try:
        target = os.readlink("/etc/localtime")
        name = target.split("zoneinfo/", 1)[1]
        ZoneInfo(name)
        return name
    except (OSError, IndexError, ZoneInfoNotFoundError, ValueError):
        return "UTC"


@dataclass(frozen=True)
class Rule:
    kind: str  # once | daily | weekly
    at: str  # "HH:MM", local to tz
    tz: str  # IANA name
    weekdays: tuple[int, ...] = ()  # weekly: ISO weekdays, 1 = Monday
    day: str = ""  # once: "YYYY-MM-DD"

    def validate(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"repeat must be one of {', '.join(KINDS)}")
        try:
            hh, mm = (int(x) for x in self.at.split(":"))
            time(hh, mm)
        except ValueError:
            raise ValueError("time must be HH:MM") from None
        try:
            ZoneInfo(self.tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {self.tz!r}") from None
        if self.kind == "weekly" and (not self.weekdays or any(d not in range(1, 8) for d in self.weekdays)):
            raise ValueError("weekly needs weekdays 1-7 (1 = Monday)")
        if self.kind == "once":
            try:
                date.fromisoformat(self.day)
            except ValueError:
                raise ValueError("once needs a date YYYY-MM-DD") from None

    def _at(self, d: date) -> float:
        hh, mm = (int(x) for x in self.at.split(":"))
        return datetime.combine(d, time(hh, mm), ZoneInfo(self.tz)).timestamp()

    def next_after(self, ts: float) -> float | None:
        """The first occurrence strictly after instant ts, or None (a once rule already past)."""
        zone = ZoneInfo(self.tz)
        if self.kind == "once":
            t = self._at(date.fromisoformat(self.day))
            return t if t > ts else None
        d = datetime.fromtimestamp(ts, zone).date()
        for i in range(0, 8 + 1):  # today .. a week ahead covers every weekly rule
            day = d + timedelta(days=i)
            if self.kind == "weekly" and day.isoweekday() not in self.weekdays:
                continue
            if (t := self._at(day)) > ts:
                return t
        return None

    def between(self, start: float, end: float, limit: int = 1000) -> list[float]:
        """Occurrences in (start, end]: what a sleeping machine missed."""
        out, t = [], start
        while len(out) < limit and (t := self.next_after(t)) is not None and t <= end:
            out.append(t)
        return out

    def describe(self) -> str:
        names = "一二三四五六日"
        if self.kind == "once":
            return f"{self.day} {self.at}"
        if self.kind == "daily":
            return f"每天 {self.at}"
        if set(self.weekdays) == {1, 2, 3, 4, 5}:
            return f"工作日 {self.at}"
        return "每周" + "、".join(names[d - 1] for d in sorted(self.weekdays)) + f" {self.at}"


@dataclass(frozen=True)
class Due:
    """What the scheduler should do for one schedule now."""

    planned: float | None  # the occurrence to run (the latest one missed), or None to run nothing
    missed: int  # how many occurrences are being folded into this decision (1 = on time)
    action: str  # run | skip | ask | none
    next_run: float | None  # where next_run moves to


def decide(rule: Rule, next_run: float | None, now: float, catch_up: str) -> Due:
    """next_run is the earliest occurrence not yet handled. On time (within GRACE): run it. Late: one or more
    occurrences were missed while the machine slept or the server was down, and catch_up decides."""
    if next_run is None or next_run > now:
        return Due(None, 0, "none", next_run)
    missed = [next_run, *rule.between(next_run, now)]
    following = rule.next_after(now)
    latest = missed[-1]
    if now - latest <= GRACE:  # the latest one is on time: run it; earlier misses are folded into it
        return Due(latest, len(missed), "run", following)
    action = {"once": "run", "skip": "skip", "ask": "ask"}[catch_up]
    return Due(latest, len(missed), action, following)
