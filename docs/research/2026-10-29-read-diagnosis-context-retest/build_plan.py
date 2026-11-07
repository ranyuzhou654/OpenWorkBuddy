"""Build a reviewable 24-trial proposal, without changing or executing the evaluation runner."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from nano_workbuddy.context import estimate_tokens

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
RUN = ROOT / 'eval-runs/quantification-2026-10-29-tools-format-v2-real'


def main():
    freeze = json.loads((RUN / 'freeze.json').read_text())
    original = {c['id']: c for c in freeze['cases']}
    rows = [json.loads(p.read_text()) for p in RUN.glob('*/result.json')]
    indexed = {(r['case_id'], r['arm']): r for r in rows}
    spill = original['CTX-SPILL']
    compact = original['CTX-COMPACT']
    compact_result = indexed['CTX-COMPACT', 'B']
    log = next((RUN / compact_result['directory'] / 'home/sessions').rglob(
        compact_result['session_id'] + '.jsonl'))
    seed = []
    for raw in log.read_text().splitlines():
        e = json.loads(raw)
        if e.get('type') in ('user', 'assistant') and not e.get('run_id'):
            body = ''.join(b['text'] for b in e['data']['content'] if b['type'] == 'text')
            seed.append({'role': e['type'], 'text': body})
    assert len(seed) == 22
    materials = {}
    common = {'tool_profile': freeze['tool_profile'], 'max_output': 2048,
              'model_calls_per_trial': 12, 'wall_seconds_per_trial': 180,
              'tool_calls_per_trial': 36, 'repeats': 3, 'sdk_retries': 0}
    materials['S1-SPILL-REPLICATION'] = {
        'purpose': 'Exact v2 CTX-SPILL task/material replication; measure within-case variability',
        'base_case_id': 'CTX-SPILL', 'task': spill['task'], 'inputs': copy.deepcopy(spill['inputs']),
        'seed_history': [], 'gold': copy.deepcopy(spill['gold']), 'context_window': 100000,
        'fact_lines': {'supplier': 1, 'budget': 91, 'deadline': 180},
        'primary_comparison': 'Input tokens/time for common-success, complete-usage pairs; spill coverage recorded'}
    lines = [f'附录{i:03}：' + '设备清单与例行维护记录，仅供背景参考。' * 20 for i in range(180)]
    lines[43] = '正式决定：supplier=南桥；其余附录只是背景。'
    lines[95] = '正式决定：budget=81400；这个金额替代早期草案。'
    lines[145] = '正式决定：deadline=2026-12-03。'
    materials['S2-SPILL-MIDDLE'] = {
        'purpose': 'Move all required facts beyond spill head/tail; measure targeted evidence reacquisition',
        'base_case_id': 'CTX-SPILL', 'task': spill['task'],
        'inputs': {'material.txt': '\n'.join(lines)}, 'seed_history': [],
        'gold': {'supplier': '南桥', 'budget': 81400, 'deadline': '2026-12-03'}, 'context_window': 100000,
        'fact_lines': {'supplier': 44, 'budget': 96, 'deadline': 146},
        'primary_comparison': 'Same as S1; no required grep/read sequence; no-trigger runs remain in denominator'}
    materials['C1-COMPACT-REPLICATION'] = {
        'purpose': 'Exact v2 CTX-COMPACT seed/task replication; completion beyond local input budget',
        'base_case_id': 'CTX-COMPACT', 'task': compact['task'], 'inputs': copy.deepcopy(compact['inputs']),
        'seed_history': copy.deepcopy(seed), 'gold': copy.deepcopy(compact['gold']), 'context_window': 32768,
        'primary_comparison': 'Three critical constraints and artifact completion; no savings vs failed A baseline'}
    updated_seed = copy.deepcopy(seed[:2]) + [
        {'role': 'user', 'text': '更新此前决定：owner 改为许澄，替代旧的林杉；'
                                 'protected_file=input.json仍不得修改，总价公式不变。'},
        {'role': 'assistant', 'text': '收到，以更新后的负责人为准，其余约束保留。'},
    ] + copy.deepcopy(seed[2:])
    materials['C2-COMPACT-UPDATED-CONSTRAINT'] = {
        'purpose': 'Preserve latest of conflicting historical instructions through summarization',
        'base_case_id': 'CTX-COMPACT', 'task': compact['task'],
        'inputs': {'input.json': '{"quantity": 5, "unit_price": 1600}'},
        'seed_history': updated_seed, 'gold': {'owner': '许澄', 'total': 8000, 'protected_file': 'input.json'},
        'context_window': 32768,
        'primary_comparison': 'Latest owner, computed total, protected file; stale owner explicitly fails'}
    # A non-billing estimate based on the actual v2 post-read request, without any model invocation.
    r = indexed['CTX-SPILL', 'A']
    requests = [json.loads(s) for s in (RUN / r['directory'] / 'requests.jsonl').read_text().splitlines()]
    before_write = [q for q in requests if q['purpose'] == 'agent'][1]
    old_text = max((b['text'] for m in before_write['messages'] for b in m['content'] if b['type'] == 'text'), key=len)
    old_prefix = old_text.split('\n', 1)[0]
    for name, card in materials.items():
        card['limits'] = common
        if card['base_case_id'] == 'CTX-SPILL':
            content = card['inputs']['material.txt']
            rendered = old_prefix + '\n' + '\n'.join(f'{n:6}\t{line[:2000]}'
                                                    for n, line in enumerate(content.splitlines(), 1))
            if name == 'S1-SPILL-REPLICATION':
                assert rendered == old_text
            estimate = before_write['estimated_tokens'] - estimate_tokens(old_text) + estimate_tokens(rendered)
            card['offline_precheck'] = {'full_tool_result_chars': len(rendered), 'spill_threshold_chars': 30000,
                'post_full_read_request_tokens_estimate': estimate, 'input_budget_estimate': 83616,
                'uses_v2_request_prefix_as_proxy': True, 'not_provider_token_count': True}
            assert len(rendered) > 30000 and estimate < 83616
            if name == 'S2-SPILL-MIDDLE':
                for value in card['gold'].values():
                    assert str(value) not in rendered[:4000] and str(value) not in rendered[-4000:]
        else:
            seed_estimate = sum(estimate_tokens(e['text']) for e in card['seed_history'])
            card['offline_precheck'] = {'seed_text_tokens_estimate': seed_estimate,
                'input_budget_estimate': 16384, 'seed_alone_exceeds_input_budget': seed_estimate > 16384,
                'not_provider_token_count': True}
            assert seed_estimate > 16384
        folder = HERE / 'proposed-materials' / name
        folder.mkdir(parents=True, exist_ok=True)
        input_hashes = {}
        for filename, body in card['inputs'].items():
            (folder / filename).write_text(body)
            input_hashes[filename] = hashlib.sha256(body.encode()).hexdigest()
        card['input_sha256'] = input_hashes
        (folder / 'seed-history.json').write_text(json.dumps(card['seed_history'], ensure_ascii=False, indent=2) + '\n')
        (folder / 'task.txt').write_text(card['task'] + '\n')
    schedule = []
    for repeat in (1, 2, 3):
        for index, name in enumerate(materials):
            order = ('A', 'B') if (repeat + index) % 2 else ('B', 'A')
            for arm in order:
                schedule.append({'sequence': len(schedule) + 1, 'case_id': name, 'repeat': repeat, 'arm': arm})
    # Analogue budget, not a promise or hard token cap; new variants have no observed usage.
    references = {}
    for base in ('CTX-SPILL', 'CTX-COMPACT'):
        members = [indexed[base, a] for a in ('A', 'B')]
        references[base] = {'paired_calls': sum(r['metrics']['started_calls'] for r in members),
            'paired_observed_input': sum(r['metrics']['observed_usage']['input'] for r in members),
            'paired_observed_output': sum(r['metrics']['observed_usage']['output'] for r in members)}
    analogue = {key: sum(references[c['base_case_id']][key] * 3 for c in materials.values())
                for key in ('paired_calls', 'paired_observed_input', 'paired_observed_output')}
    result = {'proposal_version': 'context-retest-plan-v1',
        'status': 'proposal-only', 'execution_authorized': False, 'remote_model_calls': 0,
        'split': 'development', 'not_holdout': True, 'source_run': str(RUN.relative_to(ROOT)),
        'model': freeze['model'], 'provider': freeze['provider'], 'case_count': 4, 'trials': 24,
        'hard_limits': {'requests_per_trial': 12, 'total_requests': 288, 'seconds_per_trial': 180,
                        'serial_trial_wall_seconds': 4320, 'tool_calls_per_trial': 36, 'total_tool_calls': 864},
        'analogue_reference_only': analogue, 'cost': None, 'cards': materials, 'schedule': schedule,
        'implementation_boundary': 'Existing runner has six fixed cases/one repeat; '
            'subset/repeats/new seeds need implementation and offline verification after approval; '
            'no new runner behavior implemented here',
        'grading_boundary': 'Gold stays outside future SUT workspaces; '
            'these are reviewer materials, not a runnable public suite'}
    (HERE / 'retest-plan.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    assert len(schedule) == 24
    assert all(sum(r['case_id'] == name and r['arm'] == arm for r in schedule) == 3
               for name in materials for arm in ('A', 'B'))
    print(json.dumps({'trials_proposed': 24, 'remote_calls': 0, 'hard_limits': result['hard_limits'],
                      'analogue': analogue, 'prechecks': {k: c['offline_precheck'] for k,c in materials.items()}},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
