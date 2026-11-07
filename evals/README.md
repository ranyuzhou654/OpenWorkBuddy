# 本地任务评测

当前主线为[个人助手内核评测](../docs/research/2026-10-28-core-evaluation/README.md)，入口 `evals/run_core_trial.py`。以下保留原业务量化方案与历史任务集。

这是简历量化方案的首阶段实现：`eval runner + JSONL + Python graders + Markdown 报告`。
开发集用于调试和预算校准，不能作为留出集成绩。产品默认的权限和三级上下文治理保持开启。

## 已有范围

`personal-work-dev-v2/suite.json` 包含 6 个合成开发任务。首轮使用的 v1 和它的 freeze 原样保留。
v2 修正数值格式、明确输出路径，并配合新版 runner 提供输入文件清单；v2 尚未追加真实模型试跑。

| 任务 | 材料及要求 | 自动验收 | 待人工评阅 |
| --- | --- | --- | --- |
| DEV-DOC-01 | PDF 条款 + Excel 报价 → Word 比较报告及 JSON | 精确金额、文件结构、报告数字、输入 hash | 事实、定位、续约限制；Word 视觉版式另验 |
| DEV-DOC-02 | Word 会议记录 + 旧版 PPT → 冲突简报 | 新旧日期、预算、责任人、期限 | 最终决定与旧方案的语义区分 |
| DEV-RES-01 | 三个本地来源快照 → 版本比较 | 数字、日期、文件及来源 URI | 生效日期、冲突判断、引用是否支持主张 |
| DEV-RES-02 | 不完整供应商说明 → 证据清单 | 未知项、来源、部分禁止编造值 | 是否存在其它编造或误判 |
| DEV-CTX-01 | 大段本地资料 + 两轮请求 → 约束 JSON | 全字段精确值、输入 hash | 约束解释；本题只校准 spill，不证明 compact 效果 |
| DEV-WF-01 | 草稿 + 修订要求 → 本地修订版 | 内容、原文件不变 | 修改范围与实际交付状态 |

所有材料均为自建合成资料，`example.invalid` 是固定快照的标识，不进行网页访问。
开发集中的工作流题没有运行真实 scheduler、飞书或浏览器，不能替代这些能力的验收。

## 执行

在仓库根目录运行；也可将 `.venv/bin/python` 换成 `uv run python`。

```bash
# 首次建立或明确创建新版数据集时使用。现有目录会拒绝覆盖。
.venv/bin/python evals/build_development.py /tmp/new-development-suite

# 先冻结材料、答案、验收规则和当前源码 hash，不访问模型。
.venv/bin/python -m nano_workbuddy.eval freeze evals/personal-work-dev-v2/suite.json \
  --output /tmp/nwb-development-freeze.json

# 这一步调用真实模型；先明确连接、调用/时长和费用边界。
# 6题 × 两配置 × 1次 = 12个开发trial，总计至多144次请求。
.venv/bin/python -m nano_workbuddy.eval run evals/personal-work-dev-v2/suite.json \
  --freeze /tmp/nwb-development-freeze.json \
  --model ds-flash --output eval-runs/development-01

.venv/bin/python -m nano_workbuddy.eval report eval-runs/development-01
```

`--home` 可显式选择模型配置目录。默认只读取选定连接和该模型密钥，不会把日常 home、
Skills、MCP、历史索引、浏览器登录态或其它密钥复制到 trial。
SDK 隐式重试固定为 0，单次调用预算覆盖主循环、摘要、上下文溢出重试和收尾。
当前开发集限制每 trial 最多 12 次模型调用、180 秒，超时由父进程 SIGKILL，保留已有轨迹。
文件工具限制在本 trial 的 workspace 和 spill；不提供 shell、通用代码执行、搜索或远端连接器。
这是 Agent 工具能力隔离，不是运行任意 Python 的操作系统安全沙箱。

B0 关闭 spill/prune/compact，B1 全部开启。两组的模型、窗口、最大输出、提示、工具、权限和预算相同。
新版 runner 在整轮开始时冻结同一份系统提示（包括日期），使用无 A/B 标签的随机物理目录。
`run.json` 保存 Python、SDK 版本和提示快照，每条结果保存实际系统提示 hash。
超窗口明确失败，不能为 B0 扩窗。主请求在恢复 pinned notices 后仍检查窗口。
同题运行顺序交错；服务端缓存无法由客户端保证清除，本版仅记录服务商可观察 usage，
尚未建立独立的冷缓存/重复前缀轨道。

## 证据与评分

每个 trial 保存独立的 `home/`、`workspace/`、公开任务 `public.json`、`calls.json`、
`result.json` 与 stdout/stderr。`public.json` 不含标准答案、grader 或人工 rubric。
父进程只复制该题输入，评分卡保存在 Agent 文件权限范围外。

`trials.jsonl` 追加 trial.started / trial.finished，`reviews.jsonl` 追加评阅。
不覆盖失败 trial，不自动重新运行。父进程意外退出留下的 started 记录，在重建报告时计为中断失败。
未开始的任务数量与已开始数量分别显示；未完成整轮时不输出成对 bootstrap 比较。
运行中修改源码会停止后续 trial；代码版本同时保存 Git SHA 和源码逐文件 hash，支持未提交工作树。
`source.zip` 保存实际运行源码、内置提示和依赖锁，避免只留下无法还原的工作树 hash。

自动 checks 全通过仍需完成所有人工 rubric。没有评阅时 `passed=null`；运行错误、关键检查失败
或越界调用会判失败。Word 能打开与事实正确分开检查，结构检查不能覆盖事实失败。
自动检查通过数不代表任务完成率，开发集亦不用于计算正式 pass^3。

```bash
# 导出不含 A/B 标签的评阅包；交叉映射仅留在原始 run 目录。
.venv/bin/python -m nano_workbuddy.eval blind eval-runs/development-01 \
  --destination /tmp/development-review-packets

# 人工检查文件格式：[ {"id":"human-facts-and-usefulness","ok":true,"note":"具体证据及定位"} ]
# 使用原始run目录内 review-map-*.json 将盲评ID映射回trial。
.venv/bin/python -m nano_workbuddy.eval review eval-runs/development-01 \
  --trial-id DEV-DOC-01-B0-1 --reviewer reviewer-name --checks /tmp/review.json
```

评阅应由独立于被测 Agent 的人工进行。不要把模型自行宣称完成或代码审查当成人工验收。
后续正式报告按题聚类，输出成对完成率差值与固定种子的 task bootstrap 区间；同时保留分组结果、
原始分子/分母与失败分类，小样本区间不能证明质量等价。

开发集允许有版本记录的离线评分器校准；不会调用模型，也不改写初始结果：

```bash
.venv/bin/python -m nano_workbuddy.eval regrade eval-runs/2026-10-25-dev-calibration-01 \
  --corrections evals/calibration-01-grading-corrections.json
```

修订只覆盖明确列出的确定性检查。`grades.jsonl` 保存原因、新 grader 源码 hash 和修订文件 hash，
每版 grader 源码独立归档。正式 holdout 禁止使用此校准命令。
首轮校准的原始判定、离线修订和缺陷说明见 [开发试跑记录](../docs/research/2026-10-25-dev-calibration/README.md)。

## Usage 与费用

产品运行时在 `home/usage/<session-id>.jsonl` 持久记录每个逻辑模型调用的开始、响应或异常，
包括 compaction、分支摘要、失败和恢复；记录本地 call_id、可获得的服务商 request/response ID、
实际返回模型名、raw usage 和规范化 usage。旧记录不迁移成虚构的逐调用账单证据。
input 已包含缓存输入，output 已包含服务商计入其中的推理 token，不重复相加。

断流/中断缺少 usage、SDK 内部重试不可观测或账单无法对账时保留 unknown。
产品 run 汇总使用此新日志，避免后续异常和恢复覆盖此前已记录的调用。
手动摘要没有 run 行，逐调用日志会保留，但现有用量页的按 run 汇总尚不包含这部分；评测直接汇总调用日志。
历史版本没有逐调用日志的用量，需要另行核对，不能据本次改动反推完整账单。
备份现在包含 usage；备份原有的一致性和迁移缺陷仍待 R-B 修订。

没有价格表时，货币费用为 unknown，套餐额度不折算成 0 元。
可通过 `--price snapshot.json` 传入有日期和来源的价格快照：

```json
{
  "provider": "实际连接名",
  "model": "实际模型ID",
  "currency": "实际币种",
  "as_of": "2026-10-25",
  "source": "实际适用的账单或价格表位置",
  "per_million": {"input": null, "output": null, "cache_read": null, "cache_write": null},
  "uniform_cache_write": false
}
```

这个示例没有可计费价格。未知费率、分层缓存写价、失败调用缺失 usage 时，不会输出完整费用。
工具 API 在当前限定任务中没有调用，工具费用为 0；其它 suite 如增加远端工具，必须先扩展费用采集。

## 后续门槛

先完成开发集人工评阅与关键 R-A/R-B 修订，再创建并冻结留出集；不要反复根据留出成绩调参。
holdout freeze 要求提供 `--development-suite`，检查任务模板族和材料 hash 不重叠，
并要求 12 或 24 道平衡题；这只是泄漏的基础检查，仍需人工检查近似模板和近似材料。
holdout run 固定每题 3 次，不会把 pass@3 当作三次全成功占比。
本版没有创建或执行 24 道留出题、144 次正式业务 trial 或 20 类/60 次边界实验。
