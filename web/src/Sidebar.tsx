import { type ReactNode, useEffect, useRef, useState } from "react";
import { api, ApiError, type Project, type SessionInfo } from "./api";

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
    <div className="modal" onClick={() => onAnswer(null)}>
      <div className="dialog" onClick={(e) => e.stopPropagation()}>
        <h3>信任这个文件夹吗？</h3>
        <code>{path}</code>
        <p><b>信任</b>：新会话默认“工作区”档，项目内读写、在沙箱里运行命令都不再询问；加载项目自带的技能（.agents/skills）。</p>
        <p><b>不信任</b>：新会话默认“只读”档，每次写入和命令都要你确认；不加载项目技能。适合刚下载、来历不明的代码。</p>
        <p className="note">两种情况都会读取项目说明（AGENTS.md）。之后可以在项目菜单里更改。</p>
        <div className="dialog-actions">
          <button onClick={() => onAnswer(null)}>取消</button>
          <button onClick={() => onAnswer(false)}>不信任</button>
          <button className="primary" onClick={() => onAnswer(true)} autoFocus>信任</button>
        </div>
      </div>
    </div>
  );
}

function PathDialog({ onDone }: { onDone: (path: string | null) => void }) {
  const [path, setPath] = useState("");
  return (
    <div className="modal" onClick={() => onDone(null)}>
      <div className="dialog" onClick={(e) => e.stopPropagation()}>
        <h3>添加项目</h3>
        <p className="note">这台机器上没有可用的文件夹选择框，请输入路径。</p>
        <input placeholder="项目目录的绝对路径" value={path} onChange={(e) => setPath(e.target.value)} autoFocus
          onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && path.trim() && onDone(path.trim())} />
        <div className="dialog-actions">
          <button onClick={() => onDone(null)}>取消</button>
          <button className="primary" disabled={!path.trim()} onClick={() => onDone(path.trim())}>下一步</button>
        </div>
      </div>
    </div>
  );
}

// A "⋯" button with a small menu; any click elsewhere closes it.
function Menu({ items }: { items: [label: string, action: () => void, danger?: boolean][] }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [open]);
  return (
    <span className="menu">
      <button className="dots" title="更多" onClick={(e) => { e.stopPropagation(); setOpen(!open); }}>⋯</button>
      {open && (
        <div className="menu-pop">
          {items.map(([label, action, danger]) => (
            <button key={label} className={danger ? "danger-text" : ""} onClick={action}>{label}</button>
          ))}
        </div>
      )}
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

export function Sidebar({ projects, current, onAddProject, onChanged, onError }: {
  projects: Project[]; current: string | null; onAddProject: () => void; onChanged: () => void;
  onError: (m: string) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null); // "s:<id>" or "p:<id>"
  const act = (p: Promise<unknown>, leave = false) =>
    p.then(() => { onChanged(); if (leave) location.hash = "#/"; }).catch((e) => onError(String(e)));
  const open = (sid: string) => (location.hash = `#/s/${sid}`);

  const sessionRow = (s: SessionInfo) =>
    editing === `s:${s.id}` ? (
      <Rename key={s.id} value={title(s)} onDone={(v) => {
        setEditing(null);
        if (v !== null && v !== title(s)) act(api.patch(s.id, { title: v }));
      }} />
    ) : (
      <div key={s.id} className={`session${s.id === current ? " on" : ""}`}>
        <button onClick={() => open(s.id)} title={title(s)}>{title(s)}</button>
        <Menu items={[["重命名", () => setEditing(`s:${s.id}`)],
          ["归档", () => act(api.patch(s.id, { archived: true }), s.id === current)]]} />
      </div>
    );

  const chats = projects.filter((p) => p.scratch).flatMap((p) => p.sessions.filter((s) => !s.archived_at));
  const archived = projects.flatMap((p) => p.sessions.filter((s) => s.archived_at).map((s) => ({ s, p })));
  return (
    <nav className="sidebar">
      <div className="brand">nano-workbuddy</div>
      <button className={`new-chat${current === null && location.hash !== "#/settings" ? " on" : ""}`}
        onClick={() => (location.hash = "#/")}>✎ 新对话</button>
      {chats.length > 0 && (
        <div className="project">
          <div className="project-head"><span>对话</span></div>
          {chats.map(sessionRow)}
        </div>
      )}
      <div className="section-head">
        <span>项目</span>
        <button title="添加项目" onClick={onAddProject}>＋</button>
      </div>
      {projects.filter((p) => !p.scratch).map((p) => (
        <div key={p.id} className="project">
          {editing === `p:${p.id}` ? (
            <Rename value={p.name} onDone={(v) => {
              setEditing(null);
              if (v && v !== p.name) act(api.patchProject(p.id, { name: v }));
            }} />
          ) : (
            <div className="project-head" title={p.root_path}>
              <span>{p.name}{!p.trusted && <span className="badge" title="未信任：新会话以只读档开始，不加载项目技能">未信任</span>}</span>
              <span>
                <button title="在此项目中新建会话" onClick={() => (location.hash = `#/new/${p.id}`)}>＋</button>
                <Menu items={[["重命名", () => setEditing(`p:${p.id}`)],
                  [p.trusted ? "取消信任" : "信任此项目", () => act(api.patchProject(p.id, { trusted: !p.trusted }))],
                  ["从列表移除", () => {
                    const n = p.sessions.length;
                    if (confirm(`从列表移除“${p.name}”？\n它的 ${n} 个会话记录会被永久删除；磁盘上的文件夹不受影响。`))
                      act(api.removeProject(p.id), p.sessions.some((s) => s.id === current));
                  }, true]]} />
              </span>
            </div>
          )}
          {p.sessions.filter((s) => !s.archived_at).map(sessionRow)}
        </div>
      ))}
      {archived.length > 0 && (
        <details className="archived">
          <summary>已归档（{archived.length}）</summary>
          {archived.map(({ s, p }) => (
            <div key={s.id} className={`session${s.id === current ? " on" : ""}`}>
              <button onClick={() => open(s.id)} title={`${p.scratch ? "对话" : p.name} · ${title(s)}`}>{title(s)}</button>
              <button className="link" title="恢复到列表" onClick={() => act(api.patch(s.id, { archived: false }))}>恢复</button>
              <button className="link danger-text" title="永久删除" onClick={() => {
                if (confirm(`永久删除会话“${title(s)}”？此操作无法撤销。`)) act(api.deleteSession(s.id), s.id === current);
              }}>删除</button>
            </div>
          ))}
        </details>
      )}
      <button className={`link settings-link${location.hash === "#/settings" ? " on" : ""}`}
        onClick={() => (location.hash = "#/settings")}>⚙ 模型与密钥</button>
    </nav>
  );
}
