// Session state on the client: snapshot first, then SSE events folded in by one reducer.
// Durable events ("entry", "checkout", "leaf") rebuild the tree; ephemeral ones update `live`.
// Each subscription starts with a "live" event, so state that changed between the snapshot and the
// subscription (an approval appearing, the run ending) is never missed. A client that falls too far behind
// has its stream closed by the server; onerror then fetches a fresh snapshot, as for any disconnect.

import { useEffect, useReducer } from "react";
import { api, type ContextUsage, type Entry, type Live, type SessionInfo, type Snapshot } from "./api";

export interface State {
  session: SessionInfo | null;
  entries: Record<string, Entry>;
  leaf: string | null;
  live: Live | null;
  connected: boolean;
  error: string | null;
  dropped: string[]; // messages of a cancelled run, handed back to the composer
  usage: Record<string, number> | null;
  context: ContextUsage | null;
}

type Action =
  | { type: "reset" }
  | { type: "snapshot"; snap: Snapshot }
  | { type: "event"; ev: any }
  | { type: "connected"; value: boolean }
  | { type: "error"; message: string | null }
  | { type: "takeDropped" };

const initial: State = {
  session: null, entries: {}, leaf: null, live: null, connected: false, error: null, dropped: [], usage: null,
  context: null,
};

const emptyLive = (): Live => ({ run_id: null, text: "", thinking: "", outputs: {}, steer: [], queue: [], approvals: [], questions: [] });

function onEvent(s: State, ev: any): State {
  const live = s.live ?? emptyLive();
  switch (ev.type) {
    case "entry": {
      const e: Entry = ev.entry;
      const next = { ...s, entries: { ...s.entries, [e.id]: e }, leaf: e.id };
      if (s.live && e.type === "assistant") next.live = { ...s.live, text: "", thinking: "" };
      if (s.live && e.type === "tool_result") {
        const outputs = { ...s.live.outputs };
        delete outputs[e.data.call_id];
        next.live = { ...s.live, outputs };
      }
      return next;
    }
    case "checkout":
    case "leaf":
      return { ...s, leaf: ev.leaf_id };
    case "live": // the active run's state as the subscription starts: replaces what the snapshot said
      return { ...s, live: ev.live };
    case "session":
      return { ...s, session: ev.session, context: ev.context ?? s.context };
    case "context": // before each model request of a run
      return { ...s, context: { tokens: ev.tokens, budget: ev.budget } };
    case "run.started":
      return { ...s, live: { ...live, run_id: ev.run_id }, error: null };
    case "run.finished":
      return { ...s, live: null, usage: ev.usage };
    case "run.error":
    case "hook.error":
      return { ...s, error: ev.message };
    case "assistant.delta":
      return { ...s, live: ev.kind === "text" ? { ...live, text: live.text + ev.text } : { ...live, thinking: live.thinking + ev.text } };
    case "tool.output":
      return { ...s, live: { ...live, outputs: { ...live.outputs, [ev.call_id]: ((live.outputs[ev.call_id] ?? "") + ev.text).slice(-20000) } } };
    case "approval.requested":
      return { ...s, live: { ...live, approvals: [...live.approvals, ev.approval] } };
    case "approval.resolved":
      return s.live ? { ...s, live: { ...s.live, approvals: s.live.approvals.filter((a) => a.id !== ev.id) } } : s;
    case "question.requested":
      return { ...s, live: { ...live, questions: [...live.questions, ev.question] } };
    case "question.resolved":
      return s.live ? { ...s, live: { ...s.live, questions: s.live.questions.filter((q) => q.id !== ev.id) } } : s;
    case "inbox":
      return { ...s, live: s.live && { ...s.live, steer: ev.steer, queue: ev.queue }, dropped: ev.dropped ?? s.dropped };
    default:
      return s;
  }
}

function reducer(s: State, a: Action): State {
  switch (a.type) {
    case "reset":
      return initial;
    case "snapshot":
      return {
        ...s,
        session: a.snap.session,
        entries: Object.fromEntries(a.snap.entries.map((e) => [e.id, e])),
        leaf: a.snap.leaf_id,
        live: a.snap.live,
        context: a.snap.context,
      };
    case "event":
      return onEvent(s, a.ev);
    case "connected":
      return { ...s, connected: a.value };
    case "error":
      return { ...s, error: a.message };
    case "takeDropped":
      return { ...s, dropped: [] };
  }
}

export function useSession(sid: string | null) {
  const [state, dispatch] = useReducer(reducer, initial);
  useEffect(() => {
    dispatch({ type: "reset" });
    if (!sid) return;
    let es: EventSource | null = null;
    let closed = false;
    let retry: number | undefined;
    // On any disconnect: fetch a fresh snapshot, then subscribe after its seq. Simple and always consistent.
    const connect = async () => {
      try {
        const snap = await api.snapshot(sid);
        if (closed) return;
        dispatch({ type: "snapshot", snap });
        es = new EventSource(`/api/sessions/${sid}/events?after=${snap.seq}`);
        es.onopen = () => dispatch({ type: "connected", value: true });
        es.onmessage = (m) => dispatch({ type: "event", ev: JSON.parse(m.data) });
        es.onerror = () => {
          es?.close();
          dispatch({ type: "connected", value: false });
          if (!closed) retry = window.setTimeout(connect, 1500);
        };
      } catch (e) {
        dispatch({ type: "error", message: String(e) });
        if (!closed) retry = window.setTimeout(connect, 3000);
      }
    };
    connect();
    return () => {
      closed = true;
      es?.close();
      window.clearTimeout(retry);
    };
  }, [sid]);
  return [state, dispatch] as const;
}

// --- derived views of the tree ---

export function pathTo(entries: Record<string, Entry>, leaf: string | null): Entry[] {
  const out: Entry[] = [];
  for (let id = leaf; id; id = entries[id]?.parent_id ?? null) {
    if (!entries[id]) break;
    out.push(entries[id]);
  }
  return out.reverse();
}

export function childrenOf(entries: Record<string, Entry>): Record<string, Entry[]> {
  const kids: Record<string, Entry[]> = {};
  for (const e of Object.values(entries)) (kids[e.parent_id ?? "root"] ??= []).push(e);
  for (const list of Object.values(kids)) list.sort((a, b) => a.seq - b.seq);
  return kids;
}

export function textOf(content: { type: string; text?: string }[]): string {
  return content.filter((b) => b.type === "text").map((b) => b.text).join("");
}
