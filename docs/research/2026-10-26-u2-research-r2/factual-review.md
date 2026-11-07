# U2-2 初步事实对照

评阅者为 Codex，与本次实施相同，并非独立人工验收。未修改 Agent 正文，只对核心外部陈述作有限对照；仍需用户评阅建议的适用性及整篇事实。

| 原稿陈述 | 对照结果与依据 |
| --- | --- |
| Anthropic 文章发表于 2025-09-29；运行期探索较慢，可采用少量预置背景加按需读取 | 与[官方文章](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)的日期及取舍说明相符。该内容不是本周新闻。 |
| LangGraph 在 super-step 保存快照，update_state 不改写旧快照 | 与[官方 Checkpointers 文档](https://docs.langchain.com/oss/python/langgraph/checkpointers)相符。由图状态快照推广到网页来源门控，是原稿已标明的推断。 |
| MCP 2026-07-28 页描述无状态、自包含请求，宿主调用工具前应取得同意 | 与[该版本规范](https://modelcontextprotocol.io/specification/2026-07-28)相符。规范也说明协议本身不能强制执行这些原则；不能据此宣称应用授权已通过验证。版本标识不是经另行核实的发布日期。 |
| OTel agent span 约定处于 Development，列有 invoke_agent、plan、execute_tool | 与[官方源码文档](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-agent-spans.md)相符。原稿简写的 span 名存在适用条件：agent name 可用时采用含名称的形式；具体系统可另定格式。不能视为稳定接口保证。 |

原稿提到的 402 项通过属于冻结项目材料中的历史阶段；本轮实现后的回归结果为 417 项。两者所处源码版本不同，正文不作事后改写。三态规则、删除快照后拒绝导出、上下文分类覆盖率等属于建议和验收目标，不是本轮已经完成的功能。本次未验证跨期更新、自然休眠、业务飞书资料或实际审批发布。

Codex 对官方页面的复核另行进行，不计入 Agent 的 3 次 Tavily 搜索；没有增加 opendesign 模型请求。来源 inspected 表示部分或全部提取文本进入请求上下文，不等同于通读整页或事实验收。
