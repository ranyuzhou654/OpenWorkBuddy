# nano-workbuddy 第二轮审查与改进方案

日期：2026-10-24（Asia/Shanghai）  
本轮基线：`66dc15b0ef5d11399e4ed089348079bb0ce96113`  
对照基线：`1ea4a4d5c0ac79f174808d1f85141d24408f6db6`  
上轮报告：[个人助手审查与改进方案](2026-10-24-personal-assistant-improvement-plan.md)

## 1. 本轮结论

**这轮改进显著扩大了可用能力，也修复了上次多个具体问题。下一阶段应收敛到“可靠性修订 + 完整工作流试用”，暂缓 Code Mode、多代理和新增平台。**

项目已有附件、文档读写、来源编号、版本化产物、飞书连接器、独立浏览器、定时任务、历史检索和备份。它从以聊天和编程为主的 harness，向个人工作助手前进了一大步。

但 M0–M3 的功能已经实现，不等于这些里程碑的全部验收条件已经满足。本轮 **221 项现有测试全部通过**，同时通过隔离探针观察到 **11 组缺陷或契约缺口**。主要集中在模块交接处：请求登记与实际接收、任务认领与投递、审批预览与最终载荷、运行失败与业务结果、备份文件与实际恢复路径。

用户本轮确认“还没有充分使用”。因此现在缺少真实任务样本和持续使用数据，无法有依据地判断哪个高级功能最值得做，也不能声称整体效率超过 pi、OpenCode 或 DeepSeek Harness。

### 本次沿用的需求

- 综合个人工作助手，调研、文档、办公自动化优先，兼顾编程。
- 自己使用、本机优先、小规模持续迭代，继续保留 Python + React 小内核。
- 本地文件、浏览器、飞书为首批集成。
- 可以定时整理和准备草稿；发送、发布、删除前确认。
- 实现教程还记录了每周调研模板、合并补跑、macOS 通知、只索引助手记录的选择。本轮不扩大到任意文件夹扫描，也不替用户修改账户授权。

用户本轮进一步确认：**历史检索默认当前项目优先，明确需要时才跨项目检索。** 这是新方案的确定需求；当前代码的全局检索行为尚未修改。

## 2. 实际审查范围与验证结果

### 2.1 改动概况

| 提交 | 内容 | 本轮判断 |
|---|---|---|
| `68fb259` | M0：持久输入、请求去重、结束状态检查、锁、SSE、CI | 原有具体故障大多得到针对性修复；跨操作事务和其它协议仍有缺口 |
| `bd76f46` | M1：附件、Office/PDF 阅读、搜索、文档和交付物 | 有了明显的产品价值；来源校验和交付质量仍需分层 |
| `222f15e` | M2：飞书、Playwright MCP 浏览器 | 后端选择合理；不能仅按工具名或 argv 字符串判断副作用 |
| `66dc15b` | M3：调度、全文检索、通知、备份恢复 | 结构保持简洁；调度交接、结果裁定和异地恢复需要补强 |

Git 跟踪源码统计：后端 Python 从 5,434 行增至 8,676 行，测试 Python 从 2,747 行增至 4,003 行，`web/src` 从 3,077 行增至 3,969 行。统计含注释与空行。代码增量仍可控，当前没有重写内核的必要。

### 2.2 本轮亲自执行

| 验证 | 结果 | 能证明什么 |
|---|---|---|
| `.venv/bin/pytest -q` | **221 passed in 27.42s** | 现有回归范围内通过 |
| `.venv/bin/ruff check` | 通过 | 静态规则通过 |
| `npm --prefix web run build` | TypeScript 检查、Vite 构建通过 | 前端可编译打包，不等于交互 E2E 已通过 |
| 本轮隔离探针 | 11 组结果，见第 4 节 | 覆盖新的中断、授权、恢复和证据边界 |
| 上游协议核验 | Playwright MCP `v0.0.83`、飞书 CLI 固定源码、pflag `v1.0.9` | 核对本项目依赖的实际参数语义 |

探针见 [可复现脚本](../research/2026-10-24-re-review-probes.py)。使用临时项目、合成数据、假模型和假飞书连接器；Anthropic 流测试使用本机 HTTP 服务器和已安装的真实 SDK。没有使用真实 API key，没有发送飞书消息，没有真实打开未授权网站，也没有修改业务源码。

教程 14–16 记录了此前真实模型与组件冒烟测试；本轮将其视为**已有测试记录**，没有重新执行、也不将其等同本轮验收。教程 14 还明确搜索因没有 Tavily key 只做过模拟测试。用户自己的四个未跟踪文件保持原样，不作为程序源码分析。

## 3. 上轮问题复核：哪些修好了，哪些仍有边界

| 上轮编号 | 当前状态 | 具体说明 |
|---|---|---|
| F01 首次 request_id 重试变成插话 | 原场景已修复 | 新增 `requests` 去重；但与 `inputs` 不是同一事务，见 R01 |
| F02 queue 仅在内存 | 基础场景已修复 | 接收后写 SQLite，启动补投并从日志对账；更早的接收窗口、调度投递窗口仍有缺口 |
| F03 call ID 复用使 spill 失败 | 已修复原场景 | 文件名加入内容 hash，复用相同内容并拒绝异常现存文件 |
| F04 搜索回退跟随软链接 | 已修复原场景 | 跳过文件软链接；没有实现每个子资源的权限检查，见 R09 |
| F05 缺少结束标记仍调度工具 | **仅部分修复** | Chat 适配器已修；Anthropic 同类问题仍可复现，见 R11 |
| F06 MCP hint 决定安全重放 | 已修复 | MCP effect 保持 exec，hint 仅参与并发标记 |
| F07 snapshot/SSE 间 live 状态丢失 | 已修复原场景 | 订阅内部补 live 状态，已有回归 |
| F08 SSE 无界队列 | 已缓解 | 限制 5,000 个事件，溢出重连；未限制字节或大 payload |
| F09 原地写文件 | 部分修复 | 临时文件、fsync、原子替换、mtime_ns + size；没有跨会话资源锁/内容前置 hash |
| F10 MCP 结构化结果丢失、长名称冲突 | 主要已修复 | structured 存入 meta，名称冲突有处理；还不是统一业务结果契约 |
| F11 全局 last_tools 串会话 | 已修复主要问题 | 变为 session_tools；重启后工具快照、手动压缩仍可进一步完善 |
| F12 日志多写者与恢复 | 部分修复 | 引入 flock 和追赶更新；跨 SQLite/日志的恢复语义及只读打开时的尾行修复仍需检查 |
| F13 shell 退出结果不结构化 | 已改进 | 退出码进入结果信息；上层业务完成仍不能只看模型文字 |
| F14 CI、长历史与前端测试 | 部分完成 | 新增 CI；前端 E2E、历史分页与规模验收尚未完成 |

本轮应保留这些修复，补齐跨模块边界。没有理由为了采用另一家的框架而重做已经可用的会话树、模型适配、SSE 和产物存储。

## 4. 本轮发现及证据

P0 表示启用真实外部写操作或无人值守工作前应完成的修订；P1 表示近期日常使用和数据可靠性所必需。该分级是本项目实施优先级，不是漏洞严重性评分。

### R01 / P1：去重先成功，输入未保存，后续重试被吞掉

**位置：** `runs.py:168–176`，`index.py:219–242`。

`claim_request()` 和 `add_input()` 是两次独立提交。模拟在第二步之前中断，重新发送相同 request_id 返回 `duplicate`，但 pending input 为 0、live run 为 0。

```json
{"probe":"request_claim_gap","retry":"duplicate","pending":0,"live":0}
```

首个请求此时没有成功 ACK，但客户端正常重试也无法完成接收，用户可能看到消息无后续。将去重与输入保存放到同一个 SQLite 事务；duplicate 应返回对应 submission 的 ID/状态，不能仅表示“曾见过一个 request_id”。同时明确去重键的会话范围和 payload 中是否包含 delivery。

### R02 / P1：调度先认领，未投递就中断，重启后不会补投

**位置：** `scheduler.py:151–188`，`index.py:287–294`。

`claim_occurrence()` 成功后才调用 `runs.send()`。在两者之间中断，下次 tick 因 occurrence 已存在而跳过 send，同时推进 next_run；超时后记为失败，没有任何 run 执行。

```json
{"probe":"schedule_claim_gap","outcome":"failed","pending":0,"runs":0,"next_advanced":true}
```

当前保证的是不重复认领，尚未保证已认领任务最终投递。应在同一 SQLite 事务写 occurrence 和 durable input，保存二者关联；进程启动和周期 tick 都消费尚未启动的输入。不要用十分钟后报失败代替投递恢复。

### R03 / P1：模型报告“无变化”后发生失败，最终仍按“无变化”处理

**位置：** `scheduler.py:84–101,172–194`。

`outcome_of()` 优先返回任意一次 `task_result`，随后才看 run.status。模型先调用 `task_result(unchanged)`、下一轮模型请求抛错，数据库 run 为 `error`，最终业务结果仍是 `unchanged`，会走静默路径。

```json
{"probe":"reported_outcome_masks_failure","actual_run_status":"error","reported":"unchanged"}
```

应先裁定错误、取消、中断和超预算，再处理模型业务报告；`task_result` 是完成候选，不是唯一完成依据。对“新报告已生成”至少核对本轮产物、文件校验和来源记录。没有报告时应显示“结果待核验”，不要显示“已完成（未报告结果）”。

### R04 / P1：备份恢复到新目录后，会话仍引用原来的目录

**位置：** `maintenance.py:65–80`，`index.py:123–139,175–183`。

备份带入 SQLite 的绝对 `sessions.file_path`。恢复后 reindex 虽然找到新目录的 JSONL，`sync_session()` 的冲突更新没有重写 file_path。移走原 home 后再打开恢复的会话，出现 `FileNotFoundError`。

```json
{"probe":"restore_new_home","old_path_retained":true,"session_open_error":"FileNotFoundError"}
```

现有 round-trip 测试只检查标题、任务等索引数据，原 home 又仍存在，因此没发现问题。恢复验收必须让原数据目录不可用，再打开历史、附件、版本文件和调度会话。应区分 home 内部可重定位路径与外部项目目录，不做全库盲目字符串替换。

### R05 / P0：审批的是文件旧内容，执行读取文件新内容

**位置：** `connectors/feishu.py:188–209,222–238,259–277`，`runtime.py:435–440`。

飞书 `--content @draft.md` 在 preview 和 execute 中分别由 CLI 打开。探针在审批回调里改变同一文件，审批显示 `APPROVED_CONTENT`，执行得到 `CHANGED_AFTER_PREVIEW`，没有再次确认。

```json
{"probe":"approval_file_not_frozen","calls":[["preview","APPROVED_CONTENT"],["execute","CHANGED_AFTER_PREVIEW"]]}
```

这里的问题不要求恶意输入：另一个会话、用户编辑器或脚本修改草稿也会触发。需要在审批前冻结正文和附件字节，保存 business payload hash、账户 ID、目标 ID，执行同一个准备好的请求；变化时重新准备和确认。

### R06 / P0：固定的飞书“每次确认”规则可被 PreToolUse allow 覆盖

**位置：** `policy.py:72–77`，`runtime.py:414–431`。

规则列表最后追加 FIXED，但 gate 随后把 hook.decision 应用到 Check。用合法的 `Outcome(decision="allow")` 模拟 hook，飞书写调用直接获准，审批回调调用次数为 0。

```json
{"probe":"hook_overrides_fixed_approval","gate_allowed":true,"approval_calls":0}
```

这违反本项目明确约定的外发确认边界。hook 可以收紧权限或补充上下文；不可取消的业务确认约束应在全部扩展决策之后生效。用户安装任意宿主脚本本身仍属于受信任代码执行，不能据此声称拥有操作系统级别的绝对保证。

### R07 / P0：dry-run 的字符串判断与 CLI 的布尔解析不一致

**位置：** `connectors/feishu.py:149–163,259–277`；测试桩 `tests/fixtures/fake_lark.py`。

参数包含 `--dry-run --dry-run=false` 时，本项目只看列表中是否出现了独立字符串 `--dry-run`，于是分类为读取，不需要写审批。官方 CLI 使用布尔 flag 的最终值决定是否执行；其 pflag 依赖每次 Set 都覆盖布尔值。

```json
{"probe":"conflicting_dry_run_flags","classified_write":false,"action":"feishu","permission":"allow"}
```

本轮复现了本地错误分类，并核验了上游解析语义；**没有实际发送消息**。现有 fake CLI 也用同样的字符串存在判断，恰好掩盖了这类协议差异。

最简单的修复是让 preview 成为宿主控制的模式，不允许模型传递 `--dry-run` 和冲突的身份/幂等参数。首批写操作改为结构化 operation + args，由适配器生成唯一规范 argv。不能依靠继续加几个字符串黑名单保证参数语义一致。

来源：[CLI runner](https://github.com/larksuite/cli/blob/017b19a793ad71794a02887ede04757f3d82e10d/shortcuts/common/runner.go)、[CLI 依赖版本](https://github.com/larksuite/cli/blob/017b19a793ad71794a02887ede04757f3d82e10d/go.mod)、[pflag bool 实现](https://github.com/spf13/pflag/blob/v1.0.9/bool.go)。

### R08 / P0：通过 browser_tabs 新建页面，可绕过站点确认

**位置：** `policy.py:66–69`，`connectors/browser.py:58–81`。

`browser_tabs` 被整体列为读取工具，而只有 `browser_navigate` 被转换成 URL scope。本项目固定使用的 Playwright MCP `v0.0.83` 中，tabs 支持 `action="new"` 和 URL，也支持 close/select，官方标记为非只读。

探针向封装后的工具传入 `new + 未授权域名`，其 scope 仍是空资源，结果为 allow。这里验证的是权限决策，没有启动真实浏览器。

```json
{"probe":"browser_new_tab_bypasses_site_gate","action":"mcp__browser__browser_tabs","resource":"","permission":"allow"}
```

必须按工具参数计算能力：tabs/list、tabs/new、tabs/close 分开；所有产生导航的入口共享 URL 策略。截图、snapshot、网络日志等工具还有 filename 参数，应规范为受控输出目录，不把“只读 hint”当成“不写文件”。危险点击/脚本也不应通过一次“总是允许”变成永久外写授权。

来源：[固定版本的 Playwright MCP 工具参数](https://github.com/microsoft/playwright-mcp/blob/v0.0.83/README.md)。

### R09 / P0：目录搜索仍可读取被子路径规则拒绝的普通文件

**位置：** `tools/search.py:31–32,50–58,115–141`。

本轮不使用软链接：在项目的普通 `private/notes.txt` 放合成标记，并配置该子路径 deny。直接读取判定为 deny，但 grep 根目录经 Python 回退仍返回该标记。

```json
{"probe":"recursive_search_ignores_child_deny","direct_permission":"deny","found":true}
```

上次软链接漏洞已修复，但只授权根目录不等于授权所有子文件。枚举、打开、读取都需遵守相同的实际资源规则；对 rg 路径也要做一致性验收。建议把“能否读取此具体文件”作为共享函数，不在 grep、glob、文档解析中复制各自规则。

### R10 / P1：引用“检查通过”仍不足以证明阅读过，导出后编号也可能失去出处

**位置：** `tools/docs.py:77–94`，`artifacts.py:186–198`，`tools/web.py` 的二进制下载分支。

`citation_checks()` 只把 kind=search 标为未阅读全文；kind=file 的下载记录即使尚未解析也会通过已知编号检查。探针创建仅下载的来源记录，生成带 `[S1]` 的 Markdown，全部 artifact checks 为通过，但文件里没有来源 URL。

```json
{"probe":"downloaded_source_and_export","exported_source_url":false,"all_artifact_checks_pass":true}
```

因此不能把“编号存在”“HTTP 抓取完成”“模型读到相关段落”“主张得到支持”合成一个绿色状态。导出文件也需要自带参考文献/脚注，不能只能回到应用里找 `[S1]`。这是报告可信度和可分享性的缺口，不是要求把所有引用正确性都交给另一轮模型裁判。

### R11 / P0：Anthropic 缺少结束事件时仍可进入 RunTools

**位置：** `llm/anthropic.py:46–56,109–128`，`loop.py:80–102`。

用现有 Anthropic 测试的 SSE 数据删除末尾 `message_delta` 和 `message_stop`，让项目已安装的真实 SDK 解析。SDK 返回累积 message，stop_reason 为 None；适配器把 None 当作 end_turn，循环允许该 message 中的工具调用。

```json
{"probe":"anthropic_missing_terminal","stop":"end","next_step":"RunTools"}
```

探针只计算 next_step，没有执行工具。这说明 F05 应在所有协议上建立一致契约，而不只修 Chat 的默认值：必须记录完整结束事件；缺失、未知或失败状态均不允许副作用工具。不要假设 SDK 的 `get_final_message()` 等同服务端发送了完整终止信号。

## 5. 仍需补足、但本轮不夸大为已复现故障的部分

| 领域 | 当前边界 | 近期动作 |
|---|---|---|
| 任务预算与并发 | max_turns 为全局值；多 schedule 可同时启动；恢复后 turns/usage 的累计不完整 | 加每任务模型/工具调用数、墙钟上限、全局后台并发；先记录 token，再接价格表 |
| 业务结果未知 | 超时主要通过工具错误文本提示模型；缺少持久操作状态约束 | 外部写操作保存 prepared/approved/started/confirmed/unknown，不允许模型换个 call ID 静默重试未知操作 |
| 飞书重放 | 消息有稳定幂等键；但需核验服务端去重有效期及查询回执能力 | 不把同 key 当无限期 exactly-once；超时、长时间停机后先对账 |
| 浏览器资源 | 一个 profile + 一次一个 owner 是合理起点；下载还未登记到产物 | 补受控下载、文件引用、重启后页面/登录状态提示；暂不为每任务建全套浏览器 |
| 主动调研体验 | 新站点 fetch 常需确认；任务可能长时间等人 | 为研究任务提供明确的公开读取策略/域名范围，创建时配置；外写仍独立确认 |
| 历史检索 | 查询前同步所有 session；查询路径同步完成文档解析；返回摘要但缺少完整证据读取接口 | 增量入队索引、独立连接/worker、按 ref 阅读命中上下文、范围过滤 |
| 解析与性能 | 上传/预览已有 to_thread；Agent read、文档生成和 recall 仍有同步重工作 | 定位事件循环阻塞；大文档解析限制页数/解压量/耗时，失败可取消 |
| 产物质量 | 能重新打开、可提取文本；Office 预览主要是抽取文本 | 显示“结构检查”而非笼统验收；版式、计算、事实分别检查；先用真实模板验证 |
| 输入/历史资源上限 | 50MB 上传限制主要靠 header 和读完后的检查；图片按原尺寸 base64；SSE 只限数量 | 流式限大小、图片规范化、payload 字节预算、长历史分页 |
| 文件冲突与锁 | 文件检查和替换间没有资源锁；SessionLog.open 的尾行修复未获取 writer 锁 | 补跨会话冲突测试；读取只忽略不完整尾行，持锁恢复才允许截断 |
| 备份一致性 | SQLite backup 是单库一致；随后复制 JSONL/blobs 不是同一时刻快照 | 首版要求停止服务备份/恢复，或明示在线备份一致性边界；增加 manifest 校验与恢复演练 |
| 前端验证 | 构建通过，没有自动浏览器 E2E | 覆盖附件发送、审批、断线恢复、文件下载、任务状态五条路径 |

这些事项应进入计划，但没有用户规模样本前，不先部署队列服务、向量数据库、复杂工作流引擎或性能监控集群。

## 6. 修订后的技术方案

### 6.1 保留当前存储方案，让 SQLite 接收成为原子操作

上轮建议过把接收事件写入 JSONL。当前实现已经将未消费 inputs、requests、schedules 放在 SQLite，**本轮建议明确承认它们是权威数据并修好事务，不为追求旧方案一致性再搬一次存储。** 对话消息仍以 JSONL 为权威。

新增一个小入口 `accept_input(...)`，由 HTTP 和 scheduler 共用：

```text
SQLite 事务：
  验证 request_id / session / payload
  写入或取得 input
  如来自调度，关联 occurrence 并更新 next_run
COMMIT
返回 input_id 与当前状态

可重试投递器：
  找 accepted input → 取得 session writer → 创建/关联 run
  向 JSONL 追加带 input_id 的 USER → 标记 consumed
```

SQLite 与 JSONL 之间仍通过 input_id 对账，而不是假装跨存储原子提交。现有 `_not_in_log` 可继续复用。投递器在启动和 tick 期间运行；因 busy 暂缓的输入不必等下一次重启。

具体调整：

- `requests` 与 `inputs` 同事务；也可以逐步将 request_id/payload_hash 合并到 inputs，避免冗余状态，迁移时保留兼容。
- occurrence 保存 input_id；以 occurrence 身份稳定去重。手动立即运行使用独立 request ID，避免同一秒内的执行被合并。
- 对 dropped/blocked/failed 的提交返回真实状态，让客户端明确区分“请求重试”与“用户重新执行”。
- 不增加 Redis/Celery，不引入通用 outbox 框架；现有 inputs 就可以充当待投递记录。

### 6.2 外部写操作先准备，再审批，最后执行同一份内容

用少量受控 operation 描述代替模型自行组合所有 CLI flag。保持官方 lark-cli 为执行后端，不从零实现飞书协议。

```mermaid
flowchart LR
    A[模型提出业务操作] --> B[校验目标和读取附件]
    B --> C[冻结正文/附件/账户/目标]
    C --> D[生成预览及 payload hash]
    D --> E{用户确认}
    E -->|确认| F[执行冻结的操作]
    E -->|拒绝| G[保留草稿]
    F --> H[回执核验]
    H --> I[confirmed 或 unknown]
```

最小新增状态只需要：operation_id、kind、account_id、target_id、payload_hash、内容/附件快照引用、CLI/version、approval_id、idempotency_key、status、receipt。凭据不写入该记录。

- `preview` 是宿主模式，不是可由模型传 `--dry-run=false` 改写的参数。
- dry-run 预览失败时，不以一张无法核对正文的卡继续真实外发；先解决预览或明确展示完整规范载荷。
- mandatory confirmation 在 hook 之后收敛；hook 不能用 allow 覆盖用户规定的外发确认。
- 确认只适用于本次目标与载荷。引用的文件变化、账户切换、目标变化都会失效。
- unknown 状态需查询、人工决定或明确安全重试；“模型被提示不要重试”不能替代执行层约束。

这可以先只覆盖现有五类飞书写操作，无需把所有工具改造为通用事务系统。对浏览器则先做参数级能力分类和一次性确认，禁止通用脚本绕开业务授权。

### 6.3 完成状态由执行结果和交付证据共同确定

继续保留 `task_result` 作为模型报告工具，但调整为 proposal，并规定优先级：

1. 进程/模型/工具的致命失败、取消、未结束、预算用尽先决定 run 状态。
2. 只有正常结束的 run 才接受 changed/unchanged；正常结束不等于任务成功。
3. changed 若声称生成文件，必须关联本轮 artifact ID 和验证状态；远端写操作必须有 confirmed receipt。
4. unchanged 保存至少一份已检查来源/比较结果的说明，不能把“搜索失败”解释为“没有变化”。
5. 多次互相矛盾的 task_result 返回待核验，不简单 last-wins。

通知从最终状态派生：有新结果、错误、需要用户时通知；无变化且检查成功才静默。模型等待人的 run 与真正后台计算分开显示，不让用户误以为任务仍在持续处理。

### 6.4 恢复应验证“能继续工作”，不只验证“包能解开”

首版采用离线一致备份：检查服务/写者状态，停止后备份；恢复到 staging 目录，验证完成后再替换目标。在线备份后续再实现明确 checkpoint 协议。

- manifest 保存格式版本、文件清单、长度/hash、原 home、必要配置引用；私钥/登录态仍按用户明确选项处理。
- 内部路径尽量保存相对 home 的引用，恢复时统一重定位；保留外部项目目录映射并检查是否存在。
- 不覆盖正在运行的服务目录；`--force` 先验证 archive，再移动现存 home，失败可回滚。
- 验收时移动原 home，打开一个历史会话、下载一份附件和旧版文件、检索记录、以假模型继续任务。
- 明示项目原始工作目录未被完整备份；已登记版本有副本不代表全部工作文件都已保护。

### 6.5 来源和文档验收保留不同层次

把现有 Source 逐步扩为：`discovered → fetched → extracted → inspected`。其中 inspected 表示具体页/段落进入可访问上下文，不自动表示结论正确。

一条来源至少保留 URI、获取时间、内容 hash、快照/文本引用、页码/段落定位、可见范围。引用应绑定具体版本，不能 URL 相同就覆盖旧报告的依据。

导出时自动为 `[S#]` 生成带标题、URL/文件定位、访问日期的文献表或脚注；Word/PDF 中保留可点击链接。UI 将校验分成四类：

- 文件可打开、结构完整；
- 引用编号可定位、来源状态正确；
- 关键数字/计算有检查；
- 版式与事实质量已人工验收或尚未验收。

优先做好 Markdown、Word、PDF 这三种已实现输出。Excel/PPT 的专用生成器、OCR 和高保真排版，应等真实材料证明缺口后加入。

### 6.6 检索范围与性能用小改动改善

先保持 SQLite FTS5，不引入向量库。将 project_id/session_id/access_scope 放进查询契约，增加 `read_record(ref, range)` 读取命中上下文；UI 全局搜索与模型可自动检索的范围分别配置。

按用户已确认的选择，默认当前项目优先，跨项目由明确用户意图启用，并在界面显示查询范围；提供项目排除/不索引选项。无项目归属的会话默认限本会话及用户明确选定的记录，避免隐式全局搜索。记录索引、来源索引和附件解析通过有界后台队列更新；每次 recall 不同步重扫所有历史。

重点处理事务失败 rollback、解析错误不使整个索引卡住、删除/权限变化传播。资料规模小的阶段，准确的来源定位和过滤比引入 embedding 更重要。

## 7. 新的实施顺序与工作量

旧方案是能力建设路线，M0–M3 已有实现。本轮不再照旧“往后做 M4”，改为四个可验收的小阶段。下表为单人专注开发日估计，不含等待用户授权和样本收集，不是日历承诺。

| 阶段 | 范围 | 估计 | 验收门槛 |
|---|---|---:|---|
| R-A 执行与授权一致性 | R05/R06/R07/R08/R09/R11；协议结束、规范参数、冻结载荷、浏览器和文件子资源策略 | 4–7 天 | 对应探针变为正式回归；未经确认的外写不进入执行；缺终止流不调工具 |
| R-B 投递与恢复 | R01/R02/R03/R04；事务接收、补投、最终结果、异地恢复 | 4–6 天 | 每个关键写入间注入退出后可恢复；失败不报无变化；旧目录不存在仍能用备份 |
| R-C 文档证据与试用体验 | R10；独立参考文献、校验分层、索引范围、最小预算/诊断和五条前端 E2E | 3–5 天 | 导出文件离开应用仍可核查；关键 UI 路径通过；能定位失败原因和费用 |
| R-D 真实任务试用 | 6 个任务 × 2 次，记录介入与失败；按最高频问题修订 | 约 1–2 周使用窗口 | 形成有证据的下一轮优先级；不把缺少用户样本算作已通过 |

核心修订约 **11–18 个专注开发日**；实际从 R-A 开始，每完成一组就验证，不一次承诺全部完成。输入事务和授权规范化的改动如果明显膨胀，应缩小首批场景，不扩大框架。

### 可直接拆分的首批工作项

| 工作项 | 主要文件 | 最小交付 |
|---|---|---|
| 统一终止契约 | `llm/anthropic.py`、其它适配器、`loop.py` | 完整终止、拒绝、截断、异常 EOF 的参数化协议测试 |
| 受控飞书 operation | `connectors/feishu.py`、`runtime.py`、`runs.py` | 宿主 preview、冻结文件、账户/目标/hash 绑定、不可被 hook 放宽的确认 |
| 浏览器能力分类 | `connectors/browser.py`、`policy.py` | tabs 新建/关闭/列表分开、全部导航共用 scope、受控输出路径 |
| 文件读取授权 | `tools/search.py`、文件工具权限公共函数 | 显式 deny 子文件不出现在递归结果；rg/Python 一致 |
| 原子接收和调度投递 | `index.py`、`runs.py`、`scheduler.py` | 接收/去重/occurrence 关联事务，待投递恢复和可观测状态 |
| 结果裁定 | `scheduler.py`、任务 UI | 错误优先、产物/回执绑定、静默条件可验证 |
| 恢复与路径迁移 | `maintenance.py`、`index.py` | staging 恢复、内部路径重定位、原 home 移走后的 round-trip |
| 证据和导出 | `artifacts.py`、`tools/docs.py`、`render.py`、文件 UI | 来源状态、版本与定位、独立文献表、检查标签分层 |

## 8. 先试什么，怎样判断好不好用

用户尚未充分使用，不建议直接跑上轮规划的 40 题大评测。先做以下 6 个高频任务，每项重复两次；第一次记录使用障碍，第二次观察修订后是否稳定。

| 用例 | 输入与输出 | 必查内容 |
|---|---|---|
| U1 本地材料简报 | 2 份 PDF/Word + 1 张表 → Word/PDF 简报 | 关键事实、页码、表格值、文档能打开和版式 |
| U2 公开信息更新 | 一个研究问题 → 3–5 个来源 + 比较报告 | 真的读过依据、日期准确、引用随导出保留；首次验收真实搜索服务 |
| U3 飞书材料整合 | 用户指定的文档/日程 → 本地会议准备包 | 身份、读取范围、文档定位、未授权写操作为 0 |
| U4 审批后提交 | 在专门测试位置发布一份本地草稿 | 卡片内容与真实提交逐字一致；拒绝、文件变化、超时路径都可解释 |
| U5 定时更新 | 同一主题两期任务，中间重启/休眠 | 不丢投递、不重复、不把错误当无变化；确有变化才产新版本 |
| U6 历史与恢复 | 找上次结论/旧版文件；恢复到新 home | 命中原文可读、来源可打开、原 home 不存在仍可继续 |

真实模型和搜索会产生费用，真实飞书写入需要用户指定测试位置并确认；本轮没有执行这些业务。没有授权或数据的用例标记“未验证”，不替换成 fake 后宣布端到端完成。

每次只记录八项：是否完成、是否有错误事实、产物检查、人工澄清次数、必要审批次数、故障救援次数、总耗时、tokens/可计费用。必要审批不算低效率；排错救援才是优先减少的摩擦。

上线试用前的确定性条件：已接受输入不丢、恢复不重复未知外部效果、取消/失败不误报成功、审批内容与执行内容一致、备份可用。试用通过条件：12 次结果逐项人工评阅，严重边界问题为 0；其它失败有可复现原因和修订记录。样本仍小，不据此宣称全面超过竞品。

## 9. 与三家竞品相比，下一步真正值得争取的优势

本轮没有重新对三家进行完整横向实测。沿用上轮固定源码版本和调研结论，针对已经出现的实现问题调整借鉴顺序：

| 参考 | 这次最应借鉴的内容 | 本项目的具体落点 |
|---|---|---|
| pi durable | durable submission、明确 replay policy | 接收事务和恢复状态；只吸收语义，不引入整个实验包 |
| OpenCode 2 | 工具参数/权限、附件与变更体验、事件契约 | 参数级浏览器权限、产物可验证、关键界面 E2E |
| DeepSeek Harness | 持久 inbox、工具 unknown outcome、资源 ownership | occurrence 到 input 的交接、操作回执、浏览器任务所有权 |

参考：[pi durable 固定版本](https://github.com/earendil-works/pi/blob/136b87e8a7f5a3e5186de0289523ba327dc592fc/packages/durable/README.md)、[OpenCode V2 工具](https://opencode.ai/v2/docs/tools)、[DSH agent-loop 固定版本](https://github.com/deepseek-ai/deepseek-harness/blob/22ddee3792402c3e70ec5f86a63c3e3e895a2f60/packages/core/agent-loop/README.md)。具体能力与版本限定见上轮报告，本轮不把它们表述成新增测得的竞品优势。

竞争目标应落到用户每天可感受到的结果：本地与飞书资料快速找回，中文报告可信且格式可用，草稿与发布内容一致，机器休眠后任务可恢复，错误不会被静默隐藏。暂时不追求比三家拥有更多工具名或更复杂编排。

Code Mode 的启动条件：试用中存在反复发生的批量工具往返瓶颈，结构化结果和授权边界已经稳定，并能用同题对比证明收益。多代理的启动条件：至少两类真实任务能独立分工，额外协调费用没有抵消时间收益。没有达到这些条件时，继续单代理。

## 10. 本轮交付与下一次复核标准

本轮仅新增本报告和隔离探针脚本，没有修复业务代码，没有更改用户资料，也没有提交或推送 Git。

复核顺序建议：先读第 4 节定位问题，按第 7 节拆分修订，再按第 8 节组织试用。下一轮审查应该收到：修订提交、故障回归结果、异地恢复记录、6 个真实任务的执行与产物记录；仅有新的功能列表或测试数量增加不足以代替这些证据。

复现命令（仓库根目录、当前虚拟环境）：

```bash
.venv/bin/python docs/research/2026-10-24-re-review-probes.py
```

脚本输出的是当前行为观察，不是修复后的期望断言。修复时应把每组结果转成正式测试：例如重复请求应仍可取得唯一 accepted input，恢复后的 session path 应位于新 home，缺终止的 Anthropic 流应拒绝调用工具。脚本中的假连接器不能替代固定版本真实 CLI 的契约测试。
