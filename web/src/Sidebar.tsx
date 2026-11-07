import { motion } from "motion/react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { CalendarClock, ChevronsUpDown, CircleHelp, Ellipsis, Gauge, PanelLeftClose, PanelLeftOpen, Plus, Search, Settings, SquarePen } from "lucide-react";
import { api, ApiError, type Info, type Project, type SessionInfo } from "./api";
import { Dialog, PopoverPanel, useConfirm, usePopover } from "./ui";

// Adding a project: a native folder dialog (served by the backend, since a browser cannot reveal a real
// path), then a question about trust. Without a dialog on the server's machine, the path is typed in.
export function useAddProject(projects: Project[], onChanged: () => void, onError: (m: string) => void) {
  const [dialog, setDialog] = useState<ReactNode>(null);
  const ask = <T,>(render: (done: (v: T) => void) => ReactNode) =>
    new Promise<T>((resolve) => setDialog(render((v) => { setDialog(null); resolve(v); })));

  const add = async (): Promise<Project | null> => {
    let path: string | null;
    try {
      path = (await api.pickFolder()).path;
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 501)) {
        onError(String(e));
        return null;
      }
      path = await ask<string | null>((done) => <PathDialog onDone={done} />);
    }
    if (!path) return null;
    const known = projects.find((p) => p.root_path === path);
    if (known) return known;
    const trusted = await ask<boolean | null>((done) => <TrustDialog path={path} onAnswer={done} />);
    if (trusted === null) return null;
    try {
      const p = await api.addProject(path, trusted);
      onChanged();
      return p;
    } catch (e) {
      onError(String(e));
      return null;
    }
  };
  return { add, dialog };
}

function TrustDialog({ path, onAnswer }: { path: string; onAnswer: (trusted: boolean | null) => void }) {
  return (
    <Dialog open onClose={() => onAnswer(null)} title="信任这个文件夹吗？" size="md"
      footer={<>
        <button onClick={() => onAnswer(null)}>取消</button>
        <button onClick={() => onAnswer(false)}>不信任</button>
        <button className="primary" data-autofocus onClick={() => onAnswer(true)}>信任</button>
      </>}>
      <code className="dialog-path">{path}</code>
      <p><b>信任</b>：新会话默认“工作区”档，项目内读写、在沙箱里运行命令都不再询问；加载项目自带的技能（.agents/skills）。</p>
      <p><b>不信任</b>：新会话默认“只读”档，每次写入和命令都要你确认；不加载项目技能。适合刚下载、来历不明的代码。</p>
      <p className="note">两种情况都会读取项目说明（AGENTS.md）。之后可以在项目菜单里更改。</p>
    </Dialog>
  );
}

function PathDialog({ onDone }: { onDone: (path: string | null) => void }) {
  const [path, setPath] = useState("");
  const submit = () => path.trim() && onDone(path.trim());
  return (
    <Dialog open onClose={() => onDone(null)} title="添加项目" size="sm"
      description="这台机器上没有可用的文件夹选择框，请输入路径。"
      footer={<>
        <button onClick={() => onDone(null)}>取消</button>
        <button className="primary" disabled={!path.trim()} onClick={submit}>下一步</button>
      </>}>
      <input className="full" data-autofocus placeholder="项目目录的绝对路径" value={path} onChange={(e) => setPath(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && submit()} />
    </Dialog>
  );
}

// A "⋯" button with a small menu; any click elsewhere closes it.
function Menu({ items }: { items: [label: string, action: () => void, danger?: boolean][] }) {
  const pop = usePopover();
  return (
    <span className="menu popover-wrap" ref={pop.wrapRef}>
      <button ref={pop.triggerRef} className="dots" title="更多" aria-label="更多" aria-haspopup="menu" aria-expanded={pop.open}
        onClick={(e) => { e.stopPropagation(); pop.toggle(); }}>
        <Ellipsis size={15} />
      </button>
      <PopoverPanel open={pop.open} className="menu-pop">
        {items.map(([label, action, danger]) => (
          <button key={label} role="menuitem" className={danger ? "danger-text" : ""}
            onClick={(e) => { e.stopPropagation(); pop.close(); action(); }}>{label}</button>
        ))}
      </PopoverPanel>
    </span>
  );
}

// Enter or leaving the field saves; Escape cancels.
function Rename({ value, onDone }: { value: string; onDone: (v: string | null) => void }) {
  const [v, setV] = useState(value);
  const done = useRef(false);
  const finish = (result: string | null) => {
    if (!done.current) {
      done.current = true;
      onDone(result);
    }
  };
  return (
    <input className="rename" value={v} autoFocus onFocus={(e) => e.target.select()} onChange={(e) => setV(e.target.value)}
      onBlur={() => finish(v)}
      onKeyDown={(e) => {
        if (e.key === "Enter" && !e.nativeEvent.isComposing) finish(v);
        if (e.key === "Escape") finish(null);
      }} />
  );
}

const title = (s: SessionInfo) => s.title ?? "新会话";

// Projects folded in the sidebar, remembered per browser.
const railPref = {
  get(): boolean {
    try {
      return localStorage.getItem("nwb.sidebar") === "rail";
    } catch {
      return false;
    }
  },
  set(on: boolean): void {
    try {
      localStorage.setItem("nwb.sidebar", on ? "rail" : "wide");
    } catch {
      // storage unavailable: the layout lasts until reload
    }
  },
};

const folded = {
  get(): string[] {
    try {
      return JSON.parse(localStorage.getItem("nwb.folded") ?? "[]");
    } catch {
      return [];
    }
  },
  set(ids: string[]): void {
    try {
      localStorage.setItem("nwb.folded", JSON.stringify(ids));
    } catch {
      // storage unavailable: folding lasts until reload
    }
  },
};

const viewedRuns = {
  get(): Record<string, string> | null {
    try {
      const raw = localStorage.getItem("nwb.viewed_runs");
      if (raw === null) return null;
      const value = JSON.parse(raw);
      return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    } catch {
      return null;
    }
  },
  set(runs: Record<string, string>): void {
    try {
      localStorage.setItem("nwb.viewed_runs", JSON.stringify(runs));
    } catch {
      // storage unavailable: viewed markers last until reload
    }
  },
};

const DONE_RUNS = new Set(["ok", "nothing", "limit"]);
const STOPPED_RUNS = new Set(["error", "interrupted", "blocked"]);

function sessionGroups(sessions: SessionInfo[]): [string, SessionInfo[]][] {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  const buckets = new Map<string, SessionInfo[]>([["今天", []], ["昨天", []], ["更早", []]]);
  for (const session of [...sessions].sort((a, b) => b.updated_at - a.updated_at)) {
    const key = session.updated_at >= today ? "今天" : session.updated_at >= today - 86400 ? "昨天" : "更早";
    buckets.get(key)!.push(session);
  }
  return [...buckets].filter(([, list]) => list.length > 0);
}

export function Sidebar({ info, projects, current, running, onAddProject, onChanged, onError, open, onNavigate }: {
  info: Info | null; projects: Project[]; current: string | null; running: string | null; onAddProject: () => void;
  onChanged: () => void; onError: (m: string) => void; open: boolean; onNavigate?: () => void;
}) {
  const confirm = useConfirm();
  const [rail, setRail] = useState(railPref.get);
  const [editing, setEditing] = useState<string | null>(null); // "s:<id>" or "p:<id>"
  const [closed, setClosed] = useState<string[]>(folded.get);
  const [viewed, setViewed] = useState<Record<string, string> | null>(viewedRuns.get);
  useEffect(() => {
    if (viewed !== null || projects.length === 0) return;
    // First run of this version: treat existing history as already seen, so only new completions light up.
    const next: Record<string, string> = {};
    for (const p of projects) for (const s of p.sessions) if (s.last_run) next[s.id] = s.last_run.id;
    viewedRuns.set(next);
    setViewed(next);
  }, [projects, viewed]);
  useEffect(() => {
    if (!current || viewed === null) return;
    const session = projects.flatMap((p) => p.sessions).find((s) => s.id === current);
    const run = session?.last_run;
    if (!run || run.status === "running" || viewed[current] === run.id) return;
    const next = { ...viewed, [current]: run.id };
    viewedRuns.set(next);
    setViewed(next);
  }, [current, projects, viewed]);
  const toggle = (pid: string) => {
    const next = closed.includes(pid) ? closed.filter((x) => x !== pid) : [...closed, pid];
    setClosed(next);
    folded.set(next);
  };
  const act = (p: Promise<unknown>, leave = false) =>
    p.then(() => { onChanged(); if (leave) location.hash = "#/"; }).catch((e) => onError(String(e)));
  const openSession = (sid: string) => {
    onNavigate?.();
    location.hash = `#/s/${sid}`;
  };
  const toggleRail = () => {
    setRail((value) => {
      railPref.set(!value);
      return !value;
    });
  };
  const onTasks = location.hash.startsWith("#/tasks");
  const onSearch = location.hash.startsWith("#/search");

  const sessionTone = (s: SessionInfo): "running" | "done" | "error" | null => {
    const run = s.last_run;
    if (s.id === running || run?.status === "running") return "running";
    if (run && STOPPED_RUNS.has(run.status)) return "error";
    if (run && DONE_RUNS.has(run.status) && viewed !== null && s.id !== current
      && viewed[s.id] !== run.id) return "done";
    return null;
  };
  const toneLabel = { running: "正在工作", done: "已完成，未查看", error: "运行出错" } as const;
  const sessionRow = (s: SessionInfo) => {
    const tone = sessionTone(s);
    return editing === `s:${s.id}` ? (
      <Rename key={s.id} value={title(s)} onDone={(v) => {
        setEditing(null);
        if (v !== null && v !== title(s)) act(api.patch(s.id, { title: v }));
      }} />
    ) : (
      <div key={s.id} className={`session${s.id === current ? " on" : ""}${tone ? ` ${tone}` : ""}`}>
        {s.id === current && (
          <motion.span layoutId="sidebar-active" className="session-active"
            transition={{ type: "spring", stiffness: 420, damping: 34 }} />
        )}
        {tone && <span className={`dot ${tone}`} role="img" aria-label={toneLabel[tone]} title={toneLabel[tone]} />}
        <button onClick={() => openSession(s.id)} title={title(s)} aria-current={s.id === current ? "page" : undefined}>{title(s)}</button>
        <Menu items={[["重命名", () => setEditing(`s:${s.id}`)],
          ["归档", () => act(api.patch(s.id, { archived: true }), s.id === current)]]} />
      </div>
    );
  };

  const chats = projects.filter((p) => p.scratch).flatMap((p) => p.sessions.filter((s) => !s.archived_at));
  const archived = projects.flatMap((p) => p.sessions.filter((s) => s.archived_at).map((s) => ({ s, p })));
  const visibleProjects = projects.filter((p) => !p.scratch);
  return (
    <nav id="sidebar" className={`sidebar${open ? " open" : ""}${rail ? " rail" : ""}`} aria-label="主导航">
      <div className="brand-row">
        <div className="brand"><span className="brand-text">nano-workbuddy</span></div>
        <button type="button" className="rail-toggle" onClick={toggleRail} aria-controls="sidebar" aria-expanded={!rail}
          aria-label={rail ? "展开侧栏" : "收起侧栏"} title={rail ? "展开侧栏" : "收起侧栏"}>
          {rail ? <PanelLeftOpen size={15} strokeWidth={1.75} /> : <PanelLeftClose size={15} strokeWidth={1.75} />}
        </button>
      </div>
      <button className={`new-chat${current === null && !/^#\/(settings|tasks|search)/.test(location.hash) ? " on" : ""}`}
        aria-label="新对话" title="新对话" aria-current={current === null && !/^#\/(settings|tasks|search)/.test(location.hash) ? "page" : undefined}
        onClick={() => { onNavigate?.(); location.hash = "#/"; }}>
        <SquarePen size={15} strokeWidth={1.75} /><span className="nav-text">新对话</span>
      </button>
      <button className={`new-chat${onTasks ? " on" : ""}`} aria-label="定时任务" title="定时任务"
        aria-current={onTasks ? "page" : undefined}
        onClick={() => { onNavigate?.(); location.hash = "#/tasks"; }}>
        <CalendarClock size={15} strokeWidth={1.75} /><span className="nav-text">定时任务</span>
      </button>
      <button className={`new-chat${onSearch ? " on" : ""}`} aria-label="搜索记录" title="搜索记录"
        aria-current={onSearch ? "page" : undefined}
        onClick={() => { onNavigate?.(); location.hash = "#/search"; }}>
        <Search size={15} strokeWidth={1.75} /><span className="nav-text">搜索记录</span>
      </button>
      {chats.length > 0 && (
        <div className="project plain">
          <div className="project-head"><span>对话</span><span className="count">{chats.length}</span></div>
          {sessionGroups(chats).map(([label, list]) => (
            <div key={label} className="session-group">
              <div className="date-head">{label}</div>
              {list.map(sessionRow)}
            </div>
          ))}
        </div>
      )}
      <div className="section-head">
        <span>项目<span className="count">{visibleProjects.length}</span></span>
        <button title="添加项目" aria-label="添加项目" onClick={onAddProject}><Plus size={14} /></button>
      </div>
      {visibleProjects.map((p) => {
        const isClosed = closed.includes(p.id) && !p.sessions.some((s) => s.id === current);
        const shown = p.sessions.filter((s) => !s.archived_at && (!isClosed || s.id === current));
        return (
          <div key={p.id} className="project">
            {editing === `p:${p.id}` ? (
              <Rename value={p.name} onDone={(v) => {
                setEditing(null);
                if (v && v !== p.name) act(api.patchProject(p.id, { name: v }));
              }} />
            ) : (
              <div className="project-head" title={p.root_path}>
                <span className="name" onClick={() => toggle(p.id)}>
                  <span className={`caret${isClosed ? "" : " open"}`} />
                  <span className="label">{p.name}</span>
                  {!p.trusted && <span className="badge" title="未信任：新会话以只读档开始，不加载项目技能">未信任</span>}
                  {shown.length > 0 && <span className="count">{shown.length}</span>}
                </span>
                <span className="actions">
                  <button title="在此项目中新建会话" aria-label="在此项目中新建会话"
                    onClick={() => { onNavigate?.(); location.hash = `#/new/${p.id}`; }}>
                    <Plus size={14} />
                  </button>
                  <Menu items={[["重命名", () => setEditing(`p:${p.id}`)],
                    [p.trusted ? "取消信任" : "信任此项目", () => act(api.patchProject(p.id, { trusted: !p.trusted }))],
                    [p.no_index ? "允许索引项目记录" : "排除项目记录索引", () => act(api.patchProject(p.id, { no_index: !p.no_index }))],
                    ["从列表移除", () => {
                      void confirm({
                        title: `从列表移除“${p.name}”？`,
                        message: `它的 ${p.sessions.length} 个会话记录会被永久删除；磁盘上的文件夹不受影响。`,
                        confirmLabel: "移除项目", danger: true,
                      }).then((ok) => {
                        if (ok) void act(api.removeProject(p.id), p.sessions.some((s) => s.id === current));
                      });
                    }, true]]} />
                </span>
              </div>
            )}
            <div className={`project-sessions${isClosed ? "" : " open"}`}>
              <div className="project-sessions-inner">{shown.map(sessionRow)}</div>
            </div>
          </div>
        );
      })}
      {archived.length > 0 && (
        <details className="archived">
          <summary>已归档（{archived.length}）</summary>
          {archived.map(({ s, p }) => (
            <div key={s.id} className={`session${s.id === current ? " on" : ""}`}>
              {s.id === current && (
                <motion.span layoutId="sidebar-active" className="session-active"
                  transition={{ type: "spring", stiffness: 420, damping: 34 }} />
              )}
              <span className="dot" />
              <button onClick={() => openSession(s.id)} title={`${p.scratch ? "对话" : p.name} · ${title(s)}`}>{title(s)}</button>
              <button className="link" title="恢复到列表" onClick={() => act(api.patch(s.id, { archived: false }))}>恢复</button>
              <button className="link danger-text" title="永久删除" onClick={() => {
                void confirm({
                  title: `永久删除会话“${title(s)}”？`,
                  message: "此操作无法撤销。",
                  confirmLabel: "永久删除", danger: true,
                }).then((ok) => {
                  if (ok) void act(api.deleteSession(s.id), s.id === current);
                });
              }}>删除</button>
            </div>
          ))}
        </details>
      )}
      <Account info={info} />
    </nav>
  );
}

// The account button at the bottom of the sidebar; its menu opens upwards.
function Account({ info }: { info: Info | null }) {
  const pop = usePopover();
  const user = info?.user ?? "…";
  return (
    <div className="account popover-wrap" ref={pop.wrapRef}>
      <PopoverPanel open={pop.open} className="account-pop" side="top" align="start">
        <a href="#/settings" onClick={pop.close}><Settings size={16} strokeWidth={1.75} />设置<kbd>⌘ ,</kbd></a>
        <a href="#/settings/usage" onClick={pop.close}><Gauge size={16} strokeWidth={1.75} />用量</a>
        <a href="#/settings/help" onClick={pop.close}><CircleHelp size={16} strokeWidth={1.75} />获取帮助</a>
      </PopoverPanel>
      <button ref={pop.triggerRef} className={`account-btn${pop.open ? " open" : ""}`} aria-haspopup="menu"
        aria-expanded={pop.open} aria-label={`账户菜单（${user}）`} title="账户与设置"
        onClick={(e) => { e.stopPropagation(); pop.toggle(); }}>
        <span className="avatar">{user.slice(0, 1).toUpperCase()}</span>
        <span className="who"><b>{user}</b><small>本机{info ? ` · v${info.version}` : ""}</small></span>
        <ChevronsUpDown size={14} />
      </button>
    </div>
  );
}
