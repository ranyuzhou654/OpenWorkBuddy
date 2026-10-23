import { api, type Entry } from "./api";
import { childrenOf, pathTo, textOf, type State } from "./session";

// The whole conversation tree. Indentation grows only where the tree branches,
// so a long linear conversation stays flat. Clicking a node moves the current position there.
export function Tree({ sid, state, busy }: { sid: string; state: State; busy: boolean }) {
  const kids = childrenOf(state.entries);
  const onPath = new Set(pathTo(state.entries, state.leaf).map((e) => e.id));
  const rows: { e: Entry; depth: number }[] = [];
  const stack: { e: Entry; depth: number }[] = (kids["root"] ?? []).map((e) => ({ e, depth: 0 })).reverse();
  while (stack.length) {
    const { e, depth } = stack.pop()!;
    const children = kids[e.id] ?? [];
    if (e.type === "user" || e.type === "assistant") rows.push({ e, depth });
    const step = children.length > 1 ? 1 : 0;
    for (const c of [...children].reverse()) stack.push({ e: c, depth: depth + step });
  }
  return (
    <aside className="tree">
      <div className="tree-title">会话树</div>
      {rows.map(({ e, depth }) => (
        <button key={e.id} className={`tree-node${onPath.has(e.id) ? " on" : ""}${e.id === state.leaf ? " leaf" : ""}`}
          style={{ paddingLeft: 8 + depth * 14 }} disabled={busy} onClick={() => api.checkout(sid, e.id)}
          title={e.id}>
          {(kids[e.parent_id ?? "root"]?.length ?? 0) > 1 && <span className="fork" title="分支起点">⑂</span>}
          <span className="who">{e.type === "user" ? "你" : "AI"}</span>
          {label(e)}
        </button>
      ))}
    </aside>
  );
}

function label(e: Entry): string {
  const text = textOf(e.data.content).trim().split("\n")[0].slice(0, 40);
  const tools = (e.data.content as { type: string; name?: string }[]).filter((b) => b.type === "tool_call").map((b) => b.name);
  return [text, tools.length ? `→ ${tools.join(", ")}` : ""].filter(Boolean).join(" ") || "…";
}
