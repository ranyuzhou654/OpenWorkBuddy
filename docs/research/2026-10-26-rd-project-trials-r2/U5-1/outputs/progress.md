# OpenWorkBuddy 实施验证进度

## 本期变化

上期基线为 R-B（2026-10-26T00:09:33，pytest 340）[S5]；`current.json` 现已推进到 **R-C**（2026-10-26T11:49:42，pytest 372）[S4]，阶段、用例数、浏览器层、验证边界四类均有变化，故本期出新版本。

| 维度 | 上期（R-B）[S5] | 本期（R-C）[S4] | 判定 |
|---|---|---|---|
| stage | R-B | R-C | 推进 |
| 快照时间 | 00:09:33 | 11:49:42 | 新 |
| code_sha256 | 10a4af75…34b04 | ee5f13e0…510c6 | 变更 |
| pytest 用例 | 340 | 372（+32） | 增加 |
| pytest failures / errors / skipped | 0 / 0 / 0 | 0 / 0 / 0 | 保持通过 |
| browser_e2e | **字段缺失，未记录** | 已记录，5 expected / 0 unexpected | **本期首次有数据** |
| 真实模型请求 | `real_model_requests: 0` | 改用 `paid_provider_requests: 0` | 仍为 0（字段改名） |
| 真实外部写入 | `real_external_writes: 0` | 改用 `real_office_writes: 0` | 仍为 0（口径收窄，见下） |
| 托管 CI | false | false | 未改善 |
| 断电测试 | false | **字段消失，状态未知** | 不可视为不变 |

三点需要明确区分：

- **不是"全部变好"**：pytest 与 browser 层确有推进，但托管 CI 仍未验证、真实模型仍为 0、断电一致性从"未测"变成"连字段都没有"，边界是**扩大披露但未实质收窄**。
- **上一期遗留的缺口已补**：浏览器数据此前是缺失而非通过 [S5]，本期已具备可比数据 [S4]。
- **上一期的"时间倒挂"已消解**：上期报告指出 `current.json`(00:09 R-B) 落后于 `period-two.json`(11:49 R-C) [S5]；现在 `current.json` 内容即为 R-C 快照 [S4]，两者一致。

## 当前快照

| 项 | 值 |
|---|---|
| stage | R-C [S4] |
| at | 2026-10-26T11:49:42.795833+08:00 [S4] |
| git_sha | 66dc15b0ef5d11399e4ed089348079bb0ce96113 [S4] |
| code_sha256 | ee5f13e078ede8793f26530eb0adc93c0a5dfcbbe04965014739b47d8e1510c6 [S4] |
| web_sha256 | 651b30ff9c51699b6581044c1a0e775b8aecf9a5185d68d2bcf5521c02a52505 [S4] |
| tests_sha256 | c9f854fa447b541732a857bac9ffcd2c80de546ae93ac46a82d41aa99f456921 [S4] |
| platform | macOS-27.2-arm64-arm-64bit，Python 3.12.12 [S4] |

`git_sha` 与上期相同（66dc15b…），但 `code_sha256` 变了 [S4][S5]：代码在工作树/测试侧有改动而提交未更新，快照对应的是**未提交的工作树状态**，不是某个 commit 的可复现产物。

## pytest

- **372 用例，errors 0，failures 0，skipped 0，耗时 60.918 s** [S4]。上期为 340 / 0 / 0 / 0 / 64.464 s [S5]。
- 用例数 +32、总耗时反而 -3.5 s。原因（并行化、用例构成变化、机器状态）快照未说明，**不作推断**；不能据此认定性能提升。
- R-C 阶段用例数记为 `rc_cases: 32` [S4]（上期为 `rb_cases: 39` [S5]）。这是不同阶段各自定义的用例集，**32 与 39 不可直接比较**，也不等于 pytest 的 372。
- 两轮均 0 失败、0 跳过，pytest 维度为"通过且改善"（覆盖量增加）。但 372 个用例具体覆盖哪些模块，快照未列出，不能据此推断真实模型或外部副作用路径已被覆盖。

## browser_e2e

本期首次具备数据，来源为 `browser` 块 [S4]（上期完全缺失 [S5]）：

| 项 | 值 |
|---|---|
| 框架 / 引擎 | playwright 1.64.1 / Chromium 156.0.8078.4 [S4] |
| expected / unexpected / flaky / skipped | 5 / 0 / 0 / 0 [S4] |
| duration_ms | 5221.606 [S4] |
| workers / retries | 1 / 0 [S4] |
| real_http_sse | true [S4] |
| model | deterministic local fake [S4] |
| temporary_home_only | true [S4] |

必须同时读出的限定：

- **"real_http_sse: true" 只覆盖前后端之间的真实 HTTP/SSE 传输**，模型一侧是确定性本地假实现 [S4]。因此这 5 个用例证明的是 UI→服务→流式返回的链路，**不证明真实模型的质量、时延或失败模式**。两者不能合并成"端到端已用真实模型验证"。
- **计时口径不同**：browser 的 5221.606 ms 与 pytest 的 60.918 s 是不同运行、不同范围 [S4]，不可相加或互相印证。
- **调度节奏不具代表性**：`fixture_scheduler_tick_seconds: 0.1` 对 `product_scheduler_tick_seconds: 30` [S4]，测试把调度周期压缩了 300 倍，产品真实节奏下的定时行为仍未被验证。
- 5 个用例覆盖面偏窄，且 retries 0、workers 1，未暴露并行或重试路径。
- 截图审查仅记为 `four validation layers visible at 1440x1000` [S4]，是单分辨率的人工观察结论，未附评审人，也未覆盖其他断点。

## 质量闸门

`ruff_src_tests_evals`、`frontend_build`、`git_diff_check` 三项均 passed [S4]。上期快照未记录这三项，属本期新增披露。

## 验证边界（本期结论的适用范围）

| 边界 | 值 | 含义 |
|---|---|---|
| paid_provider_requests | 0 | 未调用付费模型服务，成本与真实响应质量均未验证 [S4] |
| live_search_requests | 0 | 联网检索路径未验证 [S4] |
| real_office_writes | 0 | 未发生真实 Office 写入；注意此口径比上期的 `real_external_writes` 窄，**只覆盖 Office 一类**，其他外部写入仍是空白 [S4][S5] |
| hosted_ci_verified | false | 托管 CI 仍未验证，结果仅出自本机 macOS-27.2-arm64 [S4] |
| rd_user_trials | `0/12; not executed` | 12 场研发用户试用一场未跑 [S4] |
| formal_holdout_run | false | 正式留出集未运行 [S4] |
| generated_document_human_review | `not_assessed` | 生成文档未经人工评审 [S4] |
| cost | null，`unknown: package billing not reconciled` | 成本未知，账单未对账 [S4] |
| original_v2_freeze | `preserved; new source freeze required before another model run` | 既有冻结保留；再跑模型前需重新冻结源码 [S4] |
| 断电/崩溃一致性 | **无字段** | 上期为 `power_loss_tested: false` [S5]，本期该字段消失，状态**未知**，既不能记作"未测"延续，也不能记作"已测" |

结论因此限定为：**本机 macOS 上，372 个自动化用例与 5 个浏览器链路用例（本地假模型）全部通过，3 项质量闸门通过，CLI 备份/恢复完整性在此前一轮已验；真实模型、联网检索、托管 CI、用户试用、留出集、成本核算均未覆盖。**

## 仍未解决

1. **`rc-tests-initial.xml` 的存在**：artifacts 中同时有 `2026-10-26-rc-tests.xml` 与 `2026-10-26-rc-tests-initial.xml` [S4]，"initial"暗示曾有一次初跑。快照只报告 0 失败，无法判断初跑是否出现过失败/抖动后被重跑覆盖。需要确认 372/0/0/0 取自哪一个产物。
2. **断电一致性字段丢失**：需明确是"未测"还是"误删"，否则该边界无法逐期跟踪。
3. **未提交工作树**：`git_sha` 未变而 `code_sha256` 变 [S4][S5]，当前 R-C 结果无法通过 SHA 复现，需确认是否会补提交。
4. **成本未知**：`cost: null` [S4]，即使付费请求为 0，打包计费未对账，不能声称零成本。

## 来源

- [S4] `/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26-r2/U5-1/workspace/current.json`（本轮读取，R-C 快照）
- [S5] `/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26-r2/U5-1/workspace/progress.md`（本轮读取的上一版本，R-B 基线）

上期版本中的 [S1]–[S3] 属于上一次运行的编号，本轮不作引用。

## 参考文献

- [S1] current.json · file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26-r2/U5-1/workspace/current.json · 访问 2026-10-26T08:44:42Z · inspected · lines 1-26 (line caps 2000) · SHA256 57009dc158c86dfaa2af757d86f643e56c33399ed0919ea5fff30e54aa1933fb
- [S3] period-one.json · file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26-r2/U5-1/workspace/period-one.json · 访问 2026-10-26T08:44:48Z · inspected · lines 1-26 (line caps 2000) · SHA256 57009dc158c86dfaa2af757d86f643e56c33399ed0919ea5fff30e54aa1933fb
- [S4] current.json · file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26-r2/U5-1/workspace/current.json · 访问 2026-10-26T08:45:22Z · inspected · lines 1-56 (line caps 2000) · SHA256 6b53253fd476b10b8b06c911001966a920abcc6745b0aa16dcb62ef52bbff1cc
- [S5] progress.md · file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26-r2/U5-1/workspace/progress.md · 访问 2026-10-26T08:45:22Z · inspected · lines 1-64 (line caps 2000) · SHA256 7016078a57de7675347d300c504506a20e59d9a8c0098308c630bef0bd415e7b

来源状态仅表示获取、提取或上下文读取进度；不代表事实或计算已经核验。
