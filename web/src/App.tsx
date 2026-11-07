import { AnimatePresence, motion } from "motion/react";
import { GitBranch, Menu, PanelRight, SlidersHorizontal } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type Entry, type Info, type Preset, type Project } from "./api";
import { Composer } from "./Composer";
import { Diagnostics } from "./Diagnostics";
import { Home } from "./Home";
import { Select } from "./Select";
import { textOf, useSession } from "./session";
import { SessionStatus } from "./SessionStatus";
import { Sidebar, useAddProject } from "./Sidebar";
import { Tree } from "./Tree";
import { FOCUSABLE, PopoverPanel, Skeleton, SkeletonList, usePopover, useToast } from "./ui";

// Heavy pages load on first visit; the conversation shell stays in the initial bundle.
const tasksPage = () => import("./Tasks");
const Tasks = lazy(() => tasksPage().then((m) => ({ default: m.Tasks })));
const SearchPage = lazy(() => tasksPage().then((m) => ({ default: m.SearchPage })));
const Settings = lazy(() => import("./Settings").then((m) => ({ default: m.Settings })));
const Chat = lazy(() => import("./Chat").then((m) => ({ default: m.Chat })));
const Library = lazy(() => import("./Files").then((m) => ({ default: m.Library })));

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

const DRAWER_KEY = "nwb.drawer";
function savedDrawer(): "library" | "tree" | null {
  if (!window.matchMedia("(min-width: 901px)").matches) return null;
  const value = localStorage.getItem(DRAWER_KEY);
  return value === "library" || value === "tree" ? value : null;
}

export function App() {
  const [info, setInfo] = useState<Info | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [unauthorized, setUnauthorized] = useState(false);
  const hash = useHash();
  const sid = hash.match(/^#\/s\/(.+)$/)?.[1] ?? null;
  const newIn = hash.match(/^#\/new\/(.+)$/)?.[1] ?? null;
  const select = useCallback((id: string) => { location.hash = `#/s/${id}`; }, []);
  const settingsOpen = hash.startsWith("#/settings");
  const [showTree, setShowTree] = useState(() => savedDrawer() === "tree");
  const [showLibrary, setShowLibrary] = useState(() => savedDrawer() === "library");
  const [navOpen, setNavOpen] = useState(false);
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const [reveal, setReveal] = useState<{ id: string; nonce: number } | null>(null);
  const libraryBtn = useRef<HTMLButtonElement>(null);
  const treeBtn = useRef<HTMLButtonElement>(null);
  const drawerRef = useRef<HTMLElement>(null);
  const drawerRestore = useRef<HTMLElement | null>(null);
  const [state, dispatch] = useSession(sid);
  const busy = state.live !== null;
  const toast = useToast();
  const run = usePopover();

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
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") refresh(); // background runs finish outside this tab
    }, 20000);
    return () => window.clearInterval(timer);
  }, [refresh]);
  const showError = useCallback((message: string) => toast.error(message), [toast]);
  const forkSession = useCallback((entryId: string) => {
    if (!sid) return;
    api.fork(sid, entryId).then((s) => { refresh(); select(s.id); }).catch((e) => showError(String(e)));
  }, [sid, refresh, select, showError]);
  useEffect(() => {
    if (!state.error) return;
    toast.error(state.error);
    dispatch({ type: "error", message: null });
  }, [state.error, toast, dispatch]);
  useEffect(() => {
    setNavOpen(false);
    setShowDiagnostics(false);
  }, [hash]);
  useEffect(() => {
    if (!sid) return;
    localStorage.setItem(DRAWER_KEY, showLibrary ? "library" : showTree ? "tree" : "");
  }, [showLibrary, showTree, sid]);
  const takeDropped = useCallback(() => dispatch({ type: "takeDropped" }), [dispatch]);
  const adder = useAddProject(projects, refresh, showError);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === ",") {
        e.preventDefault();
        location.hash = "#/settings";
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (unauthorized)
    return <div className="login">未登录。请用 <code>nwb serve</code> 在终端里打印的登录链接打开本页。</div>;

  const session = state.session;
  const project = projects.find((p) => p.id === session?.project_id);
  const sessionOpen = Boolean(sid && session);
  const routeKey = sessionOpen ? `session:${sid}` : settingsOpen ? "settings"
    : hash.startsWith("#/tasks") ? "tasks" : hash.startsWith("#/search") ? "search"
      : newIn ? `new:${newIn}` : "home";
  const pageName = settingsOpen ? "设置" : hash.startsWith("#/tasks") ? "定时任务"
    : hash.startsWith("#/search") ? "搜索记录" : newIn ? "新对话" : "新对话";

  const drawerOpen = showLibrary || showTree;
  const closeDrawer = useCallback(() => {
    setShowLibrary(false);
    setShowTree(false);
  }, []);
  const revealEntry = useCallback((id: string) => {
    setReveal({ id, nonce: Date.now() });
    if (window.matchMedia("(max-width: 900px)").matches) closeDrawer();
  }, [closeDrawer]);
  useEffect(() => setReveal(null), [sid]);
  const toggleLibrary = () => {
    if (showLibrary) return closeDrawer();
    drawerRestore.current = libraryBtn.current ?? (document.activeElement as HTMLElement | null);
    setShowTree(false);
    setShowLibrary(true);
  };
  const toggleTree = () => {
    if (showTree) return closeDrawer();
    drawerRestore.current = treeBtn.current ?? (document.activeElement as HTMLElement | null);
    setShowLibrary(false);
    setShowTree(true);
  };
  useEffect(() => {
    if (!drawerOpen) return;
    const panel = drawerRef.current;
    if (!panel) return;
    const focusables = () => [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)]
      .filter((el) => el.offsetParent !== null || el === document.activeElement);
    const timer = window.setTimeout(() => {
      (panel.querySelector<HTMLElement>(".drawer-head .icon-btn") ?? focusables()[0] ?? panel).focus();
    }, 40);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDrawer();
        return;
      }
      if (event.key !== "Tab") return;
      const list = focusables();
      if (!list.length) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const first = list[0];
      const last = list[list.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("keydown", onKey);
      drawerRestore.current?.focus?.();
    };
  }, [drawerOpen, closeDrawer]);

  return (
    <div className="app">
      <a className="skip-link" href="#main-content" onClick={(e) => {
        e.preventDefault();
        document.getElementById("main-content")?.focus();
      }}>跳到主要内容</a>
      <Sidebar info={info} projects={projects} current={sid} running={busy ? sid : null} onChanged={refresh} onError={showError}
        onAddProject={() => adder.add().then((p) => p && (location.hash = `#/new/${p.id}`))} open={navOpen} />
      {navOpen && <div className="nav-scrim" onClick={() => setNavOpen(false)} />}
      {adder.dialog}
      <main id="main-content" tabIndex={-1} aria-label="主要内容">
        {!sessionOpen && (
          <div className="mobile-bar">
            <button className="icon-btn" aria-label="打开导航" onClick={() => setNavOpen(true)}><Menu size={17} /></button>
            <span className="mobile-title">{pageName}</span>
          </div>
        )}
        <motion.div className="route" key={routeKey} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}>
          <Suspense fallback={<RouteLoading />}>
          {hash.startsWith("#/tasks") ? (
            <Tasks info={info} projects={projects} onError={showError} />
          ) : hash.startsWith("#/search") ? (
            <SearchPage projects={projects} onError={showError} />
          ) : settingsOpen ? (
            <Settings section={hash.split("/")[2] ?? "general"} onChanged={refreshInfo} onError={showError} />
          ) : sid && session ? (
            <>
              <header className="session-header">
                <button className="icon-btn nav-toggle" aria-label="打开导航" onClick={() => setNavOpen(true)}><Menu size={17} /></button>
                <div className="title" title={project?.root_path}>
                  {project && <span className="crumb">{project.scratch ? "对话" : project.name} / </span>}
                  {session.title ?? firstMessage(state.entries) ?? "新会话"}
                </div>
                <div className="header-actions">
                  <Select className="header-select" value={session.model_ref} disabled={busy}
                    label="模型" title="模型（下一次运行生效）" placeholder="模型"
                    groups={[...new Set(info?.models.map((m) => m.provider))].map((conn) => ({
                      label: conn,
                      options: (info?.models ?? []).filter((m) => m.provider === conn)
                        .map((m) => ({ value: m.name, label: m.name })),
                    }))}
                    onChange={(model) => api.patch(sid, { model }).catch((er) => showError(String(er)))} />
                  <div className="popover-wrap" ref={run.wrapRef}>
                    <button ref={run.triggerRef} className={`icon-btn${run.open ? " on" : ""}`} onClick={run.toggle}
                      aria-haspopup="dialog" aria-expanded={run.open} aria-label="运行设置" title="运行设置">
                      <SlidersHorizontal size={15} /><span className="btn-label">运行设置</span>
                    </button>
                    <PopoverPanel open={run.open} className="run-settings">
                      <div className="popover-title">运行设置</div>
                      <label className="field-row">
                        <span>权限档</span>
                        <Select value={session.settings.preset} label="权限档"
                          className={session.settings.preset === "trust" ? "warn" : ""}
                          title={PRESET_HELP[session.settings.preset]}
                          options={Object.entries(info?.presets ?? {}).map(([value, label]) => ({ value, label }))}
                          onChange={(preset) => api.patch(sid, { preset }).catch((er) => showError(String(er)))} />
                      </label>
                      <label className="field-row">
                        <span>检索范围</span>
                        <Select value={session.settings.recall_scope ?? (project?.scratch ? "session" : "project")}
                          label="模型检索范围" title="模型自动检索范围；全局搜索单独选择。跨项目需要在此明确启用。"
                          options={[
                            { value: "session", label: "当前会话" },
                            ...(!project?.scratch ? [{ value: "project", label: "当前项目" }] : []),
                            { value: "all", label: "跨项目（主动启用）" },
                            ...(session.settings.recall_scope === "selected" ? [{ value: "selected", label: "选定会话" }] : []),
                          ]}
                          onChange={(recall_scope) => api.patch(sid, { recall_scope }).catch((er) => showError(String(er)))} />
                      </label>
                      <div className="popover-actions">
                        <button disabled={busy} title="把较早的对话压缩成摘要，释放上下文"
                          onClick={() => { run.close(); api.compact(sid).catch((e) => showError(String(e))); }}>压缩上下文</button>
                        <button onClick={() => { run.close(); setShowDiagnostics(true); }}>诊断与预算</button>
                      </div>
                      {state.usage && (
                        <div className="usage-block">
                          上次运行：↑{state.usage.input ?? 0}（缓存 {state.usage.cache_read ?? 0}） ↓{state.usage.output ?? 0}
                        </div>
                      )}
                    </PopoverPanel>
                  </div>
                  <button ref={libraryBtn} className={`icon-btn${showLibrary ? " on" : ""}`} onClick={toggleLibrary}
                    aria-pressed={showLibrary} aria-label="资料" title="这个会话的来源、交付的文件和附件">
                    <PanelRight size={15} /><span className="btn-label">资料</span>
                  </button>
                  <button ref={treeBtn} className={`icon-btn${showTree ? " on" : ""}`} onClick={toggleTree}
                    aria-pressed={showTree} aria-label="会话树" title="会话树">
                    <GitBranch size={15} /><span className="btn-label">会话树</span>
                  </button>
                  <span className={`conn ${state.connected ? "ok" : ""}`} title={state.connected ? "已连接" : "连接中断，重连中"} />
                </div>
              </header>
              <div className="workspace">
                <div className="conversation">
                  <Suspense fallback={<div className="chat chat-loading"><SkeletonList count={3} /></div>}>
                    <Chat sid={sid} state={state} onFork={forkSession} reveal={reveal} />
                  </Suspense>
                  <div className="composer-dock">
                    <Composer projectId={session.project_id} busy={busy}
                      ctx={{ sid, session, info, busy, entries: state.entries, leaf: state.leaf, refresh, toggleTree }}
                      dropped={state.dropped} onTakeDropped={takeDropped}
                      onError={showError} submit={(text, delivery, files, onProgress) => api.sendWithFiles(sid, text, files, delivery, onProgress)}
                      onCancel={() => api.cancel(sid).catch((e) => showError(String(e)))} />
                    <SessionStatus entries={state.entries} leaf={state.leaf} context={state.context} busy={busy} />
                  </div>
                </div>
                <AnimatePresence>
                  {(showLibrary || showTree) && (
                    <motion.div className="drawer-layer" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
                      transition={{ duration: 0.16 }}>
                      <div className="drawer-scrim" onClick={closeDrawer} />
                      <motion.aside ref={drawerRef} className="drawer" role="dialog" aria-modal="true"
                        aria-labelledby="drawer-title" tabIndex={-1}
                        initial={{ x: 30, opacity: 0 }} animate={{ x: 0, opacity: 1 }}
                        exit={{ x: 24, opacity: 0 }} transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}>
                        <div className="drawer-head">
                          <span id="drawer-title">{showLibrary ? "资料" : "会话树"}</span>
                          <button className="icon-btn" aria-label="关闭面板" onClick={closeDrawer}>×</button>
                        </div>
                        {showLibrary ? (
                          <Suspense fallback={<div className="drawer-loading"><SkeletonList count={2} /></div>}>
                            <Library sid={sid} state={state} />
                          </Suspense>
                        ) : <Tree sid={sid} state={state} busy={busy} onReveal={revealEntry} />}
                      </motion.aside>
                    </motion.div>
                  )}
                </AnimatePresence>
              </div>
              <Diagnostics sid={sid} session={session} busy={busy} open={showDiagnostics}
                onClose={() => setShowDiagnostics(false)} onError={showError} />
            </>
          ) : sid ? (
            <div className="session-loading"><Skeleton lines={5} /></div>
          ) : (
            <Home key={newIn ?? ""} info={info} projects={projects} initial={newIn} onAddProject={adder.add}
              onError={showError} />
          )}
          </Suspense>
        </motion.div>
      </main>
    </div>
  );
}

function RouteLoading() {
  return (
    <div className="page route-loading" aria-busy="true">
      <Skeleton lines={2} />
      <SkeletonList count={2} />
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
