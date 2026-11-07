import { Check, Copy } from "lucide-react";
import { motion } from "motion/react";
import {
  type ComponentProps, isValidElement, memo, type ReactNode, useEffect, useLayoutEffect, useMemo, useRef, useState,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  api, type Approval, type Artifact, type Attachment, type Block, type DeliveryRecovery, type Entry, type FeishuPreview,
  type Question, type Receipt,
  type Source, type Todo,
} from "./api";
import { ArtifactCard, AttachmentChips } from "./Files";
import { childrenOf, pathTo, sourcesOf, textOf, type State } from "./session";
import { useToast } from "./ui";

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
const itemKey = (it: Item) => (it.kind === "entry" ? it.e.id : it.key);
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

const WINDOW = 40;

export function Chat({ sid, state, onFork, reveal }: {
  sid: string;
  state: State;
  onFork: (entryId: string) => void;
  reveal: { id: string; nonce: number } | null;
}) {
  const { entries, leaf, live } = state;
  const path = useMemo(() => pathTo(entries, leaf), [entries, leaf]);
  const kids = useMemo(() => childrenOf(entries), [entries]);
  const runs = useMemo(() => {
    const next: Runs = { results: {}, started: {}, outputs: live?.outputs ?? {}, live: live !== null,
      waiting: new Set([...(live?.approvals ?? []), ...(live?.questions ?? [])].map((a) => a.call_id)) };
    for (const e of path) {
      if (e.type === "tool_result") next.results[e.data.call_id] = e;
      if (e.type === "tool_started") next.started[e.data.call_id] = e.ts;
    }
    return next;
  }, [path, live?.outputs, live?.approvals, live?.questions, live !== null]);
  const todos = useMemo(() => latestTodos(path), [path]);
  const busy = live !== null;
  const items = useMemo(() => toItems(path), [path]);
  const cites = useMemo(() => Object.fromEntries(sourcesOf(entries).map((x) => [x.n, x])), [entries]);
  const userSiblings = useMemo(() => {
    const map = new Map<string, Entry[]>();
    for (const e of path) if (e.type === "user")
      map.set(e.id, (kids[e.parent_id ?? "root"] ?? []).filter((x) => x.type === "user"));
    return map;
  }, [path, kids]);

  // Only the newest window is mounted; older turns load on demand, keeping long sessions cheap to render.
  const maxStart = Math.max(0, items.length - WINDOW);
  const [start, setStart] = useState(maxStart);
  const windowStart = Math.min(start, maxStart);
  const visible = items.slice(windowStart);
  const anchor = useRef<number | null>(null);

  // Animate only items that arrive after the first paint (and only the newest few), never a whole reload.
  const seen = useRef<Set<string> | null>(null);
  const firstPaint = seen.current === null;
  if (seen.current === null) seen.current = new Set(items.map(itemKey));
  useEffect(() => {
    for (const item of items) seen.current?.add(itemKey(item));
  }, [items]);
  const fresh = (key: string, index: number) => !firstPaint && !seen.current?.has(key) && index >= items.length - 6;

  // Follow the bottom while streaming, unless the user scrolled up to read.
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [showJump, setShowJump] = useState(false);
  const previousCount = useRef(items.length);
  const pinnedHistory = useRef(false);
  useEffect(() => {
    const grew = items.length > previousCount.current;
    previousCount.current = items.length;
    if (grew && stick.current && !pinnedHistory.current) setStart(Math.max(0, items.length - WINDOW));
  }, [items.length]);
  useLayoutEffect(() => {
    if (anchor.current === null) return;
    const el = box.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight - anchor.current;
    anchor.current = null;
  }, [windowStart]);
  useEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });
  const onScroll = () => {
    const el = box.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 140;
    stick.current = near;
    setShowJump(!near);
  };
  const earlier = () => {
    const el = box.current;
    if (!el) return;
    stick.current = false;
    pinnedHistory.current = true;
    anchor.current = el.scrollHeight - el.scrollTop;
    setStart(Math.max(0, windowStart - WINDOW));
  };
  const jump = () => {
    const el = box.current;
    if (!el) return;
    stick.current = true;
    setShowJump(false);
    el.scrollTo({ top: el.scrollHeight,
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  };

  // A trace "定位" request may point at a turn outside the mounted window. Expand first, then
  // scroll once that node exists; the flash makes the destination obvious without moving the page.
  const located = useRef(0);
  const flash = useRef<number | null>(null);
  useEffect(() => {
    if (!reveal || located.current === reveal.nonce) return;
    const index = items.findIndex((it) => it.kind === "entry" && it.e.id === reveal.id);
    if (index < 0) return;
    if (index < windowStart) {
      setStart(Math.max(0, index - 2));
      return;
    }
    const el = box.current;
    if (!el) return;
    const node = [...el.querySelectorAll<HTMLElement>("[data-chat-key]")].find((n) => n.dataset.chatKey === reveal.id);
    if (!node) return;
    located.current = reveal.nonce;
    stick.current = false;
    setShowJump(true);
    const top = el.scrollTop + node.getBoundingClientRect().top - el.getBoundingClientRect().top - Math.min(100, el.clientHeight * 0.2);
    el.scrollTo({ top, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    node.classList.remove("jump-hit");
    void node.offsetWidth;
    node.classList.add("jump-hit");
    if (flash.current) window.clearTimeout(flash.current);
    flash.current = window.setTimeout(() => node.classList.remove("jump-hit"), 1600);
  }, [reveal, items, windowStart]);
  useEffect(() => () => { if (flash.current) window.clearTimeout(flash.current); }, []);

  return (
    <div className="chat" ref={box} onScroll={onScroll} role="log" aria-label="对话记录"
      aria-live="polite" aria-relevant="additions">
      {path.length <= 1 && <div className="empty">发一条消息开始。助手可以读写这个项目里的文件、运行命令。</div>}
      {windowStart > 0 && (
        <button className="load-earlier" onClick={earlier}>
          显示更早的 {Math.min(windowStart, WINDOW)} 条
        </button>
      )}
      {visible.map((it, n) => {
        const index = windowStart + n;
        let content;
        if (it.kind === "steps") content = <MemoStepGroup calls={it.calls} runs={runs} last={index === items.length - 1} />;
        else if (it.kind === "artifact") content = <ArtifactCard sid={sid} a={it.a} />;
        else if (it.kind === "receipt") content = <ReceiptCard r={it.r} />;
        else if (it.kind === "say") {
          content = (
            <div className="msg assistant">
              {it.blocks.map((b, i) => b.type === "thinking" ? <Thinking key={i} text={b.text} />
                : b.type === "text" ? <MemoMarkdown key={i} text={b.text} cites={cites} /> : null)}
              {it.stop === "aborted" && <div className="note">（已中止）</div>}
              {it.stop === "length" && <div className="note">（输出达到长度上限）</div>}
            </div>
          );
        } else {
          const e = it.e;
          if (e.type === "user") {
            content = <MemoUserMessage sid={sid} entry={e} siblings={userSiblings.get(e.id) ?? []}
              busy={busy} onFork={onFork} />;
          } else if (e.type === "compaction") {
            content = (
              <details className="divider">
                <summary>上下文已压缩：约 {e.data.tokens_before} tokens 的较早对话被摘要替代（原文仍保存在会话里）</summary>
                <MemoMarkdown text={e.data.summary} />
              </details>
            );
          } else if (e.type === "branch_summary") {
            content = (
              <details className="divider">
                <summary>已总结离开的分支，助手会参考这段摘要</summary>
                <MemoMarkdown text={e.data.summary} />
              </details>
            );
          } else if (e.type === "prune") {
            content = <div className="divider">已从上下文中清理 {e.data.count} 条旧工具输出（全文已另存）</div>;
          } else if (e.data.kind === "delivery_recovery") {
            content = <DeliveryRecoveryCard sid={sid} entry={e} />;
          } else {
            content = (
              <details className="divider">
                <summary>{NOTICES[e.data.kind] ?? (e.data.kind.startsWith("skill:") ? `已加载技能 ${e.data.kind.slice(6)}` : "系统通知")}</summary>
                <div className="note">{e.data.text}</div>
              </details>
            );
          }
        }
        return (
          <motion.div key={itemKey(it)} className="chat-item" data-chat-key={it.kind === "entry" ? it.e.id : undefined}
            initial={fresh(itemKey(it), index) ? { opacity: 0, y: 8 } : false} animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}>
            {content}
          </motion.div>
        );
      })}
      {live && (live.thinking || live.text) && (
        <div className="msg assistant streaming" aria-live="off">
          {live.thinking && <Thinking text={live.thinking} open />}
          {live.text && <MemoMarkdown text={live.text} cites={cites} />}
          {live.text && <span className="stream-caret" aria-hidden />}
        </div>
      )}
      {live && !live.thinking && !live.text && runs.waiting.size === 0 && <div className="working" role="status">工作中…</div>}
      {live?.approvals.map((a) => <ApprovalCard key={a.id} approval={a} />)}
      {live?.questions.map((q) => <QuestionCard key={q.id} question={q} />)}
      {live && [...live.steer.map((t) => ["插话", t]), ...live.queue.map((t) => ["排队", t])].map(([kind, t], i) => (
        <div key={i} className="msg user pending"><span className="tag">{kind}</span>{t}</div>
      ))}
      {showJump && <button className="jump-latest" onClick={jump}>回到最新 <span aria-hidden>↓</span></button>}
      {todos && (busy || todos.some((t) => t.status !== "completed")) && <TodoPanel todos={todos} />}
    </div>
  );
}

function DeliveryRecoveryCard({ sid, entry }: { sid: string; entry: Entry }) {
  const [result, setResult] = useState<DeliveryRecovery | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inspect = async () => {
    setBusy(true);
    setError("");
    setResult(null);
    try { setResult(await api.deliveries(sid, entry.run_id!)); }
    catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  };
  return <div className="note" role="region" aria-label="已交付版本恢复">
    <p><strong>运行未完成，已有交付版本保留</strong></p>
    <p>原运行状态：{entry.data.status}{entry.data.error_type ? ` · ${entry.data.error_type}` : ""}。
      本地检查不调用模型，不重新生成文件。事实与版式仍需验收。</p>
    <button onClick={inspect} disabled={busy}>{busy ? "正在检查…" : "检查已交付版本"}</button>
    {error && <p role="alert">{error}</p>}
    {result && <ul>{result.files.map(({ entry_id, artifact: a, availability }) => <li key={entry_id}>
      {a.name} · 第 {a.version} 版 · {availability === "available" ? <a
        href={api.fileUrl(sid, a.sha256, a.name, true)}>下载已交付版本</a> :
        <span role="alert">副本{availability === "corrupt" ? "损坏" : availability === "missing" ? "缺失" : "不可读"}，未重新生成</span>}
    </li>)}</ul>}
  </div>;
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

const MemoUserMessage = memo(UserMessage);

function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (isValidElement(node)) return nodeText((node.props as { children?: ReactNode }).children);
  return "";
}

function legacyCopy(text: string) {
  const field = document.createElement("textarea");
  field.value = text;
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.appendChild(field);
  field.select();
  const ok = document.execCommand("copy");
  field.remove();
  if (!ok) throw new Error("copy failed");
}

function CodeBlock(props: ComponentProps<"pre"> & { node?: unknown }) {
  const { children, node, ...rest } = props;
  void node;
  const toast = useToast();
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);
  const child = Array.isArray(children) ? children[0] : children;
  const code = isValidElement(child) ? (child.props as { children?: ReactNode; className?: string }) : null;
  const text = (code ? nodeText(code.children) : nodeText(children)).replace(/\n$/, "");
  const language = /language-([\w-]+)/.exec(code?.className ?? "")?.[1];
  useEffect(() => () => { if (timer.current) window.clearTimeout(timer.current); }, []);
  const copy = async () => {
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
      else legacyCopy(text);
    } catch {
      try { legacyCopy(text); } catch {
        toast.error("复制失败，请手动选择代码");
        return;
      }
    }
    setCopied(true);
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 1600);
  };
  return (
    <div className="code-wrap">
      <div className="code-bar">
        <span>{language ?? "代码"}</span>
        <button type="button" className={copied ? "code-copy done" : "code-copy"} onClick={copy}
          aria-label={copied ? "已复制代码" : "复制代码"} title={copied ? "已复制" : "复制代码"}>
          {copied ? <Check size={12} /> : <Copy size={12} />}{copied ? "已复制" : "复制"}
        </button>
      </div>
      <pre {...rest}>{children}</pre>
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
        pre: (props) => <CodeBlock {...props} />,
      }}>
        {linked}
      </ReactMarkdown>
    </div>
  );
}

const MemoMarkdown = memo(Markdown);

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
    <div className="steps" aria-live="off">
      {calls.length > 1 && (
        <button type="button" className="steps-head" aria-expanded={open} onClick={() => setChoice(!open)}>
          <span className={`caret${open ? " open" : ""}`} />
          <span className="count">{calls.length} 个步骤</span>
          {!open && <span className="names">{counted.join(" · ")}</span>}
          <span className="right">
            {errors > 0 && <span className="danger-text">{errors} 个出错</span>}
            {active ? <span className="working-label">进行中</span> : total > 0 && <span>{duration(total)}</span>}
          </span>
        </button>
      )}
      <div className={`steps-body${open ? " open" : ""}`} aria-hidden={!open}>
        <div className="steps-body-inner">
          {steps.map((x, i) => x.type === "thinking"
            ? <ThoughtRow key={`t${i}`} text={x.text} index={i} />
            : <StepRow key={x.id} call={x} status={statuses[calls.indexOf(x)]} runs={runs} index={i} />)}
        </div>
      </div>
    </div>
  );
}

const MemoStepGroup = memo(StepGroup);

const ICONS: Record<string, string> = {
  read: "R", write: "W", edit: "E", bash: "$", grep: "G", glob: "*", web_fetch: "↗", web_search: "⌕", todo: "✓", ask_user: "?",
  create_document: "D", present: "⇪", feishu: "飞", recall: "↺", task_result: "◎",
  skill: "S", memory: "M", tool_search: "⌕",
};

function StepRow({ call, status, runs, index }: { call: ToolCall; status: Status; runs: Runs; index: number }) {
  const [open, setOpen] = useState(false);
  const result = runs.results[call.id];
  const content: Block[] = result?.data.content ?? [];
  const took = seconds(call, runs);
  const label = { done: took !== null ? duration(took) : "完成", error: "出错", running: "运行中", pending: "排队",
    waiting: call.name === "ask_user" ? "等你回答" : "等你确认" }[status];
  const live = status === "running" ? runs.outputs[call.id] : undefined;
  return (
    <div className={`step ${status}`} style={{ animationDelay: `${Math.min(index, 6) * 24}ms` }}>
      <div className="step-row" role="button" tabIndex={0} aria-expanded={open} title={open ? "收起" : "展开参数和输出"}
        onClick={() => setOpen(!open)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen(!open); } }}>
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
                b.type === "image" ? <img key={i} src={`data:${b.mime};base64,${b.data}`} alt={`${toolLabel(call.name)} 的输出图片`} />
                  : b.type === "text" ? <ToolOutput key={i} text={b.text} /> : null)
            : live && <ToolOutput text={live} live />}
        </div>
      )}
    </div>
  );
}

function ToolOutput({ text, live = false }: { text: string; live?: boolean }) {
  const [open, setOpen] = useState(false);
  const lines = text.split("\n");
  const long = text.length > 1800 || lines.length > 18;
  const preview = text.length > 1800 ? `${text.slice(0, 1800)}\n…`
    : lines.slice(0, 18).join("\n") + (lines.length > 18 ? "\n…" : "");
  const detail = lines.length > 1 ? `${lines.length} 行` : `${text.length} 字符`;
  return (
    <div className={`tool-output${open ? " open" : ""}${long ? " long" : ""}`}>
      <pre className="out">{long && !open ? preview : text}</pre>
      {long && !live && (
        <button type="button" className="out-toggle" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "收起输出" : `展开全部 · ${detail}`}
        </button>
      )}
      {live && long && <div className="out-live">输出仍在继续…</div>}
    </div>
  );
}

function ThoughtRow({ text, index }: { text: string; index: number }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="step thought" style={{ animationDelay: `${Math.min(index, 6) * 24}ms` }}>
      <div className="step-row" role="button" tabIndex={0} aria-expanded={open}
        onClick={() => setOpen(!open)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen(!open); } }}>
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
    <div className="approval" role="group" aria-label="需要确认的工具调用">
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
              本会话总是允许
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
      {preview?.account_id && <div className="kv"><span>账户 ID</span><code>{preview.account_id}</code></div>}
      <div className="kv"><span>操作</span><b>{FEISHU_ACTIONS[preview?.action ?? ""] ?? argv.slice(0, 2).join(" ")}</b></div>
      <div className="kv"><span>对象</span><code>{preview?.target ?? "—"}</code></div>
      {preview?.payload && (
        <div className="kv"><span>已冻结内容</span><pre>{String(preview.payload.content ?? preview.payload.name ?? "")}</pre></div>
      )}
      {preview?.files?.map((file) => (
        <div key={file.sha256} className="kv"><span>已冻结附件</span><pre>{file.name} · {file.bytes} 字节</pre></div>
      ))}
      {!preview?.payload && CONTENT_FLAGS.filter((f) => flag(f) !== undefined).map((f) => (
        <div key={f} className="kv"><span>{f.slice(2)}</span><pre>{flag(f)}</pre></div>
      ))}
      {preview?.error && <div className="note warn-line">预览失败：{preview.error}</div>}
      {preview?.dry_run !== undefined && (
        <details><summary>dry-run 预览（将发出的请求）</summary><pre>{JSON.stringify(preview.dry_run, null, 2)}</pre></details>
      )}
      {preview?.payload ? (
        <details><summary>完整审批载荷</summary><pre>{JSON.stringify({ payload: preview.payload,
          payload_hash: preview.payload_hash, operation_id: preview.operation_id, files: preview.files }, null, 2)}</pre></details>
      ) : <details><summary>原始请求</summary><pre>{JSON.stringify(args, null, 2)}</pre></details>}
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
    <div className="todo-panel" role="region" aria-label="当前计划">
      <div className="todo-title">计划 {done}/{todos.length}
        <span className="todo-progress" aria-hidden><i style={{ width: `${todos.length ? (done / todos.length) * 100 : 0}%` }} /></span>
      </div>
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
    <div className="approval question" role="group" aria-label="助手的问题">
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
