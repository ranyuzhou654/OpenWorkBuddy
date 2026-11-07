"""Read frozen trial logs with old/new functions; no model request or original-file mutation."""
import ast
import json
import sqlite3
import zipfile
from pathlib import Path

from nano_workbuddy import scheduler
from nano_workbuddy.eval.runner import code_version
from nano_workbuddy.log import SessionLog
from nano_workbuddy.tools import docs
from nano_workbuddy.tools.registry import ToolCtx

root = Path("eval-runs/rd-project-2026-10-26-r2").resolve()
destination = Path("docs/research/2026-10-26-rd-project-trials-r2")

def frozen_function(module, name, assignments=()):
    with zipfile.ZipFile(root / "source.zip") as archive:
        tree = ast.parse(archive.read("src/" + module.__name__.replace(".", "/") + ".py"))
    nodes = [n for n in tree.body if getattr(n, "name", None) == name or (
        isinstance(n, ast.Assign) and any(getattr(t, "id", None) in assignments for t in n.targets))]
    namespace = vars(module).copy()
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "frozen-source", "exec"), namespace)
    return namespace[name]

card = json.loads((root / "U5-2/review-card.json").read_text())
log = SessionLog.open(next((Path(card["final_home"]) / "sessions").glob("*/*.jsonl")))
run_id = list(json.loads((destination / "U5-2/annotated-card.json").read_text())["calls_per_run"])[-1]
with sqlite3.connect(f"file:{card['final_home']}/index.db?mode=ro", uri=True) as connection:
    connection.row_factory = sqlite3.Row
    run = dict(connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
old = frozen_function(scheduler, "outcome_of")
result = {"scope": "offline diagnostic only; original grades and logs unchanged; zero model calls",
          "U5-2": {"run_id": run_id, "before": old(log, run), "after": scheduler.outcome_of(log, run)}}

trial = root / "U1-1"
log = SessionLog.open(next((trial / "home/sessions").glob("*/*.jsonl")))
ctx = ToolCtx(root=trial / "workspace", session_id=log.id, spill_dir=trial / "scratch")
for entry in log.entries.values():
    for source in entry.data.get("meta", {}).get("sources", []):
        ctx.sources[source["url"]] = source
content = next(c["args"]["content"] for entry in log.entries.values()
               if entry.type == "assistant" for c in entry.data.get("content", [])
               if c.get("name") == "create_document" and c["args"]["path"].endswith(".docx"))
old = frozen_function(docs, "bibliography", ("CITATION",))
result["U1-1"] = {"before_source_ids": [s["n"] for s in old(content, ctx)[1]],
                  "after_source_ids": [s["n"] for s in docs.bibliography(content, ctx)[1]]}
result["post_trial_code_sha256"] = code_version()["code_sha256"]
(destination / "offline-probes.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(result, ensure_ascii=False, indent=2))
