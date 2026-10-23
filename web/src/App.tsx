import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Info, type Preset, type Project } from "./api";
import { Chat } from "./Chat";
import { Composer } from "./Composer";
import { Settings } from "./Settings";
import { useSession } from "./session";
import { Tree } from "./Tree";

// The page lives in the URL hash (#/s/<id> or #/settings), so reloads and bookmarks keep it.
function useHash(): string {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const onHash = () => setHash(location.hash);
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  return hash;
}

export function App() {
  const [info, setInfo] = useState<Info | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [unauthorized, setUnauthorized] = useState(false);
  const hash = useHash();
  const sid = hash.match(/^#\/s\/(.+)$/)?.[1] ?? null;
  const select = (id: string) => (location.hash = `#/s/${id}`);
  const settingsOpen = hash === "#/settings";
  const [showTree, setShowTree] = useState(false);
  const [state, dispatch] = useSession(sid);
  const busy = state.live !== null;

  const refresh = useCallback(() => {
    api.projects().then(setProjects).catch((e) => e instanceof ApiError && e.status === 401 && setUnauthorized(true));
  }, []);
  const refreshInfo = useCallback(() => {
    api.info().then(setInfo).catch(() => {});
  }, []);
  useEffect(() => {
    refresh();
    refreshInfo();
  }, [refresh, refreshInfo]);
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
        {state.error && (
          <div className="banner" onClick={() => dispatch({ type: "error", message: null })}>{state.error}（点击关闭）</div>
        )}
        {settingsOpen ? (
          <Settings onChanged={refreshInfo} onError={showError} />
        ) : sid && session ? (
          <>
            <header>
              <div className="title">{session.title ?? "新会话"}</div>
              <select value={session.model_ref} disabled={busy} title="模型（下一次运行生效）"
                onChange={(e) => api.patch(sid, { model: e.target.value }).catch((er) => showError(String(er)))}>
                {[...new Set(info?.models.map((m) => m.provider))].map((conn) => (
                  <optgroup key={conn} label={conn}>
                    {info?.models.filter((m) => m.provider === conn).map((m) => <option key={m.name}>{m.name}</option>)}
                  </optgroup>
                ))}
              </select>
              <select value={session.settings.preset} className={session.settings.preset === "trust" ? "warn" : ""}
                title={PRESET_HELP[session.settings.preset]}
                onChange={(e) => api.patch(sid, { preset: e.target.value }).catch((er) => showError(String(er)))}>
                {Object.entries(info?.presets ?? {}).map(([k, label]) => <option key={k} value={k}>{label}</option>)}
              </select>
              {state.context && (
                <span className={`meter${state.context.tokens > state.context.budget * 0.8 ? " high" : ""}`}
                  title={`下一次请求约 ${state.context.tokens} tokens，上限 ${state.context.budget}（超出时先清理旧工具输出，再压缩）`}>
                  上下文 {kTokens(state.context.tokens)}/{kTokens(state.context.budget)}
                </span>
              )}
              <button disabled={busy} title="把较早的对话压缩成摘要，释放上下文"
                onClick={() => api.compact(sid).catch((e) => showError(String(e)))}>压缩</button>
              <button className={showTree ? "on" : ""} onClick={() => setShowTree(!showTree)}>会话树</button>
              <span className={`conn ${state.connected ? "ok" : ""}`} title={state.connected ? "已连接" : "连接中断，重连中"} />
              {state.usage && (
                <span className="usage" title="上一次运行的 token 用量">
                  ↑{state.usage.input ?? 0}（缓存 {state.usage.cache_read ?? 0}） ↓{state.usage.output ?? 0}
                </span>
              )}
            </header>
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

const PRESET_HELP: Record<Preset, string> = {
  readonly: "只读：读文件不询问；写入、编辑、命令都要确认。命令在只读沙箱中运行、不能联网。",
  workspace: "工作区：项目内读写不询问，命令在沙箱里运行（只能写项目目录、不能联网）；项目外写入、沙箱外运行需确认。",
  trust: "完全信任：不进沙箱、不询问，以你的全部权限运行。",
};

function kTokens(n: number): string {
  return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
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
      <button className={`link settings-link${location.hash === "#/settings" ? " on" : ""}`}
        onClick={() => (location.hash = "#/settings")}>⚙ 模型与密钥</button>
    </nav>
  );
}
