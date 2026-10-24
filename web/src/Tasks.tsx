import { CalendarClock, Play, Search as SearchIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, type Info, type Occurrence, type Project, type Schedule, type ScheduleBody, type SearchHit } from "./api";

// Scheduled tasks (M3). Each task has its own session: open it to see every run, its sources and its files.

const OUTCOME: Record<string, [string, string]> = {
  changed: ["有新结果", "ok"], unchanged: ["无变化", ""], failed: ["失败", "bad"], needs_user: ["需要你处理", "bad"],
  unknown: ["未报告结果", "warn"], cancelled: ["已取消", ""], skipped: ["已跳过", ""], missed: ["错过", "warn"],
};
const CATCH_UP = { once: "合并补跑一次", skip: "跳过", ask: "问我" } as const;
const DAYS = ["一", "二", "三", "四", "五", "六", "日"];

function when(ts: number): string {
  return new Date(ts * 1000).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function Outcome({ o }: { o: Occurrence["outcome"] | null }) {
  if (!o) return <span className="note">还没运行过</span>;
  const [label, cls] = OUTCOME[o] ?? [o, ""];
  return <span className={`outcome ${cls}`}>{label}</span>;
}

export function Tasks({ info, projects, onError }: { info: Info | null; projects: Project[]; onError: (m: string) => void }) {
  const [list, setList] = useState<Schedule[] | null>(null);
  const [creating, setCreating] = useState(false);
  const refresh = useCallback(() => { api.schedules().then(setList).catch((e) => onError(String(e))); }, [onError]);
  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 15000); // next run times and outcomes change on their own
    return () => window.clearInterval(t);
  }, [refresh]);
  const act = (p: Promise<unknown>) => p.then(refresh).catch((e) => onError(String(e)));

  return (
    <div className="page tasks">
      <div className="page-head">
        <h1><CalendarClock size={18} />定时任务</h1>
        <span className="grow" />
        {!creating && <button className="primary" onClick={() => setCreating(true)}>新建任务</button>}
      </div>
      <p className="note">
        到点时助手在任务自己的会话里工作：结果有变化、失败或需要你确认时发系统通知，没有变化不打扰。电脑休眠或关机时不会运行，醒来后按“错过时”的设置处理。
        需要服务一直在后台：在终端运行 <code>nwb service install</code> 可以让它随登录启动。
      </p>
      {creating && <TaskForm info={info} projects={projects} onCancel={() => setCreating(false)}
        onSave={(body) => api.createSchedule(body).then(() => { setCreating(false); refresh(); })} onError={onError} />}
      {list === null ? <div className="note">加载中…</div> : list.length === 0 && !creating
        ? <div className="empty-state">还没有定时任务。试试“每周调研更新”：给一个主题，助手每周搜索新进展，有变化才生成新一期简报。</div>
        : list.map((s) => (
          <div key={s.id} className={`card task${s.enabled ? "" : " off"}`}>
            <div className="card-head">
              <b>{s.name}</b>
              <span className="tag">{s.describe}</span>
              {s.template.template === "weekly_research" && <span className="tag">调研更新 · {s.template.topic}</span>}
              <span className="grow" />
              {s.running ? <span className="outcome warn">运行中</span> : <Outcome o={s.last_outcome} />}
              <label className="check"><input type="checkbox" checked={s.enabled}
                onChange={(e) => act(api.patchSchedule(s.id, { enabled: e.target.checked }))} />启用</label>
            </div>
            <div className="task-meta">
              <span>下次：{s.enabled && s.next_local ? s.next_local : "—"}</span>
              <span>错过时：{CATCH_UP[s.catch_up]}</span>
              <span>时区：{s.tz}</span>
            </div>
            {s.last_summary && <div className="task-summary">{s.last_summary}</div>}
            {s.recent.length > 0 && (
              <details className="task-history">
                <summary>最近 {s.recent.length} 次</summary>
                {s.recent.map((o) => (
                  <div key={o.planned_at} className="occ">
                    <span>{when(o.planned_at)}{o.missed > 1 ? `（含错过 ${o.missed - 1} 次）` : ""}</span>
                    {o.status === "submitted" ? <span className="outcome warn">进行中</span> : <Outcome o={o.outcome} />}
                    <span className="note">{o.summary}</span>
                  </div>
                ))}
              </details>
            )}
            <div className="row">
              <button disabled={s.running} onClick={() => act(api.runSchedule(s.id))}><Play size={13} />立即运行</button>
              <a className="button" href={`#/s/${s.session_id}`}>打开任务会话</a>
              <select value={s.catch_up} title="错过执行时间时"
                onChange={(e) => act(api.patchSchedule(s.id, { catch_up: e.target.value as Schedule["catch_up"] }))}>
                {Object.entries(CATCH_UP).map(([k, v]) => <option key={k} value={k}>错过时：{v}</option>)}
              </select>
              <button className="danger" onClick={() => confirm(`删除任务「${s.name}」？它的会话和产物会保留。`) &&
                act(api.deleteSchedule(s.id))}>删除</button>
            </div>
          </div>
        ))}
    </div>
  );
}

function TaskForm({ info, projects, onSave, onCancel, onError }: {
  info: Info | null; projects: Project[]; onSave: (b: ScheduleBody) => Promise<unknown>; onCancel: () => void;
  onError: (m: string) => void;
}) {
  const [template, setTemplate] = useState(true);
  const [name, setName] = useState("每周调研更新");
  const [topic, setTopic] = useState("");
  const [file, setFile] = useState("调研简报.md");
  const [extra, setExtra] = useState("");
  const [prompt, setPrompt] = useState("");
  const [project, setProject] = useState<string>("");
  const [kind, setKind] = useState<Schedule["kind"]>("weekly");
  const [at, setAt] = useState("09:00");
  const [days, setDays] = useState<number[]>([1]);
  const [day, setDay] = useState(new Date().toISOString().slice(0, 10));
  const [catchUp, setCatchUp] = useState<Schedule["catch_up"]>("once");
  const [saving, setSaving] = useState(false);
  const ready = name.trim() && (template ? topic.trim() && file.trim() : prompt.trim()) && (kind !== "weekly" || days.length);
  return (
    <div className="card form">
      <label>类型
        <select value={template ? "research" : "custom"} onChange={(e) => {
          const t = e.target.value === "research";
          setTemplate(t);
          setName(t ? "每周调研更新" : "");
        }}>
          <option value="research">每周调研更新（模板）</option>
          <option value="custom">自定义指令</option>
        </select>
      </label>
      <label>名称<input value={name} onChange={(e) => setName(e.target.value)} /></label>
      {template ? (
        <>
          <label>调研主题<input value={topic} onChange={(e) => setTopic(e.target.value)} placeholder="如：本地 Agent 框架的新进展" /></label>
          <label>简报文件<input value={file} onChange={(e) => setFile(e.target.value)} placeholder="调研简报.md / .docx / .pdf" /></label>
          <label>补充要求<input value={extra} onChange={(e) => setExtra(e.target.value)} placeholder="可选，如：重点关注中文资料" /></label>
        </>
      ) : (
        <label>指令<textarea rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)}
          placeholder="到点时发给助手的任务说明；最后请它用 task_result 报告结果" /></label>
      )}
      <label>工作目录
        <select value={project} onChange={(e) => setProject(e.target.value)}>
          <option value="">无项目（对话文件夹）</option>
          {projects.filter((p) => !p.scratch && p.trusted).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </label>
      <label>重复
        <select value={kind} onChange={(e) => setKind(e.target.value as Schedule["kind"])}>
          <option value="weekly">每周</option><option value="daily">每天</option><option value="once">仅一次</option>
        </select>
      </label>
      {kind === "weekly" && (
        <label>星期
          <span className="days">{DAYS.map((d, i) => (
            <button key={d} className={days.includes(i + 1) ? "on" : ""}
              onClick={() => setDays(days.includes(i + 1) ? days.filter((x) => x !== i + 1) : [...days, i + 1].sort())}>{d}</button>
          ))}</span>
        </label>
      )}
      {kind === "once" && <label>日期<input type="date" value={day} onChange={(e) => setDay(e.target.value)} /></label>}
      <label>时间<input type="time" value={at} onChange={(e) => setAt(e.target.value)} /></label>
      <label>错过时
        <select value={catchUp} onChange={(e) => setCatchUp(e.target.value as Schedule["catch_up"])}>
          {Object.entries(CATCH_UP).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      {template && <div className="note">调研更新需要“联网搜索”密钥（设置 → 模型与密钥）。助手不会对外发送任何内容；默认模型：{info?.default_model}。</div>}
      <div className="row">
        <button onClick={onCancel}>取消</button>
        <button className="primary" disabled={!ready || saving} onClick={async () => {
          setSaving(true);
          try {
            await onSave({ name, project_id: project || null, kind, at, weekdays: days, day, catch_up: catchUp,
              ...(template ? { template: { kind: "weekly_research", topic, file, extra } } : { prompt }) });
          } catch (e) {
            onError(String(e));
          } finally {
            setSaving(false);
          }
        }}>保存</button>
      </div>
    </div>
  );
}

const KIND: Record<SearchHit["kind"], string> = { message: "对话", source: "来源", file: "交付的文件", upload: "附件" };

// Full-text search over the assistant's own records: every session, the sources cited, files delivered and attached.
export function SearchPage({ onError }: { onError: (m: string) => void }) {
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("all");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const run = () => q.trim() && api.searchRecords(q.trim(), kind).then(setHits).catch((e) => onError(String(e)));
  return (
    <div className="page search">
      <div className="page-head"><h1><SearchIcon size={18} />搜索记录</h1></div>
      <div className="row search-box">
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索以前的对话、来源和文件"
          onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && run()} />
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="all">全部</option><option value="messages">对话</option><option value="sources">来源</option>
          <option value="files">文件</option>
        </select>
        <button className="primary" onClick={run} disabled={!q.trim()}>搜索</button>
      </div>
      <p className="note">只搜索助手自己的记录（会话、引用过的来源、交付的文件和附件），不扫描你的其他文件。删除会话后，它的内容也从搜索中移除。</p>
      {hits?.length === 0 && <div className="note">没有找到。</div>}
      {hits?.map((h, i) => (
        <a key={i} className="hit" href={h.kind === "source" ? h.ref : `#/s/${h.session_id}`}
          target={h.kind === "source" ? "_blank" : undefined} rel="noreferrer">
          <div className="hit-head"><span className="tag">{KIND[h.kind]}</span><b>{h.title || "（未命名会话）"}</b>
            <span className="note">{new Date(h.at * 1000).toLocaleDateString("zh-CN")}</span></div>
          <div className="hit-snippet">{h.snippet}</div>
        </a>
      ))}
    </div>
  );
}
