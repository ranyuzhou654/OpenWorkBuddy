# OpenWorkBuddy 实施验证进度

报告版本：首期（本目录此前无 progress.md）｜数据时点：2026-10-26T00:09:33.776413+08:00（快照记录时间）

## 本期变化

**本期为首次建立进度基线**，因此不存在可对比的旧 progress.md。但与同目录内的上一份快照 `period-two.json` 对比，`current.json` 出现**验证边界收缩**，属于需要关注的实质变化，不能按"无变化"处理：

1. **browser_e2e 证据整体消失。** `period-two.json` 记录了完整的浏览器端到端结果块（playwright 1.64.1 / Chromium 156.0.8078.4，expected 5、unexpected 0、flaky 0、skipped 0、duration 5221.606 ms、workers 1、retries 0、real_http_sse true）[S3]；`current.json` 中**没有任何 browser 字段**，`web_sha256` 与 `tests_sha256` 也已不存在 [S1]。本轮**无法据 current.json 判定 browser_e2e 通过或失败**——该维度是"无证据"，不是"通过"。按任务口径，这不构成"无变化"。
2. **pytest 数量与代码指纹同时回退。** 测试数 372 → 340（−32），耗时 60.918 s → 64.464 s，code_sha256 由 `ee5f13e0…` 变为 `10a4af75…` [S3][S1]。两端 `git_sha` 均为 `66dc15b0…` 未变 [S1][S3]。即同一提交下代码哈希与测试规模不一致，推断 `current.json` 反映的是较早的一次运行状态而非当前代码状态；此为推断，材料未说明原因。
3. **快照时间戳倒序。** current.json 的 `at` 为 00:09:33，早于 period-two.json 的 11:49:42，进一步支持第 2 点为"旧状态覆盖"而非新进展 [S1][S3]。
4. **工程校验与审计项缺失。** `checks`（ruff_src_tests_evals / frontend_build / git_diff_check 均 passed）、`ui_screenshot_review`、`artifacts`（4 个带哈希产物）仅见于 period-two.json，current.json 未记录 [S3][S1]。

结论：本轮不能视为"验证前进"，应视为 current.json 所承载的验证证据范围较上期收窄。

## pytest

| 指标 | current.json（本轮） | period-two.json（上期） | 变化 |
| --- | --- | --- | --- |
| tests | 340 [S1] | 372 [S3] | −32 |
| errors | 0 [S1] | 0 [S3] | 持平 |
| failures | 0 [S1] | 0 [S3] | 持平 |
| skipped | 0 [S1] | 0 [S3] | 持平 |
| time (s) | 64.464 [S1] | 60.918 [S3] | +3.546 s |

pytest 本身在本轮**无失败**（errors 0 / failures 0 / skipped 0）[S1]。但测试总数下降 32 例，与 `rc_cases 32` 的数量恰好相等 [S3][S1]，推断减少的部分很可能对应 R-C 阶段新增的用例集；材料未明确断言，仅作提示。

`period-one.json` 与 `current.json` 在全部记录字段上完全一致（stage R-B、pytest 340/0/0/0、64.464 s、rb_cases 39、cli 各项相同）[S2][S1]，即 current.json 等同于 period-one 的内容，未携带 period-two 的任何增量。

## browser_e2e

- R-B 快照（`current.json`、`period-one.json`）：**无 browser 字段**，本维度无证据 [S1][S2]。
- R-C 快照（`period-two.json`）：expected 5 / unexpected 0 / flaky 0 / skipped 0，单 worker、零重试，真实 HTTP+SSE，模型为确定性本地假实现（deterministic local fake），仅使用临时 home 目录 [S3]。
- 对比结论：**browser_e2e 从"有通过证据"退回"无记录"**。按任务要求不将其计为"无变化"；同时也不将其表述为失败——本轮没有产生任何可判定为失败的浏览器结果记录。

## 验证边界

| 边界项 | current.json | period-two.json |
| --- | --- | --- |
| real_model_requests | 0 [S1] | —（未记录该字段） |
| paid_provider_requests | —（未记录） | 0 [S3] |
| real_external_writes | 0 [S1] | —（改以 real_office_writes 记录） |
| real_office_writes | —（未记录） | 0 [S3] |
| live_search_requests | —（未记录） | 0 [S3] |
| hosted_ci_verified | false [S1] | false [S3] |
| power_loss_tested | false [S1] | —（未记录） |
| cli 备份/恢复 | backup_exit 0、restore_exit 0、format 2、integrity_verified true、仅临时 home [S1] | —（未记录） |
| 成本 | —（未记录） | null，状态 unknown：package billing not reconciled [S3] |
| 真人评审 / 用户试用 / 正式留出集 | —（未记录） | generated_document_human_review not_assessed、rd_user_trials 0/12 未执行、formal_holdout_run false [S3] |
| 源冻结 | —（未记录） | 原 v2 冻结保留，再次模型运行前需重新冻结源 [S3] |
| 平台 / Python | macOS [S1] | macOS-27.2-arm64-arm-64bit / 3.12.12 [S3] |

边界要点：

- **零真实外部副作用**：本轮可核实的口径为零真实模型请求、零真实外部写入（real_model_requests 0、real_external_writes 0）[S1]；上期以付费供应商请求 0、实时搜索 0、真实 Office 写入 0 表述 [S3]。两组字段名不同、数值同为 0，**不能据此认定口径完全等价**，仅能认定两期各自记录范围内均为零。
- **托管 CI 未验证**（两期均 false）[S1][S3]，断电/掉电恢复本轮未测试（power_loss_tested false）[S1]。
- **成本与真人维度在本轮当前快照中不被覆盖**：上期已明确成本状态未知、生成文档未做人工评审、12 例用户试用 0 例执行、未跑正式留出集 [S3]；本轮 `current.json` 既不确认也不否认这些，属证据缺口。

## 阶段与代码标识

| 项 | current.json | period-two.json |
| --- | --- | --- |
| stage | R-B [S1] | R-C [S3] |
| git_sha | 66dc15b0ef5d11399e4ed089348079bb0ce96113 [S1] | 66dc15b0ef5d11399e4ed089348079bb0ce96113 [S3] |
| code_sha256 | 10a4af75…34b04 [S1] | ee5f13e0…1510c6 [S3] |
| rb_cases / rc_cases | 39 [S1] | 32 [S3] |

阶段标识由 R-C 回到 R-B，且 git_sha 未变而 code_sha256 变化 [S1][S3]。这说明 current.json 并非 R-C 之后的又一次推进快照。

## 下轮跟进建议

1. 确认 `current.json` 为何缺少 browser 块与 `checks`/`artifacts`：若为采集脚本漏写字段，应补齐；若确为回退到 R-B 状态，应在快照中显式标注取代关系与时间。
2. 在同一 git_sha 下复核 372 → 340 的用例差额来源，避免把用例减少误读为测试通过。
3. 补记成本核对状态、真人评审与用户试用进度，使验证边界各字段在各期快照间可比。

---

来源：S1 = workspace/current.json；S2 = workspace/period-one.json；S3 = workspace/period-two.json。本轮仅读取本目录内文件，未使用网络或其他目录。

## 参考文献

- [S1] current.json · file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26-r2/U5-2/workspace/current.json · 访问 2026-10-26T08:46:10Z · inspected · lines 1-26 (line caps 2000) · SHA256 57009dc158c86dfaa2af757d86f643e56c33399ed0919ea5fff30e54aa1933fb
- [S2] period-one.json · file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26-r2/U5-2/workspace/period-one.json · 访问 2026-10-26T08:46:16Z · inspected · lines 1-26 (line caps 2000) · SHA256 57009dc158c86dfaa2af757d86f643e56c33399ed0919ea5fff30e54aa1933fb
- [S3] period-two.json · file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26-r2/U5-2/workspace/period-two.json · 访问 2026-10-26T08:46:16Z · inspected · lines 1-56 (line caps 2000) · SHA256 6b53253fd476b10b8b06c911001966a920abcc6745b0aa16dcb62ef52bbff1cc

来源状态仅表示获取、提取或上下文读取进度；不代表事实或计算已经核验。
