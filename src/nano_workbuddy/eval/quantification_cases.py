"""Six public development tasks; their gold answers stay in the parent process."""
from __future__ import annotations

import re

EXPERIMENT_VERSION = "development-v2-tools-format"


def output_contract(case_id: str) -> dict:
    fields = {
        "CTX-SPILL": ("supplier", "budget", "deadline"),
        "CTX-COMPACT": ("owner", "total", "protected_file"),
        "MEM-FRESH": ("locale", "project_rule"),
        "MEM-REUSE": ("locale", "format"),
        "HIST-LOCATE": ("deadline", "budget", "evidence"),
        "HIST-VERSION": ("deadline", "budget", "evidence"),
    }[case_id]
    properties = {name: ({"type": "integer", "minimum": 0} if name in ("budget", "total") else
                         {"type": "string", "minLength": 1}) for name in fields}
    if "deadline" in properties:
        properties["deadline"]["pattern"] = r"\d{4}-\d{2}-\d{2}"
    if "evidence" in properties:
        properties["evidence"]["pattern"] = r"(?:rec_[0-9a-f]{24}|previous-notes\.txt)"
    return {"type": "object", "properties": properties, "required": list(fields), "additionalProperties": False}


def contract_instructions(contract: dict) -> str:
    fields = "；".join(f"{key}={'非负整数（不写小数）' if spec['type'] == 'integer' else '非空字符串'}"
                      for key, spec in contract["properties"].items())
    extra = ("deadline 使用 YYYY-MM-DD。" if "deadline" in contract["properties"] else "")
    if "evidence" in contract["properties"]:
        extra += ("evidence 必须是单个字符串：填实际支持答案的一个 rec_ 加24位小写十六进制字符的历史记录ID，"
                  "或原始文件相对路径 previous-notes.txt；不能填数组、对象、来源编号或解释文字。"
                  "来源依据可以来自 grep/read 返回的原文，或 recall/read_record 返回的完整相关片段；"
                  "已经足以支持答案时，不要求为了验证而额外读取另一条路径。")
    return ("\n\n输出格式约定：将一个合法 JSON 对象保存到 result.json，且只能包含以下字段：" + fields + "。"
            "所有字段必填，禁止添加 project、note、说明或其它键；文件中不写 Markdown 代码围栏或 JSON 之外的文字。"
            + extra)


def contract_errors(actual: object, contract: dict) -> list[str]:
    """Validate the small explicit contract above; never coerce or repair a submitted answer."""
    if not isinstance(actual, dict):
        return ["$: expected object"]
    errors = [f"{key}: missing" for key in contract["required"] if key not in actual]
    errors.extend(f"{key}: unexpected field" for key in actual if key not in contract["properties"])
    for key, spec in contract["properties"].items():
        if key not in actual:
            continue
        value = actual[key]
        if spec["type"] == "integer":
            if type(value) is not int:
                errors.append(f"{key}: expected integer")
            elif value < spec["minimum"]:
                errors.append(f"{key}: below minimum")
        elif not isinstance(value, str):
            errors.append(f"{key}: expected string")
        elif not value.strip():
            errors.append(f"{key}: empty string")
        elif "pattern" in spec and re.fullmatch(spec["pattern"], value) is None:
            errors.append(f"{key}: pattern mismatch")
    return errors


def pilot_cases() -> list[dict]:
    lines = [f"附录{i:03}：" + "设备清单与例行维护记录，仅供背景参考。" * 20 for i in range(180)]
    lines[0] = "正式决定：supplier=北杉；其余附录只是背景。"
    lines[90] = "正式决定：budget=72800；这个金额替代早期草案。"
    lines[-1] = "正式决定：deadline=2026-11-18。"
    cases = [
        dict(id="CTX-SPILL", track="context", mechanism="spill", window=100_000,
             inputs={"material.txt": "\n".join(lines)},
             task="读取 material.txt，将其中正式决定的 supplier、budget（整数）、deadline 写入 result.json。"
                  "三个字段缺一不可。文件是合成试验材料，不需联网。",
             gold={"supplier": "北杉", "budget": 72800, "deadline": "2026-11-18"}),
        dict(id="CTX-COMPACT", track="context", mechanism="compact", window=32_768,
             inputs={"input.json": '{"quantity": 4, "unit_price": 1800}'},
             task="继续此前任务。读取 input.json，按历史中已确定的负责人和禁止修改项，"
                  "把 owner、total（整数）、protected_file 写入 result.json。不要修改输入文件。",
             gold={"owner": "林杉", "total": 7200, "protected_file": "input.json"}),
        dict(id="MEM-FRESH", track="memory_freshness", mechanism="refresh", window=100_000,
             inputs={}, task="按照当前保存的 preferred_locale 和 project_rule，"
                  "写 result.json，仅包含 locale 和 project_rule 两个字段。不要修改保存的记忆或规则。",
             gold={"locale": "ja-JP", "project_rule": "RULE_V2"}),
        dict(id="MEM-REUSE", track="memory_reuse", mechanism="injection", window=100_000,
             inputs={"original-notes.txt": "此前约定：preferred_locale=ja-JP；output_format=bullet_list。\n" +
                     "历史备忘：按需检索资料并保留来源。\n" * 300},
             task="按此前约定的 preferred_locale 和 output_format 写 result.json，"
                  "仅包含 locale 和 format 两个字段。需要时可查 original-notes.txt。",
             gold={"locale": "ja-JP", "format": "bullet_list"}),
        dict(id="HIST-LOCATE", track="history", mechanism="recall", window=100_000,
             inputs={"previous-notes.txt": "北杉迁移项目最终确认：交付日期2026-11-18，预算72000元。\n"
                     "这是已确认决定。"},
             history="北杉迁移项目最终确认：交付日期2026-11-18，预算72000元。",
             task="找出以前确定的北杉迁移项目交付日期和预算，写入 result.json："
                  "deadline、budget（整数）、evidence（所用历史 record ID 或原始文件相对路径）。"
                  "原始材料 previous-notes.txt 可读。",
             gold={"deadline": "2026-11-18", "budget": 72000}),
        dict(id="HIST-VERSION", track="history", mechanism="recall", window=100_000,
             inputs={"previous-notes.txt": "南桥交付项目草案：交付日期2026-11-04，预算61000元。\n"
                     "南桥交付项目最终确认：交付日期2026-11-18，预算72000元；此条替代草案。"},
             history="南桥交付项目最终确认：交付日期2026-11-18，预算72000元；此条替代草案。",
             task="根据以前最终确认的南桥交付项目决定，写 result.json：deadline、budget（整数）、"
                  "evidence（所用历史 record ID 或原始文件相对路径）。区分草案和其它项目同名记录。"
                  "原始材料 previous-notes.txt 可读。",
             gold={"deadline": "2026-11-18", "budget": 72000}),
    ]
    for case in cases:
        case["dataset_version"] = EXPERIMENT_VERSION
        case["output_contract"] = output_contract(case["id"])
        case["task"] += contract_instructions(case["output_contract"])
    return cases


def compact_history() -> list[tuple[str, str]]:
    # Closed synthetic turns force real automatic compaction; no model-generated seed is billed or implied.
    out = [("user", "已确定：owner=林杉；protected_file=input.json，不得修改；总价=quantity*unit_price。"),
           ("assistant", "收到，按这些约束继续。")]
    for i in range(10):
        out.extend([("user", f"整理第{i}批背景，保留原有决定。"),
                    ("assistant", "背景整理记录：" + "这是无关紧要的例行盘点信息，不能替代任务约束。" * 110)])
    return out
