# Python 个人 Agent 助手：源码调研与地基建议

调研日期：2026-10-23，Asia/Shanghai。

结论：建议使用 Python 自研一个小而完整的 harness，前端采用轻量 React + TypeScript。第一版做单用户、多项目、多会话、可恢复的单 Agent 执行；围绕执行循环、事件记录、模型适配、上下文构建、工具调度和权限建立边界。MCP、Skill 和沙箱是这些边界上的接入能力。暂不引入图编排框架、通用插件内核、分布式任务系统和多 Agent。

这里的“从零实现”是自己掌握 Agent 的控制流与状态，而不是重写模型网络协议、MCP 协议或操作系统隔离机制。

## 1. 调研范围和证据强度

本次阅读了三个仓库的实际源码，固定到如下 revision：

| 项目 | revision | 已检查的主要内容 |
| --- | --- | --- |
| CatCatUncle/openworkbuddy | `5dd72bf8979cd5eb324cdcb0922962a570ff5549` | HTTP/SSE 路由、Agent、压缩、模型适配、账号、项目规则、工具、MCP、Skill、沙箱 |
| deepseek-ai/deepseek-harness | `687f282e8625c6b8d11ff58211a90524be2e4317` | Cordis 组合架构、执行循环、提示词服务、会话投影、压缩、工具执行管线、沙箱接口、Python SDK |
| HKUDS/nanobot | `0254bf121736bf02bbcd9e988a2c1b3fc265f8d9` | Python 执行器、上下文构建、模型接口、工具注册、上下文治理 |

Pi、OpenCode、Deep Agents、Kimi Code 主要核对官方文档和仓库页面；Pi 额外读取了当前 compaction 参考文档。未完成 Pi 和旧 Kimi CLI 的本地 clone，没有将它们描述成完整源码审查。

没有安装或启动这些助手，没有调用真实付费模型，没有执行其沙箱与跨平台测试。下文对可靠性、安全性和代码简洁程度的判断是源码层面的分析，不是运行验收或性能排名。当前工作区只新增本调研文档。

## 2. OpenWorkBuddy 实际做了什么

它是一个本机优先的办公产品，覆盖文件交付、桌面、Web、CLI、账号组织、连接器、IM 和媒体任务。它比“模型调用 + 工具循环”大很多。

| 关心的问题 | 源码入口 | 看到的实现 | 对新项目的启发 |
| --- | --- | --- | --- |
| 前后端通信 | `server.js:8188`、`:8709` | POST `/api/chat` 返回 SSE；运行事件放在 active run，额外 GET stream 接口按位置补发；另有整页共用直播流 | 运行和订阅分离，页面断开不等于任务取消 |
| 执行循环 | `src/agent/agent.js` | 自己维护调用模型、执行工具、上限、停止、压缩和交付检查 | 不必先用复杂 Agent 框架 |
| 账号管理 | `src/domains/account/account.js` | scrypt 密码哈希、会话令牌、组织与角色、设备及账目 | 单人项目先保留 owner 归属，不需要复制整套组织系统 |
| 项目管理 | `server.js:4487`、`src/core/memory/project-memo.js` | 项目目录及切换；从工作目录到仓库根加载项目规则 | project、session、run 的身份应独立；目录和规则应属于 project |
| 模型切换 | `src/core/model/llm.js` | 公共入口接不同协议，包含消息转换、流式工具调用和供应商差异 | 统一核心语义，但不能认为换 model 名字就完成兼容 |
| 压缩 | `src/agent/agent.js:2366` | 按预算触发，保留尾部，调用模型写接手摘要，归档旧内容；额外保留指令、已加载技能和文件操作等状态 | 摘要与必须准确保存的运行状态要分开 |
| 工具 | `src/agent/tools.js` | 工具定义和执行门面，结合工作目录与安全策略 | 新项目用小注册表和公共执行入口，避免大 switch 持续增长 |
| MCP | `src/agent/mcp.js` | 自己实现 stdio、Streamable HTTP、JSON-RPC 握手、工具列表与调用 | 理解机制值得读，Python 项目应优先使用官方 MCP SDK |
| Skill | `src/core/ext/skills.js` | 扫描 frontmatter，先展示说明，需要时读取正文，兼容不同文件名情况 | 目录和描述先加载，正文和资源按需读取 |
| 沙箱 | `src/platform/sandbox.js` | macOS Seatbelt；Windows helper 与完整性级别；当前 `supported()` 不包含 Linux | 权限开关不能代替实际隔离；跨平台实现不能凭一个布尔值声称一致 |

表中路径对应固定 revision；可从 [OpenWorkBuddy 源码树](https://github.com/CatCatUncle/openworkbuddy/tree/5dd72bf8979cd5eb324cdcb0922962a570ff5549) 查阅。[通信实现](https://github.com/CatCatUncle/openworkbuddy/blob/5dd72bf8979cd5eb324cdcb0922962a570ff5549/server.js#L8188)、[压缩实现](https://github.com/CatCatUncle/openworkbuddy/blob/5dd72bf8979cd5eb324cdcb0922962a570ff5549/src/agent/agent.js#L2366)、[沙箱实现](https://github.com/CatCatUncle/openworkbuddy/blob/5dd72bf8979cd5eb324cdcb0922962a570ff5549/src/platform/sandbox.js#L5)。

该快照中，`server.js` 为 10,226 行、`agent.js` 为 5,020 行、`tools.js` 为 5,482 行。这包括注释和空行，不是复杂度评分。它的七层依赖规范表明作者也在控制持续扩张的成本；但这个产品范围不适合作为小项目逐文件仿写的模板。值得参考的是“任务过程可见、成果可检查、出错可接续”。[架构说明](https://github.com/CatCatUncle/openworkbuddy/blob/5dd72bf8979cd5eb324cdcb0922962a570ff5549/docs/代码架构.md)

许可方面，仓库主许可证写明 PolyForm Noncommercial 1.0.0，并列有部分 MIT 例外。建议独立实现通用思路；若直接复用代码，逐项核对其许可范围。[项目许可原文](https://github.com/CatCatUncle/openworkbuddy/blob/5dd72bf8979cd5eb324cdcb0922962a570ff5549/LICENSE)

## 3. 各参考项目该学什么

| 项目 | 最值得学的部分 | 第一版不必带入的部分 | 证据 |
| --- | --- | --- | --- |
| DeepSeek Harness | 模型、工具、循环、存储可替换；日志与模型输入投影分开；权限与执行隔离分开 | Cordis 插件树、热挂载、动态扩展、完整跨平台产品体系 | [架构](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/architecture.md) |
| Pi | 追加压缩记录，用摘要与保留边界重建上下文；工具调用和结果成组处理 | 会话树、分支切换摘要、完整扩展事件系统 | [压缩文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md) |
| OpenCode | HTTP 服务与客户端分离；OpenAPI；SSE 事件；项目、会话、Provider 各自管理 | IDE、LSP、完整 TUI、远程控制的所有接口 | [Server 文档](https://opencode.ai/docs/server/) |
| nanobot | Python 接手消息、构建上下文、调用模型、派发工具的完整链路 | 当前大量渠道、自动化、Dream、子 Agent 等功能 | [执行器](https://github.com/HKUDS/nanobot/blob/0254bf121736bf02bbcd9e988a2c1b3fc265f8d9/nanobot/agent/runner.py)、[上下文](https://github.com/HKUDS/nanobot/blob/0254bf121736bf02bbcd9e988a2c1b3fc265f8d9/nanobot/agent/context.py) |
| Deep Agents | 大工具结果外置、文件系统上下文、审批、沙箱和摘要的分工 | 为学习小内核而先引入完整 LangChain/LangGraph 体系 | [官方概览](https://docs.langchain.com/oss/python/deepagents/overview) |
| OpenWorkBuddy | 办公产品的交付闭环与可见执行过程 | 组织计费、专家团、媒体系统及大量业务提示词 | [仓库](https://github.com/CatCatUncle/openworkbuddy) |

DeepSeek Harness 的原则可以缩成“只在真正需要替换的地方设接口”。初期只需 Model、Tool、Executor 三类边界，存储通过一个小模块集中访问。没有必要为了可插拔先写服务容器、插件依赖解析和热加载系统。

特别注意：DeepSeek 提供 Python SDK，但其文档明确说明通过 stdio JSON-RPC 驱动打包的 dsh 子进程。使用 Python SDK 不等于从零实现了 Python harness。[Python SDK 说明](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/python/README.md)

## 4. 推荐技术栈及最小结构

| 部分 | 建议 | 理由与边界 |
| --- | --- | --- |
| Agent/服务端 | Python + asyncio + FastAPI + Pydantic | 异步模型和工具 I/O；输入与配置集中校验 |
| 前端 | Vite + React + TypeScript | 聊天、工具状态、审批与文件区容易持续演进；不需要 Next.js 服务端 |
| 通信 | 普通 HTTP 命令 + SSE 事件 | 单向事件足够表达流式回答与工具进度，停止/审批走 HTTP |
| 持久化 | SQLite WAL + aiosqlite，少量显式 SQL | 单机先保持一个数据库；不预置 Redis、向量库、消息队列和通用 Repository 层 |
| 模型 | 首先实现一种 OpenAI-compatible 协议的适配器 | 用成熟 SDK 处理网络与流解析；实际接入的每个模型做工具调用验证 |
| 执行 | 容器执行作为首个隔离后端 | 先明确支持一种执行环境；本机进程模式需明确标示为受信任、无隔离 |
| MCP | 官方 Python SDK | 复用协议和生命周期实现 |
| Skill | 标准 SKILL.md + Markdown/资源文件 | 少量扫描与按需读取即可，不需要 Skill 框架 |
| 依赖/质量工具 | uv + ruff + pytest | 一套依赖、格式和有价值的行为检查 |

FastAPI 已提供异步流响应；Python asyncio 提供网络、子进程、队列与同步能力。这条链路并不要求 JavaScript 才能流畅运行。[FastAPI 流响应](https://fastapi.tiangolo.com/advanced/custom-response/)、[asyncio](https://docs.python.org/3/library/asyncio.html)

建议目录（这是设计草案，未创建实现）：

```text
backend/assistant/
  api.py                 # HTTP/SSE、身份解析；不写 Agent 决策
  types.py               # Message/ToolCall/Event/RunConfig 等少量类型
  runner.py              # 单 Agent 循环、停止、预算、状态转换
  store.py               # SQLite、会话事件、摘要检查点
  context.py             # 选择历史、组装模型请求、计算预算
  compact.py             # 选切点、摘要、原子提交检查点
  models.py              # Provider/Model 配置、模型适配
  policy.py              # allow/ask/deny
  tools.py               # 注册表及统一执行入口
  executor.py            # 子进程/容器执行、取消、输出上限
  mcp.py                 # 外部工具接入同一注册表
  skills.py              # 元数据发现和按需加载
  prompts/               # 简短、版本可追踪的提示词
frontend/src/
tests/
```

这些是职责，不是必须每个都写成一个类。只有一个实现的内部逻辑使用普通函数和少量 dataclass 即可。暂不设置多层 Manager、Service、Factory、Repository 和全局事件总线。`runner.py` 不导入 FastAPI；`api.py` 通过明确参数传入 store、模型和工具，避免 import 时创建全局服务。

## 5. 通信与运行：先把生命周期做对

建议把运行创建与事件订阅拆开：

```text
POST /projects
POST /sessions                      -> session_id
POST /sessions/{id}/runs             -> run_id
GET  /runs/{id}/events?after={seq}    -> SSE
POST /runs/{id}/cancel
POST /approvals/{id}/resolve
GET  /sessions/{id}                  -> 已持久化状态/历史
```

创建 run 的 POST 带客户端生成的 request_id 并设唯一约束，避免网络重试启动两遍。运行由服务端持有，SSE 仅订阅；关标签页不取消任务，点击停止才沿取消信号停止模型请求和工具进程组。

第一版仅运行一个服务进程、一个 Uvicorn worker；同一 session 同时只有一个 run，不同 session 可并行。进程持有运行任务和会话锁即可，不引入分布式锁。CPU 密集工作放进独立进程或容器，不能堵塞 asyncio 事件循环。

事件最小公共字段是 `session_id/run_id/seq/type/data`，例如 `run.started`、`assistant.delta`、`assistant.completed`、`tool.started`、`tool.completed`、`approval.required`、`context.compacted`、`run.finished`。内部事件与供应商的流式事件分开，UI 不直接依赖某家模型的字段名。

业务事件持久化后发布，token delta 可合并推送；客户端重连时读取当前快照和高水位 seq，再补事件。临时 delta 必须能由服务端累计文本快照补齐，不能把内存订阅队列当成唯一历史。SSE 的 `id`/重连能力只是传输机制，历史补发仍需应用实现。[SSE 行为](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)

首版接受“服务重启后 run 标为 interrupted，用户继续可创建下一次 run”。不要承诺崩溃后精准自动续跑。若工具已执行但结果未落库，副作用可能已发生，不能自动重放。保留 tool.started 与调用 ID，使用户和后续恢复逻辑能识别不确定结果。

## 6. 账号、项目和凭据：保留归属，不复制组织系统

先区分三件事：应用登录账号、模型 Provider 凭据、MCP 连接授权，它们不应该共用一张“账号”配置。

建议的最小数据关系：

```text
owner -> projects -> sessions -> runs
                       \------> events
owner -> providers -> models
owner/project -> mcp_servers
session -> compaction checkpoints
run -> approvals/artifacts
```

单用户版可以使用固定 owner，不做注册、组织、部门、计费、RBAC 和 SSO。所有服务端查询集中按 owner 取项目和会话，便于以后增加真实账号；仅有 owner_id 字段还不代表多用户隔离已经完成。

服务默认监听回环地址，前后端同源。仍要有一个简单本地鉴权凭据或配对机制，并检查请求 Origin，避免其他网页调用本机命令执行接口；回环地址本身不是鉴权。等支持远程/多用户时再接成熟认证方案，并对事件订阅、附件下载、审批和运行管理一起做权限验证。

Project 保存明确的 workspace root、默认模型和策略。Session 创建时绑定 project 与工作目录，不依赖可随意变化的进程全局 cwd。前端传 project_id，服务端解析目录，不把前端提交的任意宿主路径直接变成写入权限。

Provider 配置保存 `id/protocol/base_url/credential_ref`；Model 配置保存 `provider_id/model_id/context_window/max_output/capabilities`。密钥使用环境变量或系统钥匙串，前端只看到是否配置和引用 ID。启动工具与 MCP 子进程时按白名单构造环境变量，不继承整个服务环境。

## 7. 模型切换：统一调用，保留差异

Provider 是协议、地址与凭据；Model 是具体模型与能力。同一模型可经不同 Provider 提供，同一 Provider 也可提供不同模型。把两者拆开，设置页和运行记录都会简单。

运行开始时冻结 ModelSpec 和生成设置；用户切换会话模型后，下一次 run 生效。不要在一批工具调用执行到一半时更换模型。历史仍记录当时的 provider、model 和使用量。

内部至少表达 text、tool call、tool result、usage 和 finish reason，并保留供应商要求回传的 opaque blocks/metadata。某些 reasoning/thinking 内容有专门回传要求，不能在通用消息转换时一律丢弃。流式工具调用须收全、校验参数后再执行，不执行半截 JSON。

切换 Provider 时重新渲染消息：不兼容的供应商状态不能直接挪过去。必要时在完整工具调用边界处生成一次交接摘要，并记录发生了转换。切换到小窗口模型前重新计算完整请求预算，超限先压缩或拒绝这次切换。

“兼容某接口”不等于行为完全一致。每个实际支持的模型至少检查：普通流式输出、完整 tool call、工具结果回传、取消、usage 和超窗错误。只有真实故障证据才加兼容补丁；不提前为所有中转服务写正文工具调用救援与 JSON 自动修复。

## 8. 上下文、压缩和提示词：三种状态分开

最重要的区分是：

1. 完整历史：发生了什么，用于恢复、审计和界面展示。
2. 请求上下文：本次模型真正接收到什么，是历史的有预算投影。
3. 长期记忆：跨会话稳定的偏好与事实，拥有明确来源与作用域。

先用 SQLite 持久化会话语义事件作为历史来源，避免 JSONL 和数据库同时维护两份可写真相；需要导出时再生成 JSONL。会话摘要可以作为检查点记录，包含 `covered_until/first_kept_event/summary/model/usage`，原事件仍保留。数据库中的项目和 run 行是索引与状态，不是第二份独立聊天历史。

建议的请求组成：

```text
稳定系统规则和工具使用约定
+ 可信项目规则（AGENTS.md 等，单独有预算）
+ 当前启用工具的 schema
+ Skill 元数据目录与确已加载的技能
+ 精简运行环境（目录/权限/日期等）
+ 会话摘要
+ 最近的完整消息与工具结果
+ 用户当前任务
```

这表示逻辑构成，具体 system/user/tool 位置由各 Provider 协议适配。正文资料、网页、工具输出和摘要不能因为被加入上下文就取得系统规则优先级。项目规则和技能也不能越过服务端权限策略。

实现上用显式函数组装有序片段，保持稳定前缀和工具排序；不需要先做模板 DSL。时间、近期状态等变化频繁的内容放在后部。记录请求所用配置、提示词/Skill 版本或内容哈希与输入事件范围，便于解释“为什么这一轮行为变了”；不要把密钥记进 trace。

预算必须计算整个请求：系统文本、历史、工具 schema、附件及输出预留。初始可以使用保守估算并结合实际 usage 校准，但不能长期用固定字符倍数覆盖所有语言、图片和模型。

```text
可用输入预算 = context_window - output_reserve - safety_margin
```

初始在接近可用输入预算时触发；具体比例由模型和任务验证，不照抄某个大窗口模型的固定预留值。DeepSeek 的当前压缩实现有明确的 per-model policy，且说明压缩不能解决 system prompt、工具定义或不可分割单元本身过大的问题。[DeepSeek compaction-basic](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/packages/compaction/compaction-basic/README.md)

压缩顺序建议：

1. 大工具结果先外置为 artifact，模型获得摘录和可重新读取的位置。
2. 在预算不足时，压缩较早、已闭合的消息单元，保留近期完整工具调用与结果。
3. 模型生成结构化接手摘要：目标、约束、已完成、当前问题、决定、下一步、关键路径和证据。
4. 在同一事务中追加成功的检查点，再让下一轮使用新的投影。

摘要失败不推进检查点，不删历史；摘要过大或预算仍不满足时明确返回未解决。用户当前目标、已批准事项、活跃 Skill、工作目录、预算、待审批调用等准确状态存结构化字段，不依赖摘要模型复述。批准记录绑定具体工具参数与目标，摘要中的“用户同意了”不能充当授权。

长期记忆先做一个可查看、可编辑的用户偏好文件和项目说明。暂不做自动记忆写入、向量检索与后台记忆整合。历史压缩解决单会话窗口问题，长期记忆解决跨会话复用问题，两者独立演进。

## 9. 工具、权限和沙箱：一个执行入口

工具注册表可以只是 `dict[str, Tool]`；Tool 只有名称、说明、参数 schema、执行函数及副作用属性。所有内置工具和 MCP 工具都通过一个 dispatcher，减少两套权限逻辑。

```text
完整工具调用
 -> 参数校验
 -> 服务端策略 allow / ask / deny
 -> 绑定具体参数的一次性审批（若需要）
 -> 执行器与实际隔离
 -> 结果/错误落库，大输出外置
 -> 返回模型
```

早期只提供 `read_file`、`list_files`、`search`、`write_file`、`edit_file`；shell 在隔离后端可用后开放。目录规则在文件工具中落实，并用沙箱约束任意命令；把 cwd 设成项目目录或扫描 `rm` 字符串都不是沙箱。

权限应描述可做的动作；沙箱应描述进程实际能读写/联网的范围。前者可保持 allow/ask/deny 三种结果，后者独立实现 `execute(argv, cwd, policy, cancel)`。用户准许一次操作不会自动授予后续任意 shell 命令权限。

首个真正隔离的执行器建议使用容器：仅挂载当前 workspace，单独临时目录，非特权用户，不挂宿主主目录、密钥目录和 Docker socket；默认限制网络及资源。容器隔离取决于配置，不能把运行在 Docker 里等同于已经安全。[Docker 安全机制](https://docs.docker.com/engine/security/)

在 macOS 上，容器环境与宿主工具和桌面应用不同；若需要操作宿主 App，再增加受信任的本机连接器。这是独立能力，不能声称由容器覆盖。Native sandbox-exec、Linux bwrap/Landlock、Windows 受限进程都可作为后续后端，不建议第一版同时实现三套。

沙箱后端不可用时，受隔离的执行请求返回明确错误，不静默降级到宿主执行。超时/取消清理整棵子进程或容器任务。只读工具可有限并发；涉及写入、shell 或依赖前序结果的调用先串行。审批、顺序和取消是必要正确性，不属于过度防御。

DeepSeek 将策略、审批、工具管线和 sandbox provider 分开，可以借鉴这种分工；无需复制全部 hooks 与事件机制。[工具执行管线](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/tool-execution-pipeline.md)、[沙箱接口](https://github.com/deepseek-ai/deepseek-harness/blob/687f282e8625c6b8d11ff58211a90524be2e4317/docs/subsystems/sandbox.md)

## 10. MCP 和 Skill 的最小实现

MCP 提供外部工具和上下文的协议，不负责你应用的执行循环、上下文预算或权限策略。[MCP 架构](https://modelcontextprotocol.io/docs/learn/architecture)

建议用[官方 Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)，第一步支持受控 stdio server，随后支持 Streamable HTTP。把远程工具映射成带 server 命名空间的本地 Tool，经过同一 dispatcher；不要让 MCP 在旁边直连模型、绕开审批。工具 descriptions 和 annotations 是提示，不是可信授权。

stdio server 是能运行代码的子进程。初期优先让它在既定执行环境里启动；需要宿主权限的 MCP 应明确作为受信任扩展启用。远程 MCP 的实际操作发生在对方服务，限制本机 shell 不能限制远程写操作，仍要做调用权限和凭据 scope。网络协议认证、资源下载和错误处理交给 SDK 与成熟库，并按实际接入范围验证。[MCP 安全实践](https://modelcontextprotocol.io/specification/latest/basic/security_best_practices)

Skill 使用标准目录：

```text
skills/example/
  SKILL.md              # name、description、正文
  scripts/              # 可选
  references/           # 可选
  assets/               # 可选
```

启动时发现名称、描述和路径；用户显式指定或模型选择后读取正文，引用资源按需加载。Skill 是操作知识；Tool 是可执行能力；MCP 是能力接入协议。Skill 里的脚本仍经同一个执行器，Skill 文本无法更改服务端策略。[Agent Skills 规范](https://agentskills.io/specification)

初期不做自动搜索安装、任意 Python 插件 import、后台自修改或一个 Skill 启动一个子 Agent。安装与启用是用户可见的配置动作；已启用技能 ID 作为会话状态保存，压缩后不靠猜测恢复。

## 11. 简洁的标准与验收

应当减少的是：重复的状态、未使用的抽象层、供应商特判散落、提前设计的企业需求和没有故障证据的兼容分支。优先做小函数、集中输入校验、明确依赖和清晰异常。

应当保留的是：身份归属、参数校验、目录约束、超时取消、工具配对、事务持久化、可见错误，以及崩溃后不重复执行副作用。不要为了行数把这些放进提示词交给模型决定。

实现中只捕获能处理的已知错误；未知错误由统一外层记录并终止本次 run。用户错误、模型协议错误、工具失败、沙箱不可用分别表达；不全都返回一个“成功的字符串”。重试只用于已识别的暂时性网络故障，并限制次数；工具副作用默认不自动重试。

最值得写的验收用例：

| 场景 | 必须看见的结果 |
| --- | --- |
| 多步文件任务 | 确实读取输入、调用工具、写出可打开成果，历史保存完整 |
| SSE 断线重连 | run 继续，恢复文本和工具状态，不启动第二个 run |
| 同一会话重复发送 | 串行或明确拒绝，不交错修改历史 |
| 停止模型与 shell | 请求结束，子进程/容器结束，记录 cancelled |
| 重启时工具结果不确定 | 标 interrupted/unknown，不自动重放副作用 |
| 切换小窗口模型 | 完整请求预算重算，压缩或明确提示超限 |
| 反复压缩 | 原历史仍在、工具配对合法、当前目标与审批状态不丢 |
| 文件越界与软链接 | 不允许从文件工具或执行器访问未授权路径 |
| 沙箱缺失 | 明确拒绝隔离执行，没有悄悄在宿主跑 |
| MCP/Skill | 都走统一工具和权限路径，关闭后下一次 run 不再暴露 |

这些应先用假 Provider 与可控工具覆盖关键状态，再对真实支持的 Provider/沙箱做少量端到端验证。不能把单元测试成功当成跨平台隔离或真实模型兼容的证明。

## 12. 建议分四步推进

1. 核心闭环：一个真实模型、一个 session、少量文件工具、事件持久化、停止；先从 CLI 验证 runner 能独立运行。
2. 可用工作台：薄 Web UI、项目/会话、HTTP + SSE、重连、附件与成果、简单本地鉴权；此时仍是单服务进程。
3. 完整地基：第二个协议适配、上下文预算、压缩、审批、一个实际隔离执行器；完成上表关键验收。
4. 扩展接入：MCP 与 Skill，再按真实需求增加长期记忆、宿主 App、定时任务和子 Agent。

每步都交付一个可以真实使用的闭环。身份、事件、工具和上下文边界在第一步明确，功能按阶段实现。先写少量设计与行为测试，再增加实现，不一开始把所有能力铺成空接口。

## 13. 为什么很多产品用 TypeScript/JavaScript

不是所有 Agent 项目都使用 JS/TS。nanobot 和 Deep Agents 证明 Python 能承担真实 harness；选型要区分“桌面/交互产品”与“模型研究/后端工具”。当前已核对：旧 [Python Kimi CLI](https://github.com/MoonshotAI/kimi-cli) 已归档，新 [Kimi Code](https://github.com/MoonshotAI/kimi-code) 使用 Node/pnpm 和 TypeScript 工具链，不应继续把旧 Kimi CLI 当作活跃 Python 产品推荐。

以下是基于源码结构的工程判断，不是这些作者公开声明的动机：

- Web UI 必须与浏览器运行时交互，桌面产品常用 Electron；前后端都用 TS 可共享事件类型，减少跨语言适配。OpenWorkBuddy 的 package.json 直接显示 Electron、Express 与大量 Node 文档生成依赖。
- 长期运行的 Agent 很多时间在等待模型、工具进程和网络，Node 的流和异步生态适配良好；Python 的 asyncio 同样能处理这些任务。GIL 不是这种 I/O 工作负载的主要选型障碍。
- npm/npx、前端构建、Node 工具库和桌面包装可形成一套交付路径。Python 也有 uv/uvx、成熟网络库和更强的数据处理生态，但把 Python runtime、前端和桌面壳一同分发需要额外工程。
- TypeScript 的静态类型有利于跨包协议和 UI 重构；Python 的 typing、Pydantic 与明确的数据模型也能做到良好边界。语言不会自动减少耦合。
- 本项目若主要做文档、数据处理和本机任务，Python 的工具生态、可读性和你希望掌握控制流的目标相符。若第一天就要做高度定制的跨平台桌面产品，全 TypeScript 的整体工程成本可能更低。

因此，Python 后端 + 薄 TypeScript 前端是本项目的建议组合。是否使用框架、是否好维护，要看状态与职责有没有重复，而不是看是不是用一门语言写完整个产品。

