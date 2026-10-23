import { useEffect, useState } from "react";
import {
  api, type ConnectionView, type KeyStatus, type ModelTest, type ModelView, type Protocol, type RemoteModel,
  type SettingsView,
} from "./api";
import { McpSection } from "./Mcp";
import { MemorySection, SkillsSection } from "./Skills";

// A connection is an endpoint plus one API key. Two keys for the same service = two connections.
// Every model belongs to one connection; sessions pick a model, new sessions start on the default.

const TEMPLATES: { label: string; name: string; protocol: Protocol; base_url: string }[] = [
  { label: "OpenDesign Go", name: "opendesign", protocol: "openai-chat", base_url: "https://amr-link.open-design.ai/v1" },
  { label: "DeepSeek", name: "deepseek", protocol: "openai-chat", base_url: "https://api.deepseek.com" },
  { label: "Anthropic（Claude）", name: "anthropic", protocol: "anthropic", base_url: "https://api.anthropic.com" },
  { label: "OpenAI", name: "openai", protocol: "openai-chat", base_url: "https://api.openai.com/v1" },
  { label: "OpenRouter", name: "openrouter", protocol: "openai-chat", base_url: "https://openrouter.ai/api/v1" },
  { label: "Kimi（Moonshot）", name: "moonshot", protocol: "openai-chat", base_url: "https://api.moonshot.cn/v1" },
  { label: "智谱 GLM", name: "zhipu", protocol: "openai-chat", base_url: "https://open.bigmodel.cn/api/paas/v4" },
  { label: "阿里云百炼（Qwen）", name: "dashscope", protocol: "openai-chat", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1" },
  { label: "本机 Ollama", name: "ollama", protocol: "openai-chat", base_url: "http://localhost:11434/v1" },
];

const PROTOCOLS: Record<Protocol, string> = { "openai-chat": "OpenAI 兼容", anthropic: "Anthropic" };

export function Settings({ onChanged, onError }: { onChanged: () => void; onError: (m: string) => void }) {
  const [view, setView] = useState<SettingsView | null>(null);
  const [editingConn, setEditingConn] = useState<ConnectionView | "new" | null>(null);
  const [editingModel, setEditingModel] = useState<Partial<ModelView> | null>(null);
  const [remote, setRemote] = useState<Record<string, RemoteModel[] | string>>({});
  const [tests, setTests] = useState<Record<string, ModelTest | "running">>({});

  useEffect(() => {
    api.settings().then(setView).catch((e) => onError(String(e)));
  }, [onError]);

  // Every write returns the new settings; the model pickers elsewhere are refreshed via onChanged.
  const save = async (p: Promise<SettingsView>): Promise<boolean> => {
    try {
      setView(await p);
      onChanged();
      return true;
    } catch (e) {
      onError(String(e));
      return false;
    }
  };

  const fetchRemote = async (name: string) => {
    setRemote((r) => ({ ...r, [name]: "加载中…" }));
    try {
      const list = await api.remoteModels(name);
      setRemote((r) => ({ ...r, [name]: list }));
    } catch (e) {
      setRemote((r) => ({ ...r, [name]: String(e) }));
    }
  };

  const test = async (name: string) => {
    setTests((t) => ({ ...t, [name]: "running" }));
    const result = await api.testModel(name).catch((e) => ({ ok: false, error: String(e) }) as ModelTest);
    setTests((t) => ({ ...t, [name]: result }));
  };

  if (!view) return <div className="empty">加载中…</div>;
  return (
    <div className="settings">
      <h2>连接</h2>
      <p className="note">一个连接 = 一个服务地址 + 一把密钥。同一服务商有多把密钥，就建多个连接。密钥保存在 ~/.nwb/secrets.json（仅你可读），配置文件里只记引用，页面上只显示末 4 位。</p>
      {view.providers.map((p) =>
        editingConn !== "new" && editingConn?.name === p.name ? (
          <ConnectionForm key={p.name} initial={p} onCancel={() => setEditingConn(null)}
            onSave={async (name, body) => (await save(api.putProvider(name, body))) && setEditingConn(null)} />
        ) : (
          <div key={p.name} className="card">
            <div className="card-head">
              <b>{p.name}</b>
              <span className="tag">{PROTOCOLS[p.protocol]}</span>
              <code>{p.base_url}</code>
              <KeyBadge k={p.key} />
            </div>
            <div className="row">
              <button onClick={() => fetchRemote(p.name)}>获取模型列表</button>
              <button onClick={() => setEditingConn(p)}>编辑 / 换密钥</button>
              <button className="danger" onClick={() => confirm(`删除连接 ${p.name}？它保存的密钥也会删除。`) && save(api.deleteProvider(p.name))}>删除</button>
            </div>
            <RemoteList value={remote[p.name]} onPick={(m) => setEditingModel({
              provider: p.name, id: m.id, name: slug(m.id),
              context_window: m.context_window ?? 128_000, max_output: m.max_output ?? 8_192, params: {},
            })} />
          </div>
        ),
      )}
      {editingConn === "new" ? (
        <ConnectionForm initial={null} onCancel={() => setEditingConn(null)}
          onSave={async (name, body) => (await save(api.putProvider(name, body))) && setEditingConn(null)} />
      ) : (
        <button className="link" onClick={() => setEditingConn("new")}>＋ 添加连接</button>
      )}

      <h2>模型</h2>
      <p className="note">会话顶栏可以随时换模型（下一次运行生效）；★ 是新会话的默认模型。上下文窗口决定何时清理与压缩，请按服务商文档填写。</p>
      <table className="models">
        <thead>
          <tr><th /><th>名称</th><th>连接</th><th>模型 ID</th><th>上下文</th><th>最大输出</th><th /></tr>
        </thead>
        <tbody>
          {view.models.map((m) => (
            <tr key={m.name}>
              <td>{m.name === view.default_model ? "★" : ""}</td>
              <td>{m.name}</td>
              <td>{m.provider}</td>
              <td><code>{m.id}</code></td>
              <td>{m.context_window.toLocaleString()}</td>
              <td>{m.max_output.toLocaleString()}</td>
              <td className="actions">
                <button onClick={() => test(m.name)} disabled={tests[m.name] === "running"}>测试</button>
                {m.name !== view.default_model && <button onClick={() => save(api.setDefault(m.name))}>设为默认</button>}
                <button onClick={() => setEditingModel(m)}>编辑</button>
                {m.name !== view.default_model && (
                  <button className="danger" onClick={() => confirm(`删除模型 ${m.name}？`) && save(api.deleteModel(m.name))}>删除</button>
                )}
                <TestResult value={tests[m.name]} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {editingModel ? (
        <ModelForm initial={editingModel} connections={view.providers.map((p) => p.name)}
          existing={view.models.some((m) => m.name === editingModel.name && editingModel === m)}
          onCancel={() => setEditingModel(null)}
          onSave={async (name, body) => (await save(api.putModel(name, body))) && setEditingModel(null)} />
      ) : (
        <button className="link" disabled={!view.providers.length}
          onClick={() => setEditingModel({ provider: view.providers[0]?.name, context_window: 128_000, max_output: 8_192, params: {} })}>
          ＋ 添加模型
        </button>
      )}

      <McpSection view={view} save={save} onError={onError} />
      <SkillsSection view={view} save={save} />
      <MemorySection onError={onError} />
    </div>
  );
}

function slug(id: string): string {
  return id.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64) || "model";
}

function KeyBadge({ k }: { k: KeyStatus }) {
  const where = { secret: "本地文件", env: `环境变量 ${k.name}`, keyring: `钥匙串 ${k.name}` }[k.source] ?? k.source;
  return k.set
    ? <span className="key ok" title={where}>✓ {k.hint} · {where}</span>
    : <span className="key missing" title={where}>未设置密钥 · {where}</span>;
}

function RemoteList({ value, onPick }: { value?: RemoteModel[] | string; onPick: (m: RemoteModel) => void }) {
  if (value === undefined) return null;
  if (typeof value === "string") return <div className="note">{value}</div>;
  if (!value.length) return <div className="note">该连接没有返回任何模型。</div>;
  return (
    <div className="remote">
      {value.map((m) => (
        <button key={m.id} className="chip" onClick={() => onPick(m)} title="添加为模型">{m.id}</button>
      ))}
    </div>
  );
}

function TestResult({ value }: { value?: ModelTest | "running" }) {
  if (!value) return null;
  if (value === "running") return <span className="note">测试中…</span>;
  return value.ok
    ? <span className="key ok" title={value.reply}>✓ {value.ms}ms「{(value.reply ?? "").slice(0, 20)}」</span>
    : <span className="key missing" title={value.error}>✗ {(value.error ?? "").slice(0, 80)}</span>;
}

function ConnectionForm({ initial, onSave, onCancel }: {
  initial: ConnectionView | null;
  onSave: (name: string, body: { protocol: Protocol; base_url: string; api_key?: string; key_ref?: string }) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [protocol, setProtocol] = useState<Protocol>(initial?.protocol ?? "openai-chat");
  const [baseUrl, setBaseUrl] = useState(initial?.base_url ?? "");
  const [key, setKey] = useState("");
  const [useEnv, setUseEnv] = useState(initial?.key.source === "env");
  const [envName, setEnvName] = useState(initial?.key.source === "env" ? initial.key.name : "");

  return (
    <div className="card form">
      {!initial && (
        <label>从模板开始
          <select defaultValue="" onChange={(e) => {
            const t = TEMPLATES[Number(e.target.value)];
            if (t) { setName(t.name); setProtocol(t.protocol); setBaseUrl(t.base_url); }
          }}>
            <option value="" disabled>选择服务商…</option>
            {TEMPLATES.map((t, i) => <option key={t.label} value={i}>{t.label}</option>)}
          </select>
        </label>
      )}
      <label>名称<input value={name} disabled={!!initial} onChange={(e) => setName(e.target.value)} placeholder="如 opendesign-work" /></label>
      <label>协议
        <select value={protocol} onChange={(e) => setProtocol(e.target.value as Protocol)}>
          {Object.entries(PROTOCOLS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
      </label>
      <label>地址<input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://…/v1" /></label>
      <label className="check"><input type="checkbox" checked={useEnv} onChange={(e) => setUseEnv(e.target.checked)} />从环境变量读取密钥</label>
      {useEnv ? (
        <label>变量名<input value={envName} onChange={(e) => setEnvName(e.target.value)} placeholder="如 OPENDESIGN_API_KEY" /></label>
      ) : (
        <label>API 密钥
          <input type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)}
            placeholder={initial?.key.set && initial.key.source === "secret" ? `已保存 ${initial.key.hint}；留空则不修改` : "粘贴密钥"} />
        </label>
      )}
      <div className="row">
        <button onClick={onCancel}>取消</button>
        <button className="primary" disabled={!name || !baseUrl || (useEnv && !envName)}
          onClick={() => onSave(name, useEnv
            ? { protocol, base_url: baseUrl, key_ref: `env:${envName}` }
            : { protocol, base_url: baseUrl, api_key: key || undefined })}>保存</button>
      </div>
    </div>
  );
}

function ModelForm({ initial, connections, existing, onSave, onCancel }: {
  initial: Partial<ModelView>; connections: string[]; existing: boolean;
  onSave: (name: string, body: Omit<ModelView, "name">) => void; onCancel: () => void;
}) {
  const [m, setM] = useState({
    name: initial.name ?? "", provider: initial.provider ?? connections[0] ?? "", id: initial.id ?? "",
    context_window: initial.context_window ?? 128_000, max_output: initial.max_output ?? 8_192,
  });
  const [params, setParams] = useState(Object.keys(initial.params ?? {}).length ? JSON.stringify(initial.params, null, 2) : "");
  let parsed: Record<string, unknown> | null = {};
  try {
    parsed = params.trim() ? JSON.parse(params) : {};
  } catch {
    parsed = null;
  }
  const set = (k: keyof typeof m) => (e: { target: { value: string } }) =>
    setM({ ...m, [k]: k === "context_window" || k === "max_output" ? Number(e.target.value) : e.target.value });
  return (
    <div className="card form">
      <label>名称<input value={m.name} disabled={existing} onChange={set("name")} placeholder="会话里显示的名字" /></label>
      <label>连接
        <select value={m.provider} onChange={set("provider")}>{connections.map((c) => <option key={c}>{c}</option>)}</select>
      </label>
      <label>模型 ID<input value={m.id} onChange={set("id")} placeholder="服务商的模型名，如 DeepSeek-v4.1-flash" /></label>
      <label>上下文窗口<input type="number" value={m.context_window} onChange={set("context_window")} /></label>
      <label>最大输出<input type="number" value={m.max_output} onChange={set("max_output")} /></label>
      <label>额外参数（JSON，可选）
        <textarea rows={3} value={params} onChange={(e) => setParams(e.target.value)}
          placeholder='如 {"thinking": {"type": "adaptive", "display": "summarized"}}' />
      </label>
      {parsed === null && <div className="note warn-line">JSON 格式不正确</div>}
      <div className="row">
        <button onClick={onCancel}>取消</button>
        <button className="primary" disabled={!m.name || !m.id || !m.provider || parsed === null}
          onClick={() => onSave(m.name, { provider: m.provider, id: m.id, context_window: m.context_window,
            max_output: m.max_output, params: parsed ?? {} })}>保存</button>
      </div>
    </div>
  );
}
