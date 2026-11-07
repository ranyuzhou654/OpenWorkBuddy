# OpenWorkBuddy 第一期 AI 技术研究简报：Agent 可靠性与上下文治理

资料截止日 2026-10-26；访问日期 2026-10-26。本期首次建立来源与决策基线，尚无跨期变化结论。

## 结论

上下文治理不必从零建设。官方资料一致把上下文视为有限资源：token 越多，模型准确召回其中信息的能力越差，应对手段是压缩、结构化笔记与子代理隔离 [S3]。本项目 R-C 已实现来源状态、内容版本与快照、实际上下文片段记录、导出文献表和四层校验；本期只需把这些记录接到“是否变化”的判定上。

变化判定应默认走确定性路径，模型只做候选发现与成文。项目记录显示，R-D 第二轮 U5 出现应判 changed 或 unchanged 却落成 unknown、规范路径与相对路径核验不一致的情况，说明宽松比较会污染周报结论。

一周内可落地三项小改动：来源基线表、笔记式上下文回读、失败可归因。验收看可复现、可回溯、失败不冒充成功，不看测试通过数。当前全量 402 项测试与 6 项浏览器 E2E 通过属工程回归证据，不代表业务成功率，也不能替代仍待完成的人工评阅。

## 来源与日期

本期从 Anthropic、LangChain、OpenTelemetry、MCP 选出 5 个官方来源，均读取正文后引用：

- Anthropic《Effective context engineering for AI agents》，发布 2025-09-29 [S3]。
- Anthropic《How we built our multi-agent research system》，发布 2025-06-13 [S17]。
- LangChain《Checkpointers》，发布日期 unknown [S9]。
- OpenTelemetry GenAI 语义约定的 agent span 文档，发布日期 unknown，属性标注为 Development [S12]。
- MCP 规范 Tools 页，协议版本 2025-06-18，发布日期 unknown [S18]。

访问日期均为 2026-10-26。这些多为 2025 年材料，引用因为其是被广泛参照的官方做法，不表示它们属于最近一周的新闻。

## 原文事实

- 上下文变长会降低召回准确度；压缩是把接近上限的对话摘要后重开窗口，结构化笔记把要点落到窗口外再回读，子代理各用独立窗口、只回传约 1,000–2,000 token 的浓缩结果 [S3]。
- Anthropic 报告多代理系统比聊天多耗约 15 倍 token、单代理约 4 倍，并在其内部评测中领先单代理 90.2%。这是其内部结果，不是本项目成绩 [S17]。
- Agent 有状态且错误会累积，失败应从断点恢复而非重跑；回答“为什么没找到信息”需要完整生产追踪 [S17]。
- LangGraph checkpointer 在每个 super-step 保存快照并按 thread_id 组织，支撑人工介入、回溯与容错；节点失败时保留同一步已成功节点的 writes，恢复时不重跑 [S9]。其 async 模式在进程崩溃时可能丢 checkpoint，sync 则有性能开销 [S9]。
- OTel 约定 agent span 名为 invoke_agent 加 agent 名，error.type 出错时条件必需且须为低基数类别，相关约定仍为 Development [S12]。MCP 区分协议错误与工具执行错误，给出 outputSchema 时服务端必须返回符合 schema 的结果，客户端应校验结果并设超时 [S18]。

## 两种处理方式的比较

方式 A 为快照门禁：每期固定采集同一组来源，保存正文快照与哈希，仅哈希变化或权威来源增删才触发改写，模型只负责成文。它确定、可复现、成本低，与 R-C 的内容版本和四层校验直接衔接 [S3][S9]；缺点是语义等价的重写、页面改版、锚点迁移会误报变化，需显式豁免。

方式 B 为代理式重检索：每期由 agent 自主搜索、抓取并判断是否构成实质进展。它能发现基线外来源与语义级变化，接近 Anthropic 描述的动态检索 [S3]；缺点是成本与不确定性高，易把旧材料当作本周进展，须强制回读正文并保留引用。

建议混合而门禁归 A：B 只产生候选，任何 changed 必须附新来源或新快照证据，无证据一律 unknown。这与 MCP 区分协议错误与工具执行错误同理——工具失败只能得 unknown，不能得 unchanged [S18]。

## 一周内三项改进与验收条件

1. 来源基线表。在导出文献表之上加固定字段：来源 ID、发布日期或 unknown、访问日期、正文快照哈希、上期判定。验收：同周输入重跑两次，除时间戳外判定表一致；每个 unknown 有原因字段；未成功读取的来源不得记为 unchanged，也不出现未实际读到的来源。

2. 笔记式上下文回读。沿用已有 B0/B1 的 spill/prune/compact 开关，新增本期笔记（已读要点、待核事实、判定表指针），续跑与重跑先回读笔记而非重复抓取。验收：同期重跑抓取次数下降而引用集合与结论不变；写入上下文的片段都能对应来源 ID；不修改日常产品默认值。

3. 失败可归因。按 OTel 对齐 gen_ai.operation.name、gen_ai.agent.name、error.type 等字段，使每一步可还原归属与错误类别 [S12]，沿用已有 usage 与事件追加机制。验收：失败运行能定位到具体步骤与错误类别，而非只报整体 error；失败与超预算运行单列，不并入成功；统计只计已完成人工评阅的任务，测试数、请求数、任务数分列。

## 下期更新规则

满足任一条件才更新正文，否则只追加“本期无实质变化”：已基线来源的正文哈希变化且涉及 API、默认行为、限制或推荐做法；出现提出不同做法的新官方来源；上期为 unknown 的项被正文核实。判定顺序为哈希、逐条比对、人工确认，前两步由脚本完成，人工只判语义是否实质；仅改版、迁移、导航变化计入豁免清单。每期保留判定表快照与豁免清单，便于回看结论为何改变。该规则为设计，尚未运行验证。

## 局限

本期未执行第二期，跨期判定与豁免规则只有设计；R-D 尚未通过验收，U2 是首个研究题，真实来源与发布位置仍待确定。检索仅 3 次，未覆盖其他规范版本；OpenTelemetry 与 MCP 两页无页面级发布日期，只能记 unknown。未调用飞书，未做外部写入，也未新增收费模型调用。

## 参考文献

- [S3] [Effective context engineering for AI agents \\ Anthropic](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents) · https://www.anthropic.com/research/effective-context-engineering-for-ai-agents · 访问 2026-10-26T13:31:57Z · inspected · source text characters 0-24171 · SHA256 66044d08063071ab6078ae58f392bc85117f59e0cebdd750f79ff92bcfe26cbc
- [S9] [Checkpointers - Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/checkpointers) · https://docs.langchain.com/oss/python/langgraph/checkpointers · 访问 2026-10-26T13:31:58Z · inspected · source text characters 0-48572; lines 1-150 (line caps 2000); lines 190-309 (line caps 2000); lines 430-549 (line caps 2000) · SHA256 1a93bdc1742b25648b1cb419ddc87ef718887dc199e5a9a3d9c7b4215bb40413
- [S12] [semantic-conventions-genai/docs/gen-ai/gen-ai-agent-spans.md at main · open-telemetry/semantic-conventions-genai · GitHub](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md) · https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md · 访问 2026-10-26T13:31:57Z · inspected · source text characters 0-200362; lines 1-60 (line caps 2000); lines 290-399 (line caps 2000) · SHA256 0aff37de7ae9071d4bc30329d2887c957abde9a346d87f1dbea5027397012fb2
- [S17] [How we built our multi-agent research system \\ Anthropic](https://www.anthropic.com/research/multi-agent-research-system) · https://www.anthropic.com/research/multi-agent-research-system · 访问 2026-10-26T13:32:05Z · inspected · source text characters 0-28667 · SHA256 ba687a7f96355ff9dbb8e9e12bba4cf7e7756aec24f0bf253cb16ab79748f800
- [S18] [Tools - Model Context Protocol](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) · https://modelcontextprotocol.io/specification/2025-06-18/server/tools · 访问 2026-10-26T13:32:36Z · inspected · source text characters 0-12990 · SHA256 88a0c9c1e3b345697c8a2272692081facfa74ca19566ad8dd5df58d08353d8f5

来源状态仅表示获取、提取或上下文读取进度；不代表事实或计算已经核验。
