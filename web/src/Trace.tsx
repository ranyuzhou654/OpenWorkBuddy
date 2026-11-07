import { memo, useEffect, useMemo, useRef, useState } from "react";
import type { Block, Entry } from "./api";
import { formatDuration, formatRate, formatTokens } from "./metrics";
import { pathTo, textOf } from "./session";

type ToolCall = Extract<Block, { type: "tool_call" }>;
type Usage = { input?: number; output?: number; cache_read?: number; cache_write?: number };

interface ModelStep {
  kind: "model";
  id: string;
  entry: Entry;
  usage: Usage;
  seconds: number | null;
  calls: ToolCall[];
  text: string;
  thinking: string;
}

interface ToolStep {
  kind: "tool";
  id: string;
  call: ToolCall;
  started: Entry;
  result?: Entry;
  error: boolean;
}

interface LiveStep {
  kind: "live";
  id: string;
  since: number;
}

type Step = ModelStep | ToolStep | LiveStep;

export interface TraceGroup {
  id: string;
  user: Entry;
  steps: Step[];
  status: "running" | "done" | "error" | "stopped";
  tokens: number;
  elapsed: number;
  tools: number;
}

const FOLLOW_KEY = "nwb.traceFollow";

function num(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function summary(call: ToolCall): string {
  const args = call.args ?? {};
  if (call.name === "bash") return String(args.command ?? "");
  if (call.name === "grep") return `${args.pattern ?? ""}${args.glob ? `  (${args.glob})` : ""}`;
  if (call.name === "glob") return String(args.pattern ?? "");
  if (call.name === "web_fetch" || call.name === "browser") return String(args.url ?? "");
  if (call.name === "web_search") return String(args.query ?? "");
  if (call.name === "read" || call.name === "write" || call.name === "edit") return String(args.path ?? "");
  if (call.name === "todo") {
    const list = Array.isArray(args.todos) ? args.todos : [];
    return `${list.filter((t) => t.status === "completed").length}/${list.length} 完成`;
  }
  return String(args.path ?? args.query ?? args.name ?? "");
}

function toolName(name: string): string {
  const mcp = name.match(/^mcp__(.+?)__(.+)$/);
  return mcp ? `${mcp[1]} › ${mcp[2]}` : name;
}

export function buildTrace(path: Entry[], busy: boolean): { groups: TraceGroup[]; signature: string } {
  const groups: TraceGroup[] = [];
  let current: TraceGroup | null = null;
  const args = new Map<string, ToolCall[]>();
  const open = new Map<string, ToolStep[]>();
  const now = Date.now() / 1000;

  for (const e of path) {
    if (e.type === "user" && !e.data.synthetic) {
      current = { id: e.id, user: e, steps: [], status: "done", tokens: 0, elapsed: 0, tools: 0 };
      groups.push(current);
      args.clear();
      open.clear();
      continue;
    }
    if (!current) continue;
    if (e.type === "assistant") {
      const blocks: Block[] = e.data.content ?? [];
      const calls = blocks.filter((b): b is ToolCall => b.type === "tool_call");
      const usage: Usage = e.data.usage ?? {};
      const seconds = Number(e.data.seconds);
      const step: ModelStep = {
        kind: "model", id: e.id, entry: e, usage,
        seconds: Number.isFinite(seconds) && seconds > 0 ? seconds : null,
        calls,
        text: blocks.filter((b) => b.type === "text").map((b) => b.text ?? "").join(""),
        thinking: blocks.filter((b) => b.type === "thinking").map((b) => b.text ?? "").join(""),
      };
      current.steps.push(step);
      current.tokens += num(usage.input) + num(usage.output);
      for (const call of calls) args.set(call.id, [...(args.get(call.id) ?? []), call]);
    } else if (e.type === "tool_started") {
      const callId = String(e.data.call_id ?? "");
      const call: ToolCall = args.get(callId)?.shift() ?? {
        type: "tool_call", id: callId, name: String(e.data.name ?? "tool"), args: e.data.args ?? {},
      };
      const step: ToolStep = { kind: "tool", id: e.id, call, started: e, error: false };
      current.steps.push(step);
      current.tools++;
      open.set(callId, [...(open.get(callId) ?? []), step]);
    } else if (e.type === "tool_result") {
      const callId = String(e.data.call_id ?? "");
      const step = open.get(callId)?.find((x) => !x.result);
      if (!step) continue;
      step.result = e;
      step.error = Boolean(e.data.is_error);
    }
  }

  if (busy && groups.length) {
    const last = groups[groups.length - 1];
    last.steps.push({ kind: "live", id: `live:${last.id}`, since: now });
  }

  for (const group of groups) {
    let lastTs = group.user.ts;
    let errors = 0;
    let incomplete = false;
    for (const step of group.steps) {
      if (step.kind === "model") {
        lastTs = Math.max(lastTs, step.entry.ts);
        if (step.entry.data.stop === "error") errors++;
      } else if (step.kind === "tool") {
        lastTs = Math.max(lastTs, step.started.ts, step.result?.ts ?? 0);
        if (step.error) errors++;
        else if (!step.result) incomplete = true;
      } else {
        lastTs = Math.max(lastTs, step.since);
      }
    }
    group.elapsed = Math.max(0, lastTs - group.user.ts);
    group.status = busy && group === groups[groups.length - 1] ? "running"
      : errors ? "error" : incomplete ? "stopped" : "done";
  }

  const signature = groups.map((g) => `${g.id}:${g.steps.length}:${g.status}`).join("|");
  return { groups, signature };
}

export const Trace = memo(function Trace({ entries, leaf, busy, onReveal }: {
  entries: Record<string, Entry>;
  leaf: string | null;
  busy: boolean;
  onReveal: (entryId: string) => void;
}) {
  const path = useMemo(() => pathTo(entries, leaf), [entries, leaf]);
  const { groups, signature } = useMemo(() => buildTrace(path, busy), [path, busy]);
  const [follow, setFollow] = useState(() => localStorage.getItem(FOLLOW_KEY) !== "0");
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [all, setAll] = useState(false);
  const [away, setAway] = useState(false);
  const [clock, setClock] = useState(() => Date.now() / 1000);
  const list = useRef<HTMLDivElement>(null);
  const first = all ? 0 : Math.max(0, groups.length - 40);
  const elapsedOf = (group: TraceGroup) => group.status === "running"
    ? Math.max(group.elapsed, clock - group.user.ts) : group.elapsed;
  const totalTokens = groups.reduce((sum, g) => sum + g.tokens, 0);
  const totalTools = groups.reduce((sum, g) => sum + g.tools, 0);
  const totalTime = groups.reduce((sum, g) => sum + elapsedOf(g), 0);

  useEffect(() => {
    localStorage.setItem(FOLLOW_KEY, follow ? "1" : "0");
  }, [follow]);
  useEffect(() => {
    if (!busy) return;
    setClock(Date.now() / 1000);
    const timer = window.setInterval(() => setClock(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  useEffect(() => {
    const el = list.current;
    if (!el || !follow) return;
    el.scrollTop = el.scrollHeight;
    setAway(false);
  }, [signature, follow]);

  const onScroll = () => {
    const el = list.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
    setAway(!near);
    if (!near && follow) setFollow(false);
    else if (near && !follow) setFollow(true);
  };

  const toLatest = () => {
    const el = list.current;
    if (!el) return;
    setFollow(true);
    el.scrollTo({ top: el.scrollHeight,
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  };

  return (
    <div className="trace">
      <div className="trace-head">
        <span className="trace-totals" title="按当前分支统计">
          {groups.length} 轮 · {totalTools} 步 · {formatTokens(totalTokens)} tokens
          {totalTime > 0 ? ` · ${formatDuration(totalTime)}` : ""}
        </span>
        <button type="button" className={`trace-follow${follow ? " on" : ""}`} aria-pressed={follow}
          title={follow ? "有新步骤时自动滚到底部" : "已暂停跟随；点此恢复"}
          onClick={() => { setFollow(!follow); setAway(false); }}>
          <span className="trace-follow-dot" aria-hidden="true" />自动跟随
        </button>
      </div>
      <div className="trace-list" ref={list} onScroll={onScroll} role="region" aria-label="本轮轨迹">
        {groups.length === 0 && <div className="note pad">还没有轨迹。发一条消息后，模型调用和工具步骤会按时间列在这里。</div>}
        {first > 0 && (
          <button type="button" className="trace-earlier" onClick={() => setAll(true)}>
            显示更早的 {first} 轮
          </button>
        )}
        {groups.slice(first).map((group, offset) => {
          const index = first + offset;
          const last = index === groups.length - 1;
          const active = open[group.id] ?? last;
          return (
            <section className={`trace-group ${group.status}`} key={group.id}>
              <div className="trace-group-head">
                <button type="button" className="trace-summary" aria-expanded={active}
                  onClick={() => setOpen({ ...open, [group.id]: !active })}>
                  <span className={`trace-dot${group.status === "running" ? " busy" : ""}`} aria-hidden="true" />
                  <span className="trace-title">第 {index + 1} 轮</span>
                  <span className="trace-snippet">{textOf(group.user.data.content).replace(/\s+/g, " ").slice(0, 80) || "…"}</span>
                  <span className="trace-meta">
                    {group.tools > 0 && `${group.tools} 步 · `}{formatTokens(group.tokens)} tok
                    {elapsedOf(group) >= 1 ? ` · ${formatDuration(elapsedOf(group))}` : ""}
                  </span>
                  <span className={`caret${active ? " open" : ""}`} aria-hidden="true" />
                </button>
                <button type="button" className="trace-jump" title="在对话里定位这条消息" onClick={() => onReveal(group.user.id)}>
                  定位
                </button>
              </div>
              {active && (
                <div className="trace-steps">
                  {group.steps.map((step) => step.kind === "model"
                    ? <ModelRow key={step.id} step={step} />
                    : step.kind === "tool"
                      ? <ToolRow key={step.id} step={step} busy={busy} />
                      : <LiveRow key={step.id} />)}
                  {group.steps.length === 0 && <div className="trace-empty">等待模型开始…</div>}
                </div>
              )}
            </section>
          );
        })}
      </div>
      {away && <button type="button" className="trace-latest" onClick={toLatest}>回到最新 <span aria-hidden>↓</span></button>}
    </div>
  );
});

function ModelRow({ step }: { step: ModelStep }) {
  const [open, setOpen] = useState(false);
  const model = String(step.entry.data.model ?? step.entry.data.model_ref ?? "");
  const rate = step.seconds && step.seconds >= 0.1 && num(step.usage.output) > 0 ? num(step.usage.output) / step.seconds : null;
  return (
    <div className={`trace-step model${open ? " open" : ""}`}>
      <button type="button" className="trace-row" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="trace-icon">AI</span>
        <span className="trace-name">模型</span>
        <span className="trace-detail">{model || "生成回复"}{step.calls.length ? ` · ${step.calls.length} 个调用` : ""}</span>
        <span className="trace-aside">
          {step.seconds !== null && <span>{formatDuration(step.seconds)}</span>}
          {rate !== null && <span title="模型生成速度">{formatRate(rate)} tok/s</span>}
        </span>
      </button>
      {open && (
        <div className="trace-body">
          <div className="trace-kv">
            <span>输入 {formatTokens(num(step.usage.input))}</span>
            <span>缓存 {formatTokens(num(step.usage.cache_read))}</span>
            <span>输出 {formatTokens(num(step.usage.output))}</span>
            {step.entry.data.stop && <span>结束 {String(step.entry.data.stop)}</span>}
          </div>
          {step.thinking && <pre className="trace-pre">{clip(step.thinking, 1600)}</pre>}
          {step.text && <pre className="trace-pre">{clip(step.text, 1600)}</pre>}
          {step.calls.length > 0 && (
            <div className="trace-kv">
              {step.calls.map((call) => <span key={call.id}>{toolName(call.name)} {summary(call)}</span>)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ToolRow({ step, busy }: { step: ToolStep; busy: boolean }) {
  const [open, setOpen] = useState(false);
  const status = step.error ? "error" : step.result ? "done" : busy ? "running" : "stopped";
  const took = step.result ? step.result.ts - step.started.ts : null;
  return (
    <div className={`trace-step tool ${status}${open ? " open" : ""}`}>
      <button type="button" className="trace-row" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="trace-icon tool-icon">{step.call.name.startsWith("mcp__") ? "◆" : "$"}</span>
        <span className="trace-name">{toolName(step.call.name)}</span>
        <span className="trace-detail">{summary(step.call) || "…"}</span>
        <span className="trace-aside">
          {took !== null ? <span>{formatDuration(took)}</span> : <span>{busy ? "运行中" : "未完成"}</span>}
          {step.error && <span className="trace-error">出错</span>}
        </span>
      </button>
      {open && (
        <div className="trace-body">
          <pre className="trace-pre">{clip(JSON.stringify(step.call.args ?? {}, null, 2), 2400)}</pre>
          {step.result
            ? <pre className="trace-pre">{clip(textOf(step.result.data.content ?? []), 3000)}</pre>
            : <div className="trace-empty">{busy ? "等待工具返回…" : "工具没有返回结果。"}</div>}
        </div>
      )}
    </div>
  );
}

function LiveRow() {
  return (
    <div className="trace-step live">
      <div className="trace-row">
        <span className="trace-icon"><span className="status-pulse" /></span>
        <span className="trace-name">正在生成</span>
        <span className="trace-detail">等待模型响应或继续调用工具</span>
        <span className="trace-aside"><span className="trace-live-label">进行中</span></span>
      </div>
    </div>
  );
}

function clip(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}\n…` : text;
}
