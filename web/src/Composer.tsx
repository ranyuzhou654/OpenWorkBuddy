import { ArrowUp, Loader2, Paperclip, Square, X } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { available, type Command, type Ctx } from "./commands";
import { size } from "./format";
import { useToast } from "./ui";

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
// Tab completes, Enter picks. `children` render as a quiet option row under the box.
// Files can be attached with the clip button, by dropping them on the box or by pasting (screenshots);
// they are uploaded when the message is sent. Children render as the option row under the box.
export function Composer({ projectId, ctx, busy = false, submit, onCancel, dropped = [], onTakeDropped, onError,
  placeholder, children }: {
  projectId: string | null; ctx: Ctx; busy?: boolean;
  submit: (text: string, delivery: "auto" | "queue", files: File[],
    onProgress?: (loaded: number, total: number) => void) => Promise<unknown>;
  onCancel?: () => void; dropped?: string[]; onTakeDropped?: () => void; onError: (m: string) => void;
  placeholder?: string; children?: ReactNode;
}) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const filesRef = useRef<File[]>([]);
  const updateFiles = (next: File[]) => { filesRef.current = next; setFiles(next); };
  const [sending, setSending] = useState(false);
  const [dragDepth, setDragDepth] = useState(0);
  const [dragCount, setDragCount] = useState(0);
  const [progress, setProgress] = useState<number | null>(null);
  const toast = useToast();
  const picker = useRef<HTMLInputElement>(null);
  const area = useRef<HTMLTextAreaElement>(null);
  const [sel, setSel] = useState(0);
  const addFiles = (list: FileList | File[]) => {
    const incoming = [...list].filter((f) => f.size > 0);
    if (!incoming.length) return;
    const big = incoming.filter((f) => f.size > 50 * 1024 * 1024);
    const allowed = incoming.filter((f) => f.size <= 50 * 1024 * 1024);
    if (big.length) onError(`${big[0].name}${big.length > 1 ? ` 等 ${big.length} 个文件` : ""} 超过 50 MB，不能附加`);
    if (!allowed.length) return;
    const fingerprint = (f: File) => `${f.name}\u0000${f.size}\u0000${f.type}`;
    const seen = new Set(filesRef.current.map(fingerprint));
    const fresh: File[] = [];
    for (const f of allowed) {
      const key = fingerprint(f);
      if (seen.has(key)) continue;
      seen.add(key);
      fresh.push(f);
    }
    const duplicate = allowed.length - fresh.length;
    if (fresh.length) updateFiles([...filesRef.current, ...fresh]);
    if (duplicate) toast.info(`已忽略 ${duplicate} 个重复附件`);
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

  useEffect(() => {
    const reset = () => { setDragDepth(0); setDragCount(0); };
    window.addEventListener("drop", reset);
    window.addEventListener("dragend", reset);
    return () => {
      window.removeEventListener("drop", reset);
      window.removeEventListener("dragend", reset);
    };
  }, []);

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
  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(216, el.scrollHeight)}px`;
  }, [text]);

  const send = async (delivery: "auto" | "queue" = "auto") => {
    if ((!text.trim() && !files.length) || sending) return;
    const m = /^\/(\S+)(?:\s+([\s\S]*))?$/.exec(text.trim());
    const command = !files.length && m && commands.find((c) => c.name === m[1]);
    if (command) return run(command, m[2] ?? "");
    const [body, attached] = [text, filesRef.current];
    setText("");
    updateFiles([]);
    setSending(true);
    setProgress(attached.length ? 0 : null);
    try {
      await submit(body, delivery, attached, attached.length
        ? (loaded, total) => setProgress(total ? Math.min(1, loaded / total) : null)
        : undefined);
    } catch (e) {
      setText(body);
      updateFiles(attached);
      onError(String(e));
    } finally {
      setSending(false);
      setProgress(null);
    }
  };
  const ready = (text.trim() || files.length > 0) && !sending;
  const stopping = busy && Boolean(onCancel) && !sending && !text.trim() && !files.length;
  const dragging = dragDepth > 0;
  const pct = progress === null ? null : Math.round(progress * 100);

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
      <div className={`composer-box${dragging ? " dragging" : ""}${busy ? " busy" : ""}`}
        onDragEnter={(e) => {
          if (!e.dataTransfer.types.includes("Files")) return;
          e.preventDefault();
          setDragDepth((depth) => depth + 1);
          setDragCount(e.dataTransfer.items.length);
        }}
        onDragOver={(e) => {
          if (!e.dataTransfer.types.includes("Files")) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
        }}
        onDragLeave={() => setDragDepth((depth) => Math.max(0, depth - 1))}
        onDrop={(e) => {
          if (e.dataTransfer.files.length) {
            e.preventDefault();
            addFiles(e.dataTransfer.files);
          }
          setDragDepth(0);
          setDragCount(0);
        }}>
        {files.length > 0 && (
          <div className="attach-list">
            {files.map((f, i) => (
              <span key={i} className="attach" title={`${f.name} · ${size(f.size)}`}
                style={{ animationDelay: `${Math.min(i, 5) * 30}ms` }}>
                <Paperclip size={12} />{f.name}<i>{size(f.size)}</i>
                <button aria-label={`移除 ${f.name}`} onClick={() => updateFiles(filesRef.current.filter((_, j) => j !== i))}><X size={12} /></button>
              </span>
            ))}
          </div>
        )}
        <textarea
          ref={area}
          value={text}
          placeholder={busy ? "运行中：回车插话（下一步前送达），或点“排队”" : placeholder ?? "回复，或输入 / 查看命令和技能"}
          rows={2}
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
          <span className="grow" />
          {pct !== null && (
            <span className="upload-meter" role="progressbar" aria-label="附件上传进度"
              aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct} title={`上传 ${pct}%`}>
              <i style={{ width: `${pct}%` }} />
            </span>
          )}
          <span className="hint">{sending ? (pct !== null ? `上传中 ${pct}%` : "发送中…")
            : <><kbd>Shift</kbd><span>+</span><kbd>Enter</kbd> 换行</>}</span>
          {busy && !stopping && <button className="queue" onClick={() => send("queue")} disabled={!ready} title="等这次运行结束后再发">排队</button>}
          {busy && onCancel && (text.trim() || files.length > 0) && (
            <button className="danger stop" onClick={onCancel} title="停止这次运行">停止</button>
          )}
          <button className={`send${stopping ? " stopping" : ""}`}
            onClick={sending ? undefined : stopping ? onCancel : () => send()}
            disabled={sending || (!ready && !stopping)}
            title={stopping ? "停止这次运行" : sending ? "发送中…" : busy ? "插话：下一步之前送达" : "发送"}
            aria-label={sending ? "发送中" : stopping ? "停止" : busy ? "插话" : "发送"}>
            {sending ? <Loader2 className="spin" size={16} /> : stopping ? <Square size={13} fill="currentColor" /> : <ArrowUp size={16} strokeWidth={2.4} />}
          </button>
        </div>
        {dragging && (
          <div className="drop-veil" aria-hidden>
            <Paperclip size={17} />
            <b>松开以附加{dragCount > 0 ? ` ${dragCount} 个文件` : "文件"}</b>
          </div>
        )}
      </div>
      {children && <div className="composer-options">{children}</div>}
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
