import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type Approval, type Block, type Entry } from "./api";
import { childrenOf, pathTo, textOf, type State } from "./session";

type ToolCall = Extract<Block, { type: "tool_call" }>;

export function Chat({ sid, state, onFork }: { sid: string; state: State; onFork: (entryId: string) => void }) {
  const { entries, leaf, live } = state;
  const path = pathTo(entries, leaf);
  const kids = childrenOf(entries);
  const results: Record<string, Entry> = {};
  const started = new Set<string>();
  for (const e of path) {
    if (e.type === "tool_result") results[e.data.call_id] = e;
    if (e.type === "tool_started") started.add(e.data.call_id);
  }
  const waiting = new Map((live?.approvals ?? []).map((a) => [a.call_id, a]));
  const busy = live !== null;

  // Follow the bottom while streaming, unless the user scrolled up to read.
  const end = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = box.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 200) end.current?.scrollIntoView();
  });

  return (
    <div className="chat" ref={box}>
      {path.length <= 1 && <div className="empty">发一条消息开始。助手可以读写这个项目里的文件、运行命令。</div>}
      {path.map((e) => {
        if (e.type === "user") {
          const siblings = (kids[e.parent_id ?? "root"] ?? []).filter((x) => x.type === "user");
          return <UserMessage key={e.id} sid={sid} entry={e} siblings={siblings} busy={busy} onFork={onFork} />;
        }
        if (e.type === "assistant")
          return (
            <AssistantMessage key={e.id} entry={e} results={results} started={started} waiting={waiting}
              outputs={live?.outputs ?? {}} />
          );
        return null;
      })}
      {live && (live.thinking || live.text) && (
        <div className="msg assistant streaming">
          {live.thinking && <Thinking text={live.thinking} open />}
          {live.text && <Markdown text={live.text} />}
        </div>
      )}
      {live && !live.thinking && !live.text && live.approvals.length === 0 && <div className="working">工作中…</div>}
      {live?.approvals.map((a) => <ApprovalCard key={a.id} approval={a} />)}
      {live && [...live.steer.map((t) => ["插话", t]), ...live.queue.map((t) => ["排队", t])].map(([kind, t], i) => (
        <div key={i} className="msg user pending"><span className="tag">{kind}</span>{t}</div>
      ))}
      <div ref={end} />
    </div>
  );
}

function UserMessage({ sid, entry, siblings, busy, onFork }: {
  sid: string; entry: Entry; siblings: Entry[]; busy: boolean; onFork: (id: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const text = textOf(entry.data.content);
  const i = siblings.findIndex((s) => s.id === entry.id);
  const go = (to: Entry) => api.checkout(sid, to.id, true);

  if (editing)
    return (
      <div className="msg user editing">
        <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={Math.min(10, draft.split("\n").length + 1)} autoFocus />
        <div className="row">
          <button onClick={() => setEditing(false)}>取消</button>
          <button className="primary" disabled={!draft.trim()} onClick={async () => {
            await api.send(sid, { text: draft, parent_id: entry.parent_id });
            setEditing(false);
          }}>保存并重新生成</button>
        </div>
      </div>
    );
  return (
    <div className={`msg user${entry.data.synthetic ? " synthetic" : ""}`}>
      <div className="body">{text}</div>
      <div className="meta">
        {siblings.length > 1 && (
          <span className="branches" title="这条消息的不同版本（分支）">
            <button disabled={busy || i === 0} onClick={() => go(siblings[i - 1])}>‹</button>
            {i + 1}/{siblings.length}
            <button disabled={busy || i === siblings.length - 1} onClick={() => go(siblings[i + 1])}>›</button>
          </span>
        )}
        {!busy && !entry.data.synthetic && (
          <>
            <button className="link" onClick={() => { setDraft(text); setEditing(true); }}>编辑</button>
            <button className="link" onClick={() => onFork(entry.id)} title="把到这里为止的对话复制成新会话">分叉</button>
          </>
        )}
      </div>
    </div>
  );
}

function AssistantMessage({ entry, results, started, waiting, outputs }: {
  entry: Entry; results: Record<string, Entry>; started: Set<string>; waiting: Map<string, Approval>;
  outputs: Record<string, string>;
}) {
  const blocks: Block[] = entry.data.content;
  return (
    <div className={`msg assistant${entry.data.stop === "aborted" ? " aborted" : ""}`}>
      {blocks.map((b, i) => {
        if (b.type === "thinking") return <Thinking key={i} text={b.text} />;
        if (b.type === "text") return <Markdown key={i} text={b.text} />;
        if (b.type === "tool_call")
          return (
            <ToolCard key={b.id} call={b} result={results[b.id]} running={started.has(b.id)}
              waiting={waiting.has(b.id)} output={outputs[b.id]} />
          );
        return null;
      })}
      {entry.data.stop === "aborted" && <div className="note">（已中止）</div>}
      {entry.data.stop === "length" && <div className="note">（输出达到长度上限）</div>}
    </div>
  );
}

function Thinking({ text, open = false }: { text: string; open?: boolean }) {
  return (
    <details className="thinking" open={open}>
      <summary>思考过程</summary>
      <div>{text}</div>
    </details>
  );
}

function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: (p) => <a {...p} target="_blank" rel="noreferrer" /> }}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

function summary(name: string, args: Record<string, any>): string {
  if (name === "bash") return String(args.command ?? "");
  return String(args.path ?? JSON.stringify(args));
}

function ToolArgs({ name, args }: { name: string; args: Record<string, any> }) {
  if (name === "edit")
    return (
      <div className="diff">
        <pre className="del">{args.old_string}</pre>
        <pre className="add">{args.new_string}</pre>
      </div>
    );
  if (name === "write") return <pre>{String(args.content ?? "").slice(0, 4000)}</pre>;
  if (name === "bash") return <pre>{args.command}</pre>;
  return <pre>{JSON.stringify(args, null, 2)}</pre>;
}

function ToolCard({ call, result, running, waiting, output }: {
  call: ToolCall; result?: Entry; running: boolean; waiting: boolean; output?: string;
}) {
  const status = result ? (result.data.is_error ? "error" : "done") : waiting ? "waiting" : running ? "running" : "pending";
  const label = { done: "完成", error: "出错", waiting: "等待确认", running: "运行中", pending: "排队" }[status];
  const [open, setOpen] = useState(false);
  const content: Block[] = result?.data.content ?? [];
  return (
    <div className={`tool ${status}`}>
      <div className="tool-head" onClick={() => setOpen(!open)}>
        <span className="dot" />
        <b>{call.name}</b>
        <code>{summary(call.name, call.args)}</code>
        <span className="status">{label}</span>
      </div>
      {(open || status === "running") && (
        <div className="tool-body">
          {open && <ToolArgs name={call.name} args={call.args} />}
          {result
            ? content.map((b, i) =>
                b.type === "image" ? <img key={i} src={`data:${b.mime};base64,${b.data}`} /> : b.type === "text" ? <pre key={i} className="out">{b.text}</pre> : null)
            : output && <pre className="out">{output}</pre>}
        </div>
      )}
    </div>
  );
}

function ApprovalCard({ approval }: { approval: Approval }) {
  const [rejecting, setRejecting] = useState(false);
  const [feedback, setFeedback] = useState("");
  const decide = (d: "once" | "always" | "reject") => api.approve(approval.id, d, d === "reject" ? feedback || undefined : undefined);
  return (
    <div className="approval">
      <div className="approval-title">需要你确认：<b>{approval.tool}</b></div>
      <ToolArgs name={approval.tool} args={approval.args} />
      {approval.tool !== "bash" && approval.args.path && <div className="note">文件：{approval.args.path}</div>}
      {rejecting ? (
        <div className="row">
          <input placeholder="告诉助手为什么拒绝、应该怎么做（可选）" value={feedback} onChange={(e) => setFeedback(e.target.value)} autoFocus
            onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && decide("reject")} />
          <button className="danger" onClick={() => decide("reject")}>拒绝</button>
        </div>
      ) : (
        <div className="row">
          <button className="primary" onClick={() => decide("once")}>允许</button>
          <button onClick={() => decide("always")}>本会话总是允许 {approval.tool}</button>
          <button className="danger" onClick={() => setRejecting(true)}>拒绝…</button>
        </div>
      )}
    </div>
  );
}
