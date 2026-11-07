"""Execute the approved four-task, three-repeat context plan; --fake never calls a provider."""
from __future__ import annotations

import argparse
import dataclasses
import json
import statistics
import time
from collections import Counter
from pathlib import Path

from evals import run_quantification as q
from nano_workbuddy import config, usage
from nano_workbuddy.eval.quantification import save
from nano_workbuddy.eval.quantification_cases import output_contract
from nano_workbuddy.eval.schema import digest, file_hash

ROOT = Path(__file__).resolve().parents[1]
PROPOSAL = ROOT / 'docs/research/2026-10-29-read-diagnosis-context-retest/retest-plan.json'
VERSION = 'development-v3-context-retest'


def approved_plan(path=PROPOSAL):
    manifest = json.loads((path.parent / 'evidence-sha256.json').read_text())
    if any(file_hash(ROOT / name) != expected for name, expected in manifest.items()):
        raise ValueError('approved proposal evidence changed')
    plan = json.loads(path.read_text())
    expected_limits = {'requests_per_trial': 12, 'total_requests': 288, 'seconds_per_trial': 180,
                       'serial_trial_wall_seconds': 4320, 'tool_calls_per_trial': 36, 'total_tool_calls': 864}
    expected_order = []
    for repeat in (1, 2, 3):
        for index, case_id in enumerate(plan['cards']):
            for arm in (('A', 'B') if (repeat + index) % 2 else ('B', 'A')):
                expected_order.append({'sequence': len(expected_order) + 1, 'case_id': case_id,
                                       'repeat': repeat, 'arm': arm})
    if (len(plan['cards']) != 4 or plan['trials'] != 24 or plan['schedule'] != expected_order or
            plan['hard_limits'] != expected_limits):
        raise ValueError('schedule or limits differ from the approved 24-trial plan')
    if any(c['limits']['tool_profile'] != q.TOOL_PROFILE for c in plan['cards'].values()):
        raise ValueError('tool profile differs from approved v2 calibration')
    return plan


def cases_from_plan(plan):
    return [dict(id=name, base_case_id=c['base_case_id'], track='context',
                 mechanism='spill' if c['base_case_id'] == 'CTX-SPILL' else 'compact',
                 window=c['context_window'], inputs=c['inputs'], task=c['task'], gold=c['gold'],
                 seed_history=c['seed_history'], dataset_version=VERSION,
                 output_contract=output_contract(c['base_case_id'])) for name, c in plan['cards'].items()]


def stats(values):
    return ({'n': len(values), 'min': min(values), 'median': statistics.median(values), 'max': max(values)}
            if values else None)


def summary_for(results):
    pairs = []
    for case_id, repeat in dict.fromkeys((r['case_id'], r['repeat']) for r in results):
        arms = {r['arm']: r for r in results if (r['case_id'], r['repeat']) == (case_id, repeat)}
        a, b = arms.get('A'), arms.get('B')
        comparable = bool(a and b and all(r['passed'] and r['mode'] == 'real-model' and
                          r['metrics']['usage_complete'] for r in (a, b)) and a['metrics']['actual_input_tokens'])
        pairs.append({'case_id': case_id, 'repeat': repeat,
            'arms': {arm: r['directory'] for arm, r in arms.items()},
            'both_passed': bool(a and b and a['passed'] and b['passed']),
            'actual_input_reduction': 1 - b['metrics']['actual_input_tokens'] / a['metrics']['actual_input_tokens']
                                      if comparable else None,
            'a_input': a['metrics']['actual_input_tokens'] if a else None,
            'b_input': b['metrics']['actual_input_tokens'] if b else None})
    cells = []
    for case_id, arm in dict.fromkeys((r['case_id'], r['arm']) for r in results):
        rows = [r for r in results if (r['case_id'], r['arm']) == (case_id, arm)]
        cells.append({'case_id': case_id, 'arm': arm, 'completed': len(rows), 'planned': 3,
            'passes': sum(r['passed'] for r in rows), 'format_valid': sum(r['format_valid'] for r in rows),
            'local_context_rejections': sum(r.get('error_type') == 'ContextTooLarge' for r in rows),
            'inputs_unchanged': sum(r.get('inputs_unchanged', False) for r in rows),
            'stale_owner_outputs': sum((r.get('actual') or {}).get('owner') == '林杉' for r in rows)
                                  if case_id.startswith('C2-') else None,
            'spill_trials': sum(bool(r.get('spilled_results')) for r in rows),
            'compact_trials': sum(bool(r.get('governance_events', {}).get('compaction')) for r in rows),
            'prune_trials': sum(bool(r.get('governance_events', {}).get('prune')) for r in rows),
            'requests': stats([r['metrics']['started_calls'] for r in rows]),
            'seconds': stats([r['elapsed_seconds'] for r in rows]),
            'input_tokens': stats([r['metrics']['actual_input_tokens'] for r in rows
                                   if r['mode'] == 'real-model' and r['metrics']['usage_complete']]),
            'paired_input_reduction': stats([p['actual_input_reduction'] for p in pairs if p['case_id'] == case_id
                                             and p['actual_input_reduction'] is not None])})
    spill_pairs = [p for p in pairs if p['case_id'].startswith('S') and p['actual_input_reduction'] is not None]
    return {'dataset_version': VERSION, 'split': 'development', 'trials': len(results), 'planned_trials': 24,
        'remote_requests': sum(r['metrics']['started_calls'] for r in results if r['mode'] == 'real-model'),
        'started_calls': sum(r['metrics']['started_calls'] for r in results),
        'responses': sum(r['metrics']['responses'] for r in results),
        'observed_usage': {k: sum(r['metrics']['observed_usage'][k] for r in results)
                           for k in ('input', 'output', 'cache_read')},
        'usage_complete_or_no_requests': bool(results) and all(r['metrics']['usage_complete'] or
                                          r['metrics']['started_calls'] == 0 for r in results),
        'mode': results[0]['mode'] if results else None, 'pairs': pairs, 'cells': cells,
        'spill_comparable_pairs': len(spill_pairs), 'spill_planned_pairs': 6,
        'spill_weighted_input_reduction': 1 - sum(p['b_input'] for p in spill_pairs) /
            sum(p['a_input'] for p in spill_pairs) if spill_pairs else None,
        'cost': None,
        'scope': 'Four synthetic development tasks, three repeats; not 24 independent tasks or production SLA'}


def report_retest(output, results):
    summary = summary_for(results)
    save(output / 'summary.json', summary)
    lines = ['# Context retest', '', summary['scope'], '',
        f"Mode: {summary['mode']}; trials {len(results)}/24; requests {summary['started_calls']}/288; cost unknown.",
        '', '| Sequence | Task | Repeat | Arm | Facts | Format | Pass | Requests | Input | Seconds |',
        '|---|---|---|---|---|---|---|---|---|---|']
    for r in results:
        tokens = r['metrics']['actual_input_tokens']
        shown = (str(tokens) if tokens is not None else
                 ('0 (no request)' if not r['metrics']['started_calls'] else 'unknown'))
        lines.append(f"| {r['sequence']} | {r['case_id']} | {r['repeat']} | {r['arm']} | "
            f"{r['factual_fields_correct']}/{r['factual_fields_required']} | {r['format_valid']} | {r['passed']} | "
            f"{r['metrics']['started_calls']} | {shown} | {r['elapsed_seconds']:.2f} |")
    lines += ['', 'Only both-passing real-model pairs with complete usage compare input tokens. '
        'Compaction local-failure controls do not provide a token-savings baseline. '
        'Fake usage is scripted; cache is already included in input. All failures remain in results.']
    (output / 'report.md').write_text('\n'.join(lines) + '\n')


def run(output, fake=False, model_name='ds-flash', source_home=None):
    plan = approved_plan()
    cases = cases_from_plan(plan)
    if fake:
        original = config.ModelSpec('deterministic', config.Provider('fake', 'openai-chat', 'http://invalid', ''),
                                    'fake', 100000, 2048)
        secret = ''
    else:
        source = config.load(source_home)
        original = source.model(model_name)
        provider = dataclasses.asdict(dataclasses.replace(original.provider, api_key='env:NWB_QUANT_KEY'))
        model = dataclasses.asdict(original)
        model.pop('provider')
        model['max_output'] = 2048
        if provider != plan['provider'] or model != plan['model']:
            raise ValueError('selected provider/model configuration differs from approved plan')
        secret = config.secret(original.provider.api_key, source.home)
        if not secret:
            raise ValueError('selected model key unavailable')
    output.mkdir(parents=True, exist_ok=False)
    version = q.source_snapshot(output / 'source.zip', [Path(__file__).resolve(),
        ROOT / 'tests/test_context_retest.py', ROOT / 'tests/conftest.py'])
    model = dataclasses.asdict(original)
    model.pop('provider')
    model['max_output'] = 2048
    freeze = {'source': version, 'cases': cases, 'schedule': plan['schedule'], 'dataset_sha256': digest(cases),
        'dataset_version': VERSION, 'tool_profile': q.TOOL_PROFILE,
        'tool_schemas': [t.schema() for t in q.trial_tools(cases[0], 'A')],
        'provider': dataclasses.asdict(dataclasses.replace(original.provider, api_key='env:NWB_QUANT_KEY')),
        'model': model, 'fake': fake, 'split': 'development', 'approved_plan_sha256': file_hash(PROPOSAL),
        'authorization': 'User confirmed the submitted 24-trial/288-request/72-minute context proposal in this chat',
        'limits': {'trials': 24, 'per_trial_requests': 12, 'total_requests': 288,
                   'per_trial_seconds': 180, 'sdk_retries': 0}, 'created_at': time.time()}
    save(output / 'approved-plan.json', plan)
    return q.scheduled_trials(output, freeze, secret, report_retest)


def audit_tools(output):
    """Classify visible information acquisition; retain uncertainty about model motives."""
    freeze = json.loads((output / 'freeze.json').read_text())
    cases = {c['id']: c for c in freeze['cases']}
    rows, totals = [], Counter()
    for path in sorted(output.glob('*/result.json')):
        result = json.loads(path.read_text())
        case = cases[result['case_id']]
        if not result['metrics']['started_calls']:
            continue
        folder = path.parent
        log_file = next((folder / 'home/sessions').rglob(result['session_id'] + '.jsonl'))
        log = q.SessionLog.open(log_file)
        chain = log.path()
        requests = [r for r in usage.events(folder / 'requests.jsonl') if r['purpose'] == 'agent']
        assistants = [e for e in chain if e.type == 'assistant' and e.run_id]
        assert len(requests) == len(assistants)
        returned = {e.data['call_id']: e for e in chain if e.type == 'tool_result'}
        wrote, input_seen, spill_seen = False, False, False
        for index, (request, assistant) in enumerate(zip(requests, assistants, strict=True), 1):
            visible = request['system'] + '\n' + '\n'.join(
                b['text'] for m in request['messages'] for b in m['content'] if b['type'] == 'text')
            known = {k for k,v in case['gold'].items() if str(v) in visible} if case['mechanism'] == 'spill' else None
            calls = assistant.tool_calls
            for call in calls:
                response = returned[call['id']]
                body = ''.join(b['text'] for b in response.data['content'] if b['type'] == 'text')
                name, args = call['name'], call['args']
                basename = Path(args.get('path', '')).name
                new = {k for k,v in case['gold'].items() if str(v) in body} - known if known is not None else None
                classification = 'other'
                if name == 'read' and basename == 'result.json' and wrote:
                    classification = 'artifact_verification'
                elif name in ('read', 'grep'):
                    if known == set(case['gold']):
                        classification = 'known_fact_confirmation_or_verification'
                    elif new:
                        classification = 'needed_after_spill' if spill_seen else 'initial_or_targeted_acquisition'
                    elif not input_seen and name == 'read':
                        classification = 'initial_input_read'
                    else:
                        classification = 'verification_or_uncertain'
                    input_seen = True
                elif name in ('write', 'edit') and basename == 'result.json':
                    classification = 'artifact_write'
                    wrote = not response.data.get('is_error')
                spilled = bool(response.data.get('meta', {}).get('exposure'))
                rows.append({'sequence': result['sequence'], 'case_id': case['id'], 'repeat': result['repeat'],
                    'arm': result['arm'], 'request_index': index, 'call_id': call['id'], 'tool': name,
                    'args': {k:v for k,v in args.items() if k != 'content'},
                    'batch_tools': [c['name'] for c in calls], 'classification': classification,
                    'successful': not response.data.get('is_error'), 'response_chars': len(body), 'spilled': spilled,
                    'literal_facts_before': sorted(known) if known is not None else None,
                    'new_literal_facts': sorted(new) if new is not None else None,
                    'session_file': (str(log_file.relative_to(ROOT))
                                     if log_file.is_relative_to(ROOT) else str(log_file))})
                totals[classification] += 1
                spill_seen |= spilled
        log.close()
    rows.sort(key=lambda r: (r['sequence'], r['request_index']))
    save(output / 'tool-audit.json', {'method': 'Visible text/tool calls only; no reasoning or motive inference',
        'classifications': dict(totals), 'events': rows})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--fake', action='store_true')
    parser.add_argument('--model', default='ds-flash')
    args = parser.parse_args()
    run(args.output.resolve(), args.fake, args.model)
    audit_tools(args.output.resolve())


if __name__ == '__main__':
    main()
