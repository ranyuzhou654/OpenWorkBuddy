# OpenWorkBuddy 开发者 AI 技术研究简报

第 1 期 · Agent 可靠性与上下文治理 · 资料截止 2026-10-26

## 结论

本期是首次建立来源与决策基线，尚无跨期变化结论。可执行的主线有三条。

1. "要不要更新周报"应是可判定的三态 changed / unchanged / unknown，由来源快照比对产出，而不是让模型复述后自行判断；抓取或读取失败一律记 unknown，不得当作无变化。
2. 上下文治理采用"最小的高信号 token + 按需检索 + 压缩留痕"，与项目已实现的 B0/B1（关闭/启用 spill、prune、compact）口径一致，不需要改产品默认值[S1]。
3. 建议的三项改进都挂在既有 R-C 来源状态、内容版本/快照、实际上下文片段记录和文献表四层校验之上[S1]，属于扩展而非从零新建。

项目记录另显示：Tavily 配置已在本机保存，飞书本人连接器已启用，文档与日历只读 scope 均已核验；但 U2–U4 业务试用、真实办公写回的人工验收都未完成，费用 unknown[S1]。因此本期严格只读，不外发、不写入飞书。

## 来源与日期

| 编号 | 来源 | 版本或发布日期 |
| --- | --- | --- |
| [S3] | Anthropic，《Effective context engineering for AI agents》 | 发布 2025-09-29 |
| [S7] | LangChain 文档，LangGraph《Checkpointers》 | 发布日期 unknown |
| [S17] | Model Context Protocol 规范 | 版本标识 2026-07-28 |
| [S18] | OpenTelemetry GenAI 语义约定《Agent and framework spans》 | 状态 Development，发布日期 unknown |

访问日期：2026-10-26（以上四篇均于当日读取正文）。四篇均为项目方或标准组织官方资料；[S3] 发布于 2025-09-29，是已发表的技术文章，不是本周新闻。

## 方案比较

方案 A：快照比对 + 三态门控。每周对每个来源保存规范 URL、正文哈希、抓取状态与快照路径，只比较结构化差异（版本号、规范状态、关键段落哈希），输出 changed/unchanged/unknown。事实依据：LangGraph 的 checkpointer 在每个 super-step 保存状态快照，并保留可回溯的历史与父指针，`update_state` 生成新快照而不改写旧快照[S7]。推断：同样的快照语义可以用在"来源"而非"图状态"上，成本低且可复算。代价是必须先做正文规范化，否则页脚、时间戳、镜像站会造成假变化；且它只能说明"文本变了"，说明不了"变更是否实质"。

方案 B：定期重检索 + 模型判定。每周重新检索并压缩摘要，由模型对照上期结论判断是否变化。事实依据：Anthropic 指出 agent 按需检索与压缩能在长任务中维持连贯，但运行期探索比预取更慢，且需要足够好的工具与启发式，否则会浪费上下文[S3]。推断：这种方式对"变化含义"的判断更灵活，但同一输入重跑结果可能不同，失败与"无变化"也难以区分。代价是可追溯性弱，人工复核成本高。

取舍建议：以 A 作为门控层（决定是否更新），B 只用于对 changed 项做一次含义归纳，并把该归纳标为推断。上下文层面取折中：预置少量固定背景（相当于 CLAUDE.md 式前置），其余靠 glob/grep 按需读取，压缩时保留架构决策与未解问题、清理已消费的工具原始输出[S3]。若一周内只能做一件事，选 A。

可观测性上，OpenTelemetry 已把 `invoke_agent`、`plan`、`execute_tool` 等操作列入 GenAI 语义约定，span 名建议为 `invoke_agent {gen_ai.agent.name}`，但整体状态仍是 Development，属性可能变化[S18]；MCP 规范把请求定义为"无状态、自包含"，并要求宿主在调用工具前取得明确同意[S17]。推断：本项目当前不宜直接绑定该 span 命名，先在自己的调用记录里保留同名字段做映射即可。

## 一周内的 3 项改进

1. 周报变化门控文件。为每个来源记录规范 URL、sha256、抓取状态（fetched/failed/skipped）与快照路径，生成 week-over-week delta。验收：对同一冻结输入重跑两次，delta 文件字节一致；任一来源 failed 时该条只能为 unknown；新增至少 6 项确定性回归（含 1 项"failure 不得降级为 unchanged"）。

2. 引用到快照的强绑定。正文每个 [S#] 在导出前校验：来源存在、正文已实际读取、快照哈希匹配；文献表由工具自动生成，不手工重写。验收：删除任一被引快照后导出必须失败并报出具体 source ID；未读来源与页码误当 source ID 的用例全部报错（沿用已修复的复合引用解析[S1]）。

3. 上下文组成与恢复的留痕。在逐调用 usage 上增加上下文分类字段（系统提示 / 检索片段 / 工具结果 / 历史），并在压缩触发前后各记一次；恢复沿用累计日志，不覆盖前半段用量。验收：本期周报运行的 context 组成字段覆盖率 100%，缺失处显示 unknown；中断后续跑能产出产物且用量为累计值；最终状态为 error 的 trial 不得被计为 unchanged 或成功。

三项都不改产品默认值，也不以测试通过数代表业务成功率：全量 402 项通过是历史回归证据[S1]，与周报可用性无关。

## 下期更新规则

实质变化（触发更新）：官方来源发布新版本或新规范版本；规范状态由 Development 变为 Stable；出现破坏性 API 变更；被引用的关键段落被改写或删除；新增官方章节直接涉及本期结论。

非实质变化（不更新，仅记录）：措辞微调、页脚与版权年份、访问计数、第三方转载或社区解读、镜像页差异。

判定流程：哈希变化 → 结构化字段比对 → 对 changed 项做一次人工确认 → 只有确认后才改结论。unknown 不算无变化；连续两期读取失败的来源要在正文显式标注，不得静默沿用旧结论。每期保留上期 delta 与快照，形成可回放基线；本期尚无第二期数据，上述规则未经验证。

## 不足

本任务仅有 project-context.md 与公开网页，无飞书、浏览器、shell；Tavily 搜索 3 次、正文读取 4 篇，覆盖面有限。[S7][S18] 页面无发布日期，只能标 unknown；MCP 规范以版本号 2026-07-28 标识，未核对其 changelog 的具体改动。三态门控与 OTel 字段映射均为推断，尚未在本项目运行验证。

## 参考文献

- [S1] project-context.md · file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-research-2026-10-26-r2/U2-2/workspace/project-context.md · 访问 2026-10-26T14:14:27Z · inspected · lines 1-199 (line caps 2000) · SHA256 5f1ea5a32cced554bcdb431b9d2cfa7d36cf39ad2d423f35f540430403e823d1
- [S3] [Effective context engineering for AI agents \\ Anthropic](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents) · https://www.anthropic.com/research/effective-context-engineering-for-ai-agents · 访问 2026-10-26T14:14:38Z · inspected · source text characters 0-24171 · SHA256 3cb879c63d2df435f14d2c9f74223439095fa1da456b399bc49c855fd9646ac9
- [S7] [Checkpointers - Docs by LangChain](https://docs.langchain.com/oss/python/langgraph/checkpointers) · https://docs.langchain.com/oss/python/langgraph/checkpointers · 访问 2026-10-26T14:14:38Z · inspected · source text characters 0-48572; lines 120-239 (line caps 2000); lines 240-389 (line caps 2000); lines 430-559 (line caps 2000); lines 553-642 (line caps 2000) · SHA256 09c5e2d39729323b8288a24388e2c54f7682fe6e48fd28979023ed833615be37
- [S17] [Specification - Model Context Protocol](https://modelcontextprotocol.io/specification/2026-07-28) · https://modelcontextprotocol.io/specification/2026-07-28 · 访问 2026-10-26T14:14:40Z · inspected · source text characters 0-8269 · SHA256 e2e33dabfbeee2387cbad86d522033fd2cf0c3e5e9112eddefa0ab7ff19ad961
- [S18] <https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-agent-spans.md> · 访问 2026-10-26T14:15:16Z · inspected · source text characters 0-147973 · SHA256 3b643ca21097d2889a87e714e01e8729b4729f636ff1df14a1cabaf2e0f93615

来源状态仅表示获取、提取或上下文读取进度；不代表事实或计算已经核验。
