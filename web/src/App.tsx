import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Info, type Project } from "./api";
import { Chat } from "./Chat";
import { Composer } from "./Composer";
import { useSession } from "./session";
import { Tree } from "./Tree";

// The selected session lives in the URL hash (#/s/<id>), so reloads and bookmarks keep it.
function useHashSession(): [string | null, (sid: string) => void] {
  const read = () => location.hash.match(/^#\/s\/(.+)$/)?.[1] ?? null;
  const [sid, setSid] = useState(read);
  useEffect(() => {
    const onHash = () => setSid(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return [sid, (id) => (location.hash = `#/s/${id}`)];
}

export function App() {
  const [info, setInfo] = useState<Info | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [unauthorized, setUnauthorized] = useState(false);
  const [sid, select] = useHashSession();
  const [showTree, setShowTree] = useState(false);
  const [state, dispatch] = useSession(sid);
  const busy = state.live !== null;

  const refresh = useCallback(() => {
    api.projects().then(setProjects).catch((e) => e instanceof ApiError && e.status === 401 && setUnauthorized(true));
  }, []);
  useEffect(() => {
    refresh();
    api.info().then(setInfo).catch(() => {});
  }, [refresh]);
  useEffect(() => {
    if (!busy) refresh(); // titles and ordering change when a run ends
  }, [busy, refresh]);
  const showError = useCallback((message: string) => dispatch({ type: "error", message }), [dispatch]);
  const takeDropped = useCallback(() => dispatch({ type: "takeDropped" }), [dispatch]);

  if (unauthorized)
    return <div className="login">未登录。请用 <code>nwb serve</code> 在终端里打印的登录链接打开本页。</div>;

  const session = state.session;
  return (
    <div className="app">
      <Sidebar projects={projects} current={sid} onSelect={select} onChanged={refresh} onError={showError} />
      <main>
        {sid && session ? (
          <>
            <header>
              <div className="title">{session.title ?? "新会话"}</div>
              <select value={session.model_ref} disabled={busy} title="模型（下一次运行生效）"
                onChange={(e) => api.patch(sid, { model: e.target.value }).catch((er) => showError(String(er)))}>
                {info?.models.map((m) => <option key={m}>{m}</option>)}
              </select>
              <select value={session.settings.mode} className={session.settings.mode === "trust" ? "warn" : ""}
                title={info?.modes[session.settings.mode]}
                onChange={(e) => api.patch(sid, { mode: e.target.value }).catch((er) => showError(String(er)))}>
                <option value="confirm">写入和命令需确认</option>
                <option value="trust">完全信任（不询问）</option>
              </select>
              <button className={showTree ? "on" : ""} onClick={() => setShowTree(!showTree)}>会话树</button>
              <span className={`conn ${state.connected ? "ok" : ""}`} title={state.connected ? "已连接" : "连接中断，重连中"} />
              {state.usage && (
                <span className="usage" title="上一次运行的 token 用量">
                  ↑{state.usage.input ?? 0}（缓存 {state.usage.cache_read ?? 0}） ↓{state.usage.output ?? 0}
                </span>
              )}
            </header>
            {state.error && (
              <div className="banner" onClick={() => dispatch({ type: "error", message: null })}>{state.error}（点击关闭）</div>
            )}
            <div className="workspace">
              <div className="conversation">
                <Chat sid={sid} state={state}
                  onFork={(id) => api.fork(sid, id).then((s) => { refresh(); select(s.id); }).catch((e) => showError(String(e)))} />
                <Composer sid={sid} busy={busy} dropped={state.dropped} onTakeDropped={takeDropped} onError={showError} />
              </div>
              {showTree && <Tree sid={sid} state={state} busy={busy} />}
            </div>
          </>
        ) : (
          <div className="empty">在左侧选择或新建一个会话。</div>
        )}
      </main>
    </div>
  );
}

function Sidebar({ projects, current, onSelect, onChanged, onError }: {
  projects: Project[]; current: string | null; onSelect: (sid: string) => void; onChanged: () => void;
  onError: (m: string) => void;
}) {
  const [adding, setAdding] = useState(false);
  const [path, setPath] = useState("");
  const add = async () => {
    try {
      await api.addProject(path);
      setPath("");
      setAdding(false);
      onChanged();
    } catch (e) {
      onError(String(e));
    }
  };
  return (
    <nav className="sidebar">
      <div className="brand">nano-workbuddy</div>
      {projects.map((p) => (
        <div key={p.id} className="project">
          <div className="project-head" title={p.root_path}>
            <span>{p.name}</span>
            <button title="新会话" onClick={() => api.newSession(p.id).then((s) => { onChanged(); onSelect(s.id); })}>＋</button>
          </div>
          {p.sessions.map((s) => (
            <button key={s.id} className={`session${s.id === current ? " on" : ""}`} onClick={() => onSelect(s.id)}>
              {s.title ?? "新会话"}
            </button>
          ))}
        </div>
      ))}
      {adding ? (
        <div className="add-project">
          <input placeholder="项目目录的绝对路径" value={path} onChange={(e) => setPath(e.target.value)} autoFocus
            onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && add()} />
          <button onClick={add}>添加</button>
        </div>
      ) : (
        <button className="link" onClick={() => setAdding(true)}>＋ 添加项目</button>
      )}
    </nav>
  );
}
