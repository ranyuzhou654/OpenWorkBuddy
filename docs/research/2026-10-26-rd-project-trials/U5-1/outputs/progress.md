# OpenWorkBuddy 实施验证进度

**本期变化（R-B → R-C，有变化，出新版）**

1. **阶段推进**：R-B → R-C，快照时间 `00:09:33` → `11:49:42`，代码哈希 `10a4af75…` → `ee5f13e0…`；新增 `web_sha256`、`tests_sha256` 两个独立指纹 [S2]。
2. **pytest 扩容**：340 → 372 用例（+32），仍为 0 错误 / 0 失败 / 0 跳过，耗时 64.464 s → 60.918 s [S2]。增量与新增 `rc_cases: 32` 数目吻合 [S2]，两条证据互相印证。
3. **上期最大缺口已补上**：`browser_e2e` 上期完全缺失，本期出现结构化 `browser` 块——Playwright 1.64.1 + Chromium 156.0.8078.4，expected 5 / unexpected 0 / flaky 0 / skipped 0，5221.606 ms [S2]。**但覆盖面窄，不足以支撑"端到端已验证"（见第 4 节）。**
4. **新增质量闸**：`checks` 块首现，ruff、frontend_build、git_diff_check 三项均 passed [S2]。
5. **证据链出现新问题**：`artifacts` 声明的 4 个产物文件，在声明的路径上**逐一读取均失败（不存在）**（详见第 6 节）。这比上期更值得警惕——上期只是没写细节，本期给了哈希却拿不到文件。
6. **上期 CLI 证据块消失**：`cli`（backup_exit / restore_exit / format / integrity_verified）在 R-C 中整块不再出现 [S2]，上期该结论目前无新证据支撑。
7. **`power_loss_tested` 字段从 false 变为"不存在"** [S2]——不是变好了，是**不再记录**，按未验证继续挂账。
8. **仍未闭合**：`hosted_ci_verified: false` 与上期一致 [S2]；新增暴露 `cost_status` 未对账、`rd_user_trials` 0/12 未执行、`formal_holdout_run: false` [S2]。

## 1. 当前基线事实

| 项目 | R-B [S3] | R-C [S2] |
| --- | --- | --- |
| 阶段 | R-B | R-C |
| 快照时间 | 2026-10-26T00:09:33.776413+08:00 | 2026-10-26T11:49:42.795833+08:00 |
| git_sha | `66dc15b0…` | `66dc15b0…`（未变） |
| code_sha256 | `10a4af75…` | `ee5f13e0…`（已变） |
| web_sha256 | — | `651b30ff…` |
| tests_sha256 | — | `c9f854fa…` |
| 平台 | macOS | macOS-27.2-arm64-arm-64bit |
| Python | — | 3.12.12 |

注意 `git_sha` 未变而 `code_sha256` 变了 [S2][S3]：说明改动**尚未提交到 git**，当前验证跑的是工作区内容，不是任何已记录的提交。这一点直接限制了结果的可复现性——别人按 `git_sha` 检出得不到同一份代码。

## 2. pytest

| 指标 | R-B [S3] | R-C [S2] | 判读 |
| --- | --- | --- | --- |
| tests | 340 | 372 | +32 |
| errors | 0 | 0 | 持平 |
| failures | 0 | 0 | 持平 |
| skipped | 0 | 0 | 持平，无 skip 掩盖 |
| time | 64.464 s | 60.918 s | 用例更多却更快 |

- 用例 +32 而耗时反而少 3.5 s，可能是并行度或缓存差异，也可能是新增的 32 个用例以轻量单元测试为主 [S2]。仅凭快照无法区分，不宜过度解读。
- 仍是 `current.json` 的**自报结果**，本轮未重跑测试 [S2]。

## 3. R-C 用例数与新增质量闸

- `rc_cases: 32` [S2]，与 pytest 增量 32 一致 [S2]，是本轮唯一一处可交叉验证的自洽点。
- `checks`：`ruff_src_tests_evals: passed`、`frontend_build: passed`、`git_diff_check: passed` [S2]。`git_diff_check: passed` 说明工作区 diff 格式干净，但结合第 1 节的未提交状态，它也提示改动仍停留在本地。

## 4. browser_e2e（上期缺口已出现，但覆盖面有限）

事实（全部来自 [S2]）：Playwright 1.64.1、Chromium 156.0.8078.4、expected 5、unexpected 0、flaky 0、skipped 0、duration 5221.606 ms、workers 1、retries 0、`real_http_sse: true`、`model: deterministic local fake`、`temporary_home_only: true`。

必须同时看清的边界 [S2]：

- **仅 5 个用例**、单 worker、retries 0。没有重试是好事（说明不是靠重试刷过的），但 5 例对"端到端"而言样本很小。
- **`model: deterministic local fake`** —— 模型是确定性本地假件。`real_http_sse: true` 只证明 SSE 传输链路是真的，**不证明真实模型接入可用**；两者不能混为一谈。
- **`fixture_scheduler_tick_seconds: 0.1` 而 `product_scheduler_tick_seconds: 30`** —— 测试把调度节拍加速了 300 倍。真实 30 s 节拍下的时序、竞态、长驻行为**未被覆盖**。这是本期最容易被忽略的一处验证边界。
- `temporary_home_only: true` —— 仍在临时 HOME 内，未触及真实用户目录。

结论：browser_e2e 从"未验证"升为"**部分验证**"，不能计为"端到端通过"。

## 5. 验证边界总览

| 边界项 | R-C 值 | 含义 |
| --- | --- | --- |
| `hosted_ci_verified` | false | 托管 CI 仍未验证，绿色仅来自本地 macOS [S2] |
| `paid_provider_requests` | 0 | 未发起付费模型请求，真实供应商链路未覆盖 [S2] |
| `live_search_requests` | 0 | 未做真实联网检索 [S2] |
| `real_office_writes` | 0 | 未发生真实 Office 文件写入 [S2] |
| `temporary_home_only` | true | 仅在临时 HOME 运行 [S2] |
| `generated_document_human_review` | `not_assessed` | 生成文档未经人工审阅 [S2] |
| `ui_screenshot_review` | 四层验证在 1440x1000 可见 | 仅单一分辨率、单次目视 [S2] |
| `cost` / `cost_status` | null / 未知，账单未对账 | 成本不可知 [S2] |
| `rd_user_trials` | 0/12，未执行 | 无真实用户试用数据 [S2] |
| `formal_holdout_run` | false | 未跑正式留出集 [S2] |
| `original_v2_freeze` | preserved；再次跑模型前需重新冻结源码 | 旧冻结仍在，新冻结待办 [S2] |
| `power_loss_tested` | **字段缺失** | 掉电/中断一致性仍未测 [S2][S3] |
| `real_model_requests` | **字段缺失** | 由上期 0 变为不再记录 [S2][S3] |
| `platform` | 仅 macOS-27.2-arm64 | 无 Linux/Windows 证据 [S2] |

## 6. 产物证据缺口（本期新增的硬问题）

`current.json` 声明了 4 个产物及 SHA256 [S2]：

| 产物 | 声明哈希 | 本轮直读结果 |
| --- | --- | --- |
| `2026-10-26-rc-tests.xml` | `84204811…` | **文件不存在** |
| `2026-10-26-rc-tests-initial.xml` | `756d307f…` | 未逐一直读（受限于不枚举目录） |
| `2026-10-26-rc-browser.json` | `bcee0ede…` | **文件不存在** |
| `2026-10-26-rc-validation-layers.png` | `c8a4a98c…` | **文件不存在** |

- 四者中三个已确认在**工作目录直读路径**上不存在。受"不枚举目录、不用 shell"的限制，无法断定它们在整个工作区都不存在（可能落在别的子目录）。
- 但结论不变：**这些哈希目前无法与任何可读文件对账**，因此 browser 的 5 个用例结果、pytest 的 372 个用例结果、以及那张"四层验证"截图，在本轮都属于**不可独立核验的自报数据**。
- 特别提示：`-rc-tests-initial.xml` 的存在暗示曾有一轮初始测试结果被留档，两轮并存可能是"先失败后修复"或"改了用例"，快照未说明，**不要假定是同一基线**。

## 7. 下期比较口径

1. `pytest` 的 tests / errors / failures / skipped / time；
2. `browser` 块：expected / unexpected / flaky / skipped / retries / workers，以及 `model`、`fixture_scheduler_tick_seconds` 是否变化；
3. 验证边界：`hosted_ci_verified`、`power_loss_tested`（现存字段缺失，出现即为变化）、`paid_provider_requests`、`live_search_requests`、`real_office_writes`、`cost_status`、`rd_user_trials`、`formal_holdout_run`、`platform`；
4. 附带 `stage`、`git_sha`（重点看是否终于提交）、`code_sha256`、`web_sha256`、`tests_sha256`、`rc_cases`、`checks`；
5. **artifacts 是否终于可读**——若仍读不到，继续记为证据缺口。

**失败与缺失的处理原则**：读取失败、字段缺失、或值为 false / not_assessed 的验证项，一律按"未通过/未验证"记录并触发新版输出，绝不计入"无变化"。本期 `power_loss_tested` 与 `real_model_requests` 从有值变为缺失，即按此原则继续挂账，而非视为改善。

## 待办缺口（按优先级）

- [ ] **最高**：补齐或重新生成 4 个产物文件，使声明的哈希可与实际文件对账 [S2]。
- [ ] 提交当前工作区改动，让 `code_sha256` 对应一个可检出的 `git_sha` [S2]。
- [ ] 托管 CI 验证（`hosted_ci_verified` 仍为 false）[S2]。
- [ ] 以真实 30 s 节拍跑一次调度器，覆盖 0.1 s 加速档掩盖的时序行为 [S2]。
- [ ] 至少一次真实模型供应商的端到端请求（当前 `paid_provider_requests: 0`）[S2]。
- [ ] 掉电/中断恢复测试（字段已缺失，需重新纳入记录）[S2][S3]。
- [ ] 成本对账（`cost_status` 未知）与 `rd_user_trials` 0/12 [S2]。
- [ ] 生成文档人工审阅（`not_assessed`）[S2]。

## 参考文献

- [S2] `current.json`（R-C 快照）· file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26/U5-1/workspace/current.json · 访问 2026-10-26T04:42 · inspected · lines 1-56
- [S3] `progress.md`（上期 R-B 版本）· file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26/U5-1/workspace/progress.md · 访问 2026-10-26T04:42 · inspected · lines 1-65

来源状态仅表示获取、提取或上下文读取进度；不代表事实或计算已经核验。

## 参考文献

- [S2] [current.json](file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26/U5-1/workspace/current.json) · file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26/U5-1/workspace/current.json · 访问 2026-10-26T04:42:11Z · inspected · lines 1-56 (line caps 2000) · SHA256 6b53253fd476b10b8b06c911001966a920abcc6745b0aa16dcb62ef52bbff1cc
- [S3] [progress.md](file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26/U5-1/workspace/progress.md) · file:///Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/rd-project-2026-10-26/U5-1/workspace/progress.md · 访问 2026-10-26T04:42:11Z · inspected · lines 1-65 (line caps 2000) · SHA256 005c5dafc1e760f582aa69c2a1592b258a512ab6efd9ddf65ec378c070110e25

来源状态仅表示获取、提取或上下文读取进度；不代表事实或计算已经核验。
