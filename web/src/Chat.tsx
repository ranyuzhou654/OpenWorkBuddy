import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  api, type Approval, type Artifact, type Attachment, type Block, type Entry, type FeishuPreview, type Question, type Receipt,
  type Source, type Todo,
} from "./api";
import { ArtifactCard, AttachmentChips } from "./Files";
import { childrenOf, pathTo, sourcesOf, textOf, type State } from "./session";

type ToolCall = Extract<Block, { type: "tool_call" }>;

// What the conversation shows, in order. Consecutive tool calls, even across several model turns, form one
// collapsible group of steps; text, thinking and anything the user sees as an event break the group.
type Item =
  | { kind: "entry"; e: Entry }
  | { kind: "say"; key: string; blocks: Block[]; stop?: string }
  | { kind: "steps"; key: string; calls: Step[] }
  | { kind: "artifact"; key: string; a: Artifact }
  | { kind: "receipt"; key: string; r: Receipt };

type Thought = Extract<Block, { type: "thinking" }>;
type Step = ToolCall | Thought; // thinking that only leads into tool calls stays inside the group

function toItems(path: Entry[]): Item[] {
  const items: Item[] = [];
  let steps: Step[] = [];
  const flush = () => {
    const first = steps.find((x) => x.type === "tool_call");
    if (first) items.push({ kind: "steps", key: first.id, calls: steps });
    else if (steps.length) items.push({ kind: "say", key: `t:${items.length}`, blocks: steps });
    steps = [];
  };
  for (const e of path) {
    if (e.type === "assistant") {
      const blocks: Block[] = e.data.content;
      const speaks = blocks.some((b) => b.type === "text" && b.text.trim());
      let said: Block[] = [];
      blocks.forEach((b, i) => {
        if (b.type === "tool_call" || (b.type === "thinking" && b.text && !speaks)) {
          if (said.length) items.push({ kind: "say", key: `${e.id}:${i}`, blocks: said });
          said = [];
          steps.push(b);
        } else if (b.type === "text" || (b.type === "thinking" && b.text)) {
          flush();
          said.push(b);
        }
      });
      if (said.length) items.push({ kind: "say", key: e.id, blocks: said });
      if (e.data.stop === "aborted" || e.data.stop === "length") {
        flush();
        items.push({ kind: "say", key: `${e.id}:stop`, blocks: [], stop: e.data.stop });
      }
    } else if (VISIBLE.has(e.type)) {
      flush();
      items.push({ kind: "entry", e });
    } else if (e.type === "tool_result" && e.data.meta?.artifact) { // a delivered file is an event the user sees
      flush();
      items.push({ kind: "artifact", key: e.id, a: e.data.meta.artifact });
    } else if (e.type === "tool_result" && e.data.meta?.receipt) { // so is a message sent or a document written
      flush();
      items.push({ kind: "receipt", key: e.id, r: e.data.meta.receipt });
    }
  }
  flush();
  return items;
}

const VISIBLE = new Set(["user", "compaction", "branch_summary", "prune", "notice"]);

export function Chat({ sid, state, onFork }: { sid: string; state: State; onFork: (entryId: string) => void }) {
  const { entries, leaf, live } = state;
  const path = pathTo(entries, leaf);
  const kids = childrenOf(entries);
  const runs: Runs = { results: {}, started: {}, outputs: live?.outputs ?? {}, live: live !== null,
    waiting: new Set([...(live?.approvals ?? []), ...(live?.questions ?? [])].map((a) => a.call_id)) };
  for (const e of path) {
    if (e.type === "tool_result") runs.results[e.data.call_id] = e;
    if (e.type === "tool_started") runs.started[e.data.call_id] = e.ts;
  }
  const todos = latestTodos(path);
  const busy = live !== null;
  const items = toItems(path);
  const cites = Object.fromEntries(sourcesOf(entries).map((x) => [x.n, x]));

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
      {items.map((it, n) => {
        if (it.kind === "steps")
          return <StepGroup key={it.key} calls={it.calls} runs={runs} last={n === items.length - 1} />;
        if (it.kind === "artifact") return <ArtifactCard key={it.key} sid={sid} a={it.a} />;
        if (it.kind === "receipt") return <ReceiptCard key={it.key} r={it.r} />;
        if (it.kind === "say")
          return (
            <div key={it.key} className="msg assistant">
              {it.blocks.map((b, i) => b.type === "thinking" ? <Thinking key={i} text={b.text} />
                : b.type === "text" ? <Markdown key={i} text={b.text} cites={cites} /> : null)}
              {it.stop === "aborted" && <div className="note">（已中止）</div>}
              {it.stop === "length" && <div className="note">（输出达到长度上限）</div>}
            </div>
          );
        const e = it.e;
        if (e.type === "user") {
          const siblings = (kids[e.parent_id ?? "root"] ?? []).filter((x) => x.type === "user");
          return <UserMessage key={e.id} sid={sid} entry={e} siblings={siblings} busy={busy} onFork={onFork} />;
        }
        if (e.type === "compaction")
          return (
            <details key={e.id} className="divider">
              <summary>上下文已压缩：约 {e.data.tokens_before} tokens 的较早对话被摘要替代（原文仍保存在会话里）</summary>
              <Markdown text={e.data.summary} />
            </details>
          );
        if (e.type === "branch_summary")
          return (
            <details key={e.id} className="divider">
              <summary>已总结离开的分支，助手会参考这段摘要</summary>
              <Markdown text={e.data.summary} />
            </details>
          );
        if (e.type === "prune")
          return <div key={e.id} className="divider">已从上下文中清理 {e.data.count} 条旧工具输出（全文已另存）</div>;
        return (
          <details key={e.id} className="divider">
            <summary>{NOTICES[e.data.kind] ?? (e.data.kind.startsWith("skill:") ? `已加载技能 ${e.data.kind.slice(6)}` : "系统通知")}</summary>
            <div className="note">{e.data.text}</div>
          </details>
        );
      })}
      {live && (live.thinking || live.text) && (
        <div className="msg assistant streaming">
          {live.thinking && <Thinking text={live.thinking} open />}
          {live.text && <Markdown text={live.text} cites={cites} />}
        </div>
      )}
      {live && !live.thinking && !live.text && runs.waiting.size === 0 && <div className="working">工作中…</div>}
      {live?.approvals.map((a) => <ApprovalCard key={a.id} approval={a} />)}
      {live?.questions.map((q) => <QuestionCard key={q.id} question={q} />)}
      {live && [...live.steer.map((t) => ["插话", t]), ...live.queue.map((t) => ["排队", t])].map(([kind, t], i) => (
        <div key={i} className="msg user pending"><span className="tag">{kind}</span>{t}</div>
      ))}
      <div ref={end} />
      {todos && (busy || todos.some((t) => t.status !== "completed")) && <TodoPanel todos={todos} />}
    </div>
  );
}

function UserMessage({ sid, entry, siblings, busy, onFork }: {
  sid: string; entry: Entry; siblings: Entry[]; busy: boolean; onFork: (id: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const text = textOf(entry.data.content);
  const files: Attachment[] = entry.data.attachments ?? [];
  const i = siblings.findIndex((s) => s.id === entry.id);
  const go = (to: Entry) => api.checkout(sid, to.id, true);

  if (editing)
    return (
      <div className="msg user editing">
        <textarea value={draft} onChange={(e) => setDraft(e.target.value)} rows={Math.min(10, draft.split("\n").length + 1)} autoFocus />
        <div className="row">
          <button onClick={() => setEditing(false)}>取消</button>
          <button className="primary" disabled={!draft.trim() && !files.length} onClick={async () => {
            await api.send(sid, { text: draft, parent_id: entry.parent_id, attachments: files.map((f) => f.id) });
            setEditing(false);
          }}>保存并重新生成</button>
        </div>
      </div>
    );
  return (
    <div className={`msg user${entry.data.synthetic ? " synthetic" : ""}`}>
      {text && <div className="body">{text}</div>}
      {files.length > 0 && <AttachmentChips sid={sid} files={files} />}
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

function Thinking({ text, open = false }: { text: string; open?: boolean }) {
  return (
    <details className="thinking" open={open}>
      <summary>思考过程</summary>
      <div>{text}</div>
    </details>
  );
}

// [S3] in an answer becomes a link to that source; unknown numbers stay plain text.
function Markdown({ text, cites = {} }: { text: string; cites?: Record<number, Source> }) {
  const linked = text.replace(/\[S(\d+)\](?!\()/g, (m, n) => {
    const s = cites[Number(n)];
    return s ? `[S${n}](${encodeURI(s.url)} "${(s.title || s.url).replace(/"/g, "'")}")` : m;
  });
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
        a: ({ children, ...p }) => (
          <a {...p} target="_blank" rel="noreferrer" className={/^S\d+$/.test(String(children)) ? "cite" : undefined}>{children}</a>
        ),
      }}>
        {linked}
      </ReactMarkdown>
    </div>
  );
}

const NOTICES: Record<string, string> = {
  permissions: "已告知模型当前的权限设置",
  mcp: "已告知模型 MCP 服务器状态",
  skills: "已告知模型可用的技能",
};

// mcp__github__create_issue → github › create_issue
function toolLabel(name: string): string {
  const m = name.match(/^mcp__(.+?)__(.+)$/);
  return m ? `${m[1]} › ${m[2]}` : name;
}

function summary(name: string, args: Record<string, any>): string {
  if (name === "bash") return (args.unsandboxed ? "[沙箱外] " : "") + String(args.command ?? "");
  if (name === "tool_search") return String(args.query ?? "");
  if (name === "skill") return String(args.name ?? "");
  if (name === "grep") return `${args.pattern ?? ""}${args.glob ? `  (${args.glob})` : ""}${args.path && args.path !== "." ? `  in ${args.path}` : ""}`;
  if (name === "glob") return `${args.pattern ?? ""}${args.path && args.path !== "." ? `  in ${args.path}` : ""}`;
  if (name === "web_fetch") return String(args.url ?? "");
  if (name === "web_search") return String(args.query ?? "");
  if (name === "feishu") return Array.isArray(args.command) ? args.command.join(" ") : "";
  if (name === "recall") return String(args.query ?? "");
  if (name === "task_result") return `${args.outcome ?? ""}：${args.summary ?? ""}`;
  if (name.startsWith("mcp__browser__")) return String(args.url ?? args.element ?? args.text ?? args.key ?? "");
  if (name === "create_document" || name === "present") return String(args.path ?? "");
  if (name === "ask_user") return String(args.question ?? "");
  if (name === "todo") {
    const list: Todo[] = Array.isArray(args.todos) ? args.todos : [];
    return `${list.filter((t) => t.status === "completed").length}/${list.length} 完成`;
  }
  if (name === "memory") return `${{ add: "添加", replace: "修改", remove: "删除" }[args.action as string] ?? args.action} ${args.file === "user" ? "USER.md" : "AGENTS.md"}`;
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
  if (name === "todo") return <TodoList todos={Array.isArray(args.todos) ? args.todos : []} />;
  if (name === "write") return <pre>{String(args.content ?? "").slice(0, 4000)}</pre>;
  if (name === "create_document") return <pre>{String(args.content ?? "").slice(0, 4000)}</pre>;
  if (name === "bash") return <pre>{args.command}</pre>;
  if (name === "memory")
    return (
      <div className="diff">
        {args.old_text && <pre className="del">{args.old_text}</pre>}
        {args.text && <pre className="add">{args.text}</pre>}
      </div>
    );
  return <pre>{JSON.stringify(args, null, 2)}</pre>;
}

interface Runs {
  results: Record<string, Entry>; // call id -> its tool_result
  started: Record<string, number>; // call id -> when it started
  outputs: Record<string, string>; // call id -> streamed output of a running call
  waiting: Set<string>; // call ids waiting for an approval or an answer
  live: boolean;
}

type Status = "done" | "error" | "running" | "waiting" | "pending";

function statusOf(call: ToolCall, runs: Runs): Status {
  const r = runs.results[call.id];
  if (r) return r.data.is_error ? "error" : "done";
  if (runs.waiting.has(call.id)) return "waiting";
  return call.id in runs.started && runs.live ? "running" : "pending";
}

function seconds(call: ToolCall, runs: Runs): number | null {
  const r = runs.results[call.id];
  const t0 = runs.started[call.id];
  return r && t0 ? Math.max(0, r.ts - t0) : null;
}

function duration(s: number): string {
  if (s < 1) return `${Math.round(s * 1000)}ms`;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`;
}

// Open while something in it is still happening; a finished group of more than three steps folds away.
// Clicking the header overrides that either way.
function StepGroup({ calls: steps, runs, last }: { calls: Step[]; runs: Runs; last: boolean }) {
  const calls = steps.filter((x): x is ToolCall => x.type === "tool_call");
  const statuses = calls.map((c) => statusOf(c, runs));
  const active = statuses.some((s) => s === "running" || s === "waiting") || (runs.live && last);
  const [choice, setChoice] = useState<boolean | null>(null);
  const open = choice ?? (active || calls.length <= 3);
  const errors = statuses.filter((s) => s === "error").length;
  const total = calls.reduce((sum, c) => sum + (seconds(c, runs) ?? 0), 0);
  const names = [...new Set(calls.map((c) => toolLabel(c.name)))];
  const counted = names.map((n) => {
    const k = calls.filter((c) => toolLabel(c.name) === n).length;
    return k > 1 ? `${n} ×${k}` : n;
  });
  return (
    <div className="steps">
      {calls.length > 1 && (
        <div className="steps-head" onClick={() => setChoice(!open)}>
          <span className={`caret${open ? " open" : ""}`} />
          <span className="count">{calls.length} 个步骤</span>
          {!open && <span className="names">{counted.join(" · ")}</span>}
          <span className="right">
            {errors > 0 && <span className="danger-text">{errors} 个出错</span>}
            {active ? <span style={{ color: "var(--clay-text)" }}>进行中</span> : total > 0 && <span>{duration(total)}</span>}
          </span>
        </div>
      )}
      {open && steps.map((x, i) => x.type === "thinking" ? <ThoughtRow key={`t${i}`} text={x.text} />
        : <StepRow key={x.id} call={x} status={statuses[calls.indexOf(x)]} runs={runs} />)}
    </div>
  );
}

const ICONS: Record<string, string> = {
  read: "R", write: "W", edit: "E", bash: "$", grep: "G", glob: "*", web_fetch: "↗", web_search: "⌕", todo: "✓", ask_user: "?",
  create_document: "D", present: "⇪", feishu: "飞", recall: "↺", task_result: "◎",
  skill: "S", memory: "M", tool_search: "⌕",
};

function StepRow({ call, status, runs }: { call: ToolCall; status: Status; runs: Runs }) {
  const [open, setOpen] = useState(false);
  const result = runs.results[call.id];
  const content: Block[] = result?.data.content ?? [];
  const took = seconds(call, runs);
  const label = { done: took !== null ? duration(took) : "完成", error: "出错", running: "运行中", pending: "排队",
    waiting: call.name === "ask_user" ? "等你回答" : "等你确认" }[status];
  const live = status === "running" ? runs.outputs[call.id] : undefined;
  return (
    <div className={`step ${status}`}>
      <div className="step-row" onClick={() => setOpen(!open)} title={open ? "收起" : "展开参数和输出"}>
        <span className="ic">{call.name.startsWith("mcp__") ? "◆" : ICONS[call.name] ?? "·"}</span>
        <span className="name">{toolLabel(call.name)}</span>
        <code>{summary(call.name, call.args)}</code>
        {call.name === "edit" && <Delta args={call.args} />}
        <span className="st">{label}</span>
      </div>
      {(open || live) && (
        <div className="step-body">
          {open && <ToolArgs name={call.name} args={call.args} />}
          {result
            ? content.map((b, i) =>
                b.type === "image" ? <img key={i} src={`data:${b.mime};base64,${b.data}`} />
                  : b.type === "text" ? <pre key={i} className="out">{b.text}</pre> : null)
            : live && <pre className="out">{live}</pre>}
        </div>
      )}
    </div>
  );
}

function ThoughtRow({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="step thought">
      <div className="step-row" onClick={() => setOpen(!open)}>
        <span className="ic">…</span>
        <span className="name">思考</span>
        <code>{text.trim().split("\n")[0]}</code>
      </div>
      {open && <div className="step-body"><div className="thought-text">{text}</div></div>}
    </div>
  );
}

// Lines removed and added by an edit, the way a diff stat shows them.
function Delta({ args }: { args: Record<string, any> }) {
  const lines = (s: unknown) => (typeof s === "string" && s ? s.split("\n").length : 0);
  return (
    <span className="delta"><span className="a">+{lines(args.new_string)}</span> <span className="d">−{lines(args.old_string)}</span></span>
  );
}

function ApprovalCard({ approval }: { approval: Approval }) {
  const [rejecting, setRejecting] = useState(false);
  const [feedback, setFeedback] = useState("");
  const decide = (d: "once" | "always" | "reject") => api.approve(approval.id, d, d === "reject" ? feedback || undefined : undefined);
  return (
    <div className="approval">
      <div className="approval-title">需要你确认：<b>{toolLabel(approval.tool)}</b></div>
      {approval.action === "bash.unsandboxed" && (
        <div className="warn-line">这条命令要求在沙箱外运行：可以联网，也可以写项目目录以外的文件。</div>
      )}
      {approval.tool === "feishu" ? <FeishuApproval args={approval.args} preview={approval.preview ?? null} />
        : <ToolArgs name={approval.tool} args={approval.args} />}
      {approval.resource && approval.tool !== "bash" && approval.tool !== "feishu" && (
        <div className="note">{approval.resource.startsWith("http") ? "地址" : "实际路径"}：{approval.resource}</div>
      )}
      {approval.tool.startsWith("mcp__") && <div className="note">来自 MCP 服务器的工具。“总是允许”会放行这个工具的所有调用（本会话内）。</div>}
      {rejecting ? (
        <div className="row">
          <input placeholder="告诉助手为什么拒绝、应该怎么做（可选）" value={feedback} onChange={(e) => setFeedback(e.target.value)} autoFocus
            onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && decide("reject")} />
          <button className="danger" onClick={() => decide("reject")}>拒绝</button>
        </div>
      ) : (
        <div className="row">
          <button className="primary" onClick={() => decide("once")}>允许</button>
          {approval.always ? (
            <button onClick={() => decide("always")} title={`添加规则：${approval.always.action} ${approval.always.resource}`}>
              本会话总是允许{approval.always.resource.endsWith("/*") ? ` ${approval.always.resource}`
                : approval.always.resource ? "这条命令" : "此工具"}
            </button>
          ) : <span className="note">这类操作每次单独确认</span>}
          <button className="danger" onClick={() => setRejecting(true)}>拒绝…</button>
        </div>
      )}
    </div>
  );
}

// What a Feishu write will do: who, what, to which object, the content, and the CLI's own dry run.
const CONTENT_FLAGS = ["--markdown", "--text", "--content", "--title", "--name", "--pattern", "--command", "--file"];
const FEISHU_ACTIONS: Record<string, string> = {
  "send a message": "发送消息", "reply to a message": "回复消息", "create a document": "创建文档",
  "change a document": "修改文档", "import a local file as a cloud document": "导入文件为云文档",
};

function FeishuApproval({ args, preview }: { args: Record<string, any>; preview: FeishuPreview | null }) {
  const argv: string[] = Array.isArray(args.command) ? args.command.map(String) : [];
  const flag = (f: string) => { const i = argv.indexOf(f); return i >= 0 ? argv[i + 1] : undefined; };
  return (
    <div className="feishu-approval">
      <div className="kv"><span>账户</span><b>{preview?.account ?? "（未知）"}</b></div>
      <div className="kv"><span>操作</span><b>{FEISHU_ACTIONS[preview?.action ?? ""] ?? argv.slice(0, 2).join(" ")}</b></div>
      <div className="kv"><span>对象</span><code>{preview?.target ?? "—"}</code></div>
      {CONTENT_FLAGS.filter((f) => flag(f) !== undefined).map((f) => (
        <div key={f} className="kv"><span>{f.slice(2)}</span><pre>{flag(f)}</pre></div>
      ))}
      {preview?.error && <div className="note warn-line">预览失败：{preview.error}</div>}
      {preview?.dry_run !== undefined && (
        <details><summary>dry-run 预览（将发出的请求）</summary><pre>{JSON.stringify(preview.dry_run, null, 2)}</pre></details>
      )}
      <details><summary>完整命令</summary><pre>lark-cli {argv.join(" ")} --as user</pre></details>
    </div>
  );
}

function ReceiptCard({ r }: { r: Receipt }) {
  return (
    <div className="receipt">
      <div className="receipt-head"><span className="ic">飞</span><b>已在飞书{FEISHU_ACTIONS[r.what] ?? r.command}</b>
        <code>{r.target}</code></div>
      {r.links.map((l) => <a key={l} href={l} target="_blank" rel="noreferrer">{l}</a>)}
      <details><summary>回执</summary><pre>{JSON.stringify(r.result, null, 2).slice(0, 4000)}</pre></details>
    </div>
  );
}

function latestTodos(path: Entry[]): Todo[] | null {
  for (let i = path.length - 1; i >= 0; i--) {
    const e = path[i];
    if (e.type === "tool_result" && e.data.name === "todo" && e.data.meta?.todos) return e.data.meta.todos;
  }
  return null;
}

const TODO_MARK = { pending: "○", in_progress: "◐", completed: "●" } as const;

function TodoList({ todos }: { todos: Todo[] }) {
  return (
    <ul className="todo-list">
      {todos.map((t, i) => <li key={i} className={t.status}><span>{TODO_MARK[t.status]}</span>{t.content}</li>)}
    </ul>
  );
}

// The latest plan stays in view at the bottom of the conversation while there is work left.
function TodoPanel({ todos }: { todos: Todo[] }) {
  const done = todos.filter((t) => t.status === "completed").length;
  return (
    <div className="todo-panel">
      <div className="todo-title">计划 {done}/{todos.length}</div>
      <TodoList todos={todos} />
    </div>
  );
}

function QuestionCard({ question }: { question: Question }) {
  const [text, setText] = useState("");
  const [sent, setSent] = useState(false);
  const answer = async (a: string) => {
    if (!a.trim()) return;
    setSent(true);
    await api.answer(question.id, a.trim()).catch(() => setSent(false));
  };
  return (
    <div className="approval question">
      <div className="approval-title">助手想问你：</div>
      <div className="question-text">{question.question}</div>
      {question.options.length > 0 && (
        <div className="row options">
          {question.options.map((o) => <button key={o} disabled={sent} onClick={() => answer(o)}>{o}</button>)}
        </div>
      )}
      <div className="row">
        <input placeholder={question.options.length ? "或者输入你自己的回答" : "输入回答"} value={text} disabled={sent}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && answer(text)} />
        <button className="primary" disabled={sent || !text.trim()} onClick={() => answer(text)}>回答</button>
      </div>
    </div>
  );
}
