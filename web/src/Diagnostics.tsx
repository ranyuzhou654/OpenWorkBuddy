import { useEffect, useState } from "react";
import { api, type Diagnostics as View, type SessionInfo } from "./api";
import { Dialog, Skeleton } from "./ui";

export function Diagnostics({ sid, session, busy, open, onClose, onError }: {
  sid: string; session: SessionInfo; busy: boolean; open: boolean; onClose: () => void;
  onError: (message: string) => void;
}) {
  const [view, setView] = useState<View | null>(null);
  const [limits, setLimits] = useState<Record<string, number>>({});
  useEffect(() => {
    if (!open) return;
    api.diagnostics(sid).then((v) => {
      setView(v);
      setLimits({ ...v.budget_defaults, ...session.settings.budget });
    }).catch((e) => onError(String(e)));
  }, [open, sid, busy, session.settings.budget, onError]);

  return (
    <Dialog open={open} onClose={onClose} title="运行诊断与预算" size="lg"
      footer={<button onClick={onClose}>关闭</button>}>
      {!view ? <Skeleton lines={5} /> : (
        <div className="diagnostics">
          <p className="note">{view.recall_scope} · 索引待更新 {view.index.pending} 个会话</p>
          <p className="note">调用数包含失败请求与摘要；墙钟时间包含审批等待及重启停机。套餐费用未知，token 不换算为节省金额。</p>
          <div className="diag-limits">
            {Object.entries({ model_calls: "模型请求上限", tool_calls: "工具调用上限", wall_seconds: "墙钟秒数上限" }).map(([key, label]) => (
              <label key={key} className="field-row"><span>{label}</span><input type="number" min={1} max={100000} value={limits[key] ?? ""}
                onChange={(e) => setLimits({ ...limits, [key]: Number(e.target.value) })} /></label>))}
          </div>
          <div className="dialog-actions inline">
            <button disabled={busy || !["model_calls", "tool_calls", "wall_seconds"].every((k) => limits[k] >= 1)}
              onClick={() => api.patch(sid, { budget: Object.fromEntries(["model_calls", "tool_calls", "wall_seconds"].map((k) => [k, limits[k]])) })
                .then(onClose).catch((e) => onError(String(e)))}>保存（下次新运行生效）</button>
          </div>
          {view.index.recent_errors.map((e, i) => <p key={i} className="note">索引错误：{e.session_id} · {e.error}</p>)}
          {view.runs.map((run) => <section key={run.run_id} className="diag-run">
            <h4>{run.run_id} · {run.status}</h4>
            <p>{run.model} · 模型 {run.model_calls} / {run.limits?.model_calls ?? "历史未知"} · 工具 {run.tool_calls} / {run.limits?.tool_calls ?? "历史未知"}</p>
            <p className="note">输入 {run.usage.input ?? 0} / 输出 {run.usage.output ?? 0} tokens · 工具错误 {run.tool_errors} · 未知请求 {run.unknown_calls} · 费用未知</p>
            {run.failures.map((f, i) => <p key={i} className="note">{Object.values(f).filter(Boolean).join(" · ")}</p>)}
          </section>)}
        </div>
      )}
    </Dialog>
  );
}
