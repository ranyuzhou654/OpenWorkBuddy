"""Rebuild the phase-one diagnostic tables without changing any original grade."""
from __future__ import annotations

import csv
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
RUN = REPO / 'eval-runs/quantification-2026-10-29-real'
summary = json.loads((RUN / 'summary.json').read_text())
freeze = json.loads((RUN / 'freeze.json').read_text())
cases = {c['id']: c for c in freeze['cases']}
rows = []
for p in RUN.glob('*/result.json'):
    r = json.loads(p.read_text())
    case = cases[r['case_id']]
    actual = r['actual'] or {}
    tools = [json.loads(line) for line in (p.parent / 'tools.jsonl').read_text().splitlines()] if (
        p.parent / 'tools.jsonl').exists() else []
    row = dict(case_id=r['case_id'], arm=r['arm'], automatic_pass=r['passed'], status=r['status'],
        error_type=r.get('error_type', ''),
        factual_fields_correct=sum(actual.get(k) == v for k, v in case['gold'].items()),
        factual_fields_required=len(case['gold']), calls=r['metrics']['started_calls'],
        responses=r['metrics']['responses'], usage_complete=r['metrics']['usage_complete'],
        actual_input=r['metrics']['actual_input_tokens'], observed_input=r['metrics']['observed_usage']['input'],
        observed_output=r['metrics']['observed_usage']['output'],
        observed_cache_read=r['metrics']['observed_usage']['cache_read'],
        seconds=r['elapsed_seconds'], recall_calls=r['recall_calls'], read_record_calls=r['read_record_calls'],
        source_read_attempts=sum(e['type'] == 'read' and Path(e['path']).name in case['inputs'] for e in tools),
        exact_repeat_reads=r['reads']['exact_repeat_reads'], summary_calls=r['metrics']['summary_calls'],
        projection_reduction=r['same_history_projection']['reduction'], directory=r['directory'])
    rows.append(row)
rows.sort(key=lambda r: (list(cases).index(r['case_id']), r['arm']))
with (HERE / 'trials.csv').open('w') as f:
    writer = csv.DictWriter(f, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)

lines = [
    '12次开发试验已经结束，无自动收费重跑。模型连接为opendesign / deepseek-v4.1-flash，响应记录别名deepseek-flash。',
    '',
    f"实际发起 **{summary['started_calls']}次模型请求**，收到 **{summary['responses']}次完整响应**；"
    '两个连接错误调用缺少用量，因此整轮完整token总量和费用均unknown。',
    f"可观测小计：输入{summary['observed_usage']['input']:,}、输出{summary['observed_usage']['output']:,}；"
    f"缓存读取{summary['observed_usage']['cache_read']:,}已包含在输入中，不相加。",
    '',
    '| 开发任务 | A结果/请求数 | B结果/请求数 | 实际输入token A→B | B相对变化 |',
    '|---|---|---|---|---|',
]
for pair in summary['pairs']:
    indexed = {r['arm']: r for r in rows if r['case_id'] == pair['case_id']}
    a, b = indexed['A'], indexed['B']
    def label(r):
        return ('通过' if r['automatic_pass'] else '未通过') + f" / {r['calls']}"

    def actual(r):
        return str(r['actual_input']) if r['actual_input'] is not None else (
            '0（模型前拒绝）' if r['calls'] == 0 else f"unknown（已记录{r['observed_input']}）")
    change = pair.get('actual_input_reduction')
    value = f"{-change:+.2%}" if change is not None else '不比较'
    lines.append(f"| {pair['case_id']} | {label(a)} | {label(b)} | {actual(a)} → {actual(b)} | {value} |")
lines += [
    '',
    'B相对变化只计算两组都通过、用量完整的配对；失败试验不拿来制造节省。这里每题每组仅一次，不能报告稳定成功率或显著性。',
    '',
    '### 逐题解释',
    '',
    '- **CTX-SPILL**：三个事实两组均3/3正确。B在自身实际轨迹上的同一历史投影缩减53.38%，'
    '但发起11次请求、读取原材料多个区间，实际输入由128,177增至276,893（+116.02%），'
    '耗时约24.16→72.73秒。精确重复区间计数为0仍不能说明没有冗余：后续分段读取覆盖了最初已读取材料。'
    '该开发工具集只有read/write/edit，未提供产品默认已有的grep；结果只能代表该受限配置，后续需要两组均开放相同grep能力校准。',
    '- **CTX-COMPACT**：A在本地32,768测试窗口下、调用模型前超窗拒绝。B完成1次真实摘要，'
    '随后主请求出现APIConnectionError，未交付结果；异常根因未定位，不能计作压缩后任务成功。'
    '保留已观察到的11,823输入、707输出，但该trial完整用量unknown。',
    '- **MEM-FRESH**：A仍输出en-US/RULE_V1，两个事实0/2正确；B输出ja-JP/RULE_V2，2/2正确。'
    '这支持当前刷新机制在这一场景中的有效性，不是100%的长期记忆正确率。',
    '- **MEM-REUSE**：两组事实均2/2正确，也都重新读取了original-notes.txt一次；请求均4次，'
    '输入20,654→21,217（+2.73%）。保存的记忆没有在该题消除重新确认原始材料的行为，不能宣称降低重复读取。',
    '- **HIST-LOCATE**：两组日期和预算均2/2正确。B调用recall一次、read_record一次，'
    '随后仍读取原始说明，最终引用previous-notes.txt；请求5→6，输入10,432→16,537（+58.52%）。'
    '历史能找到，但在这道短材料题中未带来节省。',
    '- **HIST-VERSION**：B调用recall一次、read_record两次，随后APIConnectionError，无最终文件。'
    'A日期、预算2/2正确，但evidence写成数组且增加project/note，未通过严格schema和来源字段检查。'
    '原评分保持未通过；这也暴露任务提示需要明确evidence必须是字符串以及禁止额外字段。不能把该A结果描述为事实判断错误。',
    '',
    '逐次指标见[trials.csv](trials.csv)，原始[运行报告](../../../eval-runs/quantification-2026-10-29-real/report.md)、'
    '[汇总](../../../eval-runs/quantification-2026-10-29-real/summary.json)、'
    '[冻结任务及源码hash](../../../eval-runs/quantification-2026-10-29-real/freeze.json)。本脚本仅生成诊断表，不改写原始判定。',
    '',
    '## 后续建议与正式阶段条件',
    '',
    '暂不开始132次正式实验。先改进开发任务契约（明确JSON字段和类型）、使两组工具集更贴近默认核心（同样提供grep），'
    '为记忆和历史检索加入更接近实际工作量的材料与干扰；检查为何模型在得到记忆/检索结果后仍重复确认原文。'
    '另需定位两次连接错误并补充安全的异常诊断；本次没有追加收费试验或借剩余请求额度重跑第13次trial。',
    '',
    '索引初建及增量更新值得单独优化。当前只观察到规模增长带来的耗时上升，没有执行性能剖析或据此认定具体根因。'
    '正式任务须独立于开发材料，冻结模型参数、工具、预算及评分规则后再获得授权运行。',
    '',
    '### 可用于简历的现阶段表述',
    '',
    '建立上下文、记忆和历史检索的分层评测，完成12次真实模型开发对照；在50,000条合成记录上，'
    '80条冻结查询中有答案的72条Hit@5为64/72，平均Recall@5为88.9%，冻结查询P95约0.61ms，'
    '全查询120次越权ID读取探针未发现越权；同时定位到分段补读增加token消耗和大规模索引入库较慢的问题。',
    '',
    '不采用“压缩节省X% token”“记忆减少重复读取”“检索提升任务完成率”作为当前结论。'
    '结构压缩比例、开发查询成绩、真实任务收益和生产可靠性分别陈述。',
]
p = HERE / 'README.md'
content = p.read_text()
marker = '## 真实模型开发试验\n\n'
prefix, rest = content.split(marker, 1)
_, validation = rest.split('## 验证记录', 1)
p.write_text(prefix + marker + '<!-- generated by build_report.py -->\n' + '\n'.join(lines) +
             '\n\n## 验证记录' + validation)
