// Types mirror the backend's wire format; requests are thin wrappers over fetch.

export type Block =
  | { type: "text"; text: string }
  | { type: "image"; mime: string; data: string }
  | { type: "thinking"; text: string; provider: string }
  | { type: "tool_call"; id: string; name: string; args: Record<string, unknown>; error?: string };

export interface Entry {
  id: string;
  parent_id: string | null;
  type: string; // system_prompt | user | assistant | tool_started | tool_result | state
  data: any;
  run_id?: string;
  ts: number;
  seq: number;
}

export interface Settings {
  mode: "confirm" | "trust";
  allow: string[];
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
}

export interface Info {
  models: string[];
  default_model: string;
  modes: Record<string, string>;
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
  patch: (sid: string, body: { model?: string; mode?: string }) => call<SessionInfo>("PATCH", `/api/sessions/${sid}`, body),
  send: (sid: string, body: { text: string; parent_id?: string | null; delivery?: "auto" | "steer" | "queue" }) =>
    call<{ delivery: string }>("POST", `/api/sessions/${sid}/messages`, { request_id: crypto.randomUUID(), ...body }),
  checkout: (sid: string, entry_id: string, descend = false) =>
    call<{ leaf_id: string }>("POST", `/api/sessions/${sid}/checkout`, { entry_id, descend }),
  fork: (sid: string, entry_id: string) => call<SessionInfo>("POST", `/api/sessions/${sid}/fork`, { entry_id }),
  cancel: (sid: string) => call<{ ok: boolean }>("POST", `/api/sessions/${sid}/cancel`),
  approve: (aid: string, decision: "once" | "always" | "reject", feedback?: string) =>
    call<{ ok: boolean }>("POST", `/api/approvals/${aid}`, { decision, feedback }),
};
