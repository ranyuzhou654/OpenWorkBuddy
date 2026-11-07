# v2 真实模型开发实测

日期：2026-10-29（Asia/Hong_Kong）。

用户授权执行新一轮12次开发试验，总上限144次模型请求，每次180秒；SDK重试关闭。本轮没有修改产品或冻结后的评测源码，没有收费重跑或正式留出集运行。

连接：opendesign / deepseek-v4.1-flash。版本：development-v2-tools-format。两组共享read/write/edit/grep；历史任务B另有recall/read_record。

共12次试验、45次模型请求、45次完整响应。用量完整：True。货币费用unknown。

可观测输入312,987、输出4,180；缓存读取237,312已含在输入中，不重复相加。缺失用量时这些仅是小计，不能称为完整总量。

| 任务 | 组 | 事实字段 | 格式 | 自动通过 | 请求数 | grep | 输入token | 秒 |
|---|---|---|---|---|---|---|---|---|
| CTX-SPILL | A | 3/3 | True | True | 4 | 0 | 130162 | 12.71 |
| CTX-SPILL | B | 3/3 | True | True | 6 | 2 | 38897 | 17.36 |
| CTX-COMPACT | A | 0/3 | False | False | 0 | 0 | 0（模型前拒绝） | 0.01 |
| CTX-COMPACT | B | 3/3 | True | True | 5 | 0 | 35400 | 15.33 |
| MEM-FRESH | A | 0/2 | True | False | 3 | 0 | 7702 | 7.12 |
| MEM-FRESH | B | 2/2 | True | True | 3 | 0 | 8317 | 7.60 |
| MEM-REUSE | A | 2/2 | True | True | 4 | 0 | 22518 | 9.23 |
| MEM-REUSE | B | 2/2 | True | True | 4 | 0 | 22821 | 10.16 |
| HIST-LOCATE | A | 2/2 | True | True | 3 | 0 | 7650 | 7.70 |
| HIST-LOCATE | B | 2/2 | True | True | 4 | 0 | 12818 | 9.73 |
| HIST-VERSION | A | 2/2 | True | True | 5 | 2 | 14030 | 13.92 |
| HIST-VERSION | B | 2/2 | True | True | 4 | 0 | 12672 | 9.62 |

## 本轮观察

- 大材料题：两组事实均3/3正确，输入130,162→38,897，本次配对减少70.12%；B发生1次spill并使用2次grep。请求4→6，耗时12.71→17.36秒。输入减少没有转化为本次耗时降低，费用也不能按token比例直接推算。
- 自动摘要题：B完成1次真实摘要后交付，owner、total、protected_file共3/3字段正确；A在本地测试窗口下模型请求前被拒绝。这说明本场景能继续执行，不计算相对失败基线的token节省。
- 记忆刷新：B使用ja-JP/RULE_V2，2/2字段正确；固定快照A输出en-US/RULE_V1，0/2正确。固定快照是人为对照条件，不作为产品失败率。
- 记忆复用：两组仍各读一次original-notes.txt，请求均4次；输入22,518→22,821，增加1.35%。本轮没有观察到长期记忆降低重读或输入开销。
- 历史定位题：B调用1次recall，随后仍读原始说明，输入7,650→12,818（增加67.56%）。历史版本题：B调用1次recall，也读原始说明，输入14,030→12,672（减少9.68%）；A另有2次grep。两题均事实、来源与格式通过，收益方向不同，不能宣称历史检索稳定节省token。
- 11份实际生成的JSON均符合v2输出约定。另1个A组在模型前超窗，未生成文件；其中格式正确的固定快照结果仍有2个错误事实，说明格式合规不能替代事实验证。
- 本轮45次请求全部收到完整响应，连接错误未复现；没有实施连接异常修复，因此不据此认定旧问题已解决。

## 后续范围建议

先重复并扩展上下文对照，检验70.12%这一单次结果的波动；记忆和历史检索需要在仍允许原始材料访问的前提下，加入更真实的多会话、材料规模和复用压力，分别衡量检索收益与重复确认开销。正式留出任务和132次正式试验未运行。

## 同题A/B对照

仅两组均通过且用量完整时比较实际输入token。每组仅一次，不给稳定性、显著性或生产成功率结论。

- CTX-SPILL：B相对A输入token变化-70.12%。
- CTX-COMPACT：不比较输入节省。
- MEM-FRESH：不比较输入节省。
- MEM-REUSE：B相对A输入token变化+1.35%。
- HIST-LOCATE：B相对A输入token变化+67.56%。
- HIST-VERSION：B相对A输入token变化-9.68%。

## 失败与格式诊断

- CTX-COMPACT / A：状态error，异常ContextTooLarge，未通过检查：owner, total, protected_file, exact_schema, runtime_ok；格式问题：['$: expected object']；实际输出：`null`。
- MEM-FRESH / A：状态ok，异常无，未通过检查：locale, project_rule；格式问题：[]；实际输出：`{"locale": "en-US", "project_rule": "RULE_V1"}`。

## 解释边界

CTX-SPILL必须结合实际spill次数解释；如果模型先用grep缩小结果、没有触发spill，该题说明工具选择路径，不能据此声称压缩机制节省token。CTX-COMPACT使用32,768本地测试窗口，并非模型原生窗口；关闭治理后超窗是对照条件。MEM-FRESH的固定快照基线预期可能输出旧值，应看新旧值采纳，不汇总为业务成功率。

首轮与本轮同时改变了工具配置和输出格式，运行时间、服务状态、目录提示及模型采样也不同。两轮可以做开发诊断参考，不能把差异归因于单个改动；不合并统计或按v2评分器改写旧成绩。

这轮复用同样的六个开发场景，不是新增独立任务或正式泛化测试。原始输入、JSON产物、模型请求、用量、工具调用、源码zip和逐文件hash均在新run目录保留。

原始[运行报告](../../../eval-runs/quantification-2026-10-29-tools-format-v2-real/report.md)、[汇总](../../../eval-runs/quantification-2026-10-29-tools-format-v2-real/summary.json)、[冻结记录](../../../eval-runs/quantification-2026-10-29-tools-format-v2-real/freeze.json)、[逐次指标](trials.csv)。
