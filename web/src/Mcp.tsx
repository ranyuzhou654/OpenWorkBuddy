import { useState } from "react";
import { api, type Exposure, type KeyStatus, type McpSpec, type McpView, type SettingsView } from "./api";

// MCP servers live in [mcp.NAME] of config.toml and are shared by all projects. stdio servers run in
// the sandbox: they may use the network, but write only to a temp dir and the paths listed here.

const TEMPLATES: { label: string; name: string; spec: Partial<McpSpec> }[] = [
  { label: "文件系统（npx）", name: "filesystem", spec: {
    command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "~/Documents"],
    writable: ["~/.npm", "~/Documents"] } },
  { label: "网页抓取 Fetch（uvx）", name: "fetch", spec: {
    command: "uvx", args: ["mcp-server-fetch"], writable: ["~/.cache/uv", "~/.local/share/uv"] } },
  { label: "Context7 文档（远程）", name: "context7", spec: { url: "https://mcp.context7.com/mcp" } },
  { label: "GitHub（远程，需令牌）", name: "github", spec: {
    url: "https://api.githubcopilot.com/mcp/", headers: { Authorization: "Bearer 你的令牌" } } },
];

const EXPOSURES: Record<Exposure, string> = {
  auto: "自动（工具多时延迟加载）", direct: "直接声明", deferred: "延迟加载（tool_search）",
};

const EMPTY: McpSpec = {
  command: "", args: [], env: {}, url: "", headers: {}, enabled: true, exposure: "auto", sandbox: true,
  network: true, writable: [], timeout: 300, description: "",
};

const isRef = (v: string) => /^(env|keyring|secret):.+$/.test(v);

export function McpSection({ view, save, onError }: {
  view: SettingsView;
  save: (p: Promise<SettingsView>) => Promise<boolean>;
  onError: (m: string) => void;
}) {
  const [editing, setEditing] = useState<McpView | "new" | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});

  const connect = async (name: string) => {
    setBusy(name);
    try {
      await save(api.connectMcp(name));
      setOpen((o) => ({ ...o, [name]: true }));
    } finally {
      setBusy(null);
    }
  };

  const form = (initial: McpView | null) => (
    <McpForm key={initial?.name ?? "new"} initial={initial} onCancel={() => setEditing(null)} onError={onError}
      onSave={async (name, body) => (await save(api.putMcp(name, body))) && setEditing(null)} />
  );

  return (
    <>
      <h2>连接器（MCP 服务器）</h2>
      <p className="note">
        所有项目共用。每次运行开始时连接已启用的服务器，关掉的服务器从下一次运行起不再出现。MCP 工具和内置工具走同一套权限：
        默认每次调用都要你确认，可以在审批时选“本会话总是允许此工具”。本机（stdio）服务器默认在沙箱里运行：能联网，但只能写临时目录和下面列出的路径。
      </p>
      {view.mcp.map((m) =>
        editing !== "new" && editing?.name === m.name ? form(m) : (
          <div key={m.name} className="card">
            <div className="card-head">
              <label className="check"><input type="checkbox" checked={m.enabled}
                onChange={(e) => save(api.enableMcp(m.name, e.target.checked))} /></label>
              <b>{m.name}</b>
              <span className="tag">{m.url ? "远程 HTTP" : "本机 stdio"}</span>
              <Status m={m} busy={busy === m.name} />
              {!m.url && !m.sandbox && <span className="key missing">无沙箱</span>}
              <span className="note">{EXPOSURES[m.exposure]}</span>
            </div>
            <code className="cmd">{m.url || [m.command, ...m.args].join(" ")}</code>
            {Object.entries(m.keys).map(([k, st]) => <KeyLine key={k} name={k} k={st} />)}
            <div className="row">
              <button onClick={() => connect(m.name)} disabled={!m.enabled || busy === m.name}>
                {m.status === "connected" ? "重新连接" : "连接测试"}
              </button>
              {m.tools.length > 0 && (
                <button onClick={() => setOpen((o) => ({ ...o, [m.name]: !o[m.name] }))}>
                  {open[m.name] ? "收起工具" : `查看 ${m.tools.length} 个工具`}
                </button>
              )}
              <button onClick={() => setEditing(m)}>编辑</button>
              <button className="danger" onClick={() => confirm(`删除 MCP 服务器 ${m.name}？它保存的密钥也会删除。`) && save(api.deleteMcp(m.name))}>删除</button>
            </div>
            {m.status === "error" && <div className="note warn-line">{m.error}</div>}
            {open[m.name] && (
              <ul className="mcp-tools">
                {m.tools.map((t) => <li key={t.name}><code>{t.name}</code> {t.description}</li>)}
              </ul>
            )}
          </div>
        ),
      )}
      {editing === "new" ? form(null) : (
        <button className="link" onClick={() => setEditing("new")}>＋ 添加 MCP 服务器</button>
      )}
    </>
  );
}

function Status({ m, busy }: { m: McpView; busy: boolean }) {
  if (busy) return <span className="note">连接中…</span>;
  switch (m.status) {
    case "connected": return <span className="key ok">✓ 已连接 · {m.tools.length} 个工具</span>;
    case "error": return <span className="key missing">✗ 连接失败</span>;
    case "disabled": return <span className="note">已停用</span>;
    default: return <span className="note">未启动（下次运行时连接）</span>;
  }
}

function KeyLine({ name, k }: { name: string; k: KeyStatus }) {
  return (
    <div className="note">
      {name}：{k.set ? <span className="key ok">✓ {k.hint}</span> : <span className="key missing">未设置</span>}
      {" "}<span>（{k.source}:{k.name}）</span>
    </div>
  );
}

// KEY=VALUE lines <-> object (headers use "Name: value")
function toLines(o: Record<string, string>, sep: string): string {
  return Object.entries(o).map(([k, v]) => `${k}${sep}${v}`).join("\n");
}
function fromLines(s: string, sep: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of s.split("\n")) {
    const i = line.indexOf(sep);
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + sep.length).trim();
  }
  return out;
}
const list = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);

function McpForm({ initial, onSave, onCancel, onError }: {
  initial: McpView | null;
  onSave: (name: string, body: McpSpec & { secret_keys: string[] }) => void;
  onCancel: () => void;
  onError: (m: string) => void;
}) {
  const start = initial ?? EMPTY;
  const [name, setName] = useState(initial?.name ?? "");
  const [remote, setRemote] = useState(!!start.url);
  const [spec, setSpec] = useState<McpSpec>({ ...EMPTY, ...start });
  const [args, setArgs] = useState(start.args.join("\n"));
  const [env, setEnv] = useState(toLines(start.env, "="));
  const [headers, setHeaders] = useState(toLines(start.headers, ": "));
  const [writable, setWritable] = useState(start.writable.join("\n"));
  const [asSecrets, setAsSecrets] = useState(true);
  const set = <K extends keyof McpSpec>(k: K, v: McpSpec[K]) => setSpec({ ...spec, [k]: v });

  const pick = (i: number) => {
    const t = TEMPLATES[i];
    if (!t) return;
    const s = { ...EMPTY, ...t.spec };
    setName(t.name); setRemote(!!s.url); setSpec(s);
    setArgs(s.args.join("\n")); setEnv(toLines(s.env, "=")); setHeaders(toLines(s.headers, ": "));
    setWritable(s.writable.join("\n"));
  };

  const submit = () => {
    const e = remote ? {} : fromLines(env, "=");
    const h = remote ? fromLines(headers, ":") : {};
    if (Object.values(h).some((v) => v.includes("你的令牌"))) return onError("请先把请求头里的“你的令牌”换成真实令牌");
    const body: McpSpec = {
      ...spec, command: remote ? "" : spec.command, url: remote ? spec.url : "", args: remote ? [] : list(args),
      env: e, headers: h, writable: remote ? [] : list(writable),
    };
    const secretKeys = asSecrets ? Object.entries({ ...e, ...h }).filter(([, v]) => v && !isRef(v)).map(([k]) => k) : [];
    onSave(name, { ...body, secret_keys: secretKeys });
  };

  return (
    <div className="card form">
      {!initial && (
        <label>从模板开始
          <select defaultValue="" onChange={(e) => pick(Number(e.target.value))}>
            <option value="" disabled>选择…</option>
            {TEMPLATES.map((t, i) => <option key={t.label} value={i}>{t.label}</option>)}
          </select>
        </label>
      )}
      <label>名称<input value={name} disabled={!!initial} onChange={(e) => setName(e.target.value)} placeholder="如 github" /></label>
      <label>类型
        <select value={remote ? "http" : "stdio"} onChange={(e) => setRemote(e.target.value === "http")}>
          <option value="stdio">本机命令（stdio）</option>
          <option value="http">远程地址（Streamable HTTP）</option>
        </select>
      </label>
      {remote ? (
        <>
          <label>地址<input value={spec.url} onChange={(e) => set("url", e.target.value)} placeholder="https://…/mcp" /></label>
          <label>请求头（每行 名称: 值）
            <textarea rows={2} value={headers} onChange={(e) => setHeaders(e.target.value)} placeholder="Authorization: Bearer …" />
          </label>
        </>
      ) : (
        <>
          <label>命令<input value={spec.command} onChange={(e) => set("command", e.target.value)} placeholder="npx / uvx / 可执行文件路径" /></label>
          <label>参数（每行一个）<textarea rows={3} value={args} onChange={(e) => setArgs(e.target.value)} /></label>
          <label>环境变量（每行 KEY=值）<textarea rows={2} value={env} onChange={(e) => setEnv(e.target.value)} placeholder="GITHUB_TOKEN=…" /></label>
          <label className="check"><input type="checkbox" checked={spec.sandbox} onChange={(e) => set("sandbox", e.target.checked)} />在沙箱里运行（推荐）</label>
          {spec.sandbox ? (
            <>
              <label className="check"><input type="checkbox" checked={spec.network} onChange={(e) => set("network", e.target.checked)} />允许联网</label>
              <label>可写路径（每行一个）
                <textarea rows={2} value={writable} onChange={(e) => setWritable(e.target.value)} placeholder="~/.npm（npx 的缓存）" />
              </label>
            </>
          ) : <div className="note warn-line">不进沙箱：服务器以你的全部权限运行，只对完全信任的服务器这样做。</div>}
        </>
      )}
      <label className="check"><input type="checkbox" checked={asSecrets} onChange={(e) => setAsSecrets(e.target.checked)} />
        把{remote ? "请求头" : "环境变量"}的值存为密钥（~/.nwb/secrets.json，配置文件里只留引用）</label>
      <label>工具声明
        <select value={spec.exposure} onChange={(e) => set("exposure", e.target.value as Exposure)}>
          {Object.entries(EXPOSURES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      <label>一句话说明<input value={spec.description} onChange={(e) => set("description", e.target.value)}
        placeholder="延迟加载时告诉模型这个服务器能做什么（可选）" /></label>
      <label>单次调用超时（秒）<input type="number" value={spec.timeout} onChange={(e) => set("timeout", Number(e.target.value))} /></label>
      <div className="row">
        <button onClick={onCancel}>取消</button>
        <button className="primary" disabled={!name || (remote ? !spec.url : !spec.command)} onClick={submit}>保存</button>
      </div>
    </div>
  );
}
