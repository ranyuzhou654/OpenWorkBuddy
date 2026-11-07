"""Summarize frozen v2 results without regrading or modifying any trial evidence."""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
RUN = ROOT / 'eval-runs/quantification-2026-10-29-tools-format-v2-real'


def main():
    summary = json.loads((RUN / 'summary.json').read_text())
    freeze = json.loads((RUN / 'freeze.json').read_text())
    if summary['trials'] != 12:
        raise ValueError('wait for all twelve trials; do not present a partial run as complete')
    cases = {c['id']: c for c in freeze['cases']}
    results = [json.loads(p.read_text()) for p in RUN.glob('*/result.json')]
    results.sort(key=lambda r: (list(cases).index(r['case_id']), r['arm']))
    rows = []
    for r in results:
        trial = RUN / r['directory']
        tools_file = trial / 'tools.jsonl'
        tools = [json.loads(line) for line in tools_file.read_text().splitlines()] if tools_file.exists() else []
        rows.append(dict(case_id=r['case_id'], arm=r['arm'], passed=r['passed'], status=r['status'],
            error_type=r.get('error_type', ''), facts_correct=r['factual_fields_correct'],
            facts_required=r['factual_fields_required'], format_valid=r['format_valid'],
            format_errors='; '.join(r['format_errors']), model_calls=r['metrics']['started_calls'],
            responses=r['metrics']['responses'], usage_complete=r['metrics']['usage_complete'],
            input_tokens=r['metrics']['actual_input_tokens'],
            observed_input=r['metrics']['observed_usage']['input'],
            observed_output=r['metrics']['observed_usage']['output'],
            observed_cache_read=r['metrics']['observed_usage']['cache_read'],
            seconds=r['elapsed_seconds'], grep_calls=r['grep_calls'], recall_calls=r['recall_calls'],
            read_record_calls=r['read_record_calls'], read_calls=r['reads']['reads'],
            source_read_attempts=sum(e['type'] == 'read' and
                Path(e['path']).name in cases[r['case_id']]['inputs'] for e in tools),
            exact_repeat_reads=r['reads']['exact_repeat_reads'], spill_results=r['spilled_results'],
            summary_calls=r['metrics']['summary_calls'],
            projection_reduction=r['same_history_projection']['reduction'], directory=r['directory']))
    with (HERE / 'trials.csv').open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    lines = ['# v2 真实模型开发实测', '', '日期：2026-10-29（Asia/Hong_Kong）。', '',
        '用户授权执行新一轮12次开发试验，总上限144次模型请求，每次180秒；SDK重试关闭。'
        '本轮没有修改产品或冻结后的评测源码，没有收费重跑或正式留出集运行。', '',
        '连接：opendesign / deepseek-v4.1-flash。版本：development-v2-tools-format。'
        '两组共享read/write/edit/grep；历史任务B另有recall/read_record。', '',
        f"共{len(results)}次试验、{summary['started_calls']}次模型请求、{summary['responses']}次完整响应。"
        f"用量完整：{summary['usage_complete']}。货币费用unknown。", '',
        f"可观测输入{summary['observed_usage']['input']:,}、输出{summary['observed_usage']['output']:,}；"
        f"缓存读取{summary['observed_usage']['cache_read']:,}已含在输入中，不重复相加。"
        '缺失用量时这些仅是小计，不能称为完整总量。', '',
        '| 任务 | 组 | 事实字段 | 格式 | 自动通过 | 请求数 | grep | 输入token | 秒 |',
        '|---|---|---|---|---|---|---|---|---|']
    for r in rows:
        tokens = r['input_tokens'] if r['input_tokens'] is not None else (
            '0（模型前拒绝）' if r['model_calls'] == 0 else f"unknown（已记录{r['observed_input']}）")
        lines.append(f"| {r['case_id']} | {r['arm']} | {r['facts_correct']}/{r['facts_required']} | "
            f"{r['format_valid']} | {r['passed']} | {r['model_calls']} | {r['grep_calls']} | "
            f"{tokens} | {r['seconds']:.2f} |")
    indexed = {(r['case_id'], r['arm']): r for r in rows}
    a, b = indexed['CTX-SPILL', 'A'], indexed['CTX-SPILL', 'B']
    lines += ['', '## 本轮观察', '',
        f"- 大材料题：两组事实均3/3正确，输入{a['input_tokens']:,}→{b['input_tokens']:,}，"
        f"本次配对减少{1-b['input_tokens']/a['input_tokens']:.2%}；B发生1次spill并使用2次grep。"
        f"请求{a['model_calls']}→{b['model_calls']}，耗时{a['seconds']:.2f}→{b['seconds']:.2f}秒。"
        '输入减少没有转化为本次耗时降低，费用也不能按token比例直接推算。',
        '- 自动摘要题：B完成1次真实摘要后交付，owner、total、protected_file共3/3字段正确；'
        'A在本地测试窗口下模型请求前被拒绝。这说明本场景能继续执行，不计算相对失败基线的token节省。',
        '- 记忆刷新：B使用ja-JP/RULE_V2，2/2字段正确；固定快照A输出en-US/RULE_V1，0/2正确。'
        '固定快照是人为对照条件，不作为产品失败率。',
        '- 记忆复用：两组仍各读一次original-notes.txt，请求均4次；输入22,518→22,821，增加1.35%。'
        '本轮没有观察到长期记忆降低重读或输入开销。',
        '- 历史定位题：B调用1次recall，随后仍读原始说明，输入7,650→12,818（增加67.56%）。'
        '历史版本题：B调用1次recall，也读原始说明，输入14,030→12,672（减少9.68%）；'
        'A另有2次grep。两题均事实、来源与格式通过，收益方向不同，不能宣称历史检索稳定节省token。',
        '- 11份实际生成的JSON均符合v2输出约定。另1个A组在模型前超窗，未生成文件；'
        '其中格式正确的固定快照结果仍有2个错误事实，说明格式合规不能替代事实验证。',
        '- 本轮45次请求全部收到完整响应，连接错误未复现；没有实施连接异常修复，因此不据此认定旧问题已解决。',
        '', '## 后续范围建议', '',
        '先重复并扩展上下文对照，检验70.12%这一单次结果的波动；记忆和历史检索需要在仍允许原始材料访问的前提下，'
        '加入更真实的多会话、材料规模和复用压力，分别衡量检索收益与重复确认开销。正式留出任务和132次正式试验未运行。']
    lines += ['', '## 同题A/B对照', '',
        '仅两组均通过且用量完整时比较实际输入token。每组仅一次，不给稳定性、显著性或生产成功率结论。', '']
    for pair in summary['pairs']:
        ratio = pair.get('actual_input_reduction')
        value = f"B相对A输入token变化{-ratio:+.2%}" if ratio is not None else '不比较输入节省'
        lines.append(f"- {pair['case_id']}：{value}。")
    lines += ['', '## 失败与格式诊断', '']
    for r in results:
        failed = [c['id'] for c in r['checks'] if not c['ok']]
        if failed:
            lines.append(f"- {r['case_id']} / {r['arm']}：状态{r['status']}，异常{r.get('error_type', '无')}，"
                f"未通过检查：{', '.join(failed)}；格式问题：{r['format_errors']}；实际输出："
                f"`{json.dumps(r['actual'], ensure_ascii=False)}`。")
    lines += ['', '## 解释边界', '',
        'CTX-SPILL必须结合实际spill次数解释；如果模型先用grep缩小结果、没有触发spill，'
        '该题说明工具选择路径，不能据此声称压缩机制节省token。CTX-COMPACT使用32,768本地测试窗口，'
        '并非模型原生窗口；关闭治理后超窗是对照条件。MEM-FRESH的固定快照基线预期可能输出旧值，'
        '应看新旧值采纳，不汇总为业务成功率。', '',
        '首轮与本轮同时改变了工具配置和输出格式，运行时间、服务状态、目录提示及模型采样也不同。'
        '两轮可以做开发诊断参考，不能把差异归因于单个改动；不合并统计或按v2评分器改写旧成绩。', '',
        '这轮复用同样的六个开发场景，不是新增独立任务或正式泛化测试。'
        '原始输入、JSON产物、模型请求、用量、工具调用、源码zip和逐文件hash均在新run目录保留。', '',
        '原始[运行报告](../../../eval-runs/quantification-2026-10-29-tools-format-v2-real/report.md)、'
        '[汇总](../../../eval-runs/quantification-2026-10-29-tools-format-v2-real/summary.json)、'
        '[冻结记录](../../../eval-runs/quantification-2026-10-29-tools-format-v2-real/freeze.json)、'
        '[逐次指标](trials.csv)。']
    (HERE / 'README.md').write_text('\n'.join(lines) + '\n')


if __name__ == '__main__':
    main()
