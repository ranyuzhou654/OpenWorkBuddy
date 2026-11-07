"""Verify metric denominators, real ingestion, isolation and the bounded trial pipeline."""
import dataclasses
import json

import pytest
from conftest import FakeModel, call, reply

from evals.run_quantification import (
    QuantWorkspace,
    close_workspace,
    initial_prompt,
    offline_memory,
    pilot,
    score_trial,
)
from nano_workbuddy import policy, usage
from nano_workbuddy.config import load
from nano_workbuddy.eval.quantification import (
    aggregate_retrieval,
    ledger_metrics,
    ranked_metrics,
    repeated_reads,
    retrieval_fixture,
    write_corpus,
)
from nano_workbuddy.eval.quantification_cases import pilot_cases
from nano_workbuddy.knowledge import Knowledge
from nano_workbuddy.tools import CORE, Registry


def test_retrieval_units_deduplicate_and_no_answer_is_not_zero_recall():
    assert ranked_metrics(["wrong", "right", "right"], ["right"]) == {
        "recall5": 1, "recall10": 1, "hit5": True, "mrr10": .5, "false_return": None}
    assert ranked_metrics([], ["right"])["recall5"] == 0
    assert ranked_metrics(["wrong"], [])["false_return"] is True
    assert ranked_metrics([], [])["recall5"] is None
    rows = [{"relevant": ["right"], **ranked_metrics([], ["right"]), "scope_violations": 0,
             "unauthorized_read": False, "first_ms": 1., "repeat_ms": [1.]},
            {"relevant": [], **ranked_metrics(["wrong"], []), "scope_violations": 1,
             "unauthorized_read": True, "first_ms": 2., "repeat_ms": [2.]}]
    aggregate = aggregate_retrieval(rows)
    assert aggregate["answerable_queries"] == 1 and aggregate["recall5"] == 0
    assert aggregate["false_return_rate"] == 1 and aggregate["scope_violations"] == 1


def test_fixture_roundtrips_message_source_and_file_through_real_index(tmp_path):
    records, queries = retrieval_fixture()
    assert len(queries) == 120
    assert sum(q["split"] == "development" for q in queries) == 40
    store = tmp_path / "store"
    rows = write_corpus(tmp_path / "corpus", records, 1000, store)
    db = Knowledge(tmp_path / "knowledge.db", store)
    while db.sync(rows):
        pass
    assert db.db.execute("select count(*) from items").fetchone()[0] == 1000
    for r in records:
        found = db.read_record(r["id"], 0, 6000, session_ids=[r["session_id"]])
        assert found is not None, r
        assert r["body"] in found["body"] and r["kind"] == found["kind"]
    for q in queries:
        assert db.read_record(q["forbidden_id"], 0, 100, **q["scope"]) is None
        if q["category"] == "paraphrase":
            assert db.search(q["query"], **q["scope"]) == []
    db.db.close()


def test_usage_missing_response_and_summary_overhead_remain_visible(tmp_path):
    p = tmp_path / "usage.jsonl"
    usage.append(p, {"type": "model.started", "call_id": "one", "purpose": "compaction"})
    usage.append(p, {"type": "model.response", "call_id": "one", "usage_complete": True,
                     "usage": {"input": 100, "output": 5, "cache_read": 60}})
    usage.append(p, {"type": "model.started", "call_id": "two", "purpose": "agent"})
    measured = ledger_metrics([p])
    assert measured["summary_calls"] == 1 and measured["observed_usage"]["input"] == 100
    assert measured["actual_input_tokens"] is None and not measured["usage_complete"]
    assert measured["cost"] is None


def test_changed_bytes_are_not_duplicate_reads():
    one = {"path": "a", "sha256": "v1", "offset": 1, "limit": 10, "chars": 30}
    result = repeated_reads([one, one, {**one, "sha256": "v2"}, {**one, "offset": 2}])
    assert result["exact_repeat_reads"] == 1 and result["exact_repeat_chars"] == 30


async def test_memory_lifecycle_is_real_file_mutation_without_model(tmp_path):
    await offline_memory(tmp_path / "memory")
    result = json.loads((tmp_path / "memory/result.json").read_text())
    assert result["passed"] and result["model_calls"] == 0


async def test_whole_trial_limit_stops_before_thirteenth_ledger_start(tmp_path):
    model = FakeModel(*(reply(None, call("read", path="data.txt")) for _ in range(20)))
    cfg = dataclasses.replace(load(tmp_path / "home"), plugins_enabled=(), max_turns=30,
        rules=(policy.Rule("*", "*", "deny"), policy.Rule("read", "{root}/data.txt", "allow")))
    ws = QuantWorkspace(cfg, connect=model.connect, tools=Registry([t for t in CORE if t.name == "read"]))
    ws.case_id, ws.track, ws.arm, ws.trial = "LIMIT", "memory_reuse", "B", tmp_path
    import time
    ws.calls, ws.deadline, ws.checkpoint_id = 0, time.monotonic() + 60, None
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "data.txt").write_text("data")
    log = ws.new_session(root, initial_prompt=initial_prompt(root, cfg.home))
    assert await ws.submit(log, "keep reading") == "limit"
    events = usage.events(ws.usage_path(log))
    assert sum(e["type"] == "model.started" for e in events) == 12
    assert ws.calls == 12 and len(model.requests) == 12
    await close_workspace(ws)


def test_twelve_fake_trials_exercise_real_subprocesses_and_ablation(tmp_path):
    results = pilot(tmp_path / "pilot", fake=True)
    assert len(results) == 12
    indexed = {(r["case_id"], r["arm"]): r for r in results}
    assert not indexed["CTX-COMPACT", "A"]["passed"]
    assert indexed["CTX-COMPACT", "A"]["metrics"]["started_calls"] == 0
    assert indexed["CTX-COMPACT", "B"]["passed"]
    assert indexed["CTX-COMPACT", "B"]["metrics"]["summary_calls"] >= 1
    assert indexed["CTX-SPILL", "B"]["spilled_results"] >= 1
    assert indexed["CTX-SPILL", "B"]["grep_calls"] == 1
    assert indexed["CTX-SPILL", "B"]["same_history_projection"]["reduction"] > 0
    assert not indexed["MEM-FRESH", "A"]["passed"]
    assert indexed["MEM-FRESH", "B"]["passed"]
    assert all(r["passed"] for r in results if (r["case_id"], r["arm"]) not in {
        ("CTX-COMPACT", "A"), ("MEM-FRESH", "A")})
    for r in results:
        directory = tmp_path / "pilot" / r["directory"]
        public = json.loads((directory / "public.json").read_text())
        assert "gold" not in public["case"]
        assert 'grep' in public['tool_names']
        assert public['case']['output_contract']['additionalProperties'] is False
        assert 'gold' not in public['case']['output_contract']
        assert all(not t["name"].startswith(("web", "feishu", "bash"))
                   for req in usage.events(directory / "requests.jsonl") for t in req["tools"])
    for case in pilot_cases():
        a = indexed[case['id'], 'A']['tool_names']
        b = indexed[case['id'], 'B']['tool_names']
        assert a == ['read', 'write', 'edit', 'grep']
        assert b == a + (['recall', 'read_record'] if case['track'] == 'history' else [])
    summary = json.loads((tmp_path / "pilot/summary.json").read_text())
    assert summary["started_calls"] <= 144
    assert summary['remote_model_requests'] == 0
    assert all(p.get("actual_input_reduction") is None for p in summary["pairs"])
    # A valid historical ID without a real read cannot be used to manufacture attribution credit.
    r = indexed["HIST-LOCATE", "B"]
    directory = tmp_path / "pilot" / r["directory"]
    data = {**next(c for c in pilot_cases() if c["id"] == "HIST-LOCATE")["gold"],
            "evidence": r["valid_evidence_ids"][0]}
    (directory / "workspace/result.json").write_text(json.dumps(data))
    rescored = score_trial(directory, next(c for c in pilot_cases() if c["id"] == "HIST-LOCATE"), r)
    assert not rescored["passed"]


async def test_three_offline_context_tiers_preserve_raw_bytes(tmp_path):
    from evals.run_quantification import offline_context
    await offline_context(tmp_path / "context")
    result = json.loads((tmp_path / "context/result.json").read_text())
    assert result["model_calls_remote"] == 0
    tiers = {r["mechanism"]: r for r in result["measurements"]}
    assert tiers["spill"]["full_copy_matches"]
    assert tiers["prune"]["triggered"] and tiers["compact"]["triggered"]
    assert tiers["compact"]["raw_log_preserved"]
    assert all(r["reduction"] > 0 for r in tiers.values())


@pytest.mark.parametrize('bad_value', [True, 72800.0, '72800', -1])
def test_output_contract_rejects_non_integer_or_negative_budget(bad_value):
    from nano_workbuddy.eval.quantification_cases import contract_errors, output_contract
    errors = contract_errors({'supplier': '北杉', 'budget': bad_value, 'deadline': '2026-11-18'},
                             output_contract('CTX-SPILL'))
    assert any(e.startswith('budget:') for e in errors)


def test_contract_is_public_explicit_and_not_a_copy_of_gold_answers():
    from nano_workbuddy.eval.quantification_cases import contract_errors
    for case in pilot_cases():
        contract = case['output_contract']
        actual = {**case['gold']}
        if case['track'] == 'history':
            actual['evidence'] = 'previous-notes.txt'
        assert contract_errors(actual, contract) == []
        assert '非空字符串' in case['task'] and '禁止添加' in case['task']
        assert 'expected' not in json.dumps(contract)
        assert str(case['gold'].get('budget', 'NO_GOLD_NUMBER')) not in json.dumps(contract)
        assert 'additionalProperties' in contract and not contract['additionalProperties']
    contract = next(c for c in pilot_cases() if c['id'] == 'HIST-VERSION')['output_contract']
    data = {'deadline': '2026-11-18', 'budget': 72000, 'evidence': ['previous-notes.txt'], 'note': 'explanation'}
    assert 'evidence: expected string' in contract_errors(data, contract)
    assert 'note: unexpected field' in contract_errors(data, contract)
    assert contract_errors({**data, 'evidence': '[S1]' }, contract)


@pytest.mark.parametrize('backend', ['python', 'native'])
async def test_scoped_grep_reaches_only_input_files_in_both_arms(tmp_path, monkeypatch, backend):
    import shutil
    import time

    from evals.run_quantification import trial_rules, trial_tools
    from nano_workbuddy.tools import search
    from nano_workbuddy.tools.registry import execute
    from nano_workbuddy.types import text_of

    if backend == 'native' and shutil.which('rg') is None:
        pytest.skip('ripgrep is not installed')
    if backend == 'python':
        monkeypatch.setattr(search.shutil, 'which', lambda _: None)
    case = next(c for c in pilot_cases() if c['id'] == 'CTX-SPILL')
    for arm in ('A', 'B'):
        folder = tmp_path / arm
        root = folder / 'workspace'
        root.mkdir(parents=True)
        (root / 'material.txt').write_text('PUBLIC_MARKER allowed\n')
        (root / 'grader.txt').write_text('PRIVATE_MARKER not allowed\n')
        (folder / 'outside.txt').write_text('OUTSIDE_MARKER not allowed\n')
        (root / 'link.txt').symlink_to(folder / 'outside.txt')
        cfg = dataclasses.replace(load(folder / 'home'), rules=trial_rules(case, arm), plugins_enabled=())
        tools = trial_tools(case, arm)
        assert [t.name for t in tools] == ['read', 'write', 'edit', 'grep']
        model = FakeModel()
        ws = QuantWorkspace(cfg, connect=model.connect, tools=Registry(tools))
        ws.case_id, ws.track, ws.arm, ws.trial = case['id'], case['track'], arm, folder
        ws.calls, ws.deadline, ws.checkpoint_id = 0, time.monotonic() + 60, None
        log = ws.new_session(root, initial_prompt=initial_prompt(root, cfg.home))
        run = ws.make_run(log, cfg.default_model, None, lambda _: None)
        tool = run.tools.get('grep')
        c = call('grep', path='.', pattern='MARKER', mode='content')
        assert await run.gate(tool, c) is None
        response = await execute(tool, c, run.ctx)
        assert not response.is_error
        assert 'PUBLIC_MARKER' in text_of(response.content)
        assert 'PRIVATE_MARKER' not in text_of(response.content)
        assert 'OUTSIDE_MARKER' not in text_of(response.content)
        for path in ['grader.txt', 'link.txt', '../outside.txt']:
            denied = await run.gate(tool, call('grep', path=path, pattern='MARKER', mode='content'))
            assert denied is not None and denied.is_error
        await close_workspace(ws)


@pytest.mark.parametrize('via', ['grep', 'read', 'recall', 'read_record', 'unread'])
def test_attribution_accepts_observed_support_without_requiring_extra_reads(tmp_path, via):
    from nano_workbuddy.log import SessionLog
    from nano_workbuddy.types import ASSISTANT, TOOL_RESULT, text

    case = next(c for c in pilot_cases() if c['id'] == 'HIST-VERSION')
    root = tmp_path / 'workspace'
    root.mkdir()
    record = 'rec_' + 'a' * 24
    other = 'rec_' + 'b' * 24
    evidence = 'previous-notes.txt' if via in ('grep', 'read') else record
    (root / 'result.json').write_text(json.dumps({**case['gold'], 'evidence': evidence}))
    path = tmp_path / 'home/sessions/p0/s0.jsonl'
    log = SessionLog.create(path, 's0', 'p0')
    if via != 'unread':
        args = ({'path': '.', 'pattern': '最终确认', 'mode': 'content'} if via == 'grep' else
                {'path': 'previous-notes.txt'} if via == 'read' else
                {'query': '南桥交付项目'} if via == 'recall' else {'ref': record})
        c = call(via, **args)
        log.append(ASSISTANT, {'content': [c]})
        body = ('previous-notes.txt:2:' if via == 'grep' else f'record={record}\n') + case['history']
        log.append(TOOL_RESULT, {'call_id': c['id'], 'name': via, 'content': [text(body)], 'is_error': False})
    log.close()
    state = dict(status='ok', inputs_unchanged=True, history_append_only=True, valid_evidence_ids=[record])
    scored = score_trial(tmp_path, case, state)
    assert scored['format_valid'] and scored['factual_fields_correct'] == 2
    assert scored['passed'] is (via != 'unread')
    # The record ID in the answer must be the same one read, not a different eligible ID.
    if via == 'read_record':
        (root / 'result.json').write_text(json.dumps({**case['gold'], 'evidence': other}))
        assert not score_trial(tmp_path, case, {**state, 'valid_evidence_ids': [record, other]})['passed']
    (root / 'result.json').write_text(json.dumps({**case['gold'], 'evidence': ['previous-notes.txt'], 'note': 'extra'}))
    malformed = score_trial(tmp_path, case, state)
    assert malformed['factual_fields_correct'] == 2 and not malformed['format_valid'] and not malformed['passed']
