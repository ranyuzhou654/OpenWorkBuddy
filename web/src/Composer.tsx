import { useEffect, useState } from "react";
import { api } from "./api";

// Enter sends; Shift+Enter is a newline. While a run is active, a message can join it (插话 / steer)
// or wait for its own run (排队 / queue).
export function Composer({ sid, busy, dropped, onTakeDropped, onError }: {
  sid: string; busy: boolean; dropped: string[]; onTakeDropped: () => void; onError: (m: string) => void;
}) {
  const [text, setText] = useState("");

  useEffect(() => {
    if (dropped.length) {
      setText((t) => [t, ...dropped].filter(Boolean).join("\n"));
      onTakeDropped();
    }
  }, [dropped, onTakeDropped]);

  const send = async (delivery: "auto" | "queue" = "auto") => {
    if (!text.trim()) return;
    const body = text;
    setText("");
    try {
      await api.send(sid, { text: body, delivery });
    } catch (e) {
      setText(body);
      onError(String(e));
    }
  };

  return (
    <div className="composer">
      <textarea
        value={text}
        placeholder={busy ? "运行中：回车插话（下一步前送达），或点“排队”" : "要做什么？（Shift+回车换行）"}
        rows={Math.min(8, Math.max(2, text.split("\n").length))}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            send();
          }
        }}
      />
      <div className="composer-actions">
        {busy && <button onClick={() => send("queue")} disabled={!text.trim()}>排队</button>}
        {busy && <button className="danger" onClick={() => api.cancel(sid).catch((e) => onError(String(e)))}>停止</button>}
        <button className="primary" onClick={() => send()} disabled={!text.trim()}>{busy ? "插话" : "发送"}</button>
      </div>
    </div>
  );
}
