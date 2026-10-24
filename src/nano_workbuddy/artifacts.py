"""Deliverables, sources and attachments: files and evidence as first-class records (M1).

Three kinds of record, all carried in tool results' meta (so the session log stays the single authority
and the UI can rebuild everything from entries):

  artifact  a file handed to the user (create_document, present): path, version, hash, and the checks run on it
            after it was written. Every version's bytes are kept in a content-addressed store, so a
            delivered version can still be opened after the file was changed or deleted, or the server restarted.
  source    a web page or search hit the answer may rest on, numbered [S1], [S2], ... in order of first
            appearance in the session. A search hit is only a snippet; a page read in full upgrades it.
  upload    a file the user attached to a message, kept under the session's private folder.

Store layout under ~/.nwb/files: blobs/<sha256> (artifact versions and uploads), cache/docs/ (parsed documents).
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from . import documents
from .log import SessionLog
from .types import TOOL_RESULT

MAX_SNAPSHOT_BYTES = 200 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
TEXT_SUFFIXES = {".md", ".markdown", ".txt", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".xml", ".html",
                 ".htm", ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".c", ".h", ".cpp",
                 ".sh", ".sql", ".tex", ".bib", ".ini", ".cfg"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp"}
INLINE_CHARS = 20_000  # an attachment whose text is this short goes into the message itself


def mime_of(p: Path) -> str:
    suffix = p.suffix.lower()
    return IMAGE_TYPES.get(suffix) or ("text/markdown" if suffix in (".md", ".markdown") else None) \
        or mimetypes.guess_type(p.name)[0] or "application/octet-stream"


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# --- the content-addressed store ---


def blob(store: Path, digest: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("not a sha256 digest")
    return store / "blobs" / digest


def keep(store: Path, p: Path, digest: str) -> Path:
    """Copy a file into the store under its hash (once: the same bytes are stored once)."""
    target = blob(store, digest)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        os.close(fd)
        shutil.copyfile(p, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    return target


# --- checks after a file is written ---


def check(p: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Open the file again the way its reader would, and report what was found: (checks, info).
    Saving a file and the file being usable are different claims; only the second is worth delivering."""
    checks: list[dict[str, Any]] = []
    info: dict[str, Any] = {}

    def ok(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"name": name, "ok": passed, "detail": detail})

    size = p.stat().st_size
    ok("not empty", size > 0, f"{size} bytes")
    suffix = p.suffix.lower()
    try:
        if suffix in (".md", ".markdown", ".txt"):
            body = p.read_text(encoding="utf-8")
            info["headings"] = len(re.findall(r"^#{1,6} ", body, re.M))
            ok("valid UTF-8 text", True, f"{len(body)} characters")
        elif suffix in documents.KINDS:
            doc = documents.parse(p, documents.KINDS[suffix])
            info["description"] = doc.describe()
            if doc.unit:
                info[f"{doc.unit}s"] = len(doc.units)
            chars = sum(len(u.strip()) for u in doc.units)
            ok(f"opens as {doc.kind}", True, doc.describe())
            ok("has readable text", chars > 0, f"{chars} characters")
            for note in doc.notes:
                ok("note", False, note)
        elif suffix in IMAGE_TYPES:
            from PIL import Image

            with Image.open(p) as im:
                im.verify()
                info["size"] = f"{im.width}×{im.height}"
            ok("opens as an image", True, info["size"])
    except (documents.DocumentError, UnicodeDecodeError, OSError) as e:
        ok("opens", False, str(e)[:300])
    except Exception as e:  # e.g. PIL refusing a corrupt image
        ok("opens", False, f"{type(e).__name__}: {e}"[:300])
    return checks, info


def record(store: Path | None, p: Path, versions: dict[str, list[str]], title: str = "",
           checks: list[dict[str, Any]] | None = None, info: dict[str, Any] | None = None) -> dict[str, Any]:
    """The artifact record for a file just written or presented, with its bytes kept in the store."""
    digest = sha256_of(p)
    size = p.stat().st_size
    if checks is None:
        checks, info = check(p)
    kept = store is not None and size <= MAX_SNAPSHOT_BYTES
    if kept:
        keep(store, p, digest)
    else:
        checks = [*checks, {"name": "copy kept", "ok": False, "detail": "too large to keep a copy of this version"}]
    history = versions.setdefault(str(p), [])
    if not history or history[-1] != digest:
        history.append(digest)
    return {"path": str(p), "name": p.name, "mime": mime_of(p), "size": size, "sha256": digest,
            "version": len(history), "title": title, "checks": checks, "info": info or {}, "kept": kept,
            "created_at": time.time()}


def summary(a: dict[str, Any]) -> str:
    """What the model is told about a delivered file."""
    failed = [c for c in a["checks"] if not c["ok"]]
    lines = [f"Delivered {a['path']} (version {a['version']}, {a['size']} bytes, {a['mime']})."]
    for c in a["checks"]:
        detail = f" ({c['detail']})" if c["detail"] else ""
        lines.append(f"- check passed: {c['name']}{detail}" if c["ok"] else f"- PROBLEM: {c['name']}: {c['detail']}")
    if failed:
        lines.append("Tell the user about the problems above, or fix them and deliver again.")
    return "\n".join(lines)


# --- session records, rebuilt from the log ---


def session_records(log: SessionLog) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """(sources by url, artifact version hashes by path) from every tool result in the session, all branches:
    numbering stays stable when the user switches branches."""
    sources: dict[str, dict[str, Any]] = {}
    versions: dict[str, list[str]] = {}
    for e in sorted(log.entries.values(), key=lambda e: e.seq):
        if e.type != TOOL_RESULT or not (meta := e.data.get("meta")):
            continue
        for s in meta.get("sources", ()):
            sources[s["url"]] = s
        if a := meta.get("artifact"):
            history = versions.setdefault(a["path"], [])
            if not history or history[-1] != a["sha256"]:
                history.append(a["sha256"])
    return sources, versions


def files_of(log: SessionLog) -> set[str]:
    """Hashes of every artifact version and upload in the session: what its file links may serve."""
    out: set[str] = set()
    for e in log.entries.values():
        if e.type == TOOL_RESULT and (a := e.data.get("meta", {}).get("artifact")):
            out.add(a["sha256"])
        elif e.type == "user":
            out.update(u["sha256"] for u in e.data.get("attachments", ()))
    return out


def cite(sources: dict[str, dict[str, Any]], url: str, kind: str, title: str = "", **extra: Any) -> dict[str, Any]:
    """The numbered source record for url. kind: "search" (a snippet from a search engine), "page" (read in
    full) or "file" (downloaded). A page read in full upgrades an earlier search hit and keeps its number."""
    url = url.split("#", 1)[0]
    known = sources.get(url)
    if known and known["kind"] == "page" and kind == "search":
        kind = "page"  # already read in full: a later search hit does not downgrade it
    rec = {**(known or {}), **{k: v for k, v in extra.items() if v not in (None, "")},
           "n": known["n"] if known else max((s["n"] for s in sources.values()), default=0) + 1,
           "url": url, "kind": kind, "title": title or (known or {}).get("title", "")}
    sources[url] = rec
    return rec


# --- uploads ---


def safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name.strip().lstrip(".") or "file"
    return re.sub(r"[\x00-\x1f/]", "_", name)[:120]


def save_upload(store: Path, uploads: Path, name: str, data: bytes) -> dict[str, Any]:
    """Keep an attached file: in the store by hash, and readable by the session's tools (and sandbox) at
    <session private folder>/uploads/<id>/<name>."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
    digest = hashlib.sha256(data).hexdigest()
    name = safe_name(name)
    folder = uploads / digest[:16]
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = folder / name
    if not p.exists():
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        os.replace(tmp, p)
    keep(store, p, digest)
    return describe_upload(p, digest, store)


def find_upload(uploads: Path, upload_id: str, store: Path) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{16}", upload_id):
        raise KeyError(f"no attachment {upload_id}")
    folder = uploads / upload_id
    files = [f for f in folder.iterdir() if f.is_file() and not f.name.endswith(".tmp")] if folder.is_dir() else []
    if not files:
        raise KeyError(f"no attachment {upload_id}")
    return describe_upload(files[0], sha256_of(files[0]), store)


def describe_upload(p: Path, digest: str, store: Path) -> dict[str, Any]:
    suffix = p.suffix.lower()
    kind, desc = "file", ""
    if suffix in IMAGE_TYPES:
        kind = "image"
    elif documents.is_document(p):
        kind = "document"
        try:
            desc = documents.load(p, store / "cache" / "docs").describe()
        except documents.DocumentError as e:
            desc = f"unreadable: {e}"
    elif suffix in TEXT_SUFFIXES:
        kind = "text"
    return {"id": digest[:16], "name": p.name, "mime": mime_of(p), "size": p.stat().st_size, "sha256": digest,
            "path": str(p), "kind": kind, "desc": desc}


def upload_blocks(a: dict[str, Any], store: Path, vision: bool) -> list[dict[str, Any]]:
    """What the model sees of one attachment. Short text and short documents go inline; longer documents
    are announced with how to read them; images go in as images when the model can see them. Each text
    block is marked with the attachment id, so the UI shows a chip instead of the text."""
    p = Path(a["path"])
    head = f'<attachment name="{a["name"]}" path="{p}" type="{a["mime"]}" size="{a["size"]}"'
    mark = {"attachment": a["id"]}
    if a["kind"] == "image":
        if not vision:
            return [{"type": "text", "text": f"{head}>\nAn image. The current model cannot view images; "
                                             "say so if its content matters.\n</attachment>", **mark}]
        import base64

        data = base64.b64encode(p.read_bytes()).decode()
        return [{"type": "text", "text": f"{head} />", **mark}, {"type": "image", "mime": a["mime"], "data": data}]
    if a["kind"] == "text":
        body = p.read_bytes().decode("utf-8", "replace")
        if len(body) <= INLINE_CHARS:
            return [{"type": "text", "text": f"{head}>\n{body}\n</attachment>", **mark}]
        return [{"type": "text", "text": f"{head}>\n{len(body)} characters: read it with the read tool "
                                         "(offset/limit).\n</attachment>", **mark}]
    if a["kind"] == "document":
        try:
            doc = documents.load(p, store / "cache" / "docs")
        except documents.DocumentError as e:
            return [{"type": "text", "text": f"{head}>\n{e}\n</attachment>", **mark}]
        notes = "".join(f"\nNote: {n}" for n in doc.notes)
        body = doc.text()
        if len(body) <= INLINE_CHARS:
            return [{"type": "text", "text": f"{head} document=\"{doc.describe()}\">{notes}\n{body}\n</attachment>",
                     **mark}]
        how = "read it with the read tool, e.g. pages=\"1-3\"" if doc.unit in ("page", "slide") else \
            "read it with the read tool (offset/limit)"
        return [{"type": "text", "text": f"{head} document=\"{doc.describe()}\">{notes}\n{len(body)} characters "
                                         f"of text: {how}.\n</attachment>", **mark}]
    return [{"type": "text", "text": f"{head}>\nA binary file; inspect it with bash if needed.\n</attachment>",
             **mark}]
