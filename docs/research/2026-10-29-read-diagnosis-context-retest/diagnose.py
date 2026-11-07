"""Offline diagnosis of observable v2 request/tool evidence; never inspect reasoning blocks."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
RUN = ROOT / 'eval-runs/quantification-2026-10-29-tools-format-v2-real'


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def visible_text(blocks):
    return ''.join(b['text'] for b in blocks if b['type'] == 'text')


def current_path(path):
    entries, leaf = {}, None
    for seq, raw in enumerate(read_jsonl(path)):
        if 'id' in raw and 'parent_id' in raw:
            entries[raw['id']] = {**raw, 'line': seq + 1}
            leaf = raw['id']
        elif raw.get('type') == 'checkout':
            leaf = raw['leaf_id']
    chain = []
    while leaf:
        e = entries[leaf]
        chain.append(e)
        leaf = e['parent_id']
    return list(reversed(chain))


def fact_presence(case, body):
    if case['id'] == 'MEM-REUSE':
        values = {'locale': 'ja-JP', 'format': 'bullet_list'}
    elif case['id'] in ('CTX-SPILL', 'HIST-LOCATE', 'HIST-VERSION'):
        values = {k: str(v) for k, v in case['gold'].items()}
    else:
        return None  # Arithmetic facts can be derivable without appearing literally.
    return {key for key, value in values.items() if value in body}


def replay_queries(folder, result, case, query):
    source = sqlite3.connect('file:' + str(folder / 'home/knowledge.db') + '?mode=ro&immutable=1', uri=True)
    db = sqlite3.connect(':memory:')
    source.backup(db)
    source.close()
    db.row_factory = sqlite3.Row
    session_file = next((folder / 'home/sessions').rglob(result['session_id'] + '.jsonl'))
    project = read_jsonl(session_file)[0]['project_id']
    queries = [query]
    if case['id'] == 'HIST-VERSION':
        queries.extend(['南桥交付项目', '南桥交付项目 最终确认'])
    out = []
    for q in queries:
        terms = q.split()
        long_terms = [term for term in terms if len(term) >= 3]
        where, params = ['project_id=?'], [project]
        if long_terms:
            where.append('items match ?')
            params.append(' AND '.join('"' + term.replace('"', '""') + '"' for term in long_terms))
        for term in (term for term in terms if len(term) < 3):
            where.append("(body like ? escape '!' or title like ? escape '!')")
            escaped = term.replace('!', '!!').replace('%', '!%').replace('_', '!_')
            params.extend(['%' + escaped + '%'] * 2)
        ordering = 'bm25(items)' if long_terms else 'at desc'
        hits = db.execute('select record_id,session_id,kind,body from items where ' +
            ' and '.join(where) + ' order by ' + ordering + ' limit 10', params).fetchall()
        out.append({'query': q, 'ids': [h['record_id'] for h in hits],
                    'gold_hit': any(h['record_id'] in result['valid_evidence_ids'] for h in hits),
                    'current_task_hit': any(h['session_id'] == result['session_id'] and
                                            case['task'] in h['body'] for h in hits)})
    gold_body = db.execute('select body from items where record_id=?',
                          (result['valid_evidence_ids'][0],)).fetchone()['body']
    db.close()
    return {'scope': 'In-memory copy of final index; not a rerun of the Agent or a time-of-call snapshot',
            'gold_text': gold_body, 'query_terms_in_gold': {t: t in gold_body for t in query.split()}, 'variants': out}


def main():
    manifest = json.loads((ROOT / 'docs/research/2026-10-29-quantification-v2-real/evidence-sha256.json').read_text())
    assert all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h for p, h in manifest.items())
    freeze = json.loads((RUN / 'freeze.json').read_text())
    cases = {c['id']: c for c in freeze['cases']}
    events, summaries = [], []
    for result_file in sorted(RUN.glob('*/result.json')):
        folder = result_file.parent
        result = json.loads(result_file.read_text())
        case = cases[result['case_id']]
        path_file = next((folder / 'home/sessions').rglob(result['session_id'] + '.jsonl'))
        chain = current_path(path_file)
        requests = ([r for r in read_jsonl(folder / 'requests.jsonl') if r['purpose'] == 'agent']
                    if result['metrics']['started_calls'] else [])
        assistants = [e for e in chain if e['type'] == 'assistant' and e.get('run_id')]
        assert len(requests) == len(assistants)
        returned = {e['data']['call_id']: e for e in chain if e['type'] == 'tool_result'}
        summary = {'case_id': case['id'], 'arm': result['arm'], 'directory': folder.name,
                   'request_fact_presence': [], 'recall_audits': []}
        prior_successful_write = False
        for request_index, (req, assistant) in enumerate(zip(requests, assistants, strict=True), 1):
            before = req['system'] + '\n' + '\n'.join(visible_text(m['content']) for m in req['messages'])
            known = fact_presence(case, before)
            summary['request_fact_presence'].append(sorted(known) if known is not None else None)
            calls = [b for b in assistant['data']['content'] if b['type'] == 'tool_call']
            names = [c['name'] for c in calls]
            for call in calls:
                returned_entry = returned[call['id']]
                response = returned_entry['data']
                body = visible_text(response['content'])
                after = fact_presence(case, body)
                args = {k: v for k, v in call['args'].items() if k != 'content'}
                name, kind = call['name'], 'other'
                path_name = Path(args.get('path', '')).name
                if name == 'read' and path_name == 'result.json' and prior_successful_write:
                    kind = 'artifact_verification'
                elif name == 'read' and path_name in case['inputs']:
                    if case['id'] == 'MEM-REUSE':
                        kind = ('duplicate_fact_confirmation' if known == {'locale', 'format'} else
                                'required_fact_acquisition')
                    elif case['track'] == 'history' and 'recall' in names:
                        kind = 'parallel_source_acquisition'
                    else:
                        kind = 'initial_input_read'
                elif name == 'grep':
                    if case['id'] == 'CTX-SPILL':
                        kind = 'needed_after_spill' if known != set(case['gold']) else 'decision_version_confirmation'
                    elif args.get('pattern', '').startswith('rec_'):
                        kind = 'optional_record_id_exploration'
                    else:
                        kind = 'repeat_content_confirmation'
                elif name == 'recall':
                    kind = 'parallel_history_query' if 'read' in names else 'history_query'
                    ids = re.findall(r'record=(rec_[0-9a-f]{24})', body)
                    audit = {'query': args['query'], 'returned_ids': ids,
                        'target_history_ids': result['valid_evidence_ids'],
                        'target_history_returned': bool(set(ids) & set(result['valid_evidence_ids'])),
                        'requested_facts_in_snippet': sorted(after) if after is not None else None,
                        'same_decision_as_source_read': 'read' in names,
                        'final_index_replay': replay_queries(folder, result, case, args['query'])}
                    summary['recall_audits'].append(audit)
                elif name == 'write':
                    kind = 'artifact_write'
                event = {'case_id': case['id'], 'arm': result['arm'], 'request_index': request_index,
                    'assistant_log_line': assistant['line'], 'result_log_line': returned_entry['line'],
                    'call_id': call['id'], 'tool': name, 'args': args, 'batch_tools': names,
                    'classification': kind, 'successful': not response['is_error'], 'response_chars': len(body),
                    'literal_facts_before_decision': sorted(known) if known is not None else None,
                    'literal_facts_in_response': sorted(after) if after is not None else None,
                    'new_literal_facts': sorted(after - known) if known is not None else None,
                    'response_preview': body[:550],
                    'request_evidence': str((folder / 'requests.jsonl').relative_to(ROOT)),
                    'session_evidence': str(path_file.relative_to(ROOT))}
                events.append(event)
                if name == 'write' and not response['is_error']:
                    prior_successful_write = True
        summaries.append(summary)
    events.sort(key=lambda e: (list(cases).index(e['case_id']), e['arm'], e['request_index'], e['result_log_line']))
    (HERE / 'tool-events.json').write_text(json.dumps(events, ensure_ascii=False, indent=2) + '\n')
    csv_rows = [{**e, 'args': json.dumps(e['args'], ensure_ascii=False), 'batch_tools': ','.join(e['batch_tools']),
                 'literal_facts_before_decision': json.dumps(e['literal_facts_before_decision']),
                 'literal_facts_in_response': json.dumps(e['literal_facts_in_response']),
                 'new_literal_facts': json.dumps(e['new_literal_facts'])} for e in events]
    with (HERE / 'tool-events.csv').open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)
    counts = dict(Counter(e['classification'] for e in events))
    assert len(events) == 36 and sum(e['tool'] == 'read' for e in events) == 19
    assert counts['artifact_verification'] == 10 and counts['duplicate_fact_confirmation'] == 1
    assert counts['parallel_source_acquisition'] == counts['parallel_history_query'] == 2
    audits = [a for s in summaries for a in s['recall_audits']]
    assert sum(a['target_history_returned'] for a in audits) == 1
    result = {'remote_model_calls': 0, 'source_trials': 12, 'tool_calls': len(events), 'read_calls': 19,
              'classifications': counts, 'recall_target_hits': sum(a['target_history_returned'] for a in audits),
              'recall_queries': len(audits), 'trials': summaries,
              'original_evidence_files_verified': len(manifest),
              'method': 'Only visible text, tool arguments and batch membership; no reasoning blocks',
              'causal_limit': 'Observed information overlap is not proof of motive or avoidable provider-token cost'}
    (HERE / 'diagnosis.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    assert all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == h for p, h in manifest.items())
    print(json.dumps({k: v for k, v in result.items() if k != 'trials'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
