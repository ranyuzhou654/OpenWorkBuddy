import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type Entry, type Info, type Preset, type Project } from "./api";
import { Chat } from "./Chat";
import { Composer } from "./Composer";
import { Home } from "./Home";
import { Settings } from "./Settings";
import { textOf, useSession } from "./session";
import { Sidebar, useAddProject } from "./Sidebar";
import { Tree } from "./Tree";

// The page lives in the URL hash (#/s/<id>, #/new/<project id>, #/settings; anything else is the
// new-chat page), so reloads and bookmarks keep it.
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
  const newIn = hash.match(/^#\/new\/(.+)$/)?.[1] ?? null;
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
    if (!busy) refresh(); // a new session appears, and titles and ordering change when a run ends
  }, [busy, sid, refresh]);
  const showError = useCallback((message: string) => dispatch({ type: "error", message }), [dispatch]);
  const takeDropped = useCallback(() => dispatch({ type: "takeDropped" }), [dispatch]);
  const adder = useAddProject(projects, refresh, showError);

  if (unauthorized)
    return <div className="login">未登录。请用 <code>nwb serve</code> 在终端里打印的登录链接打开本页。</div>;

  const session = state.session;
  const project = projects.find((p) => p.id === session?.project_id);
  return (
    <div className="app">
      <Sidebar projects={projects} current={sid} running={busy ? sid : null} onChanged={refresh} onError={showError}
        onAddProject={() => adder.add().then((p) => p && (location.hash = `#/new/${p.id}`))} />
      {adder.dialog}
      <main>
        {state.error && (
          <div className="banner" onClick={() => dispatch({ type: "error", message: null })}>{state.error}（点击关闭）</div>
        )}
        {settingsOpen ? (
          <Settings onChanged={refreshInfo} onError={showError} />
        ) : sid && session ? (
          <>
            <header>
              <div className="title" title={project?.root_path}>
                {project && <span className="crumb">{project.scratch ? "对话" : project.name} / </span>}
                {session.title ?? firstMessage(state.entries) ?? "新会话"}
              </div>
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
                  <i><b style={{ width: `${Math.min(100, (100 * state.context.tokens) / state.context.budget)}%` }} /></i>
                  {kTokens(state.context.tokens)}/{kTokens(state.context.budget)}
                </span>
              )}
              <button className="ghost" disabled={busy} title="把较早的对话压缩成摘要，释放上下文"
                onClick={() => api.compact(sid).catch((e) => showError(String(e)))}>压缩</button>
              <button className={showTree ? "on" : "ghost"} onClick={() => setShowTree(!showTree)}>会话树</button>
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
                <Composer projectId={session.project_id} busy={busy} dropped={state.dropped} onTakeDropped={takeDropped}
                  onError={showError} submit={(text, delivery) => api.send(sid, { text, delivery })}
                  onCancel={() => api.cancel(sid).catch((e) => showError(String(e)))} />
              </div>
              {showTree && <Tree sid={sid} state={state} busy={busy} />}
            </div>
          </>
        ) : sid ? (
          <div className="empty">加载中…</div>
        ) : (
          <Home key={newIn ?? ""} info={info} projects={projects} initial={newIn} onAddProject={adder.add}
            onError={showError} />
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

// Titles come from the first message when a run ends; until then, show that message directly.
function firstMessage(entries: Record<string, Entry>): string | null {
  const first = Object.values(entries).filter((e) => e.type === "user").sort((a, b) => a.seq - b.seq)[0];
  return first ? textOf(first.data.content).slice(0, 80) : null;
}

function kTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  return n >= 100_000 ? `${Math.round(n / 1000)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}
