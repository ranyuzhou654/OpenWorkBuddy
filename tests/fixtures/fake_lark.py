"""A stand-in for lark-cli: risk labels in --help, auth status, dry runs, writes with receipts, an expired token.
Every call is appended to .lark-calls.jsonl in the working directory (the project root), so tests can see
exactly which argv reached the CLI."""

import hashlib
import json
import sys
from pathlib import Path

RISK = {("docs", "+fetch"): "read", ("docs", "+search"): "read", ("calendar", "+agenda"): "read",
        ("im", "+messages-send"): "write", ("im", "+messages-reply"): "write", ("docs", "+create"): "write",
        ("docs", "+update"): "write", ("drive", "+import"): "write", ("drive", "+delete"): "high-risk-write",
        ("calendar", "+create"): "write"}

argv = sys.argv[1:]
options = {}
for i, arg in enumerate(argv):
    if arg.startswith("--"):
        key, sep, val = arg.partition("=")
        options[key] = val if sep else ("true" if key == "--dry-run" else
                                        argv[i + 1] if i + 1 < len(argv) else "")
if argv[:2] not in (["auth", "status"],) and "--help" not in argv and argv != ["--version"]:
    with open(Path.cwd() / ".lark-calls.jsonl", "a") as log:
        log.write(json.dumps(argv) + "\n")
body = next((options[k] for k in ("--content", "--text", "--markdown") if k in options), "")
if body == "-":
    body = sys.stdin.read()
elif body.startswith("@"):
    body = Path(body[1:]).read_text()
payload = {"content": body, "argv": argv}
if "--file" in options:
    data = Path(options["--file"]).read_bytes()
    payload["file_sha256"] = hashlib.sha256(data).hexdigest()
mode_file = Path.cwd() / ".fake-lark-mode.json"
mode = json.loads(mode_file.read_text()) if mode_file.exists() else {}
if argv == ["--version"]:
    print("lark-cli version 9.9.9")
elif argv[:2] == ["auth", "status"]:
    print(json.dumps({"appId": "cli_test", "brand": "feishu", "identities": {
        "user": {"available": True, "status": "ready", "userName": "Tester", "openId": "ou_tester"}}}))
elif "--help" in argv:
    risk = RISK.get(tuple(argv[:2]))
    print(f"Usage: lark-cli {' '.join(argv[:2])}\n\nFlags:\n  --as string\n" + (f"\nRisk: {risk}\n" if risk else ""))
elif options.get("--dry-run") == "true":
    print(json.dumps({"ok": not mode.get("preview_fail"), "dry_run": True,
                      "data": {"api": [{"method": "POST", "url": "/open-apis/x", "body": payload}]}}))
elif any("expired" in a for a in argv):
    print("Error: user access token expired; run lark-cli auth login", file=sys.stderr)
    sys.exit(1)
elif tuple(argv[:2]) == ("docs", "+fetch"):
    print(json.dumps({"ok": True, "data": {"title": "季度计划", "content": "<p>目标：上线调研工作流</p>"}}))
elif RISK.get(tuple(argv[:2])) == "write":
    with open(Path.cwd() / ".lark-effects.jsonl", "a") as log:
        log.write(json.dumps(payload) + "\n")
    key = "message_id" if argv[0] == "im" else "document_id" if argv[0] == "docs" else "token"
    receipt = {} if mode.get("missing_receipt") else {key: "om_123", "url": "https://x.feishu.cn/docx/abc"}
    print(json.dumps({"ok": not mode.get("fail_after_effect"), "data": receipt}))
else:
    print(json.dumps({"ok": True, "data": {"items": []}}))
