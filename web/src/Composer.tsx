import { type ReactNode, useEffect, useState } from "react";
import { api } from "./api";

// Enter sends; Shift+Enter is a newline. While a run is active, a message can join it (插话 / steer)
// or wait for its own run (排队 / queue). `children` go left of the buttons (the new-chat page's pickers).
export function Composer({ projectId, busy = false, submit, onCancel, dropped = [], onTakeDropped, onError,
  placeholder, children }: {
  projectId: string | null; busy?: boolean; submit: (text: string, delivery: "auto" | "queue") => Promise<unknown>;
  onCancel?: () => void; dropped?: string[]; onTakeDropped?: () => void; onError: (m: string) => void;
  placeholder?: string; children?: ReactNode;
}) {
  const [text, setText] = useState("");
  const [skills, setSkills] = useState<{ name: string; description: string }[]>([]);

  useEffect(() => {
    api.skills(projectId).then(setSkills).catch(() => setSkills([]));
  }, [projectId]);

  // "/na" at the start of an empty-ish message suggests skills; picking one loads it for this message (D15)
  const slash = busy ? null : /^\/(\S*)$/.exec(text);
  const matches = slash ? skills.filter((s) => s.name.startsWith(slash[1])).slice(0, 8) : [];

  useEffect(() => {
    if (dropped.length) {
      setText((t) => [t, ...dropped].filter(Boolean).join("\n"));
      onTakeDropped?.();
    }
  }, [dropped, onTakeDropped]);

  const send = async (delivery: "auto" | "queue" = "auto") => {
    if (!text.trim()) return;
    const body = text;
    setText("");
    try {
      await submit(body, delivery);
    } catch (e) {
      setText(body);
      onError(String(e));
    }
  };

  return (
    <div className="composer">
      {matches.length > 0 && (
        <div className="slash">
          {matches.map((s) => (
            <button key={s.name} onClick={() => setText(`/${s.name} `)} title={s.description}>
              <b>/{s.name}</b> <span>{s.description}</span>
            </button>
          ))}
        </div>
      )}
      <textarea
        value={text}
        placeholder={busy ? "运行中：回车插话（下一步前送达），或点“排队”" : placeholder ?? "要做什么？（Shift+回车换行，/ 调用技能）"}
        rows={Math.min(8, Math.max(2, text.split("\n").length))}
        autoFocus
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Tab" && matches.length) {
            e.preventDefault();
            setText(`/${matches[0].name} `);
            return;
          }
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            send();
          }
        }}
      />
      <div className="composer-actions">
        {children}
        <span className="grow" />
        {busy && <button onClick={() => send("queue")} disabled={!text.trim()}>排队</button>}
        {busy && onCancel && <button className="danger" onClick={onCancel}>停止</button>}
        <button className="primary" onClick={() => send()} disabled={!text.trim()}>{busy ? "插话" : "发送"}</button>
      </div>
    </div>
  );
}
