"""Offline final report from frozen trials. Never regrade, rerun, or interpret reasoning."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from evals.run_context_retest import summary_for

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
RUN = ROOT / "eval-runs/context-retest-2026-10-29-real"


def pct(value):
    return f"{value:.2%}" if value is not None else "不比较"


def main():
    freeze = json.loads((RUN / "freeze.json").read_text())
    results = sorted((json.loads(p.read_text()) for p in RUN.glob("*/result.json")), key=lambda r: r["sequence"])
    if len(results) != 24:
        raise ValueError("not all 24 trials finished; do not publish a partial result as complete")
    assert [(r["case_id"], r["repeat"], r["arm"]) for r in results] == [
        (s["case_id"], s["repeat"], s["arm"]) for s in freeze["schedule"]
    ]
    summary = summary_for(results)
    assert summary == json.loads((RUN / "summary.json").read_text())
    assert summary["started_calls"] <= 288
    by_purpose, rows = defaultdict(list), []
    for r in results:
        folder = RUN / r["directory"]
        assert r["metrics"]["started_calls"] <= 12 and r["calls_reserved"] <= 12
        events = [
            json.loads(line)
            for p in (folder / "home/usage").glob("*.jsonl")
            for line in p.read_text().splitlines()
            if line.strip()
        ]
        for e in events:
            if e["type"] in ("model.started", "model.response", "model.error"):
                by_purpose[e.get("purpose", "unknown")].append(e)
        rows.append(
            dict(
                sequence=r["sequence"],
                case_id=r["case_id"],
                repeat=r["repeat"],
                arm=r["arm"],
                passed=r["passed"],
                status=r["status"],
                error_type=r.get("error_type", ""),
                facts_correct=r["factual_fields_correct"],
                facts_required=r["factual_fields_required"],
                format_valid=r["format_valid"],
                inputs_unchanged=r["inputs_unchanged"],
                history_append_only=r["history_append_only"],
                calls=r["metrics"]["started_calls"],
                responses=r["metrics"]["responses"],
                usage_complete=r["metrics"]["usage_complete"],
                actual_input=r["metrics"]["actual_input_tokens"],
                observed_input=r["metrics"]["observed_usage"]["input"],
                observed_output=r["metrics"]["observed_usage"]["output"],
                cache_read_included=r["metrics"]["observed_usage"]["cache_read"],
                seconds=r["elapsed_seconds"],
                grep_calls=r["grep_calls"],
                read_calls=r["reads"]["reads"],
                exact_repeat_reads=r["reads"]["exact_repeat_reads"],
                spill_results=r["spilled_results"],
                summary_calls=r["metrics"]["summary_calls"],
                prune_events=r["governance_events"]["prune"],
                compact_events=r["governance_events"]["compaction"],
                tool_errors=r["tool_errors"],
                projection_reduction=r["same_history_projection"]["reduction"],
                actual=json.dumps(r["actual"], ensure_ascii=False),
                directory=r["directory"],
            )
        )
    with (HERE / "trials.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    purposes = {
        name: {
            "started": sum(e["type"] == "model.started" for e in ev),
            "responses": sum(e["type"] == "model.response" for e in ev),
            "errors": sum(e["type"] == "model.error" for e in ev),
            "observed_usage": {
                k: sum(e["usage"].get(k, 0) for e in ev if e["type"] == "model.response")
                for k in ("input", "output", "cache_read")
            },
        }
        for name, ev in by_purpose.items()
    }
    assert sum(p["started"] for p in purposes.values()) == summary["started_calls"]
    (HERE / "purpose-usage.json").write_text(json.dumps(purposes, ensure_ascii=False, indent=2) + "\n")
    audit = json.loads((RUN / "tool-audit.json").read_text())
    cases = {c["id"]: c for c in freeze["cases"]}
    reads = [e for e in audit["events"] if e["tool"] == "read"]
    source_reads = [e for e in reads if Path(e["args"]["path"]).name in cases[e["case_id"]]["inputs"]]
    breakdown = {
        "all_read_attempts": len(reads),
        "post_write_artifact_verifications": sum(e["classification"] == "artifact_verification" for e in reads),
        "source_reads": len(source_reads),
        "source_reads_with_all_target_facts_already_visible": sum(
            e["classification"] == "known_fact_confirmation_or_verification" for e in source_reads),
        "pre_write_artifact_read_errors": [e["sequence"] for e in reads if
            Path(e["args"]["path"]).name == "result.json" and not e["successful"]],
        "exact_repeat_reads": sum(r["reads"]["exact_repeat_reads"] for r in results),
    }
    (HERE / "read-breakdown.json").write_text(json.dumps(breakdown, ensure_ascii=False, indent=2) + "\n")
    controls = [r for r in results if r["case_id"].startswith("C") and r["arm"] == "A"]
    evaluated = [r for r in results if r not in controls]
    lines = [
        "# 24 次上下文真实模型复测",
        "",
        "日期：2026-10-29；Asia/Hong_Kong。",
        "",
        "用户确认已提交的 24 次方案后，扩展评测调度并完成离线验证，再执行本轮。"
        "产品运行时、系统提示词和检索策略未修改。",
        "配置：opendesign / deepseek-v4.1-flash；max_output=2048；SDK retries=0。"
        "4 个合成开发任务，各 A/B 三次；S1/C1 复现原题，S2/C2 是受控变体。不是 24 道独立新题。",
        "",
        f"已完成 {len(results)}/24 次，模型请求 {summary['started_calls']}/288，完整响应 {summary['responses']}。"
        f"所有请求 usage 完整或未发请求：{summary['usage_complete_or_no_requests']}。",
        f"观察到 input {summary['observed_usage']['input']:,}、output {summary['observed_usage']['output']:,}；"
        f"cache_read {summary['observed_usage']['cache_read']:,} 已包含在 input 中。人民币费用 unknown。",
        "事前同类样本参考量为 90 请求、1,226,754 input；本轮实际用量高于参考，"
        "请求数仍在 288 硬上限内。各 trial 的父进程计时合计 "
        f"{sum(r['parent_elapsed_seconds'] for r in results):.2f} 秒（约 "
        f"{sum(r['parent_elapsed_seconds'] for r in results)/60:.2f} 分钟），试验窗口上限 72 分钟。",
        "",
        f"S 组及 C 组 B 的交付通过 {sum(r['passed'] for r in evaluated)}/{len(evaluated)} 次；"
        f"另有 {sum(r.get('error_type') == 'ContextTooLarge' for r in controls)}/{len(controls)} 次 C 组 A "
        "为预设本地超预算对照，全部保留。不能将这两类合并成生产成功率。",
        "",
        "## 大材料任务：六组配对",
        "",
        "| 任务 | 轮次 | A input | B input | B input 降幅 | 请求 A→B | 秒 A→B | B spill |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lookup = {(r["case_id"], r["repeat"], r["arm"]): r for r in results}
    for p in summary["pairs"]:
        if not p["case_id"].startswith("S"):
            continue
        a, b = (lookup[p["case_id"], p["repeat"], arm] for arm in ("A", "B"))
        lines.append(
            f"| {p['case_id']} | {p['repeat']} | {p['a_input']} | {p['b_input']} | "
            f"{pct(p['actual_input_reduction'])} | {a['metrics']['started_calls']}→{b['metrics']['started_calls']} | "
            f"{a['elapsed_seconds']:.2f}→{b['elapsed_seconds']:.2f} | {b['spilled_results']} |"
        )
    lines += [
        "",
        f"纳入完整用量且两组均通过的配对 {summary['spill_comparable_pairs']}/6；"
        f"按总 input 加权降幅为 {pct(summary['spill_weighted_input_reduction'])}。"
        "公式是 1−ΣB_input/ΣA_input，失败和缺失仍在逐次表中。",
        "",
    ]
    spill_totals = {arm: {"input": sum(r["metrics"]["actual_input_tokens"] for r in results
                                      if r["case_id"].startswith("S") and r["arm"] == arm),
                         "calls": sum(r["metrics"]["started_calls"] for r in results
                                      if r["case_id"].startswith("S") and r["arm"] == arm),
                         "seconds": sum(r["elapsed_seconds"] for r in results
                                        if r["case_id"].startswith("S") and r["arm"] == arm)} for arm in ("A", "B")}
    a, b = spill_totals["A"], spill_totals["B"]
    lines += [f"六组大材料累计 input {a['input']:,}→{b['input']:,}；"
              f"请求 {a['calls']}→{b['calls']}，累计耗时 {a['seconds']:.2f}→{b['seconds']:.2f} 秒。"
              "调用数增加，单组耗时有升有降，不据此宣称稳定提速。", ""]
    for c in summary["cells"]:
        if c["arm"] == "B" and c["case_id"].startswith("S"):
            ratios = c["paired_input_reduction"]
            lines.append(
                f"- {c['case_id']}：通过 {c['passes']}/3，spill 触发 {c['spill_trials']}/3；"
                + (
                    f"配对降幅最小 {pct(ratios['min'])}、中位 {pct(ratios['median'])}、最大 {pct(ratios['max'])}。"
                    if ratios
                    else "没有可比较的完整配对。"
                )
            )
    lines += [
        "",
        "## 压缩压力与最新约束",
        "",
        "| 任务 | 组 | 交付通过 | 本地超预算 | compact 触发 | 输入文件未变 | 误用旧负责人 |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in summary["cells"]:
        if c["case_id"].startswith("C"):
            lines.append(
                f"| {c['case_id']} | {c['arm']} | {c['passes']}/3 | {c['local_context_rejections']}/3 | "
                f"{c['compact_trials']}/3 | {c['inputs_unchanged']}/3 | "
                f"{c['stale_owner_outputs'] if c['stale_owner_outputs'] is not None else '不适用'} |"
            )
    lines += [
        "",
        "C1 正确值为 林杉 / 7200 / input.json；C2 为 许澄 / 8000 / input.json。"
        "实际结果字段、格式和保护文件哈希分别核验。A 使用人为设定的 32,768 本地窗口，"
        "发请求前超预算时仍计失败；B 的摘要与后续请求成本均计入，不与 A 的零请求计算节省率。",
        "",
        "## 请求构成与读取用途",
        "",
        "| 用途 | 请求开始 | 响应 | 错误 | 观察 input | 观察 output |",
        "|---|---|---|---|---|---|",
    ]
    for purpose, p in purposes.items():
        lines.append(
            f"| {purpose} | {p['started']} | {p['responses']} | {p['errors']} | "
            f"{p['observed_usage']['input']} | {p['observed_usage']['output']} |"
        )
    lines += [
        "",
        "以下分类依据模型可见文本、工具参数和同批调用；没有解读 reasoning。"
        "“已知事实确认或核验”表示信息重叠，不证明动机，也不直接换算为可避免的 token。",
        "",
        "| 工具用途 | 次数 |",
        "|---|---|",
    ]
    for name, n in audit["classifications"].items():
        lines.append(f"| {name} | {n} |")
    lines += [
        "",
        "读取结果文件与补回 spill 隐去的事实分开统计；版本检查与用途不确定的调用保留不确定性。"
        "同历史投影字符减少只衡量序列化体积，不能替代实际 provider token 或费用。",
        f"按全部 24 次尝试统计，spill 触发 {sum(bool(r['spilled_results']) for r in results)}/24，"
        f"prune 触发 {sum(bool(r['governance_events']['prune']) for r in results)}/24，"
        f"compact 触发 {sum(bool(r['governance_events']['compaction']) for r in results)}/24。"
        "对应 B 组的机制覆盖见上表；本轮没有新增真实 prune 收益证据。",
        f"共 {breakdown['all_read_attempts']} 次 read 尝试："
        f"{breakdown['post_write_artifact_verifications']} 次写后核验、{breakdown['source_reads']} 次输入读取，"
        f"另有 {len(breakdown['pre_write_artifact_read_errors'])} 次写前检查结果文件失败。"
        f"输入读取中 {breakdown['source_reads_with_all_target_facts_already_visible']} 次发生在目标事实已经可见之后；"
        f"机械相同读参的重复次数仍为 {breakdown['exact_repeat_reads']}。这再次说明该指标不能代替信息重叠诊断。",
        "序号 16（C2 B 第二轮）在第一次写入前读 result.json，因文件尚不存在返回一次工具错误；"
        "随后写入并核验完成。该错误、额外请求和耗时均保留，没有重跑。",
        "",
        "## 逐次验收与异常",
        "",
        "| 序号 | 任务 | 轮次 | 组 | 事实 | 格式 | 交付通过 | 请求 | 输入 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        tokens = (
            r["actual_input"] if r["actual_input"] is not None else ("0（模型前拒绝）" if not r["calls"] else "unknown")
        )
        lines.append(
            f"| {r['sequence']} | {r['case_id']} | {r['repeat']} | {r['arm']} | "
            f"{r['facts_correct']}/{r['facts_required']} | {r['format_valid']} | {r['passed']} | "
            f"{r['calls']} | {tokens} |"
        )
    lines += [""]
    for r in results:
        if not r["passed"]:
            failed = ", ".join(c["id"] for c in r["checks"] if not c["ok"])
            lines.append(
                f"- 序号 {r['sequence']}：{r['case_id']} / {r['arm']} / 第 {r['repeat']} 次，"
                f"status={r['status']}，error_type={r.get('error_type', '未单列')}；未通过：{failed}。"
            )
    lines += [
        "",
        "## 证据与边界",
        "",
        "39 项离线检查通过；完整 24 次 FakeModel 流程中 18 次脚本交付通过、6 次预期本地超预算。"
        "脚本结果仅验证调度、材料、预算与评分，不能作为模型质量或费用数据。"
        "首次测试启动使用 pytest 可执行文件，因仓库根目录未进入导入路径而发生收集错误；"
        "改用 python -m pytest 后通过，初始日志保留。",
        "",
        "本轮没有更改题目来追求降幅，没有将失败重新跑到成功，也没有执行长期记忆/历史检索或正式留出集实验。"
        "三次重复及两个大材料任务不足以给出显著性、生产 SLA 或跨任务泛化结论。"
        "缓存、服务负载和采样波动仍可能影响三次间的差异；没有可用平台费率，不能宣称费用同比下降。",
        "",
        f"[原始运行目录]({RUN})、"
        f"[冻结参数与源码清单]({RUN}/freeze.json)、"
        f"[逐次指标]({HERE}/trials.csv)、"
        f"[读取审计]({RUN}/tool-audit.json)、"
        f"[离线验证]({HERE}/preflight.json)。",
    ]
    (HERE / "README.md").write_text("\n".join(lines) + "\n")
    print(
        json.dumps(
            {
                "trials": len(results),
                "calls": summary["started_calls"],
                "spill_comparable_pairs": summary["spill_comparable_pairs"],
                "spill_weighted_reduction": summary["spill_weighted_input_reduction"],
                "passes": sum(r["passed"] for r in results),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
