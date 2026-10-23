"""Agent Skills (D15): a directory with a SKILL.md whose frontmatter names and describes it.

Only names and descriptions are shown to the model (as a notice); the `skill` tool loads a body on
demand. A loaded body is pinned: if compaction summarises it away, it is put back (loop.sync_notices),
so the model never works from a guess of what the instructions said.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field

from .tools.builtin import Args
from .tools.registry import Result, Tool, ToolCtx, ToolError, tool
from .types import text

MAX_DESCRIPTION = 1_024  # the Agent Skills spec limit
MAX_FILES = 50
SHARED = Path.home() / ".agents" / "skills"  # the cross-tool location from the Agent Skills spec


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path  # the SKILL.md file
    source: str  # "project" or "user"

    @property
    def dir(self) -> Path:
        return self.path.parent


def roots(project: Path | None, home: Path, extra: tuple[str, ...] = ()) -> list[tuple[Path, str]]:
    """Where skills live, most specific first: a project's skills shadow the user's of the same name.
    extra: more directories from [skills] dirs in config.toml, e.g. "~/.claude/skills"."""
    found = [(project / ".agents" / "skills", "project")] if project else []
    return found + [(home / "skills", "user"), (SHARED, "user"), *((Path(d).expanduser(), "user") for d in extra)]


def discover(project: Path | None, home: Path, extra: tuple[str, ...] = ()) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    for base, source in roots(project, home, extra):
        for f in sorted(base.glob("*/SKILL.md")) if base.is_dir() else ():
            if (s := parse(f, source)) and s.name not in skills:
                skills[s.name] = s
    return skills


def parse(f: Path, source: str) -> Skill | None:
    """A skill needs a description; its name defaults to the directory name."""
    meta, _ = split(f.read_text(encoding="utf-8", errors="replace"))
    desc = " ".join(meta.get("description", "").split())
    if not desc:
        return None
    return Skill(meta.get("name") or f.parent.name, desc[:MAX_DESCRIPTION], f, source)


def split(doc: str) -> tuple[dict[str, str], str]:
    """Frontmatter (top-level `key: value` pairs, quoted or block scalars) and body. Nested keys are ignored."""
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", doc, re.S)
    if not m:
        return {}, doc
    meta: dict[str, str] = {}
    key = None
    for line in m.group(1).splitlines():
        if key and (line.startswith((" ", "\t")) or not line.strip()):
            meta[key] += " " + line.strip()  # continuation of a block scalar (| or >)
            continue
        key = None
        if kv := re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line):
            k, v = kv.group(1), kv.group(2).strip()
            if v in ("|", ">", "|-", ">-", ""):
                key, meta[k] = k, ""
            else:
                meta[k] = v[1:-1] if len(v) > 1 and v[0] == v[-1] and v[0] in "'\"" else v
    return {k: v.strip() for k, v in meta.items()}, doc[m.end():]


def listing(skills: dict[str, Skill]) -> str:
    if not skills:
        return ""
    lines = "\n".join(f"- {s.name}: {s.description}" for s in skills.values())
    return ("Skills are packaged instructions for specific tasks. When a task matches a skill's description, "
            "load it with the skill tool before starting, then follow it. Available skills:\n" + lines)


def files(skill: Skill) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(skill.dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        out += [str(Path(dirpath, f).relative_to(skill.dir)) for f in sorted(filenames) if not f.startswith(".")]
    return [f for f in out if f != "SKILL.md"][:MAX_FILES]


def render(skill: Skill) -> str:
    """What the model gets back: the instructions, plus where their relative paths point."""
    _, body = split(skill.path.read_text(encoding="utf-8", errors="replace"))
    out = f'<skill name="{skill.name}" dir="{skill.dir}">\n{body.strip()}\n</skill>'
    if more := files(skill):
        out += ("\nRelative paths above are inside the skill directory. Files there: " + ", ".join(more)
                + ". Read them only when the instructions call for them.")
    return out


class SkillArgs(Args):
    name: str = Field(description="Skill name from the list of available skills")


def skill_tool(available: dict[str, Skill]) -> Tool:
    """The skill tool for one run, over the skills enabled when it started."""

    @tool(effect="read", parallel=True, scope=lambda args, ctx: ("skill", args.name))
    async def skill(args: SkillArgs, ctx: ToolCtx) -> Result:
        """Load a skill's instructions by name. Call this before starting a task that matches the skill."""
        s = available.get(args.name)
        if s is None:
            raise ToolError(f"no skill {args.name!r}; available: {', '.join(available) or 'none'}")
        return Result([text(render(s))], meta={"pin": f"skill:{s.name}"})

    return skill
