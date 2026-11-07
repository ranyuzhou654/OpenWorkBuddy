# R-D 仓库资料本地试用包

这是项目真实文档的历史快照回放，不是业务用户自然连续使用，也不是正式 holdout。用户已确认“先用当前仓库材料推进本地试用”：6 次本地 trial，opendesign / deepseek-v4.1-flash，每次跨全部阶段合计最多 12 次模型请求、180 秒，SDK 重试 0，套餐费用 unknown。

输入只来自本仓库，不扫描个人目录、不复制真实应用 home。`sources/` 保存原文件，`manifest.json` 保存原件和转换件 SHA-256；`cases.json` 固定请求。原件中的“本次”“已授权”等属于历史材料。

| 用例 | 实际路径 | 两次 trial 的区别 |
| --- | --- | --- |
| U1-1 / U1-2 | Workspace 读取两份 PDF/Word 与 CSV，创建 Word/PDF | 项目简报 / 简历证据备忘录 |
| U5-1 / U5-2 | Scheduler.run_now → 接收入库 → RunManager → 执行 → 结果裁定，中间关闭并重建 Workspace | R-B→R-C 有变化 / 同一 R-B 快照无变化 |
| U6-1 / U6-2 | 生成历史笔记 → 索引 → 移除当前笔记 → 新会话 recall/read_record | 普通重建 / 原 home 移走后离线恢复到新 home |

U5 是手动触发和同一进程内 Workspace 重建，未验证操作系统休眠、跨周自然使用或进程崩溃恢复。U6 针对专用 trial home；外部项目工作目录仍存在，不在应用备份中。通知仅记录本地事件，未验证原生通知。

U2 缺实际问题和搜索服务；U3/U4 缺启用的飞书、读取来源及专门测试发布目标。本包不执行这 6 个槽位。

## 执行与证据

```sh
uv run python evals/run_project_trial.py preflight evals/project-trial-v1
uv run python evals/run_project_trial.py run evals/project-trial-v1 eval-runs/rd-project-2026-10-26
```

preflight 不请求模型；run 会使用选定连接，必须处于已确认范围。输出目录必须是新路径，失败不覆盖、不自动重跑。每次一个新进程；两期共用 12 次调用额度，父进程 180 秒硬超时终止进程组。模型仅可用声明的本地文件和当前项目历史工具，没有 shell、目录枚举、网络、浏览器和连接器；模型服务会收到本轮输入。

每次保留 public.json、会话、调用账本、产物检查、调度输入或备份恢复清单和 review-card.json。八项指标为完成、错误事实、产物检查、澄清、必要审批、救援、总耗时、token/费用。运行 ok 不等于人工验收完成。

输入 PDF 与 Word 已由 Codex 逐页检查，不代替输出验收。准备时发现 LibreOffice 缺字体配置导致中文未显示，其 PDF 转换还出现提取文字错序；最终输入 PDF 使用嵌入 TrueType 字体并核对提取文字，详见 manifest.input_qa。

重新准备必须使用新空目录：用 dependency loader 返回的 bundled Python 执行 prepare；按 documents 技能渲染并检查 Word；执行 finalize，再检查最终 PDF。此环境的 LibreOffice 需显式设置 bundled FONTCONFIG_FILE。复制已确认任务卡并记录 hash 与实际 QA，未检查不得设置 visual_qa=inspected。

## 首轮后的脚本修订

首轮实际执行脚本保存在结果目录的 runner.py，证据包另存 executed-runner.py。首轮把 scratch_dir 误设为项目目录，U6 两次受到会话范围限制，保留为受脚本配置影响的失败，不据此评价跨会话检索。现有脚本已将 scratch 与 project 分开，并断言检索范围为当前项目；回归还会确认读到旧笔记，而不是本轮提问本身。

首轮禁用了目录枚举，首次判断旧报告是否存在只能尝试 read，错误被保守裁定为 unknown。修订后的脚本开放仅 trial 项目目录内的 glob，并说明来源中提及的附件未必在输入包中；累计调用超限也改用产品 BudgetExceeded，保留 limit 状态。以上修订只做了本地回归，未追加真实模型重跑。上面的“无目录枚举”描述对应首轮冻结脚本。
