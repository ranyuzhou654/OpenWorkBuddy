# Agent 可靠性与上下文治理简报

OpenWorkBuddy 第一期　资料截止日 2026年10月26日　面向项目开发者

## 本期结论

建议沿用现有小内核，优先补齐来源变化判定、单次生成多格式交付和失败定位。项目已有来源快照、上下文片段记录、文献表、预算及恢复机制；下一周的工作应补任务交付中的缺口。[S1]

本期建立资料和决策基线。选用的五份官方资料包括两篇2025年文章和三份未标页面发布日期的文档，访问日期均为2026年10月26日；跨期变化留待下一期对照。

## 来源与日期

Anthropic 上下文工程文章发布于2025年9月29日 [S3]，研究系统文章发布于2025年6月13日 [S17]。LangGraph 检查点文档 [S9]、OTel Agent 追踪文档 [S12] 的页面发布日期为 unknown；MCP 工具页 [S18] 的协议版本为2025-06-18，页面发布日期为 unknown。完整链接和快照哈希列于文末。

## 官方资料提供的依据

Anthropic 将压缩、窗口外笔记和独立上下文列为长任务的处理方式，同时提醒压缩可能丢失关键细节。[S3] 其研究系统经验还强调追踪执行过程、在失败后保留进度。[S17] 这支持优先复用本项目现有机制，不能据此推断本项目任务成功率提高。

LangGraph 的 checkpointer 按 super-step 保存状态，并将检查点组织在线程中；它还保留同一步已成功节点的写入，供失败恢复使用。[S9] 这是持久化设计的参考，采用这一思路不要求本项目迁移整个框架。

OpenTelemetry 的 Agent 追踪约定仍标为 Development，包含操作、Agent 名称及错误类型等字段。[S12] MCP 工具规范则区分协议错误与工具执行错误，并讨论结果结构和调用超时。[S18] 两者可作为已有事件字段的对照，规范状态和版本需要随来源一起保留。

## 两种处理方式及其取舍

方式 A 是固定来源基线。每期读取同一组官方页面，保存正文快照，先比较哈希，再检查内容差异。优点是容易复核，能复用已有来源版本；局限是导航和页面改版也会改变哈希。哈希变化只能产生候选，是否影响 API、行为或限制仍需判断。

方式 B 是每期重新搜索。它可以发现基线外的新资料，但结果集合和排序会变化，也可能再次找到旧文章。必须读取正文并标清日期，才能判断发现的是新证据还是旧资料。

建议以 A 承担变化判定，以 B 补充候选。确认有实质变化时更新报告；无变化需要成功读取和比较的证据；读取失败、证据不足或结果不确定时保留 unknown。以上为本项目的设计建议，本期尚未验证跨期效果。

## 下一周三项改进

### 复用来源记录建立变化判定表

在已有来源编号、快照、哈希和日期字段上补充比较对象、差异摘要、判定及原因。统一规范路径和 URL 的比较口径；已有路径核验修复也纳入真实任务复测。[S1]

验收条件：相同输入重复判定一致；只改导航不更新结论；实质差异有前后快照；未读到的页面不能判为无变化。来源发布日期 unknown 与访问日期分开保留。

### 单次成文后导出多种格式

为检索、阅读和交付分配阶段预算，尽早保存正文。增加从同一份已保存正文导出 Markdown 和 Word 的路径，避免模型重复发送整篇内容。本项目已有导出与校验机制，可在其上补充这个入口。[S1]

验收条件：两种格式的正文和引用一致；转换本身不新增模型请求；达到预算时保留已生成文件并明确未完成项；总预算不因进入收尾阶段而重置。

### 将工具错误接入失败定位

在已有 run、tool 和 usage 账本上整理错误类别及所处步骤，参考 OTel 字段命名，同时保留原事件和约定版本。[S12] 未知工具应返回可用工具提示；重复失败时优先保存进度，避免继续消耗交付预算。

验收条件：能从结果卡定位错误调用和缺失产物；缺少响应的用量保持未知；失败、超时和辅助整理分别记录。测试数、模型请求数与已验收任务数独立统计。

## 下期更新规则

对照本期来源基线，检查已读正文的实质差异、新增官方来源，以及上期 unknown 是否得到核实。纯格式、导航或 URL 迁移记录为豁免；结论变化保留差异依据。开始下一期前冻结本期判定表，再安排语义复核，之后才决定是否更新或发布。本期尚未执行第二期，也未进行飞书发布。

## 本次交付说明

OpenWorkBuddy 使用既定模型执行了3次 Tavily 搜索并读取五个官方来源，发起12次模型请求、收到11次响应，在180秒处超时，留下 Markdown 草稿但未生成 Word。已记录用量不完整，模型套餐及搜索费用均待核账。

本版本由 Codex 对照来源校订并离线导出 Word：补充了项目事实的引用，纠正草稿中的过时状态和“未新增模型调用”表述，并将建议与已实现能力分开。原试用仍记为超时，独立人工验收待完成。

## 参考文献

- [S1] project-context.md · file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-research-2026-10-26/U2-1/workspace/project-context.md · 访问 2026-10-26T13:31:23Z · inspected · lines 1-199 (line caps 2000) · SHA256 5f1ea5a32cced554bcdb431b9d2cfa7d36cf39ad2d423f35f540430403e823d1
- [S3] [Effective context engineering for AI agents \\ Anthropic](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents) · https://www.anthropic.com/research/effective-context-engineering-for-ai-agents · 访问 2026-10-26T13:31:57Z · inspected · source text characters 0-24171 · SHA256 66044d08063071ab6078ae58f392bc85117f59e0cebdd750f79ff92bcfe26cbc
- [S9] [Checkpointers - Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/checkpointers) · https://docs.langchain.com/oss/python/langgraph/checkpointers · 访问 2026-10-26T13:31:58Z · inspected · source text characters 0-48572; lines 1-150 (line caps 2000); lines 190-309 (line caps 2000); lines 430-549 (line caps 2000) · SHA256 1a93bdc1742b25648b1cb419ddc87ef718887dc199e5a9a3d9c7b4215bb40413
- [S12] [semantic-conventions-genai/docs/gen-ai/gen-ai-agent-spans.md at main · open-telemetry/semantic-conventions-genai · GitHub](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md) · https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md · 访问 2026-10-26T13:31:57Z · inspected · source text characters 0-200362; lines 1-60 (line caps 2000); lines 290-399 (line caps 2000) · SHA256 0aff37de7ae9071d4bc30329d2887c957abde9a346d87f1dbea5027397012fb2
- [S17] [How we built our multi-agent research system \\ Anthropic](https://www.anthropic.com/research/multi-agent-research-system) · https://www.anthropic.com/research/multi-agent-research-system · 访问 2026-10-26T13:32:05Z · inspected · source text characters 0-28667 · SHA256 ba687a7f96355ff9dbb8e9e12bba4cf7e7756aec24f0bf253cb16ab79748f800
- [S18] [Tools - Model Context Protocol](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) · https://modelcontextprotocol.io/specification/2025-06-18/server/tools · 访问 2026-10-26T13:32:36Z · inspected · source text characters 0-12990 · SHA256 88a0c9c1e3b345697c8a2272692081facfa74ca19566ad8dd5df58d08353d8f5

来源状态仅表示获取、提取或上下文读取进度；不代表事实或计算已经核验。
