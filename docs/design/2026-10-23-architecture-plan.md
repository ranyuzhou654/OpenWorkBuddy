# Python 个人 Agent 助手：架构方案与决策清单

日期：2026-10-23。前置调研：[agent-foundation](../research/2026-10-23-agent-foundation.md)。

本文在前一份调研的基础上，补读了 Pi、Codex、OpenCode、DeepSeek Harness、Hermes、nanobot、OpenWorkBuddy 的源码，给出可以直接动工的方案。每个决策点标为 **D 编号**，第 0 节是已拍板的结果，后面各节是展开说明，原始选项和代价保留在各节里备查。

## 0. 决策结果（2026-10-23 已定）

项目名定为 **nano-workbuddy**：Python 包 `nano_workbuddy`，命令行 `nwb`，数据目录 `~/.nwb/`。

| # | 决策 | 结论 | 与推荐的关系 |
|---|---|---|---|
| D1 | 第一个客户端 | Web UI（本机服务）+ 开发用的极简 CLI | 同推荐 |
| D2 | 前端技术 | Vite + React + TypeScript | 同推荐 |
| D3 | 前后端通信 | HTTP 命令 + SSE 事件流 | 同推荐 |
| D4 | 历史的唯一真相 | **每会话一个 JSONL**，SQLite 只做索引和非对话数据 | 改选，见 4.3 |
| D5 | 会话结构 | **会话内树形**（条目带 parent_id，可回到任意节点重来），也支持分叉成新会话 | 改选，见 4.3 |
| D6 | 崩溃恢复 | **步骤级自动续跑**：日志即检查点，重启后从日志推导下一步 | 改选，见 4.4 |
| D7 | 模型接入 | 官方 SDK（openai、anthropic）+ 自写薄适配 | 同推荐 |
| D8 | 内部消息格式 | 自有中立格式（内容块 + 供应商不透明字段） | 同推荐 |
| D9 | 提示词与缓存 | 会话内冻结前缀，变化以追加消息表达 | 同推荐 |
| D10 | 上下文治理 | 三级：写入时外置 → 清理旧工具结果 → 摘要压缩 | 同推荐 |
| D11 | 编辑格式 | 精确字符串替换 | 同推荐 |
| D12 | 权限模型 | 规则表 allow/ask/deny + 预设档位 | 同推荐 |
| D13 | 沙箱后端 | 自写 Seatbelt（macOS）+ bwrap（Linux） | 同推荐 |
| D14 | MCP 暴露 | 官方 SDK v2；按 server 配置 direct / deferred + tool_search | 同推荐 |
| D15 | Skill 加载 | 专用 `skill` 工具，激活状态写入会话状态 | 同推荐 |
| D16 | 账号与鉴权 | 单用户本机 + 随机令牌，表里保留 owner_id | 同推荐 |
| D17 | 配置与凭据 | `~/.nwb/config.toml` 为准，密钥用 env/keyring 引用 | 同推荐 |
| D18 | 长期记忆 | 可编辑 USER.md / AGENTS.md，会话开始时冻结；阶段 4 加 memory 工具 | 同推荐 |
| D19 | 扩展机制 | 第一版不做插件系统，只留内部钩子 | 同推荐 |
| D20 | 项目命名 | nano-workbuddy | 同推荐（另起名） |

阶段 4 补充决定（2026-10-23）：

| 问题 | 结论 |
|---|---|
| MCP 配置层级 | 只放全局 `config.toml` 的 `[mcp.NAME]`；项目级等“信任此项目”做了再加 |
| stdio MCP 服务器 | 默认在沙箱内运行、允许联网，只能写临时目录和配置声明的路径；可逐个标 `sandbox = false` |
| memory 工具审批 | 工作区档不审批，只读档审批 |
| shell 钩子格式 | 兼容 Claude Code（PreToolUse / PostToolUse / UserPromptSubmit / Stop，JSON 输入输出，退出码 2 表示拦截） |
| web_fetch 审批 | 按站点：只读、工作区档首次访问某站点时询问，“总是允许”放行 `scheme://host/*`（本会话）；完全信任档不问 |

D4、D5、D6 三个改选是联动的：JSONL 天然适合追加式树（Pi 的做法），而“日志即检查点”让自动续跑不需要另建任务框架。代价估计：比原推荐多约 300 行，集中在 `log.py` 和 `resume.py`，第 4.3、4.4 节给出具体设计。

## 1. 目标，以及“等同或超过”具体指什么

Codex 和 Claude Code 有大团队，覆盖 IDE、云端、组织管理。一个人做全面对标，几年也追不上。可以追平甚至超过的，是 Agent 最核心、最影响日常体验的那几块：

- 循环正确：工具配对合法、截断的调用不执行、取消能杀掉整棵进程树、重复提交不会启动两次。
- 上下文治理：长任务不爆窗口，压缩后不丢目标与授权，提示词缓存命中率高（直接决定花费和延迟）。
- 权限与隔离：真有 OS 级沙箱，不是“扫描 rm 字符串”。
- 可观察：每轮实际发给模型的请求可以回看，界面断线重连不丢状态。
- 可换模型：国产模型（DeepSeek、Kimi、GLM、Qwen）和 Claude、GPT 都能用好，不只是能调通。

我们的差异化：Python 原生、代码小而清楚、Web 工作台、定位通用个人助手（文件、文档、网页、脚本），不只写代码。

行数参考：Pi 的核心循环 `packages/agent/src` 共 2,539 行 TypeScript。我们的目标是阶段 3 结束时后端核心不超过 5,000 行 Python（不含测试和前端）。

## 2. 本轮新增调研发现

以下源码均为浅克隆，固定在调研当天的 revision：

| 项目 | revision |
|---|---|
| earendil-works/pi | `a1836b06c3f93af50c76dbbd468b4d4f8771ef15` |
| openai/codex | `e6ebdfe00b520e21ed50ac0362656f32dd64d2fc` |
| anomalyco/opencode（原 sst/opencode） | `74f8d4860f49c563909e6b039b0cc8b454ebce72` |
| deepseek-ai/deepseek-harness | `687f282e8625c6b8d11ff58211a90524be2e4317` |
| NousResearch/hermes-agent | `7489b22af47ce91b366dde81d5831ea422577498` |
| HKUDS/nanobot | `0254bf121736bf02bbcd9e988a2c1b3fc265f8d9` |
| CatCatUncle/openworkbuddy | `5dd72bf8979cd5eb324cdcb0922962a570ff5549` |

### 2.1 值得直接采用的做法

| 做法 | 出处 | 我们怎么用 |
|---|---|---|
| system prompt 变化以“追加一条 system 消息”表达，不改历史开头，保护前缀缓存 | Pi `agent/README.md`（transcript owns the system prompt）；DeepSeek `system-prompt.md`（`systemPromptUpdate: 'in-history'`） | D9 |
| 记忆文件在会话开始时冻结进提示词，会话中途写盘但不改提示词 | Hermes `tools/memory_tool.py` 文件头注释 | D18 |
| 压缩：阈值 = 窗口 − 预留；从尾部保留约 20k token；结构化摘要；追加 compaction 记录，原始记录保留 | Pi `docs/compaction.md` | D10 |
| 摘要时把对话序列化为纯文本（`[User]: … [Tool result]: …`），防止模型“接着聊”；工具结果截到 2000 字符；不带 thinking | Pi `docs/compaction.md` | D10 |
| 因输出上限被截断的那条消息里的工具调用一律不执行，回报错误让模型重发 | Pi `agent-loop.ts` `failToolCallsFromTruncatedMessage` | 循环 |
| 工具失败、参数校验失败、被拒都作为 `isError` 结果回给模型，循环本身不抛 | Pi `agent-loop.ts` | 循环 |
| 只读工具并发、批内有写操作就整批串行；结果按调用 id 配对 | Pi（`executionMode`）、OpenWorkBuddy `docs/实现细节.md` | 循环 |
| 运行中用户插话分两种：steer（下一轮前插入）和 queue（本次跑完后再处理） | Pi steering/follow-up；OpenCode `session.inbox.update` 的 `delivery: "steer" \| "queue"` | D6 |
| 权限规则 `(action, resource 通配符, effect)`，最后一条匹配者生效；拒绝时可附带纠正意见回给模型 | OpenCode `packages/core/src/permission.ts`（`evaluate`、`CorrectedError`） | D12 |
| 预设档位把“沙箱模式 + 审批策略”两个独立旋钮捆成一个选择 | DeepSeek `permission-presets.md` | D12 |
| Seatbelt 默认拒绝，写权限只给 workspace 根，根下 `.git` 等元数据目录另行保护 | Codex `sandboxing/src/seatbelt_base_policy.sbpl`、`seatbelt.rs` | D13 |
| 大工具输出写入会话私有目录（0700 目录，`open(..., 'wx', 0o600)` 防软链接），模型拿到首尾摘录和路径 | DeepSeek `spill.md` | D10 |
| MCP 工具分 direct / deferred / codemode / hidden 四种暴露方式，可按工具通配覆盖 | Pi `docs/mcp.md` | D14 |
| 项目信任：项目内的设置、MCP、skill、扩展要用户确认后才加载 | Pi `docs/security.md` | D16 |
| 撞步数上限时额外发一次不带工具的请求，只让模型说清做完了什么、产出了什么、从哪继续 | OpenWorkBuddy `docs/实现细节.md` | 循环 |
| 同一 requestId 重复提交返回同一个 submission | Pi durable `README.md` | D3 |

### 2.2 明确不学的

- Hermes：约 100 万行 Python，单 `tools/` 目录就有审批相关 8 个文件。功能面很广，但正是“过度防御、过度耦合”的反例。只借鉴记忆冻结和多执行后端的想法。
- DeepSeek：Cordis 插件树、投影单元、i18n 文档体系对一个人的项目太重。借鉴分工，不借鉴机制。
- Codex：Guardian（让模型审核高风险操作）、多 agent、实时语音。远期可选。
- Pi durable 的通用任务状态机（phase、sleep、子任务所有权）：我们要的自动续跑只针对 Agent 循环本身，用“日志即检查点 + 工具 recover”就能做到（D6），不需要通用框架。

### 2.3 外部依赖的现状

- MCP Python SDK v2.0.0 已于 2026-07-28 发布稳定版，对应 2026-07-28 协议修订，带新的一等 Client。v1 进入维护分支。新项目直接用 v2。[PyPI mcp 2.0.1](https://pypi.org/project/mcp/2.0.1/)
- LiteLLM 在 2026-03-24 被投毒：1.82.7 和 1.82.8 含凭据窃取代码，其中 1.82.8 通过 `.pth` 文件在每次 Python 启动时执行。官方已从新流水线发布 1.83.0。这不代表它现在不安全，但说明“一个依赖覆盖全部供应商”的供应链面很大。[CSA 研究报告](https://labs.cloudsecurityalliance.org/wp-content/uploads/2026/03/CSA_research_note_LiteLLM-PyPI-backdoor-AI-toolchain-supply-chain-20260327-csa-styled.pdf)、[pypistats 复盘](https://pypistats.com/blog/litellm-pypi-supply-chain-compromise-how-a-popular-llm-proxy-became-a-credential-stealing-backdoor-march-24-2026)
- nanobot 当前直接依赖 `openai` 和 `anthropic` 官方 SDK，自己写 provider（`providers/openai_compat_provider.py` 等），不经 LiteLLM。
- Anthropic 的 sandbox-runtime（`srt`，npm 包 `@anthropic-ai/sandbox-runtime`）在 macOS 用 Seatbelt、在 Linux 用 bubblewrap 加网络命名空间，网络过滤走代理；官方标为研究预览。[仓库](https://github.com/anthropic-experimental/sandbox-runtime)

## 3. 总体架构

```text
┌────────────── 客户端 ──────────────┐
│ Web UI (React)   开发 CLI   (以后：IDE/ACP、IM)│
└───────────┬────────────────────────┘
            │ HTTP 命令 + SSE 事件
┌───────────▼────────────────────────┐
│ api.py      鉴权、路由、SSE；不含 Agent 逻辑 │
├────────────────────────────────────┤
│ runs.py     运行登记：每会话一个 run、取消、│
│             插话队列、事件广播            │
├────────────────────────────────────┤
│ loop.py     单 Agent 循环（纯 asyncio，  │
│             不 import FastAPI）           │
│   ├ context.py   投影 + 预算 + 压缩       │
│   ├ prompt.py    分段组装、冻结前缀       │
│   ├ llm/         供应商适配               │
│   └ tools/       注册表 → 权限 → 执行器   │
│        ├ builtin  ├ mcp  ├ skill          │
│        └ sandbox.py（Seatbelt / bwrap）    │
├────────────────────────────────────┤
│ log.py      会话 JSONL（树、追加、fsync）   │
│ index.py    SQLite：项目、规则、run、审批、索引│
└────────────────────────────────────┘
```

依赖方向只能向下。`loop.py` 只认识 `Store`、`Model`、`Tool` 三个协议，测试时全部可用假实现替换。没有全局单例，`app.py` 在启动时把依赖组装好并传入。

## 4. 各子系统方案

### 4.1 产品形态

**D1 第一个客户端**

- A（推荐）：本机服务 + Web UI，外加一个百行级的开发 CLI，用来在没有前端时验证循环。Web 能渲染 Markdown、代码、diff、图片、审批卡片，也最贴合你关心的“前后端通信”。
- B：先做 TUI（Pi、Codex、Claude Code 的路线）。终端用户体验好，但对通用个人助手不够友好，渲染文件和图片受限。
- C：一开始就做桌面壳（Tauri/Electron）。打包 Python 运行时是额外工程。建议等 Web UI 稳定后再套壳。

**D2 前端技术**

- A（推荐）：Vite + React + TypeScript。聊天、Markdown、代码高亮、diff 组件生态最全；前端只是事件的渲染器，不放业务逻辑。
- B：Svelte。代码更少，但生态小一些。
- C：HTMX + 服务端模板。全 Python、无构建，但流式工具卡片、审批交互写起来别扭。
- D：Textual（Python TUI）。等于选了 D1-B。

### 4.2 通信协议

**D3 传输**

- A（推荐）：HTTP 命令 + SSE 事件。命令（发消息、取消、审批）是普通 POST，可以重试、幂等；事件单向推送，浏览器原生 `EventSource` 自动重连。
- B：WebSocket。双向，但要自己处理重连、心跳和补发，命令也失去 HTTP 语义。
- C：JSON-RPC（Codex app-server、ACP 走 stdio）。适合给 IDE 接入。

推荐 A，同时让事件格式与传输无关：以后加 ACP/stdio 适配器时，只需新增一个大约 100 行的翻译层。

接口草案：

```text
POST   /api/projects                         创建项目（绑定目录）
GET    /api/projects
POST   /api/projects/{pid}/sessions          创建会话
GET    /api/sessions/{sid}                   会话元数据 + 当前快照（含 last_seq、leaf_id）
GET    /api/sessions/{sid}/tree              树概要（用户消息节点 + 分支点）
GET    /api/sessions/{sid}/events?after=N    SSE：先补发 seq>N 的持久事件，再推实时事件
POST   /api/sessions/{sid}/messages          {request_id, text, attachments, parent_id?, delivery: auto|steer|queue}
POST   /api/sessions/{sid}/checkout          {entry_id, summarize?: bool}  切换当前分支
POST   /api/sessions/{sid}/cancel
POST   /api/sessions/{sid}/fork              {entry_id}  把根到该节点的路径复制成新会话
POST   /api/sessions/{sid}/compact
PATCH  /api/sessions/{sid}                   改标题、模型、权限档位（下一轮生效）
POST   /api/approvals/{aid}                  {decision: once|always|reject, feedback?}
GET    /api/models  /api/skills  /api/mcp
```

事件信封统一为 `{seq, session_id, id, parent_id, run_id, type, data, ts}`。持久事件的 `seq` 就是它在 JSONL 中的行号（会话内单调递增，不随分支变化）；临时事件（delta、tool.output）沿用最近一条持久事件的 `seq`，并带子序号。

| 事件 | 是否持久化 | 说明 |
|---|---|---|
| `run.started` / `run.finished{status: ok\|cancelled\|error\|interrupted}` | 是 | |
| `message.user` | 是 | |
| `assistant.delta{text\|thinking}` | 否 | 合并约 50ms 一推；断线后靠快照补齐 |
| `assistant.message` | 是 | 完整的一条模型输出（含工具调用、usage） |
| `tool.started` / `tool.output`（流式）/ `tool.result` | started 和 result 持久化 | |
| `approval.requested` / `approval.resolved` | 是 | |
| `context.compacted` | 是 | 指向 compaction 记录 |
| `session.updated` | 是 | 模型、档位、标题变化 |

`POST messages` 必须带客户端生成的 `request_id`（库里有唯一约束）。网络重试时返回同一结果，不会启动第二个 run。

### 4.3 持久化与会话

**D4 历史的唯一真相（已定：JSONL + SQLite 索引）**

原选项：A SQLite 事件表（原推荐）；B 每会话 JSONL + SQLite 索引（已选）；C Pi durable 式任务检查点。

职责划分只有一条规则：**对话里发生的事以 JSONL 为准，对话之外的实体以 SQLite 为准；SQLite 中关于会话的部分都可以从 JSONL 重建。**

```text
~/.nwb/
  config.toml
  index.db                       SQLite：项目、规则、会话索引、run、审批、request_id
  sessions/<project_id>/<session_id>.jsonl
  spill/<session_id>/            大工具输出
```

JSONL 格式（每行一个 JSON）：

```text
{"type":"session","v":1,"id":"s_…","project_id":"p_…","forked_from":null,"ts":…}       第 0 行：头
{"id":"e_…","parent_id":null,"type":"system_prompt","data":{…,"hash":"…"},"ts":…}
{"id":"e_…","parent_id":"e_…","type":"user","run_id":"r_…","data":{"content":[…]},"ts":…}
{"id":"e_…","parent_id":"e_…","type":"assistant","data":{"content":[…],"meta":{…}},"ts":…}
{"id":"e_…","parent_id":"e_…","type":"tool_started","data":{"call_id":"…","name":"bash","args_hash":"…"}}
{"id":"e_…","parent_id":"e_…","type":"tool_result","data":{"call_id":"…","content":[…],"is_error":false}}
{"id":"e_…","parent_id":"e_…","type":"compaction","data":{"summary":"…","first_kept_id":"e_…","files":{…}}}
{"id":"e_…","parent_id":"e_…","type":"state","data":{"active_skills":[…],"preset":"workspace","todo":[…]}}
{"type":"checkout","leaf_id":"e_…","ts":…}                                             元数据行，不进树
```

写入规则：

- 只追加。以 `O_APPEND` 打开，一次 `write` 写完整一行。`tool_started`、`tool_result`、`user`、`assistant` 写后立即 `fsync`（这些是续跑的检查点）；`state`、`checkout` 等可以批量刷盘。
- 先写 JSONL，再更新 SQLite 索引。两步之间崩溃时，启动后发现 `index.db` 记录的字节偏移小于文件大小，就把尾部重新扫描进索引。
- 读取时容忍最后一行不完整（崩溃时写了半行）：截掉它，并在日志里记一条警告。
- `state` 条目放必须准确的运行状态（已激活的 skill、权限档位、todo），沿当前路径取最近的一条。这些状态不依赖摘要模型复述。

SQLite 表：

```sql
projects(id, owner_id, name, root_path, settings_json, trusted, created_at)
sessions(id, project_id, title, file_path, indexed_bytes, leaf_id, model_ref, updated_at)  -- 可重建
runs(id, session_id, request_id UNIQUE, status, model_ref, resume_count, started_at, ended_at, usage_json)
approvals(id, session_id, run_id, call_id, tool, args_hash, status, decision, created_at)
rules(id, scope, project_id, action, resource, effect, created_at)
```

**D5 会话结构（已定：会话内树形）**

原选项：A 线性 + 分叉成新会话（原推荐）；B 会话内树形（已选）；C 纯线性。

- 每个条目带 `parent_id`。当前分支由 `leaf_id` 决定：默认是最后追加的树条目；用户切换分支时追加一条 `checkout` 元数据行（它本身不进树），之后的新条目挂到目标节点下。
- 发给模型的上下文 = 从 `leaf_id` 沿 `parent_id` 走到根，再从这条路径上最近的 `compaction` 开始投影。压缩只影响它所在的路径，其他分支不受影响。
- “编辑之前的某条消息并重发”就是：checkout 到那条消息的父节点，再追加新的 user 条目。树形结构让这个常用功能几乎不需要额外代码。
- 切换分支时可以选择生成分支摘要（Pi 的 branch summary）：把离开的那条分支总结成一条 `branch_summary` 条目挂到新位置。放在阶段 3。
- 仍然支持“分叉成新会话”：把根到某节点的路径复制进新文件，头部记录 `forked_from`。
- 界面：主视图只显示当前路径（和线性会话看起来一样）；有分支的用户消息旁显示“‹ 2/3 ›”切换器（类似 ChatGPT 的编辑分支）；另有一个树形面板。
- 同一会话同时只有一个 run，不允许两个分支并发运行，避免 `leaf_id` 竞争。

### 4.4 运行生命周期与循环

因为 D6 选了自动续跑，循环写成“从日志推导下一步”的形式：每一步只看当前路径的末尾条目，决定接下来做什么，做完追加条目，再推导。正常运行和崩溃后续跑走的是同一段代码，不需要单独的任务框架。

```python
def next_step(path) -> Step:
    """只看当前路径（根→leaf）的条目，决定下一步。纯函数，单测覆盖全部分支。"""
    last = path[-1]
    if last.type == "assistant":
        calls = last.tool_calls()
        if not calls:
            return Done()
        missing = [c for c in calls if not path.has_result(c.id)]
        if not missing:
            return CallModel()
        if last.stop == "length":
            return FailCalls(missing, "输出被截断，参数可能不完整，请重发")
        return RunTools(missing)               # 其中已有 tool_started 的，交给 recover 判断
    if last.type in ("user", "tool_result", "compaction", "state", "branch_summary"):
        return CallModel()
    ...

async def drive(sess, inbox, cancel):
    while not isinstance(step := next_step(sess.path()), Done):
        if isinstance(step, CallModel):
            if steer := inbox.take_steer():
                await sess.append(user(steer)); continue
            if over_step_limit(sess):
                await wrap_up(sess); break       # 不带工具的收尾请求
            ctx = await context.build(sess)      # 投影 + 预算；超阈值先压缩
            msg = await stream_model(sess.model, ctx, cancel, emit)
            await sess.append(assistant(msg))    # 流式过程中的半截消息不落盘
        elif isinstance(step, RunTools):
            await tools.run_batch(step.calls, sess, cancel, emit)   # 每个调用：started → 执行 → result
        elif isinstance(step, FailCalls):
            await sess.append_many(error_results(step.calls, step.reason))
    if queued := inbox.take_queue():
        schedule_next_run(sess, queued)
```

要点：

- 每会话同时只有一个 run，由 `runs.py` 持有 `asyncio.Task`。关掉浏览器标签页不取消任务；只有 cancel 会取消，并沿 `CancelledError` 杀掉模型流和子进程组。
- 中途取消时，已经发出但没有结果的工具调用要补一条 `cancelled` 结果，保证配对合法。
- 模型调用的重试只针对可识别的瞬时错误（429、5xx、连接中断），指数退避，最多 3 次。工具不自动重试。

**D6 崩溃恢复（已定：步骤级自动续跑）**

原选项：A 标记 interrupted、用户手动继续（原推荐）；B 步骤级自动续跑（已选）。

做法是“日志即检查点”，不引入 Pi durable 那样的任务状态机：

1. 启动时扫描 `runs` 表中 status=running 的记录，逐个调用 `drive()`，它会从 JSONL 推导出下一步。`resume_count` 加 1；超过 3 次就标为 error 并停下，防止“一跑就崩”的死循环。
2. 模型调用没有副作用，崩溃时没写完的 assistant 消息直接丢弃，重新请求即可（多花一次钱）。
3. 工具调用是唯一的难点。崩溃后如果一个调用有 `tool_started` 却没有 `tool_result`，就调用这个工具可选的 `recover(args, ctx) -> "done" | "not_done" | "unknown"`：
   - `read`、`grep`、`glob` 等只读工具：不需要 recover，直接重跑。
   - `edit`：`old_string` 已不存在而 `new_string` 存在，判为 done；否则判为 not_done，重跑。
   - `write`：比较文件内容哈希和参数哈希。
   - `bash` 和 MCP 工具：一律判为 unknown，补一条错误结果“执行被中断，结果未知，请先检查状态再决定是否重试”，交给模型处理。绝不自动重放有副作用的命令。
   - 判为 done 时补一条成功结果，结果正文注明“由恢复流程确认”。
4. 等待审批的调用：审批记录在 SQLite 里，重启后仍是 pending，界面重新显示审批卡片，run 继续等待。
5. 冻结的运行参数（模型、档位、生成设置）写在 run 开始时的条目里，续跑时沿用，不受重启后配置变化的影响。

限制：崩溃时正在运行的后台进程（例如长时间的 `bash`）会随服务一起结束，不会重新挂接；它的输出只保留已经写进 spill 文件的部分。

这套设计比原推荐多约 150 行（`next_step` 约 60 行，`recover` 每个工具 10 到 20 行，启动扫描约 30 行）。好处是 `next_step` 是纯函数，可以用构造出来的日志穷举测试。

### 4.5 模型层

**D7 接入方式**

- A（推荐）：用 `openai` 和 `anthropic` 官方 SDK 处理网络、流解析、重试细节，自己写三个薄适配器，每个 200 到 400 行：
  1. OpenAI Chat Completions：覆盖 DeepSeek、Qwen、Kimi、GLM、OpenRouter、Ollama、vLLM。
  2. Anthropic Messages：覆盖 Claude；DeepSeek、Kimi、GLM 也提供兼容此协议的端点。thinking 签名、显式缓存断点都靠这条路。
  3. OpenAI Responses：GPT 系列的推理状态（reasoning items）只在这条路完整保留。可以放在阶段 3。
- B：LiteLLM。覆盖最广，但推理内容、缓存控制、供应商字段经常被抽象层削平；依赖重，有 2.3 节提到的供应链事件。
- C：只用 httpx 自己解析 SSE。零 SDK 依赖，但流式工具调用拼接、错误分类要自己写，大约多 500 行。

**D8 内部消息格式**

- A（推荐）：自有中立格式，形状接近 Anthropic 的内容块：

```python
@dataclass
class Message:
    role: Literal["user", "assistant", "tool"]
    content: list[Block]       # Text | Image | Thinking | ToolCall | ToolResult
    meta: dict                 # provider, model, usage, stop_reason, 以及供应商要求回传的不透明数据
```

  `Thinking` 块保留 `signature` / `encrypted` 等原样字段。只回传给同一供应商的同一协议；换供应商时丢掉不认识的块并记录。
- B：直接用 OpenAI Chat 格式作内部格式（nanobot 的做法）。代码最少，但 Anthropic 的 thinking 签名、DeepSeek 的 `reasoning_content` 回传规则要靠旁路字段硬塞。
- C：用 Anthropic 格式作内部格式。和 A 接近，但会被单一供应商的演进绑住。

模型配置分成 Provider（协议、base_url、凭据引用）和 Model（id、上下文窗口、最大输出、能力：tools/vision/thinking/cache）。切换模型对下一个 run 生效；如果新窗口放不下当前上下文，先压缩再开跑。

### 4.6 提示词组装

**D9 前缀与缓存**

- A（推荐）：会话开始时一次性渲染并冻结 system prompt。顺序为：基础规则 → 工具使用约定 → 项目说明（AGENTS.md）→ 用户记忆（USER.md）→ skill 目录 → 环境（OS、项目根、日期）。工具 schema 排序固定。会话中途若要改动（激活 skill、改档位、MCP 上线），追加一条 system/developer 消息，不改开头。每轮易变的信息（当前时间、todo）作为短提醒附在最新一条用户消息后面。Anthropic 用显式 `cache_control` 断点（system 末尾一个，倒数第二条消息一个）；OpenAI、DeepSeek 靠稳定前缀自动命中。
- B：每轮重建完整 system prompt。简单，但任何变化都会让整段缓存失效，长会话的成本和首字延迟差距很大。

建议基础提示词用英文写，并要求模型用用户的语言回复：多数模型对英文指令遵循更稳定，token 也更省。提示词放在 `prompts/*.md`，渲染时记录内容哈希到 run，方便解释“这一轮为什么变了”。

### 4.7 上下文治理与压缩

**D10 治理层级**

推荐三级，按代价从低到高：

1. 写入时外置：单个工具结果超过阈值（默认约 8k token）时，完整内容写到 `~/.nwb/spill/<session>/`，模型拿到首尾摘录、路径和“用 read/grep 查看”的提示。这一步不需要调用模型。
2. 清理旧工具结果：接近阈值时，把较早的、已经闭合的工具结果正文替换成 `[已清理，原文见 spill:…]`，保留调用和参数。这一步也不需要调用模型。
3. 摘要压缩：仍然超限时，按 Pi 的做法从尾部保留约 20k token，切点只落在 user/assistant 消息上（工具结果必须跟着它的调用）。把要压缩的段序列化为纯文本，让模型写结构化交接摘要（目标、约束、进度、决定、下一步、关键上下文、读过和改过的文件），追加一条 `compaction` 事件，下一次投影从这里开始。摘要失败不推进，原始事件永远保留。

触发条件：`估计输入 > 窗口 − max(输出预留, 16k)`。估计值 = 上一次请求的真实 `usage.input_tokens` + 新增内容的粗估（字符数 ÷ 3，中文偏保守）。供应商返回“超窗”错误时，压缩并重试一次。

选项：

- A（推荐）：三级都做，第 1 级在阶段 1 就做。
- B：只做第 3 级摘要。实现少一些，但长读文件的任务会频繁触发昂贵的摘要。
- C：只截断不摘要。会丢掉早期目标。

子选项：摘要默认用当前模型，也可以在配置里指定一个便宜的“小模型”。

### 4.8 工具

**D11 内置工具与编辑格式**

推荐第一批（每个 50 到 150 行）：

| 工具 | 说明 |
|---|---|
| `read` | 带行号、偏移和行数上限；能读图片（转给视觉模型）；PDF、docx 后续用 skill 处理 |
| `write` | 覆盖写，要求先读过（防止盲写），按会话记录 mtime |
| `edit` | `old_string` → `new_string` 精确替换，必须唯一匹配，否则报错并给出候选 |
| `bash` | 在沙箱里执行；超时；输出上限后外置；进程组整体取消 |
| `grep` / `glob` | 调 ripgrep；没有 ripgrep 时退回 Python 实现 |
| `web_fetch` | httpx + 正文抽取；二进制下载到工作目录 |
| `todo` | 写进会话的 `state` 条目，在界面显示进度 |
| `ask_user` | 结构化提问，界面渲染成选项卡片 |

编辑格式：

- A（推荐）：只做精确字符串替换（Claude Code、Pi 的做法）。各家模型都能稳定使用，失败信息清楚。
- B：只做 `apply_patch`（Codex 的做法）。GPT 系列对它专门训练过，但其他模型出错率较高。
- C：两者都做，按模型能力切换。等接入 GPT 主力模型时再加。

工具定义写成一个函数加一个 Pydantic 参数模型，schema 自动生成，没有基类层级：

```python
@tool(effect="read", parallel=True)
async def read(args: ReadArgs, ctx: ToolCtx) -> ToolResult: ...
```

`effect` 取 `read`、`write`、`exec`、`network` 之一，供权限层判断。

### 4.9 权限

**D12 权限模型**

- A（推荐）：规则表 + 预设档位。
  - 规则：`(action, resource, effect)`，`resource` 是通配模式，例如 `("bash", "git status*", allow)`、`("write", "~/.ssh/**", deny)`。最后匹配的规则生效。来源依次叠加：内置默认 → 用户全局 → 项目 → 会话（“总是允许”写进这一层）。
  - 档位（界面上一个下拉框）：
    - 只读：沙箱只读；写和执行都 ask。
    - 工作区（默认）：沙箱可写项目目录、禁网；工作区外的写入或需要联网时 ask。
    - 完全信任：不进沙箱、不询问。界面上醒目标红。
  - 审批答复：仅本次 / 总是（沉淀为规则）/ 拒绝并附意见（意见作为工具错误回给模型，OpenCode 的 `CorrectedError`）。审批绑定具体工具名和参数哈希，摘要里的“用户同意了”不算授权。
- B：Codex 式两个独立旋钮（sandbox_mode × approval_policy），并允许模型在调用参数里请求“提权执行”。表达力强，但用户要理解两个维度。
- C：不做权限，靠外部隔离（Pi 的立场）。个人助手会碰到用户的真实文件，不推荐。

远期：Codex 的 Guardian 思路，用一个小模型先审核 ask 类请求，低风险的自动放行。

### 4.10 沙箱

**D13 沙箱后端**

只约束 `bash` 和 stdio MCP 子进程；文件工具在进程内做路径检查（规范化、解析软链接后必须落在允许的根下）。

- A（推荐）：自写最小后端，约 250 行。
  - macOS：生成 Seatbelt profile，`sandbox-exec -p <profile> -D WRITABLE_ROOT=… -- bash -c …`。默认拒绝；读放开（敏感目录如 `~/.ssh`、`~/.aws`、钥匙串单独 deny）；写只给项目根和会话临时目录；根下 `.git/hooks`、`.env*`、`.nwb/` 保护；网络二选一：全禁或全开。直接参考 Codex 的 `seatbelt_base_policy.sbpl`（Apache-2.0）。
  - Linux：bubblewrap（`--ro-bind / /`、`--bind <root> <root>`、`--unshare-net`、`--die-with-parent`）。
  - Windows：不支持隔离，只能用“完全信任”档并明确提示。
- B：调用 Anthropic `srt`。现成支持按域名过滤网络，但引入 Node 运行时依赖，接口是研究预览，可能变动。
- C：Docker 容器。跨平台、隔离最强；但在 macOS 上启动慢，看不到宿主的 Homebrew 工具和应用，不适合“帮我处理本机文件”的个人助手。适合作为可选后端，用于跑不可信代码。
- D：第一版不做沙箱，只靠审批。审批疲劳会让人一路点“允许”，不推荐。

原则（沿用前一份调研）：后端不可用时明确报错，不悄悄降级到宿主执行。

### 4.11 MCP

**D14 暴露方式**

用官方 `mcp` v2 Client，支持 stdio 和 Streamable HTTP（含 OAuth）。每个 MCP 工具注册为 `mcp__<server>__<tool>`，和内置工具走同一个权限分发入口，默认 effect 为 ask（可以在配置里给某个 server 或工具设规则）。stdio server 在沙箱里启动；需要宿主权限的必须显式标 `trusted`。

- A（推荐）：按 server 配置 `exposure: direct | deferred`。direct 直接声明给模型；deferred 只在 system prompt 里列出 server 名和一句说明，模型通过 `tool_search` 工具检索后，下一轮才声明。默认规则：工具 schema 总量不超过窗口 5% 时用 direct，否则 deferred。工具列表变化以追加消息通知，不重写开头（Pi 的做法）。
- B：全部 direct。最简单；接三五个大 server 后，每轮都要多带数万 token 的 schema。
- C：Pi 的 code mode（模型写脚本调用工具，只把脚本输出回给模型）。省 token，但需要一个安全的脚本运行时，放到远期。

### 4.12 Skill

遵循 Agent Skills 规范：目录里放 `SKILL.md`（frontmatter 含 name、description），可选 `scripts/`、`references/`、`assets/`。发现位置：`~/.nwb/skills/`、`~/.agents/skills/`、项目 `.agents/skills/`（项目级需要项目信任）。system prompt 里只放名字、描述和路径。

**D15 加载方式**

- A（推荐）：专用 `skill(name)` 工具，返回正文和目录路径，并把 skill 名写进会话 `state` 条目的 `active_skills`。压缩后由状态重新注入，不靠摘要猜测。用户也可以输入 `/name` 强制加载。
- B：让模型用 `read` 读 SKILL.md（Pi 的做法）。零新代码，但系统不知道哪些 skill 处于激活状态，压缩后可能丢失。

Skill 里的脚本照样走 `bash` 和沙箱，skill 文本改变不了权限。`allowed-tools` 字段第一版忽略。

### 4.13 账号、项目、配置

**D16 账号与鉴权**

- A（推荐）：单用户。服务只监听 127.0.0.1；首次启动生成随机令牌，存在 `~/.nwb/token`，浏览器通过一次性链接换成 HttpOnly cookie；校验 `Origin`，防止其他网页调用本机执行接口。所有表保留 `owner_id`，查询统一按 owner 过滤，以后接真实登录时不用改表。
- B：第一版就做用户名密码、多用户。对个人助手是提前负担。

项目即一个目录：创建时选择根路径，项目设置存库；项目说明读取根目录的 `AGENTS.md`（兼容 `CLAUDE.md`）。项目里的 `.nwb/`（MCP、skill、规则）首次打开时要求“信任此项目”（Pi 的 project trust）。

**D17 配置与凭据**

- A（推荐）：`~/.nwb/config.toml` 是 provider、model、MCP、默认档位的唯一来源，人可以手改，UI 改动也写回这个文件。密钥只存引用：`api_key = "env:DEEPSEEK_API_KEY"` 或 `"keyring:deepseek"`（Python `keyring` 库对接 macOS 钥匙串）。子进程按白名单构造环境变量，不继承服务进程的密钥。
- B：全部存数据库，UI 管理。界面友好，但没法手改、版本化或复制到另一台机器。

### 4.14 长期记忆

**D18**

- A（推荐，阶段 1-3）：两个人可编辑的 Markdown 文件：`~/.nwb/USER.md`（偏好、身份）和项目 `AGENTS.md`。会话开始时冻结进提示词。
- B（推荐，阶段 4）：增加 `memory` 工具，让模型可以追加或修改这两个文件。写盘立即生效，提示词下个会话才更新（Hermes 的冻结快照），界面可见、可撤销。
- C：向量检索或自动记忆整合。等真有大量跨会话资料再说。

### 4.15 扩展机制

**D19**

- A（推荐）：第一版不做插件系统。循环里只有两个内部钩子 `before_tool` / `after_tool`（一个列表，按顺序调用），权限和外置都用它实现。
- B：Python entry-point 插件（Pi 的 TypeScript 扩展同类）。插件在进程内执行，安全上等于完全信任。
- C：配置式 shell 钩子（Claude Code 的 hooks）。用户无需写 Python 也能扩展，适合阶段 4。

### 4.16 命名

**D20（已定：nano-workbuddy）** 原仓库名 OpenWorkBuddy 与 CatCatUncle/openworkbuddy 同名，对方使用 PolyForm Noncommercial 许可，因此另起名字。发行包名 `nano-workbuddy`，导入名 `nano_workbuddy`，命令行 `nwb`，数据目录 `~/.nwb/`，项目内配置目录 `.nwb/`。GitHub 仓库是否改名由你决定，不影响代码。

## 5. 代码组织与依赖预算

```text
src/nano_workbuddy/
  app.py          组装依赖、启动 uvicorn（单 worker）
  api.py          路由、鉴权、SSE
  runs.py         运行登记、取消、插话队列
  loop.py         next_step + drive
  resume.py       启动扫描、工具 recover
  types.py        Message/Block/Event/ToolResult
  log.py          会话 JSONL：追加、fsync、树、路径、截尾修复
  index.py        SQLite（纯 sqlite3 + 显式 SQL），可从 JSONL 重建会话索引
  context.py      投影、预算、外置、清理、压缩
  prompt.py       分段渲染、冻结、追加更新
  llm/            base.py  openai_chat.py  anthropic.py  (responses.py)
  tools/          registry.py  builtin/*.py  mcp.py  skill.py
  policy.py       规则匹配、档位
  sandbox.py      seatbelt / bwrap / none
  config.py       config.toml + 凭据解析
  prompts/*.md
web/              Vite + React
tests/            假模型 + 脚本化场景
```

运行时依赖白名单：`fastapi`、`uvicorn`、`pydantic`、`httpx`、`openai`、`anthropic`、`mcp>=2,<3`、`keyring`。可选：`tiktoken`。不用 ORM、LangChain、LiteLLM、Redis、Celery。Python ≥ 3.12，用 uv 管理依赖并锁版本，配 ruff 和 pytest，类型检查用 pyright basic 模式。

代码规则：只有一个实现的东西不建接口；能用函数就不用类；不捕获处理不了的异常，未知错误由 `runs.py` 统一记录并把 run 标为 error。

## 6. 里程碑

| 阶段 | 交付 | 完成标准 |
|---|---|---|
| 1 核心闭环（约 1,800 行） | types、log（树形 JSONL）、index、`next_step`/`drive`、OpenAI Chat 适配、read/write/edit/bash（暂无沙箱，默认完全信任档并标红）、外置、开发 CLI（含 `/tree` 与 checkout） | 用 DeepSeek 完成一个多步文件任务；Ctrl-C 能杀掉子进程；`next_step` 对构造日志的穷举测试通过；回到旧消息改写重发生成新分支 |
| 2 Web 工作台（后端约 900 行 + 前端） | api、runs、SSE 补发、项目和会话、分支切换器与树面板、审批卡片、令牌鉴权、steer/queue、启动时自动续跑与 recover | 断线重连不丢状态；重复 request_id 不启动第二个 run；同会话不交错；在模型流式、工具执行中、等待审批三个时刻 `kill -9` 后重启，都能正确续跑且不重放 bash |
| 3 完整地基（约 1,500 行） | Anthropic 适配、缓存断点、权限规则与档位、Seatbelt/bwrap、清理与摘要压缩（按路径）、分支摘要、模型切换预算检查 | 前一份调研第 11 节的验收表全部通过；长任务缓存命中率可在界面上看到 |
| 4 扩展接入 | MCP（direct/deferred + tool_search）、skill 工具、memory 工具、Responses 适配、shell 钩子 | MCP 和 skill 走同一权限路径；关闭后下一个 run 不再暴露 |
| 之后 | 子 agent、定时任务、IM 网关、桌面壳、Guardian 自动审核、code mode | 按真实需求逐个加 |

测试策略：`FakeModel` 按脚本吐出文本、工具调用、截断、超窗错误，覆盖循环的全部分支。续跑测试直接构造“写到一半”的 JSONL，再调用 `drive()` 检查追加的条目。真实模型只保留少量冒烟测试（流式、工具调用、取消、usage），每接入一个模型跑一遍。

## 7. 风险与未决

- macOS 的 `sandbox-exec` 被标为 deprecated，但 Codex 和 Claude Code 仍在使用。如果以后失效，退路是 `srt` 或容器后端。
- 部分国产模型经中转服务时，会把工具调用吐进正文（OpenWorkBuddy `docs/实现细节.md` 有记载）。按“有真实故障证据才加补丁”的原则处理，不预先写救援逻辑。
- 粗估 token 对中文和图片偏差大。以 usage 校准为主；如果误差导致频繁超窗，再引入各家的 token 计数接口。
- 自动续跑依赖 `fsync` 的落盘时机。macOS 上 `fsync` 不保证写到盘片（需要 `F_FULLFSYNC`），断电场景可能丢最后几条；进程崩溃场景不受影响。个人助手先接受这个边界。
- 树形会话让“上下文 = 当前路径”。界面要始终显示当前在哪个分支，否则用户会困惑模型为什么“忘了”另一条分支里的内容。
- 本文对 Pi、Codex、OpenCode 的引用基于源码和文档阅读，没有运行它们做对比测试。
