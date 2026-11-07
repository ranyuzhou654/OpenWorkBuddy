import { useEffect, useState } from "react";
import { api, branchSummary, type SystemInfo, theme, type Theme, type Usage } from "./api";
import { COMMANDS } from "./commands";
import { Skeleton } from "./ui";

// Settings sections that describe this machine and this browser rather than models or tools.

function useSystem(onError: (m: string) => void): SystemInfo | null {
  const [info, setInfo] = useState<SystemInfo | null>(null);
  useEffect(() => {
    api.system().then(setInfo).catch((e) => onError(String(e)));
  }, [onError]);
  return info;
}

export function GeneralSection({ onError }: { onError: (m: string) => void }) {
  const [look, setLook] = useState<Theme>(theme.get());
  const [summarize, setSummarize] = useState(branchSummary.get());
  const info = useSystem(onError);
  return (
    <>
      <h2>外观</h2>
      <div className="segmented">
        {([["system", "跟随系统"], ["light", "浅色"], ["dark", "深色"]] as const).map(([k, label]) => (
          <button key={k} className={look === k ? "on" : ""} onClick={() => { theme.set(k); setLook(k); }}>{label}</button>
        ))}
      </div>
      <p className="note">只对这个浏览器生效。</p>

      <h2>会话</h2>
      <label className="check">
        <input type="checkbox" checked={summarize} onChange={(e) => { branchSummary.set(e.target.checked); setSummarize(e.target.checked); }} />
        切换分支时总结离开的分支
      </label>
      <p className="note">离开一条分支时，让模型把它总结成一段摘要挂到新位置，助手就知道那条分支上做过什么。会多一次模型调用。会话树面板里的同名开关是同一个设置。</p>

      <h2>对话文件夹</h2>
      <p className="note">不选项目的对话在这个文件夹里工作，模型写的文件也在这里。</p>
      <div className="card"><code>{info?.scratch_dir ?? "…"}</code></div>
      <p className="note">要换位置，在 config.toml 顶部写 <code>scratch_dir = "/新的/路径"</code>，然后重启 nwb serve。</p>
    </>
  );
}

const fmt = (n = 0) => n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : n >= 1e4 ? `${(n / 1e3).toFixed(1)}k` : n.toLocaleString();

export function UsageSection({ onError }: { onError: (m: string) => void }) {
  const [days, setDays] = useState(30);
  const [usage, setUsage] = useState<Usage | null>(null);
  useEffect(() => {
    api.usage(days).then(setUsage).catch((e) => onError(String(e)));
  }, [days, onError]);

  const total = { runs: 0, input: 0, output: 0, cache_read: 0 };
  for (const m of usage?.per_model ?? [])
    for (const k of ["runs", "input", "output", "cache_read"] as const) total[k] += m[k] ?? 0;
  // every day in the range, including the ones without runs
  const byDay = new Map((usage?.per_day ?? []).map((d) => [d.day, (d.input ?? 0) + (d.output ?? 0)]));
  const series = Array.from({ length: days }, (_, i) => {
    const d = new Date(Date.now() - (days - 1 - i) * 86400_000);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    return { day: key, tokens: byDay.get(key) ?? 0 };
  });
  const peak = Math.max(1, ...series.map((s) => s.tokens));

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>用量</h2>
        <div className="segmented">
          {[7, 30, 90].map((n) => <button key={n} className={days === n ? "on" : ""} onClick={() => setDays(n)}>{n} 天</button>)}
        </div>
      </div>
      <p className="note">按每次运行记录的 token 数汇总，来自服务商返回的用量。只统计已结束的运行；压缩和分支摘要的调用也算在内。</p>
      {!usage ? <Skeleton lines={6} /> : <>
      <div className="stats">
        <div><span>运行次数</span><b>{total.runs}</b></div>
        <div><span>输入 tokens</span><b>{fmt(total.input)}</b></div>
        <div><span>输出 tokens</span><b>{fmt(total.output)}</b></div>
        <div><span>缓存命中</span><b>{total.input ? `${Math.round((100 * total.cache_read) / total.input)}%` : "—"}</b></div>
      </div>
      <div className="card">
        <div className="bars" aria-label={`最近 ${days} 天每天的 token 数`}>
          {series.map((s) => (
            <div key={s.day} title={`${s.day}：${s.tokens.toLocaleString()} tokens`}>
              <i style={{ height: `${(100 * s.tokens) / peak}%` }} />
            </div>
          ))}
        </div>
        <div className="bars-axis"><span>{series[0].day}</span><span>峰值 {fmt(peak)}</span><span>今天</span></div>
      </div>
      <h2>按模型</h2>
      {!usage ? <Skeleton lines={3} /> : usage.per_model.length === 0 ? <p className="note">这段时间还没有运行。</p> : (
        <table className="models">
          <thead><tr><th>模型</th><th>运行</th><th>输入</th><th>其中缓存</th><th>输出</th></tr></thead>
          <tbody>
            {usage?.per_model.map((m) => (
              <tr key={m.model}>
                <td>{m.model}</td><td>{m.runs}</td><td>{fmt(m.input)}</td><td>{fmt(m.cache_read)}</td><td>{fmt(m.output)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      </>}
    </>
  );
}

export function SystemSection({ onError }: { onError: (m: string) => void }) {
  const info = useSystem(onError);
  if (!info) return <Skeleton lines={6} />;
  const rows: [string, React.ReactNode, string?][] = [
    ["命令沙箱", info.sandbox ?? <span className="danger-text">没有可用的沙箱</span>,
      info.sandbox ? "只读档和工作区档的命令在这里面运行" : "命令无法在沙箱里运行，只能用“完全信任”档或逐条批准沙箱外运行"],
    ["ripgrep", info.ripgrep ?? "未安装", info.ripgrep ? "grep 工具用它搜索，并遵守 .gitignore" : "grep 工具会用较慢的 Python 实现；装上 rg 即可提速"],
    ["选择文件夹", info.folder_picker ? "系统对话框" : "手动输入路径", "添加项目时使用"],
    ["数据目录", <code>{info.home}</code>, "会话记录、索引、密钥（secrets.json）"],
    ["配置文件", <code>{info.config}</code>],
    ["对话文件夹", <code>{info.scratch_dir}</code>],
    ["版本", `nano-workbuddy ${info.version} · Python ${info.python} · ${info.os}`],
  ];
  return (
    <>
      <h2>系统</h2>
      <p className="note">这台电脑为助手提供了什么，文件放在哪里。</p>
      <div className="card kv">
        {rows.map(([k, v, note]) => (
          <div key={k}><span>{k}</span><div>{v}{note && <div className="note">{note}</div>}</div></div>
        ))}
      </div>
    </>
  );
}

export function DeveloperSection({ onError }: { onError: (m: string) => void }) {
  const info = useSystem(onError);
  if (!info) return <Skeleton lines={5} />;
  return (
    <>
      <h2>钩子</h2>
      <p className="note">Claude Code 格式的 shell 钩子，在 <code>{info.config}</code> 的 <code>[[hooks.事件]]</code> 里配置；改完重启 nwb serve 生效。</p>
      {info.hooks.length === 0 ? <div className="card note">没有配置钩子。</div> : (
        <table className="models">
          <thead><tr><th>事件</th><th>匹配</th><th>命令</th><th>超时</th></tr></thead>
          <tbody>
            {info.hooks.map((h, i) => (
              <tr key={i}><td>{h.event}</td><td><code>{h.matcher || "*"}</code></td><td><code>{h.command}</code></td><td>{h.timeout}s</td></tr>
            ))}
          </tbody>
        </table>
      )}
      <h2>权限规则</h2>
      <p className="note">config.toml 里 <code>[[rules]]</code> 定义的全局规则，排在权限档自带规则之后、会话里“总是允许”的规则之前，后匹配的生效。</p>
      {info.rules.length === 0 ? <div className="card note">没有自定义规则。</div> : (
        <table className="models">
          <thead><tr><th>动作</th><th>资源</th><th>效果</th></tr></thead>
          <tbody>
            {info.rules.map((r, i) => (
              <tr key={i}><td><code>{r.action}</code></td><td><code>{r.resource}</code></td><td>{{ allow: "允许", ask: "询问", deny: "拒绝" }[r.effect] ?? r.effect}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

const KEYS: [string, string][] = [
  ["⌘ ,", "打开设置"],
  ["回车", "发送；运行中则插话"],
  ["Shift + 回车", "换行"],
  ["/", "在输入框开头调出命令和技能；↑↓ 选择，Tab 补全，回车执行，Esc 关闭"],
  ["回车 / Esc", "重命名时保存 / 取消"],
];

const CLI: [string, string][] = [
  ["nwb serve", "启动网页工作台，终端里打印登录链接"],
  ["nwb", "在当前目录开一个命令行会话"],
  ["nwb -c", "继续当前目录最近的会话"],
  ["nwb \"任务\"", "执行一个任务后退出，适合脚本"],
  ["nwb key 连接名", "保存某个连接的 API 密钥（输入不回显）"],
];

export function HelpSection() {
  return (
    <>
      <h2>快捷键</h2>
      <div className="card kv">
        {KEYS.map(([k, v]) => <div key={k}><span><kbd>{k}</kbd></span><div>{v}</div></div>)}
      </div>
      <h2>斜杠命令</h2>
      <p className="note">在输入框里输入，不会发给模型（/init 除外，它替你发一段请求）。和技能重名时命令优先。</p>
      <div className="card kv">
        {COMMANDS.map((c) => (
          <div key={c.name}>
            <span><code>/{c.name}</code>{c.args && <span className="note"> {c.args}</span>}</span>
            <div>{c.description}{c.session && <span className="note">（会话中）</span>}</div>
          </div>
        ))}
      </div>
      <h2>命令行</h2>
      <div className="card kv">
        {CLI.map(([k, v]) => <div key={k}><span><code>{k}</code></span><div>{v}</div></div>)}
      </div>
      <h2>文档</h2>
      <p className="note">
        每个功能怎么做的、为什么这样做，都写在仓库的 <code>docs/tutorial/</code> 里（从 01-核心闭环 到最新一篇），设计决定在
        <code>docs/design/</code>。遇到问题时，<a href="#/settings/system">系统</a>页能看到沙箱、ripgrep 和数据目录的状态。
      </p>
    </>
  );
}
