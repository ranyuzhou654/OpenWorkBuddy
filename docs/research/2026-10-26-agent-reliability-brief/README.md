# U2 第一次真实研究任务

已交付可审阅的[研究简报 Word](research-brief.docx)与[Markdown](research-brief.md)。这是 OpenWorkBuddy 草稿经 Codex 校订、离线导出的辅助交付；**原 Agent 运行超时，U2-1 不计为独立完成，人工验收仍 pending**。

## 范围和结果

用户确认“继续，我已经验证过搜索了”后，仅执行一次 U2-1；未重复连通性测试。使用现有 opendesign / deepseek-v4.1-flash，响应模型名为 deepseek-flash。运行复用产品 Workspace、上下文治理、来源/导出工具和 Tavily 后端；限制为整次任务 12 次模型请求、180 秒、24 次工具调用、最多 3 次搜索。未提供飞书、浏览器、shell、跨项目历史或用户全局材料。

实际完成 3 次 Tavily 搜索，获取并向模型上下文投递了 5 个官方来源的片段，包含 Anthropic、LangGraph、OpenTelemetry 与 MCP；各页面原文、提取文本与片段 hash 均保存在原始 trial 的 blob 存储中，sources.json 保留定位信息。来源状态 inspected 只代表片段进入请求上下文，不代表读过全部页面或已核实全部事实。

运行发起 **12 次模型请求、收到 11 次响应**，SDK 重试为 0，在 **180.01 秒**硬超时。已记录输入 **329,378**、输出 **17,896** token；输入包含 **282,624** 缓存读取 token，不重复相加。最后一请求无终态用量，usage_complete=false；模型套餐与 Tavily 货币费用均 unknown。

Agent 三次创建/覆盖同一 Markdown，未生成 Word。期间有 1 次不存在的 AGENTS.md 读取与 2 次未知 bash 工具调用错误，均有原始日志；不存在实际 shell 执行。原始 Markdown 中“未新增收费模型调用”无法由本次记录支持，“真实来源仍待确定”未区分本期网页与后续飞书范围，关于路径核验失败成因的表述也不准确。

## 辅助交付与验证

Codex 对照原页面和冻结项目材料完成一轮内容校订，补充项目来源 S1、明确发布日期/访问日期、将建议与现有机制分开，并如实写明实际调用和超时。校订保留了五个官方来源，但去掉与本项目决策关系不大的其他系统成绩。约 1,500 个中文字的正文与文献表共用一份 Markdown，离线生成 Word，未增加模型或 Tavily 请求。

使用 documents 技能的 bundled Python / python-docx 导出，以 bundled LibreOffice 渲染，全部 **3 页**逐页查看。修正初版标题尾部孤立换行、参考文献网址重复，复查后正文、页码、引用及标题均可读，5 个官方来源链接可点击。本检查属于 Codex 技术与来源初查，独立人工验收仍待完成。

新增编排本地检查 **2 passed，0.06 秒**，验证搜索封顶、原文快照保留、工具能力范围及日志不包含注入的假密钥；使用假模型和拦截网络，不计入真实 trial。ruff 和 diff 检查通过。产品源码未修改，仍使用 d1f52f5eb9a739c3e239f0041032ab4ebe4aadbf79714cdfd7be74254d652ba6；没有把新检查数加成一次未执行的全量测试结果。

## 证据位置

- original/：原始任务、Markdown、请求与搜索账本、会话、结果卡及冻结信息的复制，原目录保持不变。
- sources.json / reviewed-citation-checks.json：6 个实际使用的来源（5 个官方页面、1 份项目资料）及引用检查。
- reviewed-body.md / build_reviewed.py / review-changes.diff：校订正文、离线导出脚本和相对原稿的差异。
- qa-word/：内部逐页渲染检查材料；validation.json 和 evidence-sha256.json：验证与 hash 清单。
- 原始 trial：eval-runs/rd-research-2026-10-26；包含源码包、完整源 blob 和请求投影，不含用户密钥。

## 下一步

优先修复交付阶段：只生成一次正文，由工具确定性导出多格式；增加可用工具提示与收尾预算。U2-2 尚未执行，后续试跑需重新冻结版本并保留 U2-1 失败。U3/U4 仍待具体飞书文档、日历范围及专用发布位置，本轮没有业务文档读取或外部发布。
