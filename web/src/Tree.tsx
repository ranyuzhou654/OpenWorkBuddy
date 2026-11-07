import { useEffect, useState } from "react";
import { api, branchSummary, type Entry } from "./api";
import { childrenOf, pathTo, textOf, type State } from "./session";
import { Trace } from "./Trace";

// The whole conversation tree. Indentation grows only where the tree branches,
// so a long linear conversation stays flat. Clicking a node moves the current position there.
export function Tree({ sid, state, busy, onReveal }: {
  sid: string;
  state: State;
  busy: boolean;
  onReveal: (entryId: string) => void;
}) {
  const [summarize, setSummarize] = useState(branchSummary.get());
  const [tab, setTab] = useState<"chat" | "trace">(() => localStorage.getItem("nwb.treeTab") === "trace" ? "trace" : "chat");
  useEffect(() => {
    localStorage.setItem("nwb.treeTab", tab);
  }, [tab]);
  const onTabKey = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const next = tab === "chat" ? "trace" : "chat";
    setTab(next);
    document.getElementById(`tree-tab-${next}`)?.focus();
  };
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
      <div className="tree-tabs" role="tablist" aria-label="会话视图">
        <button type="button" role="tab" id="tree-tab-chat" aria-selected={tab === "chat"} tabIndex={tab === "chat" ? 0 : -1}
          className={tab === "chat" ? "on" : ""} onClick={() => setTab("chat")} onKeyDown={onTabKey}>
          对话
        </button>
        <button type="button" role="tab" id="tree-tab-trace" aria-selected={tab === "trace"} tabIndex={tab === "trace" ? 0 : -1}
          className={tab === "trace" ? "on" : ""} onClick={() => setTab("trace")} onKeyDown={onTabKey}>
          轨迹{busy && <span className="tab-live" aria-label="正在运行" />}
        </button>
      </div>
      {tab === "chat" ? (
        <div className="tree-body" id="tree-pane-chat" role="tabpanel" aria-labelledby="tree-tab-chat">
          <label className="tree-option" title="离开一条分支时，让模型把它总结成一段摘要，挂到新位置，助手就知道那条分支上做过什么">
            <input type="checkbox" checked={summarize} onChange={(e) => { branchSummary.set(e.target.checked); setSummarize(e.target.checked); }} />
            切换分支时总结离开的分支
          </label>
          {rows.length === 0 && <div className="note pad">还没有可切换的对话节点。发一条消息后，分支会显示在这里。</div>}
          {rows.map(({ e, depth }) => (
            <button key={e.id} className={`tree-node${onPath.has(e.id) ? " on" : ""}${e.id === state.leaf ? " leaf" : ""}`}
              style={{ paddingLeft: 8 + depth * 14 }} disabled={busy} onClick={() => api.checkout(sid, e.id)}
              aria-current={e.id === state.leaf ? "true" : undefined} title={e.id}>
              {(kids[e.parent_id ?? "root"]?.length ?? 0) > 1 && <span className="fork" title="分支起点">⑂</span>}
              <span className="who">{e.type === "user" ? "你" : "AI"}</span>
              {label(e)}
            </button>
          ))}
        </div>
      ) : (
        <div className="tree-body trace-pane" id="tree-pane-trace" role="tabpanel" aria-labelledby="tree-tab-trace">
          <Trace entries={state.entries} leaf={state.leaf} busy={busy} onReveal={onReveal} />
        </div>
      )}
    </aside>
  );
}

function label(e: Entry): string {
  const text = textOf(e.data.content).trim().split("\n")[0].slice(0, 40);
  const tools = (e.data.content as { type: string; name?: string }[]).filter((b) => b.type === "tool_call").map((b) => b.name);
  return [text, tools.length ? `→ ${tools.join(", ")}` : ""].filter(Boolean).join(" ") || "…";
}
