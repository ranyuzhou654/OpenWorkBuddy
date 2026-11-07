# R-A：执行与授权一致性修订

日期：2026-10-25（Asia/Shanghai）。对应 [二轮复审](../design/2026-10-24-personal-assistant-re-review.md)
的 R05/R06/R07/R08/R09/R11。状态：本地实现与回归完成；真实办公外写验收未进行。

## 实现与验收

| 问题 | 当前实现 | 证据 |
| --- | --- | --- |
| R05 审批与执行读到不同文件 | 正文冻结为内存字节、通过 stdin 执行；导入文件使用私有快照。保存规范载荷 hash、账户、CLI 路径/hash/版本与 approval_id | 审批期间修改源文件，执行仍使用已展示的旧版本；账户、二进制、快照、目标或正文被替换时不写入 |
| R06 hook 放宽固定确认 | hook 后再次收敛固定规则；保留显式 deny；飞书写入和浏览器交互只能单次确认 | workspace/trust 下 hook=allow 仍询问；拒绝和取消无写入；取消记录 expired |
| R07 CLI 参数语义不一致 | 五类结构化 operation 生成唯一 argv；preview 是宿主模式。兼容的旧写 argv 先转为同一 operation，冲突、重复和身份/幂等参数被拒绝 | 多种冲突参数在 tool_started 前拒绝；fake CLI 按最终布尔值解析，避免继续掩盖原缺陷 |
| R08 浏览器入口绕过 | tabs/list 与 new/close/select 分开；new 与 navigate 使用同一 URL scope；交互固定每次确认；输出路径只在会话目录内 | 跨站点、危险 scheme、越界路径、软链接、覆盖已有文件、错误只读 hint 均有回归 |
| R09 根目录许可越过子路径拒绝 | 共享具体文件读取检查；遍历时剪枝；Python 和 rg 均检查文件，内容搜索只接收许可文件列表 | files/content/count/glob × 两种后端；禁止打开拒绝文件，并检查 rg 不接收目录或拒绝文件 |
| R11 异常协议结束仍执行 | 保留上一阶段的三协议正常结束判定、摘要终止检查与循环拒绝执行规则 | 本轮全量回归保留；不把已取得的本地协议验证说成所有服务商验收 |

飞书结构化写操作覆盖 `send_message`、`reply_message`、`create_document`、`update_document`、
`import_document`。首批消息只支持 text/markdown；文档修改只支持 append/overwrite/str_replace。
CLI 的 `--as`、`--profile`、`--dry-run`、`--idempotency-key` 和输出格式控制参数不由模型指定。

审批卡及终端展示冻结后的完整内容与 hash。Web 审批同时将 preview 存入 SQLite；会话私有的
`feishu-operations/op_*.json` 保存准备、审批、开始、确认或未知状态。确认只消费一次，不能覆盖已有 confirmed 回执。
源草稿后续编辑不会自动加入已冻结请求；需要新内容时应重新准备和确认。

CLI 成功且返回稳定结果 ID 才记录 confirmed；它是 CLI 回执验证，**不是独立远端状态复核**。
超时、取消、CLI 报错或缺少结果 ID 都不能当成成功。执行开始后结果不确定时，保存 unknown；
同一会话、同一账户与规范业务载荷即使换 call ID、重开 Workspace，也不能直接重发。
confirmed 则恢复本地回执，不再次写入。幂等键是附加机制，不声称 exactly-once。

## 验证结果

- 全量：**301 passed，0 failed、0 errors、0 skipped，65.64 秒**；JUnit 内部计时 65.593 秒。
- 新增 R-A 回归：**52 项**。这些是本地测试场景，不是真实业务任务完成率。
- `ruff check src tests evals`、smoke 脚本静态检查、前端 TypeScript/Vite 构建、`git diff --check` 通过。
- 本机官方 CLI **1.0.98**：仅运行离线 dry-run，核对 stdin 正文逐字保留，包括 `@literal` 与 `--dry-run=false`。
- 实际 Playwright MCP **0.0.83**：隔离临时 profile，只打开 `about:blank`，成功保存并验证 PNG（4,254 字节）。

第一次真实浏览器冒烟暴露了模拟器未覆盖的接口约束：服务端不允许在其声明根目录之外保存截图。
已让内置浏览器服务的 cwd 使用应用 home，从而包含受控会话输出；增加对应回归后重跑成功。
没有添加 unrestricted-file-access，也没有放开模型传入的绝对文件路径。

原始证据：

- [全量 JUnit](2026-10-25-ra-tests.xml)
- [集成冒烟记录](2026-10-25-ra-smoke.json)
- [可重跑冒烟脚本](2026-10-25-ra-smoke.py)：`.venv/bin/python docs/research/2026-10-25-ra-smoke.py --browser`
- [正式回归源码](../../tests/test_authorization.py)

冒烟 JSON 只保留合成内容的核验结果，不公开本机账户 ID 或认证配置。
本轮新增收费模型调用 **0**、真实飞书外写 **0**、外部网站访问 **0**。

## 当前范围与后续

浏览器通用脚本、配置/存储读写、未知新工具均不开放；本地文件上传也暂不开放，直到具备冻结附件的审批路径。
点击、输入、hover、历史后退和标签页切换/关闭每次确认，不允许长期授权。
站点许可仍遵循用户的预设和站点规则；全信任下的导航策略与直接 navigate 一致。
搜索保留 rg 的 ignore 筛选，Python 回退沿用原有目录过滤；两者均遵循子文件权限，不声称正则或 ignore 语义完全一致。

unknown 当前保持隔离，不提供自动核验或自动解锁入口；需要操作人核验远端结果后处理。
本地操作日志、账户重查和路径约束不构成针对任意受信任宿主脚本或同一用户恶意修改进程的 OS 级保证。
真实飞书账户写回、登录网站上的危险操作、Linux 环境及人工文档验收均不在本轮验收范围内。

R-B 的输入事务、调度补投、最终任务状态、异地恢复，以及 R-C 的来源证据与导出参考文献仍待实现。
首轮开发集的 9/12 自动检查通过、人工评阅待完成状态不变。
R-A 已改变产品源码，原 v2 的源码 freeze 不再匹配；保留原冻结文件，后续使用新的 freeze 和 run 目录，不能混用成绩。

接口核对依据：[固定版本 Playwright MCP](https://github.com/microsoft/playwright-mcp/blob/v0.0.83/README.md)、
[官方 CLI runner](https://github.com/larksuite/cli/blob/017b19a793ad71794a02887ede04757f3d82e10d/shortcuts/common/runner.go)。
本轮没有提交或推送 Git 变更。
