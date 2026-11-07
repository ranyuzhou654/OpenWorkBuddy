import type { Entry } from "./api";
import { pathTo } from "./session";

export interface UsageSummary {
  tokens: number;
  input: number;
  output: number;
  cacheRead: number;
  cacheWrite: number;
  cacheInput: number;
  calls: number;
  timedCalls: number;
  seconds: number;
  timedOutput: number;
  cacheRate: number | null;
  outputRate: number | null;
}

export interface SessionMetrics {
  total: UsageSummary;
  run: UsageSummary;
  turns: number;
  steps: number;
  runSteps: number;
  errors: number;
  runStartedAt: number | null;
}

export function usageOfEntries(list: Entry[]): UsageSummary {
  let input = 0, output = 0, cacheRead = 0, cacheWrite = 0, cacheInput = 0, timedOutput = 0;
  let calls = 0, timedCalls = 0, seconds = 0;
  let cacheKnown = false;
  for (const e of list) {
    if (e.type !== "assistant") continue;
    calls++;
    const usage = e.data.usage ?? {};
    input += Number(usage.input) || 0;
    output += Number(usage.output) || 0;
    cacheRead += Number(usage.cache_read) || 0;
    cacheWrite += Number(usage.cache_write) || 0;
    if ("cache_read" in usage || "cache_write" in usage) {
      cacheKnown = true;
      cacheInput += Number(usage.input) || 0;
    }
    const took = Number(e.data.seconds);
    if (Number.isFinite(took) && took > 0) {
      seconds += took;
      timedCalls++;
      timedOutput += Number(usage.output) || 0;
    }
  }
  return {
    tokens: input + output, input, output, cacheRead, cacheWrite, cacheInput, calls, timedCalls, seconds, timedOutput,
    // Weight the rate only across calls that actually reported cache fields.
    cacheRate: cacheKnown && cacheInput > 0 ? cacheRead / cacheInput : null,
    // Very short calls make tok/s noisy; only show a rate once there is enough time to mean something.
    outputRate: seconds >= 0.1 && timedOutput > 0 ? timedOutput / seconds : null,
  };
}

export function sessionMetrics(entries: Record<string, Entry>, leaf: string | null): SessionMetrics {
  const path = pathTo(entries, leaf);
  let start = -1;
  for (let i = path.length - 1; i >= 0; i--) {
    if (path[i].type === "user" && !path[i].data.synthetic) { start = i; break; }
  }
  const run = start >= 0 ? path.slice(start) : [];
  let errors = 0;
  for (const e of path) if (e.type === "tool_result" && e.data.is_error) errors++;
  return {
    total: usageOfEntries(path),
    run: usageOfEntries(run),
    turns: path.filter((e) => e.type === "user" && !e.data.synthetic).length,
    steps: path.filter((e) => e.type === "tool_started").length,
    runSteps: run.filter((e) => e.type === "tool_started").length,
    errors,
    runStartedAt: run[0]?.ts ?? null,
  };
}

export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 100_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

export function formatPercent(rate: number): string {
  const pct = rate * 100;
  return pct >= 99.5 ? "99%" : `${Math.round(pct)}%`;
}

export function formatRate(rate: number): string {
  return rate >= 10 ? String(Math.round(rate)) : rate.toFixed(1);
}

export function formatDuration(seconds: number): string {
  if (seconds < 1) return `${Math.max(0, Math.round(seconds * 1000))}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${String(Math.round(seconds % 60)).padStart(2, "0")}s`;
  return `${Math.floor(seconds / 3600)}h ${String(Math.round((seconds % 3600) / 60)).padStart(2, "0")}m`;
}
