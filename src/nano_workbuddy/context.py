"""Project the current log path into a model request (D10).

Context is kept inside the window by three tiers, cheapest first:
  1. spill    - a large tool result is saved to a file when it is produced (here, no model call)
  2. prune    - old tool results are replaced by pointers to saved copies (compaction.py, no model call)
  3. compact  - older turns are summarised by the model, recent turns stay verbatim (compaction.py)
Tiers 2 and 3 are log entries, so the projection changes only at those moments and the provider's
prompt cache keeps hitting in between.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from .config import ModelSpec
from .tools.registry import Result
from .types import (
    ASSISTANT,
    BRANCH_SUMMARY,
    COMPACTION,
    CONTEXT_STATE,
    NOTICE,
    PRUNE,
    SYSTEM_PROMPT,
    TOOL_RESULT,
    USER,
    Block,
    Entry,
    Message,
    text,
)

SPILL_CHARS = 30_000  # roughly 8k tokens
KEEP_CHARS = 4_000  # head and tail kept inline
ELIDE_MIN_CHARS = 1_000  # pruning leaves small results alone
IMAGE_TOKENS = 1_500
MIN_RESERVE = 16_384
CJK = re.compile(r"[　-鿿가-힯＀-￯]")


# --- estimation ---


def estimate_tokens(s: str) -> int:
    """CJK characters are about one token each, other text about 3.5 characters per token. Deliberately
    simple: real usage from the provider is what users see; this only decides when to shrink context."""
    cjk = len(CJK.findall(s))
    return cjk + int((len(s) - cjk) / 3.5) + 1


def blocks_tokens(blocks: list[Block]) -> int:
    n = 0
    for b in blocks:
        if b["type"] == "image":
            n += IMAGE_TOKENS
        elif b["type"] == "tool_call":
            n += estimate_tokens(b["name"] + json.dumps(b["args"], ensure_ascii=False))
        else:
            n += estimate_tokens(b["text"])
    return n


def entry_tokens(e: Entry) -> int:
    content = e.data.get("content")
    if isinstance(content, list):
        return blocks_tokens(content)
    return estimate_tokens(json.dumps(e.data, ensure_ascii=False))


def request_tokens(system: str, messages: list[Message], tools: list[dict[str, Any]]) -> int:
    return (estimate_tokens(system) + estimate_tokens(json.dumps(tools, ensure_ascii=False))
            + sum(blocks_tokens(m["content"]) + 4 for m in messages))


def budget(spec: ModelSpec) -> int:
    """Input tokens a request may use: the window minus room for the answer."""
    return spec.context_window - max(MIN_RESERVE, spec.max_output)


# --- projection ---


def visible(path: list[Entry]) -> list[Entry]:
    """Entries the model sees: after the latest compaction, its summary stands in for everything
    before first_kept_id. Everything from first_kept_id on (including later entries) stays."""
    body = [e for e in path if e.type != SYSTEM_PROMPT]
    at = next((i for i in range(len(body) - 1, -1, -1) if body[i].type == COMPACTION), None)
    if at is None:
        return body
    comp = body[at]
    start = next((i for i, e in enumerate(body) if e.id == comp.data["first_kept_id"]), at + 1)
    return [comp, *(e for e in body[start:] if e.type != COMPACTION)]


def loaded_tools(path: list[Entry]) -> list[str]:
    """Deferred tools that tool_search loaded on this path, in load order. The whole path counts, not just
    what is visible: a compaction must not silently take tools away from the model."""
    names = (n for e in path if e.type == TOOL_RESULT for n in e.data.get("meta", {}).get("load", ()))
    return list(dict.fromkeys(names))


def elided_file(spill_dir: str, entry_id: str) -> Path:
    return Path(spill_dir) / f"elided-{entry_id}.txt"


def result_text(e: Entry) -> str:
    return "".join(b["text"] for b in e.data["content"] if b["type"] == "text")


def is_prunable(e: Entry) -> bool:
    return e.type == TOOL_RESULT and len(result_text(e)) > ELIDE_MIN_CHARS and "pin" not in e.data.get("meta", {})


def in_view(path: list[Entry]) -> dict[str, str]:
    """Notices and pinned results the model can currently see: kind or pin key -> text."""
    seen: dict[str, str] = {}
    versions = next((e.data.get("skills", {}) for e in reversed(path) if e.type == CONTEXT_STATE), {})
    for e in visible(path):
        if e.type == NOTICE:
            seen[e.data["kind"]] = versions.get(e.data["kind"], e.data["text"])
        elif e.type == TOOL_RESULT and (key := e.data.get("meta", {}).get("pin")):
            seen[key] = versions.get(key, result_text(e))
    return seen


def pinned(path: list[Entry]) -> dict[str, str]:
    """Content that must stay in view once loaded (skills): pin key -> latest text. A tool result pins
    with meta {"pin": key}; a notice pins with {"pin": true} and its kind as the key."""
    out: dict[str, str] = {}
    for e in path:
        if e.type == TOOL_RESULT and (key := e.data.get("meta", {}).get("pin")):
            out[key] = result_text(e)
        elif e.type == NOTICE and e.data.get("pin"):
            out[e.data["kind"]] = e.data["text"]
        elif e.type == CONTEXT_STATE:
            out.update(e.data.get("skills", {}))
    return out


def to_messages(path: list[Entry], current_context: str | None = None) -> tuple[str, list[Message]]:
    head = path[0].data if path and path[0].type == SYSTEM_PROMPT else {}
    snapshot = next((e.data for e in reversed(path) if e.type == CONTEXT_STATE), None)
    snapshot_id = next((e.id for e in reversed(path) if e.type == CONTEXT_STATE), None)
    system = head.get("base_text", head.get("text", ""))
    if snapshot is not None or current_context is not None:
        # Older sessions predate base_text; remove only host-owned mutable sections from their snapshot.
        if "base_text" not in head:
            system = re.sub(r"<(project_instructions|user_memory)\b[^>]*>.*?</\1>", "", system, flags=re.S).strip()
        current = current_context if current_context is not None else snapshot["text"]
    else:
        current = head.get("context", "")
    if current:
        system += "\n\n" + current
    skill_versions = snapshot.get("skills", {}) if snapshot else {}
    shown = visible(path)
    snapshot_visible = any(e.id == snapshot_id for e in shown)

    def context_notice() -> Message:
        current = current_context if current_context is not None else snapshot["text"]
        notice = ("<system-notice>\nSaved context was refreshed at this point. Earlier preference-setting "
                  "requests and memory-tool receipts describe historical values. For the current task "
                  "use the current saved values below, unless the current user request overrides them:\n\n"
                  + (current or "No saved user memory or project rules are currently present.") +
                  "\n</system-notice>")
        return Message(role="user", content=[text(notice)])
    prune = next((e.data for e in reversed(shown) if e.type == PRUNE), None)
    # Thinking produced before the latest history edit was made against a history that no longer exists.
    # Providers that verify thinking against its context (Anthropic) must not receive it.
    edited = max((e.seq for e in path if e.type in (PRUNE, COMPACTION, CONTEXT_STATE)), default=-1)
    replacements = {e.data["kind"]: e.id for e in shown if e.type == NOTICE and e.data.get("replace")}
    messages: list[Message] = []
    for e in shown:
        if e.type == USER:
            messages.append(Message(role="user", content=e.data["content"]))
        elif e.type == ASSISTANT and e.data["content"]:
            content = e.data["content"]
            if e.seq < edited:
                content = [{**b, "stale": True} if b["type"] == "thinking" else b for b in content]
            messages.append(Message(role="assistant", content=content))
        elif e.type == TOOL_RESULT:
            content = e.data["content"]
            pin = e.data.get("meta", {}).get("pin")
            if pin in skill_versions:
                content = [text(skill_versions[pin])]
            elif prune and e.seq <= prune["upto_seq"] and is_prunable(e):
                content = [text(f"[Old tool output removed to save context ({len(result_text(e))} characters). "
                                f"The full text is saved at {elided_file(prune['dir'], e.id)}.]")]
            messages.append(Message(role="tool", content=content, call_id=e.data["call_id"],
                                    is_error=e.data["is_error"]))
        elif e.type == NOTICE:
            kind = e.data["kind"]
            if kind in replacements and replacements[kind] != e.id:
                continue
            notice = f"<system-notice>\n{skill_versions.get(kind, e.data['text'])}\n</system-notice>"
            messages.append(Message(role="user", content=[text(notice)]))
        elif e.type == CONTEXT_STATE and e.id == snapshot_id:
            # A refreshed system prefix alone can be overshadowed by recent memory-tool
            # receipts. Mark the change at its actual position without editing old messages.
            messages.append(context_notice())
        elif e.type == COMPACTION:
            messages.append(Message(role="user", content=[text(summary_message(e.data))]))
            if snapshot is not None and not snapshot_visible:
                messages.append(context_notice())
        elif e.type == BRANCH_SUMMARY:
            messages.append(Message(role="user", content=[text(branch_message(e.data))]))
    return system, messages


def summary_message(d: dict[str, Any]) -> str:
    files = d.get("files", {})
    lists = "".join(f"\n{label}: {', '.join(files[k])}" for k, label in (("read", "Files read"),
                    ("modified", "Files modified")) if files.get(k))
    return ("<conversation-summary>\nThe earlier part of this conversation was compacted into this summary.\n\n"
            f"{d['summary']}{lists}\n</conversation-summary>")


def branch_message(d: dict[str, Any]) -> str:
    changed = d.get("files", {}).get("modified")
    files = (f"\nFiles modified on that branch (still changed on disk unless reverted): {', '.join(changed)}"
             if changed else "")
    return ("<branch-summary>\nThe user went back to this point from another branch of the conversation. "
            f"What happened on that branch:\n\n{d['summary']}{files}\n</branch-summary>")


# --- tier 1 ---


def spill(r: Result, spill_dir: Path, call_id: str) -> Result:
    """Keep the head and tail inline and save the full text privately (0700 dir, 0600 file, no symlink follow).
    The file is named by the call id plus a hash of the text: providers reuse ids such as "call_0" across turns,
    and a result recorded again after a crash finds its own file instead of colliding with it."""
    full = "".join(b["text"] for b in r.content if b["type"] == "text")
    if len(full) <= SPILL_CHARS:
        return r
    digest = hashlib.sha256(full.encode()).hexdigest()[:12]
    f = write_private(spill_dir, f"{re.sub(r'[^A-Za-z0-9_-]', '_', call_id)[:64]}-{digest}.txt", full)
    omitted = len(full) - 2 * KEEP_CHARS
    note = (f"\n\n[... {omitted} characters omitted. Full output saved to {f} — "
            f"inspect it with read (offset/limit) or bash grep ...]\n\n")
    images = [b for b in r.content if b["type"] == "image"]
    meta = {**r.meta, "exposure": {"output_chars": len(full),
                                  "ranges": [[0, KEEP_CHARS], [len(full) - KEEP_CHARS, len(full)]],
                                  "full_sha256": hashlib.sha256(full.encode()).hexdigest(),
                                  "full_output": str(f)}}
    return Result([text(full[:KEEP_CHARS] + note + full[-KEEP_CHARS:]), *images], r.is_error, meta)


def write_private(directory: Path, name: str, content: str) -> Path:
    """Create a private file. If it already exists with exactly this content (the same write repeated after
    a crash) it is reused; anything else there, including a symlink, is an error."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    f = directory / name
    try:
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if not f.is_symlink() and f.is_file() and f.read_bytes() == content.encode("utf-8"):
            return f
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        out.write(content)
    return f
