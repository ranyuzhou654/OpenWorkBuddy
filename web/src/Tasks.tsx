import { CalendarClock, Play, Search as SearchIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api, type Info, type Occurrence, type Project, type Schedule, type ScheduleBody, type SearchHit } from "./api";
import { Select } from "./Select";
import { Dialog, PageHeader, SkeletonList, useConfirm } from "./ui";

// Scheduled tasks (M3). Each task has its own session: open it to see every run, its sources and its files.

const OUTCOME: Record<string, [string, string]> = {
  changed: ["有新结果", "ok"], unchanged: ["无变化", ""], failed: ["失败", "bad"], needs_user: ["需要你处理", "bad"],
  unknown: ["结果待核验", "warn"], cancelled: ["已取消", ""], skipped: ["已跳过", ""], missed: ["错过", "warn"],
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
  const confirm = useConfirm();
  const refresh = useCallback(() => { api.schedules().then(setList).catch((e) => onError(String(e))); }, [onError]);
  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 15000); // next run times and outcomes change on their own
    return () => window.clearInterval(t);
  }, [refresh]);
  const act = (p: Promise<unknown>) => p.then(refresh).catch((e) => onError(String(e)));

  return (
    <div className="page tasks">
      <PageHeader icon={<CalendarClock size={18} />} eyebrow="自动化" title="定时任务"
        description="到点时助手在任务自己的会话里工作：有变化、失败或需要你确认时通知，没有变化不打扰。电脑休眠或关机时不会运行，醒来后按“错过时”处理。"
        actions={!creating && <button className="primary" onClick={() => setCreating(true)}>新建任务</button>} />
      <p className="note">
        需要服务一直在后台：在终端运行 <code>nwb service install</code> 可以让它随登录启动。
      </p>
      {creating && <TaskForm info={info} projects={projects} onCancel={() => setCreating(false)}
        onSave={(body) => api.createSchedule(body).then(() => { setCreating(false); refresh(); })} onError={onError} />}
      {list === null ? <SkeletonList count={3} /> : list.length === 0 && !creating
        ? <div className="empty-state">还没有定时任务。试试“每周调研更新”：给一个主题，助手每周搜索新进展，有变化才生成新一期简报。</div>
        : list.map((s) => (
          <div key={s.id} className={`card task${s.enabled ? "" : " off"}`}>
            <div className="card-head">
              <b>{s.name}</b>
              <span className="tag">{s.describe}</span>
              {s.template.template === "weekly_research" && <span className="tag">调研更新 · {s.template.topic}</span>}
              <span className="grow" />
              {s.waiting ? <span className="outcome warn">等待处理</span>
                : s.running ? <span className="outcome warn">运行中</span>
                : s.pending ? <span className="outcome warn">等待投递</span> : <Outcome o={s.last_outcome} />}
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
              <button disabled={s.running || s.pending}
                onClick={() => act(api.runSchedule(s.id))}><Play size={13} />立即运行</button>
              <a className="button" href={`#/s/${s.session_id}`}>打开任务会话</a>
              <Select className="compact" value={s.catch_up} label="错过执行时间时"
                title="错过执行时间时"
                options={Object.entries(CATCH_UP).map(([value, label]) => ({ value, label: `错过时：${label}` }))}
                onChange={(catch_up) => act(api.patchSchedule(s.id, { catch_up: catch_up as Schedule["catch_up"] }))} />
              <button className="danger" onClick={() => confirm({
                title: "删除定时任务", message: `删除任务「${s.name}」？它的会话和产物会保留。`,
                confirmLabel: "删除", danger: true,
              }).then((ok) => {
                if (ok) void act(api.deleteSchedule(s.id));
              })}>删除</button>
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
        <Select value={template ? "research" : "custom"} label="类型"
          options={[
            { value: "research", label: "每周调研更新（模板）" },
            { value: "custom", label: "自定义指令" },
          ]}
          onChange={(next) => {
            const research = next === "research";
            setTemplate(research);
            setName(research ? "每周调研更新" : "");
          }} />
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
        <Select value={project} label="工作目录"
          options={[
            { value: "", label: "无项目（对话文件夹）" },
            ...projects.filter((p) => !p.scratch && p.trusted).map((p) => ({ value: p.id, label: p.name })),
          ]}
          onChange={setProject} />
      </label>
      <label>重复
        <Select value={kind} label="重复"
          options={[
            { value: "weekly", label: "每周" }, { value: "daily", label: "每天" }, { value: "once", label: "仅一次" },
          ]}
          onChange={(next) => setKind(next as Schedule["kind"])} />
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
        <Select value={catchUp} label="错过时"
          options={Object.entries(CATCH_UP).map(([value, label]) => ({ value, label }))}
          onChange={(next) => setCatchUp(next as Schedule["catch_up"])} />
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
export function SearchPage({ projects, onError }: { projects: Project[]; onError: (m: string) => void }) {
  const [q, setQ] = useState("");
  const [kind, setKind] = useState("all");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [project, setProject] = useState("");
  const [excerpt, setExcerpt] = useState<string | null>(null);
  const run = () => q.trim() && api.searchRecords(q.trim(), kind, project).then(setHits).catch((e) => onError(String(e)));
  return (
    <div className="page search">
      <PageHeader icon={<SearchIcon size={18} />} eyebrow="记录" title="搜索记录"
        description="只搜索助手自己的记录（会话、引用过的来源、交付的文件和附件），不扫描你的其他文件。" />
      <div className="row search-box">
        <input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索以前的对话、来源和文件"
          onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && run()} />
        <Select value={kind} label="搜索类型"
          options={[
            { value: "all", label: "全部" }, { value: "messages", label: "对话" },
            { value: "sources", label: "来源" }, { value: "files", label: "文件" },
          ]}
          onChange={setKind} />
        <button className="primary" onClick={run} disabled={!q.trim()}>搜索</button>
        <Select value={project} label="全局搜索范围"
          options={[
            { value: "", label: "搜索范围：全部未排除项目" },
            ...projects.filter((p) => !p.no_index).map((p) => ({ value: p.id, label: p.name })),
          ]}
          onChange={(next) => { setProject(next); setHits(null); }} />
      </div>
      {hits && <div className="result-count">{hits.length} 条结果</div>}
      {hits?.length === 0 && <div className="note">没有找到。</div>}
      {hits?.map((h, i) => (
        <div key={i} className="hit">
          <a href={`#/s/${h.session_id}`}>
          <div className="hit-head"><span className="tag">{KIND[h.kind]}</span><b>{h.title || "（未命名会话）"}</b>
            <span className="note">{new Date(h.at * 1000).toLocaleDateString("zh-CN")}</span></div>
          <div className="hit-snippet">{h.snippet}</div></a>
          <button onClick={() => api.readRecord(h.record_id).then((r) => setExcerpt(`${r.body}\n（索引共 ${r.total_chars} 字符）`))
            .catch((e) => onError(String(e)))}>读取命中片段</button>
        </div>
      ))}
      <Dialog open={excerpt !== null} onClose={() => setExcerpt(null)} title="命中片段" size="lg"
        footer={<button onClick={() => setExcerpt(null)}>关闭</button>}>
        <pre className="excerpt">{excerpt}</pre>
      </Dialog>
    </div>
  );
}
