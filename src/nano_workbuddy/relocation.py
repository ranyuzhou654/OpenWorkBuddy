"""Rebase schema-known internal paths after restoring a home; preserve user prose and external projects."""

import contextlib
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import unquote

import tomlkit

PATHS = {"path", "file_path", "root_path", "content_file", "file", "snapshot", "scratch_dir", "cwd", "cli",
         "resource", "output", "output_dir", "command"}
PATH_LISTS = {"argv", "command", "args", "skill_dirs", "writable"}


def relocate(staged: Path, original: Path, target: Path) -> dict:
    original = original.expanduser()

    def path(value: str) -> str:
        prefix = str(original).rstrip("/")
        return str(target) + value[len(prefix):] if value == prefix or value.startswith(prefix + "/") else value

    def argument(value: str) -> str:
        if value.startswith("@"):
            return "@" + path(value[1:])
        flag, sep, body = value.partition("=")
        if sep and flag in ("--file", "--output", "--output-dir", "--user-data-dir"):
            return flag + "=" + path(body)
        return path(value)

    def rebase(value, key=""):
        if isinstance(value, dict):
            return {k: v if k == "structured" else rebase(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [argument(v) if key in PATH_LISTS and isinstance(v, str) else rebase(v) for v in value]
        if isinstance(value, str):
            if key in PATHS:
                return path(value)
            if key == "url" and value.startswith("file://"):
                return Path(path(unquote(value[7:]))).as_uri()
            if key == "text" and value.startswith("<attachment "):
                return re.sub(r' path="([^"]+)"', lambda m: f' path="{path(m[1])}"', value, count=1)
        return value

    # Rebind only normalized Feishu input identities whose local file locations changed. The business
    # payload hashes and frozen bytes remain untouched; pre-execution approvals expire below.
    input_hashes = {}
    for file in (staged / "sessions").glob("*/*.jsonl"):
        raw = file.read_bytes()
        entries = [json.loads(line) for line in raw[:raw.rfind(b"\n") + 1].splitlines()]
        new = rebase(entries)
        for before, after in zip(entries, new, strict=True):
            if before.get("type") == "assistant":
                from .connectors.feishu import FeishuArgs
                from .connectors.feishu_ops import digest

                for old, moved in zip(before["data"]["content"], after["data"]["content"], strict=True):
                    if old.get("name") == "feishu":
                        with contextlib.suppress(ValueError):  # retain historical invalid calls as evidence
                            input_hashes[digest(FeishuArgs(**old["args"]).model_dump())] = digest(
                                FeishuArgs(**moved["args"]).model_dump())
            if before.get("type") == "system_prompt":
                text = after["data"]["text"]
                text = re.sub(r"(?m)^(Project root \(working directory\): )(.+)$",
                              lambda m: m[1] + path(m[2]), text)
                after["data"].update(text=text, hash=hashlib.sha256(text.encode()).hexdigest()[:16])
        file.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in new))

    for file in (staged / "spill").glob("*/feishu-operations/op_*.json"):
        record = rebase(json.loads(file.read_text()))
        record["input_hash"] = input_hashes.get(record["input_hash"], record["input_hash"])
        if record["status"] in ("prepared", "approved"):
            record["status"] = "expired"
        file.write_text(json.dumps(record, ensure_ascii=False, indent=2))

    config = staged / "config.toml"
    if config.is_file():
        config.write_text(tomlkit.dumps(rebase(tomlkit.parse(config.read_text()).unwrap())))
    db = sqlite3.connect(staged / "index.db")
    db.row_factory = sqlite3.Row
    try:
        with db:
            for table, cols in (("sessions", ("file_path",)), ("projects", ("root_path",)),
                                ("rules", ("resource",)), ("inputs", ("extra_json",)),
                                ("approvals", ("args_json", "preview_json"))):
                present = {row[1] for row in db.execute(f"pragma table_info({table})")}
                for col in cols:
                    if col not in present:
                        continue
                    for row in db.execute(f"select rowid,{col} from {table}").fetchall():
                        if row[col] is not None:
                            value = json.dumps(rebase(json.loads(row[col])), ensure_ascii=False) if col.endswith(
                                "_json") else path(row[col])
                            db.execute(f"update {table} set {col}=? where rowid=?", (value, row["rowid"]))
            db.execute("update approvals set status='expired' where status='pending'")
        projects = [dict(row) for row in db.execute("select id,root_path from projects")]
    finally:
        db.close()
    def exists(project):
        p = Path(project["root_path"])
        return (staged / p.relative_to(target)).is_dir() if p.is_relative_to(target) else p.is_dir()

    return {"original_home": str(original), "home": str(target),
            "missing_projects": [p for p in projects if not exists(p)]}
