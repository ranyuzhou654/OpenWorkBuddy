# 24 次上下文真实模型复测

日期：2026-10-29；Asia/Hong_Kong。

用户确认已提交的 24 次方案后，扩展评测调度并完成离线验证，再执行本轮。产品运行时、系统提示词和检索策略未修改。
配置：opendesign / deepseek-v4.1-flash；max_output=2048；SDK retries=0。4 个合成开发任务，各 A/B 三次；S1/C1 复现原题，S2/C2 是受控变体。不是 24 道独立新题。

已完成 24/24 次，模型请求 100/288，完整响应 100。所有请求 usage 完整或未发请求：True。
观察到 input 1,567,811、output 12,013；cache_read 1,233,024 已包含在 input 中。人民币费用 unknown。
事前同类样本参考量为 90 请求、1,226,754 input；本轮实际用量高于参考，请求数仍在 288 硬上限内。各 trial 的父进程计时合计 340.92 秒（约 5.68 分钟），试验窗口上限 72 分钟。

S 组及 C 组 B 的交付通过 18/18 次；另有 6/6 次 C 组 A 为预设本地超预算对照，全部保留。不能将这两类合并成生产成功率。

## 大材料任务：六组配对

| 任务 | 轮次 | A input | B input | B input 降幅 | 请求 A→B | 秒 A→B | B spill |
|---|---|---|---|---|---|---|---|
| S1-SPILL-REPLICATION | 1 | 130102 | 54382 | 58.20% | 4→7 | 14.37→20.38 | 1 |
| S2-SPILL-MIDDLE | 1 | 176883 | 46790 | 73.55% | 5→7 | 21.05→19.09 | 1 |
| S1-SPILL-REPLICATION | 2 | 215968 | 38827 | 82.02% | 6→6 | 21.01→15.65 | 1 |
| S2-SPILL-MIDDLE | 2 | 216147 | 59103 | 72.66% | 6→8 | 20.43→22.38 | 1 |
| S1-SPILL-REPLICATION | 3 | 172977 | 31281 | 81.92% | 5→5 | 16.94→12.78 | 1 |
| S2-SPILL-MIDDLE | 3 | 173058 | 32673 | 81.12% | 5→5 | 18.08→16.15 | 1 |

纳入完整用量且两组均通过的配对 6/6；按总 input 加权降幅为 75.76%。公式是 1−ΣB_input/ΣA_input，失败和缺失仍在逐次表中。

六组大材料累计 input 1,085,135→263,056；请求 31→38，累计耗时 111.88→106.43 秒。调用数增加，单组耗时有升有降，不据此宣称稳定提速。

- S1-SPILL-REPLICATION：通过 3/3，spill 触发 3/3；配对降幅最小 58.20%、中位 81.92%、最大 82.02%。
- S2-SPILL-MIDDLE：通过 3/3，spill 触发 3/3；配对降幅最小 72.66%、中位 73.55%、最大 81.12%。

## 压缩压力与最新约束

| 任务 | 组 | 交付通过 | 本地超预算 | compact 触发 | 输入文件未变 | 误用旧负责人 |
|---|---|---|---|---|---|---|
| C1-COMPACT-REPLICATION | A | 0/3 | 3/3 | 0/3 | 3/3 | 不适用 |
| C1-COMPACT-REPLICATION | B | 3/3 | 0/3 | 3/3 | 3/3 | 不适用 |
| C2-COMPACT-UPDATED-CONSTRAINT | B | 3/3 | 0/3 | 3/3 | 3/3 | 0 |
| C2-COMPACT-UPDATED-CONSTRAINT | A | 0/3 | 3/3 | 0/3 | 3/3 | 0 |

C1 正确值为 林杉 / 7200 / input.json；C2 为 许澄 / 8000 / input.json。实际结果字段、格式和保护文件哈希分别核验。A 使用人为设定的 32,768 本地窗口，发请求前超预算时仍计失败；B 的摘要与后续请求成本均计入，不与 A 的零请求计算节省率。

## 请求构成与读取用途

| 用途 | 请求开始 | 响应 | 错误 | 观察 input | 观察 output |
|---|---|---|---|---|---|
| agent | 94 | 94 | 0 | 1496729 | 7873 |
| compaction | 6 | 6 | 0 | 71082 | 4140 |

以下分类依据模型可见文本、工具参数和同批调用；没有解读 reasoning。“已知事实确认或核验”表示信息重叠，不证明动机，也不直接换算为可避免的 token。

| 工具用途 | 次数 |
|---|---|
| initial_input_read | 9 |
| needed_after_spill | 6 |
| artifact_write | 18 |
| known_fact_confirmation_or_verification | 18 |
| artifact_verification | 18 |
| initial_or_targeted_acquisition | 9 |
| verification_or_uncertain | 1 |

读取结果文件与补回 spill 隐去的事实分开统计；版本检查与用途不确定的调用保留不确定性。同历史投影字符减少只衡量序列化体积，不能替代实际 provider token 或费用。
按全部 24 次尝试统计，spill 触发 6/24，prune 触发 0/24，compact 触发 6/24。对应 B 组的机制覆盖见上表；本轮没有新增真实 prune 收益证据。
共 44 次 read 尝试：18 次写后核验、25 次输入读取，另有 1 次写前检查结果文件失败。输入读取中 7 次发生在目标事实已经可见之后；机械相同读参的重复次数仍为 0。这再次说明该指标不能代替信息重叠诊断。
序号 16（C2 B 第二轮）在第一次写入前读 result.json，因文件尚不存在返回一次工具错误；随后写入并核验完成。该错误、额外请求和耗时均保留，没有重跑。

## 逐次验收与异常

| 序号 | 任务 | 轮次 | 组 | 事实 | 格式 | 交付通过 | 请求 | 输入 |
|---|---|---|---|---|---|---|---|---|
| 1 | S1-SPILL-REPLICATION | 1 | A | 3/3 | True | True | 4 | 130102 |
| 2 | S1-SPILL-REPLICATION | 1 | B | 3/3 | True | True | 7 | 54382 |
| 3 | S2-SPILL-MIDDLE | 1 | B | 3/3 | True | True | 7 | 46790 |
| 4 | S2-SPILL-MIDDLE | 1 | A | 3/3 | True | True | 5 | 176883 |
| 5 | C1-COMPACT-REPLICATION | 1 | A | 0/3 | False | False | 0 | 0（模型前拒绝） |
| 6 | C1-COMPACT-REPLICATION | 1 | B | 3/3 | True | True | 5 | 35112 |
| 7 | C2-COMPACT-UPDATED-CONSTRAINT | 1 | B | 3/3 | True | True | 5 | 35354 |
| 8 | C2-COMPACT-UPDATED-CONSTRAINT | 1 | A | 0/3 | False | False | 0 | 0（模型前拒绝） |
| 9 | S1-SPILL-REPLICATION | 2 | B | 3/3 | True | True | 6 | 38827 |
| 10 | S1-SPILL-REPLICATION | 2 | A | 3/3 | True | True | 6 | 215968 |
| 11 | S2-SPILL-MIDDLE | 2 | A | 3/3 | True | True | 6 | 216147 |
| 12 | S2-SPILL-MIDDLE | 2 | B | 3/3 | True | True | 8 | 59103 |
| 13 | C1-COMPACT-REPLICATION | 2 | B | 3/3 | True | True | 5 | 35662 |
| 14 | C1-COMPACT-REPLICATION | 2 | A | 0/3 | False | False | 0 | 0（模型前拒绝） |
| 15 | C2-COMPACT-UPDATED-CONSTRAINT | 2 | A | 0/3 | False | False | 0 | 0（模型前拒绝） |
| 16 | C2-COMPACT-UPDATED-CONSTRAINT | 2 | B | 3/3 | True | True | 6 | 42256 |
| 17 | S1-SPILL-REPLICATION | 3 | A | 3/3 | True | True | 5 | 172977 |
| 18 | S1-SPILL-REPLICATION | 3 | B | 3/3 | True | True | 5 | 31281 |
| 19 | S2-SPILL-MIDDLE | 3 | B | 3/3 | True | True | 5 | 32673 |
| 20 | S2-SPILL-MIDDLE | 3 | A | 3/3 | True | True | 5 | 173058 |
| 21 | C1-COMPACT-REPLICATION | 3 | A | 0/3 | False | False | 0 | 0（模型前拒绝） |
| 22 | C1-COMPACT-REPLICATION | 3 | B | 3/3 | True | True | 5 | 35374 |
| 23 | C2-COMPACT-UPDATED-CONSTRAINT | 3 | B | 3/3 | True | True | 5 | 35862 |
| 24 | C2-COMPACT-UPDATED-CONSTRAINT | 3 | A | 0/3 | False | False | 0 | 0（模型前拒绝） |

- 序号 5：C1-COMPACT-REPLICATION / A / 第 1 次，status=error，error_type=ContextTooLarge；未通过：owner, total, protected_file, exact_schema, runtime_ok。
- 序号 8：C2-COMPACT-UPDATED-CONSTRAINT / A / 第 1 次，status=error，error_type=ContextTooLarge；未通过：owner, total, protected_file, exact_schema, runtime_ok。
- 序号 14：C1-COMPACT-REPLICATION / A / 第 2 次，status=error，error_type=ContextTooLarge；未通过：owner, total, protected_file, exact_schema, runtime_ok。
- 序号 15：C2-COMPACT-UPDATED-CONSTRAINT / A / 第 2 次，status=error，error_type=ContextTooLarge；未通过：owner, total, protected_file, exact_schema, runtime_ok。
- 序号 21：C1-COMPACT-REPLICATION / A / 第 3 次，status=error，error_type=ContextTooLarge；未通过：owner, total, protected_file, exact_schema, runtime_ok。
- 序号 24：C2-COMPACT-UPDATED-CONSTRAINT / A / 第 3 次，status=error，error_type=ContextTooLarge；未通过：owner, total, protected_file, exact_schema, runtime_ok。

## 证据与边界

39 项离线检查通过；完整 24 次 FakeModel 流程中 18 次脚本交付通过、6 次预期本地超预算。脚本结果仅验证调度、材料、预算与评分，不能作为模型质量或费用数据。首次测试启动使用 pytest 可执行文件，因仓库根目录未进入导入路径而发生收集错误；改用 python -m pytest 后通过，初始日志保留。

本轮没有更改题目来追求降幅，没有将失败重新跑到成功，也没有执行长期记忆/历史检索或正式留出集实验。三次重复及两个大材料任务不足以给出显著性、生产 SLA 或跨任务泛化结论。缓存、服务负载和采样波动仍可能影响三次间的差异；没有可用平台费率，不能宣称费用同比下降。

[原始运行目录](/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/context-retest-2026-10-29-real)、[冻结参数与源码清单](/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/context-retest-2026-10-29-real/freeze.json)、[逐次指标](/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/docs/research/2026-10-29-context-retest/trials.csv)、[读取审计](/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/context-retest-2026-10-29-real/tool-audit.json)、[离线验证](/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/docs/research/2026-10-29-context-retest/preflight.json)。
