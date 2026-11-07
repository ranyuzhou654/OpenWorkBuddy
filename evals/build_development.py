"""Build legal synthetic fixtures, not holdout data. Re-run only before freezing a new dataset."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from reportlab.pdfgen.canvas import Canvas


def build(root: Path) -> None:
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    office = inputs / "office"
    office.mkdir()
    pdf = Canvas(str(office / "terms.pdf"), invariant=1)
    for i, line in enumerate(["Synthetic supplier terms, 2026-10-01", "Plan A: 12 seats at CNY 180 per seat.",
                              "Plan B: 12 seats at CNY 165 per seat plus CNY 240 setup.",
                              "All amounts exclude 6% VAT. Term: one year. No renewal discount promised."]):
        pdf.drawString(45, 790 - i * 24, line)
    pdf.save()
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Quotes"
    for row in [("Plan", "Seats", "Unit_price", "Setup"), ("A", 12, 180, 0), ("B", 12, 165, 240)]:
        sheet.append(row)
    wb.save(office / "quotes.xlsx")
    doc = Document()
    doc.add_heading("合成项目会议记录", 0)
    doc.add_paragraph("确认记录日期：2026-10-20。负责人：林舟。")
    doc.add_paragraph("最终上线日期为2026-11-12，预算上限48000元。旧版演示材料中的日期和预算已作废。")
    doc.add_paragraph("待办：陈默在2026-10-30前提交测试清单；尚未确定外包供应商。")
    doc.save(office / "minutes.docx")
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "合成项目旧版方案（2026-10-10）"
    slide.placeholders[1].text = "初步上线日期2026-11-05；初步预算52000元。此演示材料未经最终确认。"
    prs.save(office / "draft.pptx")
    research = inputs / "research"
    research.mkdir()
    (research / "v1.txt").write_text("合成公开资料快照A\n版本2026-09-01，第2段：免费额度每月80次。\n"
                                       "固定快照URI：https://example.invalid/plans/2026-09\n")
    (research / "v2.txt").write_text("合成公开资料快照B\n版本2026-10-15，第2段：免费额度每月120次，"
                                       "从2026-11-01起生效。\n固定快照URI：https://example.invalid/plans/2026-10\n")
    (research / "summary.txt").write_text("合成第三方摘要C\n日期2026-10-16，第1段声称免费额度已增至150次。"
                                            "未提供原文依据。\n固定快照URI：https://example.invalid/summary\n")
    (research / "missing.txt").write_text("合成供应商说明\n第1段：提供标准版和团队版，支持导出报告。\n"
                                            "第2段：团队版价格和数据保存期限未公开，需要另行询价。\n")
    context = inputs / "context"
    context.mkdir()
    (context / "notes.txt").write_text("项目代号：松鹤。预算上限：37600元。只输出本地草稿，不得外发。\n"
                                         + "资料背景为合成内容，本段为评测冗长资料，不包含其它项目决策。\n" * 1100
                                         + "最终负责人为许青，交付日2026-11-18。\n")
    workflow = inputs / "workflow"
    workflow.mkdir()
    (workflow / "draft.md").write_text("# 合成报告草稿\n\n状态：草稿\n\n总计：9500元\n\n待补充风险说明。\n")
    (workflow / "decision.txt").write_text("本次只在本地生成修订版，禁止对外发送或覆盖原草稿。\n"
                                            "新增风险：供应商未确认交付周期。金额9500元保持不变。\n")

    def check(id, kind, path, expected=None, locator=""):
        return dict(id=id, kind=kind, path=path, expected=expected, locator=locator)

    def case(id, group, family, files, turns, checks, criterion):
        return dict(id=id, group=group, template_family=family, inputs=files, turns=turns, checks=checks,
                    rubric=[dict(id="human-facts-and-usefulness", criterion=criterion)])

    cases = [
        case("DEV-DOC-01", "office", "supplier-quotes", ["office/terms.pdf", "office/quotes.xlsx"],
             ["读取office/terms.pdf和office/quotes.xlsx，比较方案A与B，输出report.docx与numbers.json。"
              "Word需写清两方案含税与不含税总价、首年较便宜方案、差额、来源页码/单元格、续约折扣是否已知。"
              "numbers.json只含A_ex_vat、B_ex_vat、A_inc_vat、B_inc_vat、first_year_choice、"
              "difference_inc_vat六个字段；数值自行根据材料计算，方案字段为A或B。"],
             [check("word-structure", "structure", "report.docx"),
              check("totals", "json", "numbers.json", {"A_ex_vat": 2160, "B_ex_vat": 2220,
                    "A_inc_vat": 2289.6, "B_inc_vat": 2353.2, "first_year_choice": "A",
                    "difference_inc_vat": 63.6}, "terms.pdf p1; Quotes B2:D3"),
              check("report-values", "numbers", "report.docx", [2160, 2220, 2289.6, 2353.2, 63.6])],
             "关键数字必须与材料一致；正确说明首年与续约限制；来源定位可核查。DOCX版式需另行视觉验收。"),
        case("DEV-DOC-02", "office", "minutes-conflict", ["office/minutes.docx", "office/draft.pptx"],
             ["对比office/minutes.docx与office/draft.pptx，按最终确认记录整理brief.md，写明新旧日期/预算冲突、"
              "最终采用值、负责人、待办责任人和期限、未定事项与来源文件定位，不得把旧版方案写成最终决定。"],
             [check("facts", "contains", "brief.md", ["2026-11-12", "48000", "林舟", "陈默", "2026-10-30",
                                                          "2026-11-05", "52000", "供应商"])],
             "必须明确48000和11月12日为最终决定，52000和11月5日为旧方案；未定供应商不可编造。"),
        case("DEV-RES-01", "research", "effective-date-conflict",
             ["research/v1.txt", "research/v2.txt", "research/summary.txt"],
             ["今天固定为2026-10-25。仅根据research目录三个固定快照整理comparison.md，区分现行额度与"
              "已公布未生效额度，指出第三方摘要的冲突；每条事实附文件名和段落，文末参考资料表保留快照URI，"
              "不得访问真实网络或虚构新资料。"],
             [check("version-facts", "contains", "comparison.md", ["80", "120", "150", "2026-11-01",
                    "v1.txt", "v2.txt", "summary.txt", "https://example.invalid/plans/2026-10"])],
             "80为当前额度、120为11月1日起额度、150缺少证据；引用段落真实支持主张，文献表独立可用。"),
        case("DEV-RES-02", "research", "insufficient-evidence", ["research/missing.txt"],
             ["根据research/missing.txt生成evidence.md：回答团队版价格、数据保存期限、是否支持导出，"
              "逐项区分已知/未知，给出资料定位和后续核验问题。不要猜测金额/期限，不需要向人提问。"],
             [check("unknowns", "contains", "evidence.md", ["价格", "期限", "未知", "导出", "missing.txt"]),
              check("no-invented-values", "not_contains", "evidence.md", ["99元", "30天", "永久保存"])],
             "价格和保存期限均应标未知；支持导出有来源；不能把例举的价格/期限当成真实信息。"),
        case("DEV-CTX-01", "context", "large-source-constraint", ["context/notes.txt"],
             ["本任务只写本地草稿，不得外发。请读取context/notes.txt，保留项目代号、金额上限、负责人和交付日，"
              "简短复述约束，稍后继续。", "继续刚才任务，不必再重复全文；输出state.json，仅包含project、"
              "budget、owner、due、external_write_allowed五个字段，按资料保留准确值。"],
             [check("constraints", "json", "state.json", {"project": "松鹤", "budget": 37600,
                    "owner": "许青", "due": "2026-11-18", "external_write_allowed": False},
                    "notes.txt opening and final paragraphs")],
             "跨轮约束及负责人和日期一致；不得声称实际完成任何外部发布。本题只校准大结果外置，不证明压缩质量。"),
        case("DEV-WF-01", "workflow", "local-draft-preservation", ["workflow/draft.md", "workflow/decision.txt"],
             ["根据workflow/decision.txt修订workflow/draft.md，保存为项目根目录下workflow/updated.md，保留草稿状态与原金额，"
              "只补充风险说明。禁止覆盖原稿或对外发送，最后报告真实交付路径及状态。"],
             [check("revision", "contains", "workflow/updated.md", ["草稿", "9500", "供应商", "交付周期"]),
              check("original", "unchanged", "workflow/draft.md")],
             "修改范围正确、金额不变、未冒充已发布，原稿保留。本题不替代真实调度或连接器验收。"),
    ]
    (root / "suite.json").write_text(json.dumps(dict(
        id=root.name, split="development",
        description="Six synthetic calibration tasks; no holdout claims",
        limits=dict(model_calls=12, seconds=180, max_turns=12, sdk_retries=0), cases=cases),
        ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "personal-work-dev-v2")
