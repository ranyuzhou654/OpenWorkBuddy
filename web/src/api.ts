// Types mirror the backend's wire format; requests are thin wrappers over fetch.

export type Block =
  | { type: "text"; text: string; attachment?: string } // attachment: the model's view of an attached file
  | { type: "image"; mime: string; data: string }
  | { type: "thinking"; text: string; provider: string }
  | { type: "tool_call"; id: string; name: string; args: Record<string, unknown>; error?: string };

// A file the user attached to a message (stored per session; the model sees it as <attachment> blocks).
export interface Attachment {
  id: string;
  name: string;
  mime: string;
  size: number;
  sha256: string;
  path: string;
  kind: "image" | "document" | "text" | "file";
  desc: string; // e.g. "PDF, 12 pages"
}

export interface Check {
  name: string;
  ok: boolean;
  detail: string;
}

// A file delivered to the user (create_document / present): one version, kept by hash.
export interface Artifact {
  path: string;
  name: string;
  mime: string;
  size: number;
  sha256: string;
  version: number;
  title: string;
  checks: Check[];
  info: Record<string, unknown>;
  kept: boolean;
  created_at: number;
}

// A numbered source [S#]: a search snippet, a page read in full, or a downloaded file.
export interface Source {
  n: number;
  url: string;
  kind: "search" | "page" | "file";
  title: string;
  snippet?: string;
  published?: string;
  retrieved_at?: string;
  provider?: string;
}

// A scheduled task (M3): a rule, its own session, and what its runs found.
export interface Occurrence {
  schedule_id: string;
  planned_at: number;
  missed: number;
  status: "submitted" | "done";
  outcome: "changed" | "unchanged" | "failed" | "needs_user" | "unknown" | "cancelled" | "skipped" | "missed" | null;
  summary: string | null;
  created_at: number;
}

export interface Schedule {
  id: string;
  name: string;
  prompt: string;
  session_id: string;
  kind: "once" | "daily" | "weekly";
  at_time: string;
  weekdays: number[];
  day: string;
  tz: string;
  catch_up: "once" | "skip" | "ask";
  enabled: boolean;
  next_run: number | null;
  next_local: string | null;
  last_outcome: Occurrence["outcome"];
  last_summary: string | null;
  describe: string;
  template: { template?: string; topic?: string; file?: string };
  running: boolean;
  recent: Occurrence[];
}

export interface ScheduleBody {
  name: string;
  prompt?: string;
  template?: { kind: "weekly_research"; topic: string; file: string; extra?: string };
  project_id: string | null;
  kind: Schedule["kind"];
  at: string;
  weekdays: number[];
  day?: string;
  catch_up: Schedule["catch_up"];
  model?: string;
}

export interface SearchHit {
  title: string;
  kind: "message" | "source" | "file" | "upload";
  ref: string;
  session_id: string;
  at: number;
  snippet: string;
}

export interface Entry {
  id: string;
  parent_id: string | null;
  type: string; // system_prompt | user | assistant | tool_started | tool_result | state | notice | prune | compaction
  data: any;
  run_id?: string;
  ts: number;
  seq: number;
}

export type Preset = "readonly" | "workspace" | "trust";

export interface Settings {
  preset: Preset;
}

export interface ContextUsage {
  tokens: number;
  budget: number;
}

export interface SessionInfo {
  id: string;
  project_id: string;
  title: string | null;
  model_ref: string;
  settings: Settings;
  leaf_id: string | null;
  updated_at: number;
  archived_at: number | null;
}

export interface Project {
  id: string;
  name: string;
  root_path: string;
  trusted: boolean; // untrusted: new sessions start read-only and the project's skills are not loaded
  scratch: boolean; // the folder chats without a project run in
  sessions: SessionInfo[];
}

export interface Approval {
  id: string;
  call_id: string;
  tool: string;
  args: Record<string, any>;
  action: string; // e.g. "write", "bash.unsandboxed"
  resource: string; // the path or command the permission check saw
  always: { action: string; resource: string } | null; // the rule "always allow" would add; null: approve each call
  preview?: FeishuPreview | null; // a connector's dry run of the exact request
}

export interface FeishuPreview {
  account?: string;
  action?: string;
  target?: string;
  dry_run?: unknown;
  error?: string;
}

// The result of a write through a connector, kept with the tool result.
export interface Receipt {
  command: string;
  target: string;
  what: string;
  result: unknown;
  links: string[];
  at: number;
}

export interface FeishuStatus {
  available: boolean;
  cli: string;
  version?: string;
  app_id?: string;
  brand?: string;
  user?: { available: boolean; name?: string; message?: string };
  error?: string;
}

export interface Question {
  id: string;
  call_id: string;
  question: string;
  options: string[];
}

export interface Todo {
  content: string;
  status: "pending" | "in_progress" | "completed";
}

export interface Live {
  run_id: string | null;
  text: string;
  thinking: string;
  outputs: Record<string, string>;
  steer: string[];
  queue: string[];
  approvals: Approval[];
  questions: Question[];
}

export interface Snapshot {
  session: SessionInfo;
  seq: number;
  leaf_id: string | null;
  entries: Entry[];
  live: Live | null;
  context: ContextUsage;
}

export interface Info {
  models: { name: string; provider: string; context_window: number }[];
  default_model: string;
  presets: Record<Preset, string>;
  user: string; // the account the server runs as
  version: string;
}

export type Tokens = { runs: number; input?: number; output?: number; cache_read?: number; [k: string]: number | undefined };

export interface Usage {
  days: number;
  per_day: (Tokens & { day: string })[];
  per_model: (Tokens & { model: string })[];
}

export interface SystemInfo {
  version: string;
  python: string;
  os: string;
  home: string;
  config: string;
  scratch_dir: string;
  sandbox: string | null;
  ripgrep: string | null;
  folder_picker: boolean;
  hooks: { event: string; matcher: string; command: string; timeout: number }[];
  rules: { action: string; resource: string; effect: string }[];
}

export type Protocol = "openai-chat" | "anthropic" | "openai-responses";

export interface KeyStatus {
  source: string; // env | keyring | secret
  name: string;
  set: boolean;
  hint?: string; // last 4 characters; the key itself never leaves the server
}

export interface ConnectionView {
  name: string;
  protocol: Protocol;
  base_url: string;
  key: KeyStatus;
}

export interface ModelView {
  name: string;
  provider: string;
  id: string;
  context_window: number;
  max_output: number;
  params: Record<string, unknown>;
  vision: boolean | null; // null: guessed from the protocol
  sees_images?: boolean;
}

export interface SearchView {
  provider: string;
  key: KeyStatus | null;
}

export type Exposure = "auto" | "direct" | "deferred";

export interface McpSpec {
  command: string;
  args: string[];
  env: Record<string, string>; // values may be key references: env:X / keyring:X / secret:X
  url: string;
  headers: Record<string, string>;
  enabled: boolean;
  exposure: Exposure;
  sandbox: boolean;
  network: boolean;
  writable: string[];
  timeout: number;
  description: string;
}

export interface McpView extends McpSpec {
  name: string;
  status: "connected" | "error" | "idle" | "disabled";
  error: string;
  tools: { name: string; description: string }[];
  keys: Record<string, KeyStatus>; // env/header values that are key references
}

export interface SkillView {
  name: string;
  description: string;
  path: string;
  source: "user" | "project";
  project: string | null;
  enabled: boolean;
}

export interface MemoryFile {
  key: string; // "user" or a project id
  label: string;
  path: string;
  text: string;
}

export interface SettingsView {
  default_model: string;
  providers: ConnectionView[];
  models: ModelView[];
  mcp: McpView[];
  skills: SkillView[];
  skill_dirs: string[];
  search: SearchView;
  feishu: { enabled: boolean; cli: string };
  browser: { enabled: boolean; headless: boolean; profile: string; status: McpView["status"]; error: string;
    tools: { name: string; description: string }[] };
}

export interface RemoteModel {
  id: string;
  context_window?: number | null;
  max_output?: number | null;
}

export interface ModelTest {
  ok: boolean;
  reply?: string;
  ms?: number;
  error?: string;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) {
    let detail: unknown = r.statusText;
    try {
      detail = (await r.json()).detail ?? detail;
    } catch {
      // not JSON: keep the status text
    }
    throw new ApiError(r.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return r.json();
}

export const api = {
  info: () => call<Info>("GET", "/api/info"),
  projects: () => call<Project[]>("GET", "/api/projects"),
  addProject: (path: string, trusted: boolean) => call<Project>("POST", "/api/projects", { path, trusted }),
  patchProject: (pid: string, body: { name?: string; trusted?: boolean }) => call<Project>("PATCH", `/api/projects/${pid}`, body),
  removeProject: (pid: string) => call<{ ok: boolean }>("DELETE", `/api/projects/${pid}`),
  pickFolder: () => call<{ path: string | null }>("POST", "/api/pick-folder"), // a native dialog on the server's machine
  // project_id null: a chat without a project
  newSession: (project_id: string | null, model?: string) => call<SessionInfo>("POST", "/api/sessions", { project_id, model }),
  snapshot: (sid: string) => call<Snapshot>("GET", `/api/sessions/${sid}`),
  patch: (sid: string, body: { model?: string; preset?: string; title?: string; archived?: boolean }) =>
    call<SessionInfo>("PATCH", `/api/sessions/${sid}`, body),
  deleteSession: (sid: string) => call<{ ok: boolean }>("DELETE", `/api/sessions/${sid}`),
  send: (sid: string, body: { text: string; parent_id?: string | null; delivery?: "auto" | "steer" | "queue"; attachments?: string[] }) =>
    call<{ delivery: string }>("POST", `/api/sessions/${sid}/messages`, { request_id: crypto.randomUUID(), ...body }),
  // The raw file as the body; returns what to list in the message's attachments.
  upload: async (sid: string, file: File): Promise<Attachment> => {
    const r = await fetch(`/api/sessions/${sid}/attachments?name=${encodeURIComponent(file.name || "pasted.png")}`,
      { method: "PUT", body: file });
    if (!r.ok) throw new ApiError(r.status, (await r.json().catch(() => ({}))).detail ?? r.statusText);
    return r.json();
  },
  // Upload the files, then send the message that refers to them.
  sendWithFiles: async (sid: string, text: string, files: File[], delivery: "auto" | "queue" = "auto") => {
    const ids: string[] = [];
    for (const f of files) ids.push((await api.upload(sid, f)).id);
    return api.send(sid, { text, delivery, attachments: ids });
  },
  fileUrl: (sid: string, sha: string, name: string, download = false) =>
    `/api/sessions/${sid}/files/${sha}?name=${encodeURIComponent(name)}${download ? "&download=1" : ""}`,
  fileText: (sid: string, sha: string, name: string) =>
    call<{ description: string; notes: string[]; text: string }>("GET",
      `/api/sessions/${sid}/files/${sha}/text?name=${encodeURIComponent(name)}`),
  reveal: (sid: string, path: string) => call<{ ok: boolean }>("POST", `/api/sessions/${sid}/reveal`, { path }),
  putSearch: (body: { provider?: string; api_key?: string; key_ref?: string; clear?: boolean }) =>
    call<SettingsView>("PUT", "/api/settings/search", { provider: "tavily", ...body }),
  putFeishu: (body: { enabled?: boolean; cli?: string }) => call<SettingsView>("PUT", "/api/settings/feishu", body),
  putBrowser: (body: { enabled?: boolean; headless?: boolean }) => call<SettingsView>("PUT", "/api/settings/browser", body),
  feishuStatus: () => call<FeishuStatus>("GET", "/api/connectors/feishu"),
  schedules: () => call<Schedule[]>("GET", "/api/schedules"),
  createSchedule: (body: ScheduleBody) => call<Schedule>("POST", "/api/schedules", body),
  patchSchedule: (id: string, body: Partial<Omit<ScheduleBody, "template" | "project_id">> & { enabled?: boolean }) =>
    call<Schedule>("PATCH", `/api/schedules/${id}`, body),
  deleteSchedule: (id: string) => call<{ ok: boolean }>("DELETE", `/api/schedules/${id}`),
  runSchedule: (id: string) => call<{ request_id: string }>("POST", `/api/schedules/${id}/run`),
  searchRecords: (q: string, kind = "all") =>
    call<SearchHit[]>("GET", `/api/search?q=${encodeURIComponent(q)}&kind=${kind}`),
  testSearch: () => call<{ ok: boolean; seconds?: number; results?: number; error?: string }>("POST", "/api/settings/search/test"),
  // summarize: first summarise the branch being left (needs a model call; the new leaf arrives as events)
  checkout: (sid: string, entry_id: string, descend = false, summarize = branchSummary.get()) =>
    call<{ leaf_id: string | null }>("POST", `/api/sessions/${sid}/checkout`, { entry_id, descend, summarize }),
  fork: (sid: string, entry_id: string) => call<SessionInfo>("POST", `/api/sessions/${sid}/fork`, { entry_id }),
  cancel: (sid: string) => call<{ ok: boolean }>("POST", `/api/sessions/${sid}/cancel`),
  compact: (sid: string, instructions?: string) => call<{ ok: boolean }>("POST", `/api/sessions/${sid}/compact`, { instructions }),
  settings: () => call<SettingsView>("GET", "/api/settings"),
  putProvider: (name: string, body: { protocol: Protocol; base_url: string; api_key?: string; key_ref?: string }) =>
    call<SettingsView>("PUT", `/api/settings/providers/${encodeURIComponent(name)}`, body),
  deleteProvider: (name: string) => call<SettingsView>("DELETE", `/api/settings/providers/${encodeURIComponent(name)}`),
  remoteModels: (name: string) => call<RemoteModel[]>("GET", `/api/settings/providers/${encodeURIComponent(name)}/models`),
  putModel: (name: string, body: Omit<ModelView, "name">) =>
    call<SettingsView>("PUT", `/api/settings/models/${encodeURIComponent(name)}`, body),
  deleteModel: (name: string) => call<SettingsView>("DELETE", `/api/settings/models/${encodeURIComponent(name)}`),
  setDefault: (name: string) => call<SettingsView>("PUT", "/api/settings/default-model", { name }),
  testModel: (name: string) => call<ModelTest>("POST", `/api/settings/models/${encodeURIComponent(name)}/test`),
  putMcp: (name: string, body: McpSpec & { secret_keys: string[] }) =>
    call<SettingsView>("PUT", `/api/settings/mcp/${encodeURIComponent(name)}`, body),
  enableMcp: (name: string, enabled: boolean) =>
    call<SettingsView>("PATCH", `/api/settings/mcp/${encodeURIComponent(name)}`, { enabled }),
  deleteMcp: (name: string) => call<SettingsView>("DELETE", `/api/settings/mcp/${encodeURIComponent(name)}`),
  connectMcp: (name: string) => call<SettingsView>("POST", `/api/settings/mcp/${encodeURIComponent(name)}/connect`),
  enableSkill: (name: string, enabled: boolean) =>
    call<SettingsView>("PATCH", `/api/settings/skills/${encodeURIComponent(name)}`, { enabled }),
  setSkillDirs: (dirs: string[]) => call<SettingsView>("PUT", "/api/settings/skill-dirs", { dirs }),
  skills: (projectId: string | null) =>
    call<{ name: string; description: string }[]>("GET", `/api/skills${projectId ? `?project_id=${projectId}` : ""}`),
  usage: (days = 30) => call<Usage>("GET", `/api/usage?days=${days}`),
  system: () => call<SystemInfo>("GET", "/api/system"),
  memory: () => call<MemoryFile[]>("GET", "/api/memory"),
  putMemory: (key: string, text: string) => call<MemoryFile[]>("PUT", `/api/memory/${encodeURIComponent(key)}`, { text }),
  answer: (qid: string, answer: string) => call<{ ok: boolean }>("POST", `/api/questions/${qid}`, { answer }),
  approve: (aid: string, decision: "once" | "always" | "reject", feedback?: string) =>
    call<{ ok: boolean }>("POST", `/api/approvals/${aid}`, { decision, feedback }),
};

// Per-browser preference: summarise the branch you leave when switching branches.
export const branchSummary = {
  get(): boolean {
    try {
      return localStorage.getItem("nwb.branchSummary") === "1";
    } catch {
      return false;
    }
  },
  set(on: boolean): void {
    try {
      localStorage.setItem("nwb.branchSummary", on ? "1" : "0");
    } catch {
      // storage unavailable: the choice lasts until reload
    }
  },
};

// Per-browser preference: follow the system's light/dark setting, or force one.
export type Theme = "system" | "light" | "dark";
export const theme = {
  get(): Theme {
    try {
      return (localStorage.getItem("nwb.theme") as Theme | null) ?? "system";
    } catch {
      return "system";
    }
  },
  set(t: Theme): void {
    try {
      localStorage.setItem("nwb.theme", t);
    } catch {
      // storage unavailable: the choice lasts until reload
    }
    theme.apply(t);
  },
  apply(t: Theme = theme.get()): void {
    if (t === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
  },
};
