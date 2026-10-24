import { Paperclip, X } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { available, type Command, type Ctx } from "./commands";
import { size } from "./Files";

interface Suggestion {
  key: string;
  group: "命令" | "技能" | "选项";
  label: string;
  hint?: string;
  description: string;
  fill: string; // Tab puts this into the box
  pick: () => void; // Enter or a click
}

// Enter sends; Shift+Enter is a newline. While a run is active, a message can join it (插话 / steer)
// or wait for its own run (排队 / queue). "/" at the start opens commands and skills (D15); ↑↓ choose,
// Tab completes, Enter picks. `children` go left of the buttons (the new-chat page's pickers).
// Files can be attached with the clip button, by dropping them on the box or by pasting (screenshots);
// they are uploaded when the message is sent.
export function Composer({ projectId, ctx, busy = false, submit, onCancel, dropped = [], onTakeDropped, onError,
  placeholder, children }: {
  projectId: string | null; ctx: Ctx; busy?: boolean;
  submit: (text: string, delivery: "auto" | "queue", files: File[]) => Promise<unknown>;
  onCancel?: () => void; dropped?: string[]; onTakeDropped?: () => void; onError: (m: string) => void;
  placeholder?: string; children?: ReactNode;
}) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [sending, setSending] = useState(false);
  const [dragging, setDragging] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  const [sel, setSel] = useState(0);
  const addFiles = (list: FileList | File[]) => {
    const add = [...list].filter((f) => f.size > 0);
    const big = add.find((f) => f.size > 50 * 1024 * 1024);
    if (big) return onError(`${big.name} 超过 50 MB，不能附加`);
    setFiles((cur) => [...cur, ...add]);
  };
  const [skills, setSkills] = useState<{ name: string; description: string }[]>([]);

  useEffect(() => {
    api.skills(projectId).then(setSkills).catch(() => setSkills([]));
  }, [projectId]);

  useEffect(() => {
    if (dropped.length) {
      setText((t) => [t, ...dropped].filter(Boolean).join("\n"));
      onTakeDropped?.();
    }
  }, [dropped, onTakeDropped]);

  const commands = available(ctx);
  const run = async (c: Command, arg: string) => {
    setText("");
    try {
      const back = await c.run(ctx, arg.trim());
      if (typeof back === "string") setText(back);
    } catch (e) {
      onError(String(e));
    }
  };
  const suggestions = suggest(text, ctx, commands, busy ? [] : skills, run, setText);
  useEffect(() => setSel(0), [text]);

  const send = async (delivery: "auto" | "queue" = "auto") => {
    if ((!text.trim() && !files.length) || sending) return;
    const m = /^\/(\S+)(?:\s+([\s\S]*))?$/.exec(text.trim());
    const command = !files.length && m && commands.find((c) => c.name === m[1]);
    if (command) return run(command, m[2] ?? "");
    const [body, attached] = [text, files];
    setText("");
    setFiles([]);
    setSending(true);
    try {
      await submit(body, delivery, attached);
    } catch (e) {
      setText(body);
      setFiles(attached);
      onError(String(e));
    } finally {
      setSending(false);
    }
  };
  const ready = (text.trim() || files.length > 0) && !sending;

  return (
    <div className="composer">
      {suggestions.length > 0 && (
        <div className="slash" role="listbox">
          {suggestions.map((s, i) => (
            <div key={s.key}>
              {(i === 0 || suggestions[i - 1].group !== s.group) && <div className="slash-group">{s.group}</div>}
              <button className={i === sel ? "on" : ""} role="option" aria-selected={i === sel}
                ref={i === sel ? (el) => el?.scrollIntoView({ block: "nearest" }) : undefined}
                onMouseEnter={() => setSel(i)} onMouseDown={(e) => e.preventDefault()} onClick={s.pick}>
                <b>{s.label}</b>{s.hint && <i>{s.hint}</i>}<span>{s.description}</span>
              </button>
            </div>
          ))}
        </div>
      )}
      <div className={`composer-box${dragging ? " dragging" : ""}`}
        onDragOver={(e) => { if (e.dataTransfer.types.includes("Files")) { e.preventDefault(); setDragging(true); } }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => { if (e.dataTransfer.files.length) { e.preventDefault(); addFiles(e.dataTransfer.files); } setDragging(false); }}>
        {files.length > 0 && (
          <div className="attach-list">
            {files.map((f, i) => (
              <span key={i} className="attach" title={`${f.name} · ${size(f.size)}`}>
                <Paperclip size={12} />{f.name}<i>{size(f.size)}</i>
                <button aria-label={`移除 ${f.name}`} onClick={() => setFiles(files.filter((_, j) => j !== i))}><X size={12} /></button>
              </span>
            ))}
          </div>
        )}
        <textarea
          value={text}
          placeholder={busy ? "运行中：回车插话（下一步前送达），或点“排队”" : placeholder ?? "回复，或输入 / 查看命令和技能"}
          rows={Math.min(10, Math.max(2, text.split("\n").length))}
          autoFocus
          onChange={(e) => setText(e.target.value)}
          onPaste={(e) => { if (e.clipboardData.files.length) { e.preventDefault(); addFiles(e.clipboardData.files); } }}
          onKeyDown={(e) => {
            if (suggestions.length && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
              e.preventDefault();
              setSel((sel + (e.key === "ArrowDown" ? 1 : suggestions.length - 1)) % suggestions.length);
              return;
            }
            if (suggestions.length && e.key === "Tab") {
              e.preventDefault();
              setText(suggestions[sel].fill);
              return;
            }
            if (suggestions.length && e.key === "Escape") {
              setText("");
              return;
            }
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              if (suggestions.length) suggestions[sel].pick();
              else send();
            }
          }}
        />
        <div className="composer-actions">
          <input ref={picker} type="file" multiple hidden onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ""; }} />
          <button className="ghost icon" title="附加文件（也可以拖进来或粘贴截图）" aria-label="附加文件"
            onClick={() => picker.current?.click()}><Paperclip size={15} /></button>
          {children}
          <span className="grow" />
          <span className="hint">{sending ? (files.length ? "上传中…" : "发送中…") : "Shift+回车换行"}</span>
          {busy && <button onClick={() => send("queue")} disabled={!ready} title="等这次运行结束后再发">排队</button>}
          {busy && onCancel && <button className="danger" onClick={onCancel}>停止</button>}
          <button className="send" onClick={() => send()} disabled={!ready} title={busy ? "插话：下一步之前送达" : "发送"}
            aria-label={busy ? "插话" : "发送"}>↑</button>
        </div>
      </div>
    </div>
  );
}

// "/na" lists commands and skills starting with "na"; "/model d" lists that command's options starting with "d".
function suggest(text: string, ctx: Ctx, commands: Command[], skills: { name: string; description: string }[],
  run: (c: Command, arg: string) => void, setText: (t: string) => void): Suggestion[] {
  const name = /^\/(\S*)$/.exec(text);
  if (name) {
    const q = name[1].toLowerCase();
    const cmds = commands.filter((c) => c.name.startsWith(q)).map((c): Suggestion => ({
      key: `c:${c.name}`, group: "命令", label: `/${c.name}`, hint: c.args, description: c.description,
      fill: `/${c.name} `,
      // a command whose argument is required or chosen from a list waits for it; the others run now
      pick: () => (c.options || c.args?.startsWith("<") ? setText(`/${c.name} `) : run(c, "")),
    }));
    const taken = new Set(commands.map((c) => c.name));
    const sks = skills.filter((s) => s.name.toLowerCase().startsWith(q) && !taken.has(s.name)).map((s): Suggestion => ({
      key: `s:${s.name}`, group: "技能", label: `/${s.name}`, description: s.description, fill: `/${s.name} `,
      pick: () => setText(`/${s.name} `),
    }));
    return [...cmds, ...sks];
  }
  const arg = /^\/(\S+) (\S*)$/.exec(text);
  const command = arg && commands.find((c) => c.name === arg[1]);
  if (!arg || !command?.options) return [];
  const q = arg[2].toLowerCase();
  return command.options(ctx).filter((o) => `${o.value} ${o.label ?? ""}`.toLowerCase().includes(q)).map((o): Suggestion => ({
    key: `o:${o.value}`, group: "选项", label: o.value, description: o.label ?? "", fill: `/${command.name} ${o.value}`,
    pick: () => run(command, o.value),
  }));
}
