# v2 重复读取日志诊断与 24 次上下文复测提案

2026-10-29。本轮完成离线日志诊断和复测材料准备，新增真实模型调用 **0 次**。没有修改运行时、系统提示词、检索策略或原始实测工件；24 次复测尚未执行。

## 诊断结论

v2 的 12 次试验共出现 **36 次工具调用，其中 19 次 `read`**。按用途分开后：10 次是写完 `result.json` 后检查成品，9 次是读取输入材料。输入读取中，6 次用于首次获取原始信息，1 次在请求已包含正确偏好时再次读原文，2 次与历史检索在同一条回复中一起发出。

因此，不能把 19 次读取都当成重复读取，也不能只用“相同路径、哈希、offset、limit 重复次数为 0”来宣称没有信息重复。这里需要同时看读取前已经知道什么、同一批调用了哪些工具，以及返回内容是否补上了缺失事实。

| 场景 | 日志显示的过程 | 判断与边界 |
|---|---|---|
| MEM-REUSE B | 首个请求的 system 已有 `ja-JP` 和 `bullet_list`，后续每个请求都保留；模型仍读取 301 行原说明，返回 7,714 字符 | 两个目标偏好重复获取。没有发现记忆漏注入或在本任务中被压缩掉；无法据此确定模型动机或准确的可节省 token |
| HIST-LOCATE B | 同一条回复同时调用 `read(previous-notes.txt)` 和 `recall`；检索返回正确旧记录，也返回当前题目；最终引用原文件 | 两个信息通道重叠。发出调用时尚未看到检索返回，不能解释为“看过检索结果后仍重读” |
| HIST-VERSION B | 同批 `read` 与 `recall`；检索只命中当前题目，没有命中目标旧记录；最终引用原文件 | 答案正确来自原文证据。该组 input 比 A 少 9.68%，不能归因于成功复用历史 |
| HIST-VERSION A | 原文已提供旧、新两版决定；随后又 grep record ID（无结果）及项目名（重复相同两行） | 多了两次检索。record ID 并非格式要求，因为原文件路径本来就是合法 evidence；这会影响 A/B token 对比 |
| CTX-SPILL B | 大文件返回被 spill；可见首尾包含供应商和日期，但没有预算。第一次 grep 补到预算；第二次 grep 再检查正式决定等关键词 | 第一次 grep 是必要补信息。第二次没有新增目标值，但可能用于版本核验，不能直接视为浪费 |
| 10 次读取 result.json | 出现在写入结果之后 | 交付核验单独统计。现有 system 提示要求交付前验证，不应为降低指标而全部删掉 |

“首次获取原始信息”包含 MEM-REUSE A 的原说明读取；对于算术任务，这个分类不尝试用字面匹配判断计算结果是否已知。分类依据是工具调用和公开文本；没有解读模型 reasoning。

## 南桥历史题为什么没有召回目标

运行时查询为 `南桥交付项目 deadline budget`。目标历史正文为：

> 南桥交付项目最终确认：交付日期2026-11-18，预算72000元；此条替代草案。

当前检索按词做 AND，目标历史没有英文 `deadline`、`budget`，所以不满足这次查询。当前题目含有这两个输出字段名，且已进入当前项目的历史索引，反而被命中。

实际返回的是当前会话 `s_9bc086fb9356` 的题目 `rec_142d49584a2c9e5a9b662282`；应召回的旧会话记录为 `rec_989ab8bf3e2db1d1e05755be`，两者不同。工具调用成功、返回了一条记录，不等于召回了答案。

在最终 SQLite 索引的**内存副本**中做了离线对照：原查询仍不命中目标；`南桥交付项目`、`南桥交付项目 最终确认` 均能命中目标，后一条将目标排在第一。最终索引还含有后续执行内容，因此结果数量与当时调用不完全相同；这只验证关键词条件，不是重跑 Agent，也不能作为改写查询后端到端提升的实测结果。

后续历史检索实验应区分“目标旧记录命中”“当前题目自匹配”“答案证据来自哪里”。查询词应来自任务内容；是否降低当前题目权重、是否调整提示词，需要另做对照。本轮没有修改这些策略。

## 能确认和不能确认的事

- 能确认：MEM-REUSE B 正确偏好已经注入；两道历史 B 题均为同批获取两个来源；历史目标记录实际命中 **1/2 次**。这个 1/2 是两次开发调用的诊断统计，不是检索系统准确率。
- 能确认：通用提示词同时要求先理解相关文件、交付前验证。这可能影响保守的文件读取，但没有提示词消融，因果仍待验证。
- 能确认：目前这组任务没有展示自动记忆减少原始文件读取的收益；历史题也没有展示稳定地以历史记录替代原文读取。
- 尚不能确认：读文件是否因不信任记忆、阻止该读取能否保质省 token、某次 token 差异来自哪一个独立模块。单次 A/B 与原文可读的任务设计不足以回答这些问题。

## 逐条证据与复核

[tool-events.csv](/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/docs/research/2026-10-29-read-diagnosis-context-retest/tool-events.csv) 列出全部 36 个事件，包含调用前字面事实覆盖、同批工具、返回片段、原始请求路径及会话文件行号。[diagnosis.json](/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/docs/research/2026-10-29-read-diagnosis-context-retest/diagnosis.json) 保存每轮请求覆盖和召回对照。

以下为从真实原始会话定位的关键行，行号以文件的一行为准，不以内部 seq 代替：

- [记忆 B 的重复输入读取](/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/quantification-2026-10-29-tools-format-v2-real/92cecc9d2d86420a/home/sessions/p_f68dff4ba3fe/s_ff6f6eeb595d.jsonl:6)。请求的 system 覆盖见该轮 requests.jsonl 第 1 行。
- [北杉 B 同批 read / recall](/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/quantification-2026-10-29-tools-format-v2-real/434da52cf3ce4a4e/home/sessions/p_960a7786f6ae/s_47ca783e452c.jsonl:6)。第 10 行是 recall 的实际返回。
- [南桥 B 同批 read / recall](/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/quantification-2026-10-29-tools-format-v2-real/056900e88db14f59/home/sessions/p_dd5ded64a610/s_9bc086fb9356.jsonl:6)。第 10 行只返回当前任务。
- [spill 后补预算](/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/eval-runs/quantification-2026-10-29-tools-format-v2-real/47bf348ab9ae47d5/home/sessions/p_2e15247425fd/s_62f32c6aff9f.jsonl:10)。该决定前请求只含供应商、日期两个目标值。

离线复核命令（不会调用模型）：

```sh
.venv/bin/python docs/research/2026-10-29-read-diagnosis-context-retest/diagnose.py
.venv/bin/python docs/research/2026-10-29-read-diagnosis-context-retest/build_plan.py
```

诊断脚本会在前后校验 v2 实测证据清单中的 236 个文件。24 次任务、顺序、预算与评分口径见 [PLAN.md](/Users/Zhuanz/Documents/GitHub/OpenWorkBuddy/docs/research/2026-10-29-read-diagnosis-context-retest/PLAN.md)。旧实测报告保留原样，以本次诊断校正对调用先后的解释。
