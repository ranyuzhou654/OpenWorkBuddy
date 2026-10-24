"""A stand-in for lark-cli: risk labels in --help, auth status, dry runs, writes with receipts, an expired token.
Every call is appended to .lark-calls.jsonl in the working directory (the project root), so tests can see
exactly which argv reached the CLI."""

import json
import sys
from pathlib import Path

RISK = {("docs", "+fetch"): "read", ("docs", "+search"): "read", ("calendar", "+agenda"): "read",
        ("im", "+messages-send"): "write", ("im", "+messages-reply"): "write", ("docs", "+create"): "write",
        ("docs", "+update"): "write", ("drive", "+import"): "write", ("drive", "+delete"): "high-risk-write",
        ("calendar", "+create"): "write"}

argv = sys.argv[1:]
with open(Path.cwd() / ".lark-calls.jsonl", "a") as log:
    log.write(json.dumps(argv) + "\n")
if argv == ["--version"]:
    print("lark-cli version 9.9.9")
elif argv[:2] == ["auth", "status"]:
    print(json.dumps({"appId": "cli_test", "brand": "feishu", "identities": {
        "user": {"available": True, "status": "ready", "userName": "Tester", "openId": "ou_tester"}}}))
elif "--help" in argv:
    risk = RISK.get(tuple(argv[:2]))
    print(f"Usage: lark-cli {' '.join(argv[:2])}\n\nFlags:\n  --as string\n" + (f"\nRisk: {risk}\n" if risk else ""))
elif "--dry-run" in argv:
    print(json.dumps({"ok": True, "dry_run": True, "data": {"api": [{"method": "POST", "url": "/open-apis/x",
                                                                    "body": {"argv": argv}}]}}))
elif "expired" in argv:
    print("Error: user access token expired; run lark-cli auth login", file=sys.stderr)
    sys.exit(1)
elif tuple(argv[:2]) == ("docs", "+fetch"):
    print(json.dumps({"ok": True, "data": {"title": "季度计划", "content": "<p>目标：上线调研工作流</p>"}}))
elif RISK.get(tuple(argv[:2])) == "write":
    print(json.dumps({"ok": True, "data": {"message_id": "om_123", "url": "https://x.feishu.cn/docx/abc"}}))
else:
    print(json.dumps({"ok": True, "data": {"items": []}}))
