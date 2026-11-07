"""Source inspection follows the actual request projection, after spill/prune/compaction.

The log records exact excerpt hashes and output character ranges. Inspected means made accessible to
the model, not understood, verified, or sufficient to support a claim.
"""

from __future__ import annotations

import copy
import hashlib
from typing import TYPE_CHECKING

from . import artifacts
from .context import visible
from .types import EVIDENCE, TOOL_RESULT, USER, Message, text_of

if TYPE_CHECKING:
    from .loop import Run


def inspect_sources(run: Run, messages: list[Message]) -> None:
    changed = []
    for entry in visible(run.log.path()):
        reads = entry.data.get("meta", {}).get("source_reads", [])
        if entry.type == USER:
            reads = [b["source_read"] for b in entry.data.get("content", []) if b.get("source_read")]
        if entry.type not in (USER, TOOL_RESULT) or not reads:
            continue
        # Pruned output pointers and summaries never qualify as seeing the underlying source text.
        if not any(m["content"] == entry.data["content"] for m in messages):
            continue
        excerpt = text_of(entry.data["content"])
        for read in reads:
            rec = next((s for s in run.ctx.sources.values() if s["n"] == read["n"]), None)
            if rec is None or any(x["entry_id"] == entry.id for x in rec.get("inspections", [])):
                continue
            rec = copy.deepcopy(rec)
            exposure = entry.data.get("meta", {}).get("exposure", {})
            rec["state"] = "inspected"
            ranges = exposure.get("ranges", [[0, len(excerpt)]])
            offset, length = read.get("output_start", 0), read.get("source_length", 0)
            source_ranges = [[max(0, a - offset), min(length, b - offset)] for a, b in ranges
                             if b > offset and a < offset + length] if length else []
            rec["inspections"] = [*rec.get("inspections", []), {
                "entry_id": entry.id, "locator": read["locator"],
                "excerpt_ref": artifacts.keep_bytes(run.ctx.store, excerpt.encode()),
                "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
                "output_ranges": ranges, "source_ranges": source_ranges,
                "partial_output": bool(exposure),
            }]
            artifacts.merge_source(run.ctx.sources, rec)
            changed.append(rec)
    if changed:
        run.log.append(EVIDENCE, {"meta": {"sources": changed}}, run_id=run.run_id)
