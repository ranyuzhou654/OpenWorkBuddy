"""Tiers 2 and 3 of context control (D10): prune old tool output, then summarise older turns.

`fit()` runs before every model request. It is a no-op while the request fits the budget, which is
almost always, so the prompt prefix stays stable. Both tiers append a log entry and never delete
anything: the raw history stays in the JSONL file for the UI, exports and later branches.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .context import (
    budget,
    elided_file,
    entry_tokens,
    estimate_tokens,
    is_prunable,
    pinned,
    request_tokens,
    result_text,
    to_messages,
    visible,
    write_private,
)
from .llm import Model, Response
from .types import (
    ASSISTANT,
    BRANCH_SUMMARY,
    COMPACTION,
    PRUNE,
    TOOL_RESULT,
    USER,
    Entry,
    Message,
    text,
    text_of,
)
from .usage import purpose

if TYPE_CHECKING:
    from .loop import Run

KEEP_RECENT = 20_000  # tokens kept verbatim by both tiers
RESULT_PREVIEW = 2_000  # characters of each tool result shown to the summariser

SUMMARIZER = ("You write handoff summaries of agent conversations. Another model will continue the work from "
              "your summary plus the most recent messages, so be accurate and specific and never invent facts. "
              "Write in the language the user writes in.")

FORMAT = """Write the summary in exactly this format:

## Goal
## Constraints & Preferences
## Progress
### Done
### In Progress
### Blocked
## Key Decisions
## Next Steps
## Critical Context
(exact file paths, identifiers, commands, error messages and values needed to continue)"""


class ContextTooLarge(Exception):
    """Even after pruning and compaction the request does not fit the model's window."""


async def fit(run: Run, overflow: bool = False) -> tuple[str, list[Message]]:
    """The next request's (system, messages), shrunk first if needed. overflow=True means the provider
    already rejected a request as too long: our estimate was low, so shrink regardless."""
    tools = run.schemas()
    limit = budget(run.model.spec)

    def project() -> tuple[str, list[Message], int]:
        system, messages = to_messages(run.log.path())
        return system, messages, request_tokens(system, messages, tools)

    system, messages, used = project()
    if not run.context_governance:
        if used > limit or overflow:
            raise ContextTooLarge(f"B0 governance disabled: request needs about {used} tokens; budget is {limit}")
        run.emit({"type": "context", "tokens": used, "budget": limit})
        return system, messages
    if used > limit or overflow:
        # Loaded skills/notices may be folded into the summary, then restored verbatim by prepare().
        # Reserve their space before choosing recent history so a seemingly fitting compacted request
        # does not overflow again as soon as these mandatory blocks are restored.
        reserve = sum(estimate_tokens(body) + 32 for body in (run.notices | pinned(run.log.path())).values())
        keep = min(KEEP_RECENT, max(1, limit // 4 - reserve))
        if prune(run, keep):
            system, messages, used = project()
        if (used > limit or overflow) and await compact(run, keep=keep):
            system, messages, used = project()
        if used > limit:
            raise ContextTooLarge(f"the request needs about {used} tokens but {run.model.spec.name} allows "
                                  f"{limit}, even after compaction; switch to a model with a larger context window")
    run.emit({"type": "context", "tokens": used, "budget": limit})  # the header's meter, live during a run
    return system, messages


def prune(run: Run, keep: int = KEEP_RECENT) -> bool:
    """Replace tool results older than the most recent `keep` tokens with pointers to saved copies."""
    shown = visible(run.log.path())
    acc, boundary = 0, None
    for e in reversed(shown):
        acc += entry_tokens(e)
        if acc > keep:
            boundary = e.seq
            break
    # Results of the latest assistant message have not been seen by the model yet: never prune those.
    latest = max((e.seq for e in shown if e.type == ASSISTANT), default=-1)
    if boundary is None or (boundary := min(boundary, latest - 1)) < 0:
        return False
    done = max((e.data["upto_seq"] for e in shown if e.type == PRUNE), default=-1)
    targets = [e for e in shown if is_prunable(e) and done < e.seq <= boundary]
    if not targets:
        return False
    spill_dir = run.ctx.spill_dir
    for e in targets:
        if not elided_file(str(spill_dir), e.id).exists():
            write_private(spill_dir, elided_file(str(spill_dir), e.id).name, result_text(e))
    run.log.append(PRUNE, {"upto_seq": boundary, "dir": str(spill_dir), "count": len(targets)}, run_id=run.run_id)
    return True


async def compact(run: Run, instructions: str | None = None, keep: int = KEEP_RECENT) -> Entry | None:
    """Summarise everything before a cut point that keeps roughly the last `keep` tokens verbatim.
    Returns None when there is nothing old enough to summarise. A failed summary appends nothing."""
    shown = visible(run.log.path())
    previous = shown[0] if shown and shown[0].type == COMPACTION else None
    body = shown[1:] if previous else shown
    cut = choose_cut(body, keep)
    if cut is None:
        return None
    old, kept = body[:cut], body[cut:]
    prompt = "The conversation below is being compacted to free context space."
    if previous:
        prompt += (" Update the previous summary with it.\n\n<previous-summary>\n"
                   f"{previous.data['summary']}\n</previous-summary>")
    prompt += f"\n\n<conversation>\n{serialize(old)}\n</conversation>\n\n"
    if instructions:
        prompt += f"Focus: {instructions}\n\n"
    resp = await complete(run.model, SUMMARIZER, prompt + FORMAT)
    summary = text_of(resp.content).strip()
    if not summary:
        raise RuntimeError("the model returned an empty summary; nothing was compacted")
    for k, v in resp.usage.items():
        run.usage[k] = run.usage.get(k, 0) + v
    files = file_ops(old, previous.data.get("files", {}) if previous else {})
    return run.log.append(COMPACTION, {"summary": summary, "first_kept_id": kept[0].id, "files": files,
                                       "tokens_before": sum(entry_tokens(e) for e in shown), "usage": resp.usage},
                          run_id=run.run_id)


def choose_cut(body: list[Entry], keep: int) -> int | None:
    """Index of the first kept entry: walk back until `keep` tokens are covered, then move forward to a
    user or assistant entry (never a tool result: it must stay next to its call). At least one entry
    must remain to summarise."""
    acc, start = 0, None
    for i in range(len(body) - 1, -1, -1):
        acc += entry_tokens(body[i])
        if acc >= keep:
            start = i
            break
    if start is None:  # everything fits in `keep`: keep only the latest user turn
        start = next((i for i in range(len(body) - 1, -1, -1) if body[i].type == USER), len(body))
    cuts = [i for i in range(max(start, 1), len(body)) if body[i].type in (USER, ASSISTANT)]
    if not cuts:
        cuts = [i for i in range(1, len(body)) if body[i].type in (USER, ASSISTANT)][-1:]
    return cuts[0] if cuts else None


BRANCH = """The user abandoned the branch of a conversation shown below and went back to an earlier point.
Summarise what happened on that branch so the conversation can continue from the earlier point with that
knowledge: what was attempted, what was changed (files, commands with side effects), what was learned or
decided, and why it seems to have been abandoned. Be brief and factual; use short bullet points."""


async def summarize_branch(run: Run, target_id: str) -> Entry | None:
    """Move to target_id; if that leaves conversation behind, attach a summary of it there first.
    The summary is a child of target_id, so the next message continues from it (D5)."""
    log = run.log
    keep = {e.id for e in log.path(target_id)}
    left = [e for e in log.path() if e.id not in keep]
    if not any(e.type in (USER, ASSISTANT) for e in left):
        log.checkout(target_id)
        return None
    resp = await complete(run.model, SUMMARIZER, f"{BRANCH}\n\n<branch>\n{serialize(left)}\n</branch>",
                          kind="branch_summary")
    if not (summary := text_of(resp.content).strip()):
        raise RuntimeError("the model returned an empty summary; the branch was not switched")
    for k, v in resp.usage.items():
        run.usage[k] = run.usage.get(k, 0) + v
    log.checkout(target_id)
    return log.append(BRANCH_SUMMARY, {"summary": summary, "files": file_ops(left, {}), "from_leaf": left[-1].id,
                                       "usage": resp.usage})


def serialize(entries: list[Entry]) -> str:
    """Plain text, so the summariser reads a transcript instead of continuing a conversation.
    Notices are left out: the harness puts back the current ones (and pinned skills) by itself."""
    lines = []
    for e in entries:
        if e.type == USER:
            lines.append(f"[User]: {text_of(e.data['content'])}")
        elif e.type == ASSISTANT:
            if t := text_of(e.data["content"]):
                lines.append(f"[Assistant]: {t}")
            if calls := e.tool_calls:
                lines.append("[Assistant tool calls]: " + "; ".join(
                    f"{c['name']}({json.dumps(c['args'], ensure_ascii=False)[:300]})" for c in calls))
        elif e.type == TOOL_RESULT:
            body = result_text(e)
            more = f" ...[{len(body) - RESULT_PREVIEW} more characters]" if len(body) > RESULT_PREVIEW else ""
            lines.append(f"[Tool result{' (error)' if e.data['is_error'] else ''}]: {body[:RESULT_PREVIEW]}{more}")
        elif e.type in (COMPACTION, BRANCH_SUMMARY):
            lines.append(f"[Earlier summary]: {e.data['summary']}")
    return "\n".join(lines)


def file_ops(entries: list[Entry], previous: dict[str, list[str]]) -> dict[str, list[str]]:
    """Paths read and modified, accumulated across compactions so they survive repeated summaries."""
    read, modified = set(previous.get("read", [])), set(previous.get("modified", []))
    for e in entries:
        for c in e.tool_calls:
            path = c["args"].get("path")
            if path and c["name"] == "read":
                read.add(path)
            elif path and c["name"] in ("write", "edit"):
                modified.add(path)
    return {"read": sorted(read - modified), "modified": sorted(modified)}


async def complete(model: Model, system: str, prompt: str, kind: str = "compaction") -> Response:
    resp = None
    with purpose(kind):
        async for item in model.stream(system, [Message(role="user", content=[text(prompt)])], []):
            if isinstance(item, Response):
                resp = item
    assert resp is not None
    if resp.stop != "end":
        raise RuntimeError(f"summary did not finish normally: {resp.stop}")
    return resp

