import { useEffect, useMemo, useState } from "react";
import type { ContextUsage, Entry } from "./api";
import { formatDuration, formatPercent, formatRate, formatTokens, sessionMetrics } from "./metrics";

export function SessionStatus({ entries, leaf, context, busy }: {
  entries: Record<string, Entry>;
  leaf: string | null;
  context: ContextUsage | null;
  busy: boolean;
}) {
  const metrics = useMemo(() => sessionMetrics(entries, leaf), [entries, leaf]);
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!busy) return;
    setNow(Date.now() / 1000);
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [busy]);
  const total = metrics.total;
  const contextPct = context && context.budget > 0 ? Math.min(1, context.tokens / context.budget) : null;
  const rate = busy ? metrics.run.outputRate : null;
  const elapsed = busy && metrics.runStartedAt ? Math.max(0, now - metrics.runStartedAt) : 0;
  return (
    <div className={`session-status${busy ? " busy" : ""}`} aria-label="会话用量">
      {context && contextPct !== null && (
        <span className={`status-context${contextPct > 0.8 ? " high" : ""}`}
          title={`下一次请求约 ${context.tokens} tokens，上限 ${context.budget}（超出时先清理旧工具输出，再压缩）`}>
          <span className="status-bar" aria-hidden="true"><i><b style={{ width: `${contextPct * 100}%` }} /></i></span>
          <span className="status-value">{formatPercent(contextPct)}</span>
          <span className="status-detail">{formatTokens(context.tokens)}/{formatTokens(context.budget)}</span>
        </span>
      )}
      <span className="status-item" title={`输入 ${total.input} + 输出 ${total.output} tokens（含缓存命中）`}>
        {formatTokens(total.tokens)} tokens
      </span>
      <span className="status-item"
        title={total.cacheRate === null ? "模型供应商未返回缓存数据" : `缓存读取 ${total.cacheRead} / 已报告缓存的输入 ${total.cacheInput} tokens`}>
        缓存 {total.cacheRate === null ? "—" : formatPercent(total.cacheRate)}
      </span>
      <span className="status-item status-run">
        {busy
          ? `本轮 ${metrics.runSteps} 步${elapsed >= 1 ? ` · ${formatDuration(elapsed)}` : ""}`
          : `${metrics.turns} 轮 · ${metrics.steps} 步`}
      </span>
      {metrics.errors > 0 && <span className="status-item status-error" title="当前分支里失败的工具调用">{metrics.errors} 个错误</span>}
      <span className="status-spacer" />
      {busy && rate !== null && (
        <span className="status-live" title="本轮已回报的模型生成速度">
          <span className="status-pulse" aria-hidden="true" />{formatRate(rate)} tok/s
        </span>
      )}
    </div>
  );
}
