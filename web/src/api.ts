// Types mirror the backend's wire format; requests are thin wrappers over fetch.

export type Block =
  | { type: "text"; text: string }
  | { type: "image"; mime: string; data: string }
  | { type: "thinking"; text: string; provider: string }
  | { type: "tool_call"; id: string; name: string; args: Record<string, unknown>; error?: string };

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
}

export interface Project {
  id: string;
  name: string;
  root_path: string;
  sessions: SessionInfo[];
}

export interface Approval {
  id: string;
  call_id: string;
  tool: string;
  args: Record<string, any>;
  action: string; // e.g. "write", "bash.unsandboxed"
  resource: string; // the path or command the permission check saw
  always: { action: string; resource: string }; // the rule "always allow" would add
}

export interface Live {
  run_id: string | null;
  text: string;
  thinking: string;
  outputs: Record<string, string>;
  steer: string[];
  queue: string[];
  approvals: Approval[];
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
  addProject: (path: string) => call<Project>("POST", "/api/projects", { path }),
  newSession: (projectId: string) => call<SessionInfo>("POST", `/api/projects/${projectId}/sessions`, {}),
  snapshot: (sid: string) => call<Snapshot>("GET", `/api/sessions/${sid}`),
  patch: (sid: string, body: { model?: string; preset?: string }) => call<SessionInfo>("PATCH", `/api/sessions/${sid}`, body),
  send: (sid: string, body: { text: string; parent_id?: string | null; delivery?: "auto" | "steer" | "queue" }) =>
    call<{ delivery: string }>("POST", `/api/sessions/${sid}/messages`, { request_id: crypto.randomUUID(), ...body }),
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
  sessionSkills: (sid: string) => call<{ name: string; description: string }[]>("GET", `/api/sessions/${sid}/skills`),
  memory: () => call<MemoryFile[]>("GET", "/api/memory"),
  putMemory: (key: string, text: string) => call<MemoryFile[]>("PUT", `/api/memory/${encodeURIComponent(key)}`, { text }),
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
