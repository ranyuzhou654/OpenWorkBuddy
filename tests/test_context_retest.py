"""Offline checks of the approved context experiment; no provider requests."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from evals import run_context_retest as retest
from evals import run_quantification as q
from nano_workbuddy import usage
from nano_workbuddy.budgets import BudgetExceeded


def test_no_child_starts_without_whole_trial_global_budget(tmp_path):
    freeze = {'source': {'code_files': {}}, 'cases': [], 'schedule': [{'case_id': 'unused', 'arm': 'A'}],
              'limits': {'trials': 1, 'total_requests': 11}}
    with pytest.raises(BudgetExceeded, match='insufficient global budget'):
        q.scheduled_trials(tmp_path, freeze, '', lambda *_: None)
    assert not (tmp_path / 'trials.jsonl').exists()
    assert not list(tmp_path.glob('*/public.json'))


def test_twenty_four_trials_preserve_updates_pairs_and_isolation(tmp_path):
    output = tmp_path / 'retest'
    results = retest.run(output, fake=True)
    assert len(results) == 24
    assert sum(r['passed'] for r in results) == 18
    cells = Counter((r['case_id'], r['arm']) for r in results)
    assert len(cells) == 8 and set(cells.values()) == {3}
    for r in results:
        if r['case_id'].startswith('C') and r['arm'] == 'A':
            assert not r['passed'] and r['error_type'] == 'ContextTooLarge'
            assert r['metrics']['started_calls'] == 0
        else:
            assert r['passed']
        if r['case_id'].startswith('C2-') and r['arm'] == 'B':
            assert r['actual'] == {'owner': '许澄', 'total': 8000, 'protected_file': 'input.json'}
            assert r['metrics']['summary_calls'] == 1 and r['inputs_unchanged']
        if r['case_id'].startswith('S') and r['arm'] == 'B':
            assert r['spilled_results'] == 1 and r['grep_calls'] == 1
        folder = output / r['directory']
        public = json.loads((folder / 'public.json').read_text())
        assert 'gold' not in public['case']
        assert public['tool_names'] == ['read', 'write', 'edit', 'grep']
        assert {p.name for p in (folder / 'workspace').iterdir()} <= {*public['case']['inputs'], 'result.json'}
        assert all([t['name'] for t in req['tools']] == public['tool_names']
                   for req in usage.events(folder / 'requests.jsonl') if req['purpose'] == 'agent')
    summary = json.loads((output / 'summary.json').read_text())
    assert summary['remote_requests'] == 0
    assert len(summary['pairs']) == 12 and all(p['actual_input_reduction'] is None for p in summary['pairs'])
    events = retest.audit_tools(output)
    assert sum(e['classification'] == 'needed_after_spill' for e in events) == 6
    assert not any(e['tool'] in ('recall', 'read_record', 'bash') for e in events)

    # Synthetic arithmetic check: incomplete/failed pairs stay visible, with savings coverage reduced.
    synthetic = copy.deepcopy(results)
    for r in synthetic:
        if r['case_id'].startswith('S'):
            r['mode'] = 'real-model'
            r['metrics']['actual_input_tokens'] = 100 if r['arm'] == 'A' else 50
            r['metrics']['usage_complete'] = True
    incomplete = next(r for r in synthetic if r['case_id'].startswith('S2-') and r['repeat'] == 1 and r['arm'] == 'B')
    incomplete['metrics']['usage_complete'] = False
    incomplete['metrics']['actual_input_tokens'] = None
    combined = retest.summary_for(synthetic)
    assert combined['spill_comparable_pairs'] == 5 and combined['spill_planned_pairs'] == 6
    assert combined['spill_weighted_input_reduction'] == .5
    assert len(combined['pairs']) == 12
