# 开发评测工具与输出格式校准 v2

日期：2026-10-29（Asia/Shanghai）。版本：`development-v2-tools-format`。

本轮完成工具配置与输出格式校准，只运行本地检查和确定性模型，不调用真实模型。首轮12次真实试验、评分、轨迹和源码归档保留；不会使用v2规则修改旧成绩。新版工具与任务有变化，后续真实结果应独立报告。

## 工具配置

所有任务的A/B组共同提供 `read / write / edit / grep`；历史检索任务仅B额外提供 `recall / read_record`，保持原实验变量。两组的权限范围一致，唯一额外授权是历史B的recall读取。

grep可以查单个输入文件，也能在工作区目录搜索匹配行。根目录许可只让搜索进入目录，文件候选仍按输入白名单逐个过滤。未声明的grader文件、外部文件和软链接目标均被阻断；Python回退与原生rg两条路径都由测试覆盖。没有增加shell、联网或业务连接器工具。

相同工具提示同时交给两组，说明可用grep定位、read补上下文；不要求必须调用任何工具。冻结文件与public.json保存实际工具名单，worker校验名单后执行；新增grep调用次数指标，read的重复读取统计仍只覆盖read。

## 输出约定

每题都有公开的 `output_contract`：只包含字段、类型和格式规则，不包含标准答案。模型看到的格式说明由该契约生成，评分器使用同一份契约，避免两边要求不一致。

| 任务 | 允许的全部字段 |
|---|---|
| CTX-SPILL | supplier：字符串；budget：非负整数；deadline：日期字符串 |
| CTX-COMPACT | owner：字符串；total：非负整数；protected_file：字符串 |
| MEM-FRESH | locale、project_rule：字符串 |
| MEM-REUSE | locale、format：字符串 |
| HIST-LOCATE / HIST-VERSION | deadline：日期字符串；budget：非负整数；evidence：单个字符串 |

保存的result.json必须是单个JSON对象，字段全部必填，不允许project、note等额外键、Markdown围栏或对象外说明。字符串不能为空；金额不能是数字字符串、布尔值或小数；日期为YYYY-MM-DD。

evidence只能填写实际支持答案的一个历史record ID（rec_加24位小写十六进制字符），或相对路径previous-notes.txt。数组、对象、[S1]及解释文字均不合规。来源可以是grep/read的原文或recall/read_record返回的完整相关片段；已有足够依据时不强制额外读取其它路径。引用read_record时，答案ID必须与实际读取ID相同。

评分分别保留事实正确字段数、format_valid、format_errors及来源依据检查。不会把数组自动转成字符串或丢弃额外字段来让答案过关；“事实正确、格式错误”会明确分开呈现。

可审阅的任务说明与契约见[public-contracts.json](public-contracts.json)。预算、调用计量、失败保留规则沿用原开发评测，详见[评测说明](../../../evals/quantification/README.md)。

## 本轮验证

- 57项定向检查通过，见[checks.xml](checks.xml)，覆盖工具权限、原生与回退搜索、格式校验、来源归属、预算和上下文行为。
- 12次新版确定性试跑独立保存：见[运行报告](../../../eval-runs/quantification-2026-10-29-tools-format-v2-deterministic/report.md)。CTX-SPILL B实际经过grep执行路径；关闭治理后的超窗和固定旧记忆对照仍按预期失败，不把控制组失败改成通过。
- 确定性模型的调用计数及伪用量只验证编排；真实模型请求为0，不作为质量、节省token或成本结果。
- 首轮627个证据文件的hash在修改前已全部核验，修改后再次核验；核验结果见integrity.json。

本轮不处理上轮两次连接异常。后续真实开发校准应使用新目录和新冻结文件，观察grep是否减少分段补读，以及明确输出约定后格式偏差是否减少；不能预先宣称收益。
