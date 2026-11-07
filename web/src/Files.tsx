import { CircleAlert, CircleCheck, Download, FileText, FolderOpen, Globe, Image as ImageIcon, Paperclip } from "lucide-react";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type Artifact, type Attachment } from "./api";
import { size } from "./format";
import { artifactsOf, pathTo, sourcesOf, type State, uploadsOf } from "./session";
import { Dialog, Skeleton, useToast } from "./ui";

// Files and evidence in a session: attachment chips on user messages, a card for each delivered file,
// a preview window, and the 资料 panel (sources, delivered files, attachments).

type Viewable = { sha256: string; name: string; mime: string; kept?: boolean };

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
  const toast = useToast();
  const failed = a.checks.filter((c) => c.ok === false);
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
          title="分别查看结构、引用、数字和人工验收状态">
          {failed.length ? <><CircleAlert size={13} />{failed.length} 个问题</> : <><CircleCheck size={13} />查看校验层次</>}
        </button>
      </div>
      {checks && (
        <div className="check-list">
          {Object.entries({ structure: "文件与结构", citations: "引用与来源", numbers: "数字与计算", human: "人工版式与事实验收" }).map(([layer, label]) => (
            <section key={layer}><b>{label}</b><ul>
              {a.checks.filter((c) => (c.layer ?? "structure") === layer).map((c, i) => (
                <li key={i} className={c.ok === null ? "note" : c.ok ? "ok" : "bad"}>
                  {c.ok === null ? "待核验" : c.ok ? "✓" : "✗"} {c.name}{c.detail ? `：${c.detail}` : ""}
                </li>))}
              {!a.checks.some((c) => (c.layer ?? "structure") === layer) && <li className="note">尚未评估</li>}
            </ul></section>))}
        </div>
      )}
      <div className="row">
        {a.kept && canPreview(a) && <button onClick={() => setOpen(true)}>预览</button>}
        {a.kept && <a className="button" href={api.fileUrl(sid, a.sha256, a.name, true)}><Download size={13} />下载这一版</a>}
        <button onClick={() => api.reveal(sid, a.path).catch((e) => toast.error(String(e)))} title={a.path}>
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
    setText(null);
    setError(null);
    if (kind === "doc")
      api.fileText(sid, file.sha256, file.name).then((r) => setText([...r.notes.map((n) => `> ${n}`), r.text].join("\n\n")))
        .catch((e) => setError(String(e)));
    else if (kind === "text")
      fetch(url).then((r) => r.text()).then(setText).catch((e) => setError(String(e)));
  }, [kind, sid, file.sha256, file.name, url]);
  return (
    <Dialog open onClose={onClose} title={file.name} size="xl" className="preview-dialog"
      footer={<a className="button" href={api.fileUrl(sid, file.sha256, file.name, true)}><Download size={13} />下载</a>}>
      <div className="preview-body">
        {error ? <div className="note">{error}</div>
          : kind === "pdf" ? <iframe src={url} title={file.name} />
          : kind === "image" ? <img src={url} alt={file.name} />
          : text === null ? <div className="preview-loading"><Skeleton lines={5} /></div>
          : /\.(md|markdown|docx|pptx|xlsx|xlsm)$/i.test(file.name)
            ? <div className="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown></div>
            : <pre>{text}</pre>}
      </div>
    </Dialog>
  );
}

const STATE_LABEL = { discovered: "已发现（摘要）", fetched: "已获取", extracted: "已提取", inspected: "片段已进入上下文" };

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
              <span className={`tag ${s.kind}`}>{s.state ? STATE_LABEL[s.state] : "历史状态未核验"}</span>
              <Globe size={11} />{s.url.startsWith("http") ? new URL(s.url).hostname : "本地文件"}
              {s.published ? ` · 发布 ${s.published.slice(0, 10)}` : ""}
            </div>
            {s.snippet && <div className="source-snippet">{s.snippet}</div>}
            <div className="note">访问 {s.retrieved_at || "未知"} · {s.access_scope || "session"}
              {s.sha256 && <span title={s.sha256}> · SHA256 {s.sha256.slice(0, 12)}</span>}</div>
            {s.inspections?.map((x, i) => <div key={i} className="note">{x.locator}{x.partial_output ? "（输出截断，仅首尾片段）" : ""}</div>)}
            {s.snapshot && <a href={api.fileUrl(sid, s.snapshot, `source-${s.n}.bin`, true)}>下载来源快照</a>}
          </div>
        )) : <div className="note pad">还没有来源。来源的读取状态不代表事实已经核验。</div>
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
