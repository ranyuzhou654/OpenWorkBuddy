import { CircleAlert, CircleCheck, Download, FileText, FolderOpen, Globe, Image as ImageIcon, Paperclip, X } from "lucide-react";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type Artifact, type Attachment, type Source } from "./api";
import { artifactsOf, pathTo, sourcesOf, type State, uploadsOf } from "./session";

// Files and evidence in a session: attachment chips on user messages, a card for each delivered file,
// a preview window, and the 资料 panel (sources, delivered files, attachments).

type Viewable = { sha256: string; name: string; mime: string; kept?: boolean };

export function size(n: number): string {
  return n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const DOC = /\.(docx|pptx|xlsx|xlsm)$/i;
const canPreview = (f: Viewable) => f.mime === "application/pdf" || f.mime.startsWith("image/") ||
  f.mime.startsWith("text/") || f.mime === "application/json" || DOC.test(f.name);

export function AttachmentChips({ sid, files }: { sid: string; files: Attachment[] }) {
  const [open, setOpen] = useState<Attachment | null>(null);
  return (
    <div className="attach-list">
      {files.map((a) => (
        <button key={a.id} className="attach" title={`${a.name}${a.desc ? ` · ${a.desc}` : ""} · ${size(a.size)}`}
          onClick={() => (canPreview(a) ? setOpen(a) : window.open(api.fileUrl(sid, a.sha256, a.name, true)))}>
          {a.kind === "image" ? <ImageIcon size={12} /> : <Paperclip size={12} />}{a.name}
          <i>{a.desc || size(a.size)}</i>
        </button>
      ))}
      {open && <Preview sid={sid} file={open} onClose={() => setOpen(null)} />}
    </div>
  );
}

export function ArtifactCard({ sid, a }: { sid: string; a: Artifact }) {
  const [open, setOpen] = useState(false);
  const [checks, setChecks] = useState(false);
  const failed = a.checks.filter((c) => !c.ok);
  const desc = (a.info.description as string | undefined) ?? a.mime;
  return (
    <div className={`artifact${failed.length ? " warn" : ""}`}>
      <div className="artifact-head">
        <span className="artifact-icon"><FileText size={18} strokeWidth={1.6} /></span>
        <div className="artifact-name">
          <b>{a.title || a.name}</b>
          <span>{a.title ? `${a.name} · ` : ""}{desc} · {size(a.size)}{a.version > 1 ? ` · 第 ${a.version} 版` : ""}</span>
        </div>
        <button className={`checks ${failed.length ? "bad" : "good"}`} onClick={() => setChecks(!checks)}
          title="交付前重新打开文件做的检查">
          {failed.length ? <><CircleAlert size={13} />{failed.length} 个问题</> : <><CircleCheck size={13} />已检查</>}
        </button>
      </div>
      {checks && (
        <ul className="check-list">
          {a.checks.map((c, i) => <li key={i} className={c.ok ? "ok" : "bad"}>{c.ok ? "✓" : "✗"} {c.name}{c.detail ? `：${c.detail}` : ""}</li>)}
        </ul>
      )}
      <div className="row">
        {a.kept && canPreview(a) && <button onClick={() => setOpen(true)}>预览</button>}
        {a.kept && <a className="button" href={api.fileUrl(sid, a.sha256, a.name, true)}><Download size={13} />下载这一版</a>}
        <button onClick={() => api.reveal(sid, a.path).catch((e) => alert(String(e)))} title={a.path}>
          <FolderOpen size={13} />在访达中显示
        </button>
      </div>
      {open && <Preview sid={sid} file={a} onClose={() => setOpen(false)} />}
    </div>
  );
}

// PDFs and images as the browser shows them; Markdown and text rendered; Office files as their text.
export function Preview({ sid, file, onClose }: { sid: string; file: Viewable; onClose: () => void }) {
  const url = api.fileUrl(sid, file.sha256, file.name);
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const kind = file.mime === "application/pdf" ? "pdf" : file.mime.startsWith("image/") ? "image"
    : DOC.test(file.name) ? "doc" : "text";
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  useEffect(() => {
    if (kind === "doc")
      api.fileText(sid, file.sha256, file.name).then((r) => setText([...r.notes.map((n) => `> ${n}`), r.text].join("\n\n")))
        .catch((e) => setError(String(e)));
    else if (kind === "text")
      fetch(url).then((r) => r.text()).then(setText).catch((e) => setError(String(e)));
  }, [kind, sid, file.sha256, file.name, url]);
  return (
    <div className="preview-backdrop" onClick={onClose}>
      <div className="preview" onClick={(e) => e.stopPropagation()}>
        <div className="preview-head">
          <b>{file.name}</b>
          <span className="grow" />
          <a className="button" href={api.fileUrl(sid, file.sha256, file.name, true)}><Download size={13} />下载</a>
          <button className="ghost icon" onClick={onClose} aria-label="关闭"><X size={16} /></button>
        </div>
        <div className="preview-body">
          {error ? <div className="note">{error}</div>
            : kind === "pdf" ? <iframe src={url} title={file.name} />
            : kind === "image" ? <img src={url} alt={file.name} />
            : text === null ? <div className="note">加载中…</div>
            : /\.(md|markdown|docx|pptx|xlsx|xlsm)$/i.test(file.name)
              ? <div className="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown></div>
              : <pre>{text}</pre>}
        </div>
      </div>
    </div>
  );
}

const KIND_LABEL: Record<Source["kind"], string> = { search: "摘要", page: "已读全文", file: "文件" };

// The 资料 panel: what the answers rest on and what was delivered, for the whole session.
export function Library({ sid, state }: { sid: string; state: State }) {
  const [tab, setTab] = useState<"sources" | "files">("sources");
  const sources = sourcesOf(state.entries);
  const path = pathTo(state.entries, state.leaf);
  const delivered = artifactsOf(path);
  const uploads = uploadsOf(path);
  return (
    <aside className="library">
      <div className="library-tabs">
        <button className={tab === "sources" ? "on" : ""} onClick={() => setTab("sources")}>来源 {sources.length || ""}</button>
        <button className={tab === "files" ? "on" : ""} onClick={() => setTab("files")}>文件 {delivered.length + uploads.length || ""}</button>
      </div>
      {tab === "sources" ? (
        sources.length ? sources.map((s) => (
          <div key={s.n} className="source" id={`source-${s.n}`}>
            <div className="source-head">
              <span className="cite-n">S{s.n}</span>
              <a href={s.url} target="_blank" rel="noreferrer" title={s.url}>{s.title || s.url}</a>
            </div>
            <div className="source-meta">
              <span className={`tag ${s.kind}`}>{KIND_LABEL[s.kind]}</span>
              <Globe size={11} />{new URL(s.url).hostname}
              {s.published ? ` · 发布 ${s.published.slice(0, 10)}` : ""}
            </div>
            {s.snippet && <div className="source-snippet">{s.snippet}</div>}
          </div>
        )) : <div className="note pad">还没有来源。助手搜索或读取网页后，来源会编号列在这里，回答里的 [S1] 可以点开。</div>
      ) : (
        <>
          {delivered.length > 0 && <div className="library-label">交付的文件</div>}
          {delivered.map((versions) => <ArtifactCard key={versions[0].path} sid={sid} a={versions[0]} />)}
          {uploads.length > 0 && <div className="library-label">附件</div>}
          {uploads.length > 0 && <AttachmentChips sid={sid} files={uploads} />}
          {!delivered.length && !uploads.length && <div className="note pad">还没有文件。你附加的文件和助手交付的文档会列在这里。</div>}
        </>
      )}
    </aside>
  );
}
