from __future__ import annotations

import html
import io
import re
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from syllabus_review.rules import Issue, ReviewResult


MAX_PDF_OPINION_CHARS = 760


def build_markdown_report(result: ReviewResult) -> str:
    compliance_issues = _report_issues(_compliance_issues(result))
    format_issues = _report_issues(_format_issues(result))
    lines = [
        f"# {result.file_name} 审查报告",
        "",
        "## 一、基本信息",
        f"- 专业：{result.program.name}",
        f"- 课程名称：{result.course_name or '未识别'}",
        f"- 课程代码：{result.course_code or '未识别'}",
        f"- 课程总学时数：{_clean_hours(result.course_total_hours) or '未识别'}",
        "",
        "## 二、合规性审查结果",
    ]
    if not compliance_issues:
        lines.append("未发现自动规则可识别的合规性问题。")
    else:
        for index, issue in enumerate(compliance_issues, start=1):
            lines.extend(_issue_lines(issue, index))
    lines.extend(["", "## 三、格式审查结果"])
    if not format_issues:
        lines.append("未发现自动规则可识别的格式问题。")
    else:
        for index, issue in enumerate(format_issues, start=1):
            lines.extend(_issue_lines(issue, index))
    lines.extend(["", "## 四、内容深度审查结果"])
    lines.append(result.ai_review.strip() if result.ai_review else "未启用内容深度审查。")
    return "\n".join(lines)


def build_pdf_report(result: ReviewResult) -> bytes:
    return _build_pdf([result])


def build_combined_pdf_report(results: list[ReviewResult], program_name: str) -> bytes:
    return _build_pdf(results)


def report_pdf_filename(result: ReviewResult) -> str:
    course_code = result.course_code or "未识别课程代码"
    course_name = result.course_name or Path(result.file_name).stem
    return _safe_filename(f"{result.program.name}_{course_code}_{course_name}.pdf")


def combined_report_pdf_filename(program_name: str) -> str:
    return _safe_filename(f"{program_name}_批量审查总报告.pdf")


def markdown_to_pdf(markdown: str) -> bytes:
    font_name = _register_chinese_font()
    styles = _pdf_styles(font_name)
    story = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            story.append(Spacer(1, 5))
            continue
        text = _markdown_line_to_text(line)
        style = styles["Heading1"] if line.startswith("# ") else styles["Heading2"] if line.startswith("## ") else styles["Body"]
        story.append(Paragraph(text, style))
    return _render_story(story)


def _issue_lines(issue: Issue, index: int) -> list[str]:
    return [
        "",
        f"### 问题{index}（{issue.priority}优先级）",
        f"- 位置：{issue.location}",
        f"- 问题：{issue.message}",
        f"- 建议：{issue.suggestion}",
    ]


def _build_pdf(results: list[ReviewResult]) -> bytes:
    font_name = _register_chinese_font()
    styles = _pdf_styles(font_name)
    story = [_review_table(results, styles)]
    return _render_story(story, font_name)


def _review_table(results: list[ReviewResult], styles: dict[str, ParagraphStyle]) -> Table:
    data = [
        [
            Paragraph("课程名称", styles["TableHeader"]),
            Paragraph("课程代码", styles["TableHeader"]),
            Paragraph("课程总学时数", styles["TableHeader"]),
            Paragraph("适用专业", styles["TableHeader"]),
            Paragraph("审查结果", styles["TableHeader"]),
        ]
    ]
    for result in results:
        data.extend(_review_rows(result, styles))

    return Table(
        data,
        colWidths=[42 * mm, 28 * mm, 23 * mm, 28 * mm, 152 * mm],
        repeatRows=1,
        style=[
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
            ("BOX", (0, 0), (-1, -1), 0.7, colors.black),
            ("INNERGRID", (0, 0), (-1, -1), 0.45, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ],
    )


def _review_rows(result: ReviewResult, styles: dict[str, ParagraphStyle]) -> list[list[Paragraph]]:
    chunks = _opinion_cell_chunks(result)
    return [_review_row(result, chunk, styles, continuation=index > 0) for index, chunk in enumerate(chunks)]


def _review_row(result: ReviewResult, opinion: str, styles: dict[str, ParagraphStyle], continuation: bool = False) -> list[Paragraph]:
    course_name = result.course_name or Path(result.file_name).stem
    if continuation:
        course_name = f"{course_name}（续）"
    return [
        Paragraph(_esc(course_name), styles["TableCell"]),
        Paragraph(_esc(result.course_code or "未识别"), styles["TableCell"]),
        Paragraph(_esc(_clean_hours(result.course_total_hours) or "未识别"), styles["TableCell"]),
        Paragraph(_esc(result.program.name), styles["TableCell"]),
        Paragraph(opinion, styles["OpinionCell"]),
    ]


def _opinion_cell_text(result: ReviewResult) -> str:
    return "<br/><br/>".join(_opinion_cell_chunks(result))


def _opinion_cell_chunks(result: ReviewResult) -> list[str]:
    units = _opinion_units(result)
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for unit in units:
        unit_length = _plain_length(unit)
        if current and current_length + unit_length > MAX_PDF_OPINION_CHARS:
            chunks.append("<br/>".join(current))
            current = [unit]
            current_length = unit_length
        else:
            current.append(unit)
            current_length += unit_length
    if current:
        chunks.append("<br/>".join(current))
    return chunks or ["未发现自动规则可识别的突出问题。"]


def _opinion_units(result: ReviewResult) -> list[str]:
    units: list[str] = []
    compliance_issues = _report_issues(_compliance_issues(result))
    format_issues = _report_issues(_format_issues(result))
    if compliance_issues:
        units.append("<b>合规性审查结果：</b>")
    for index, issue in enumerate(compliance_issues, start=1):
        units.extend(_issue_pdf_units(issue, index))
    if format_issues:
        units.append("<b>格式审查结果：</b>")
    for index, issue in enumerate(format_issues, start=1):
        units.extend(_issue_pdf_units(issue, index))
    if result.ai_review:
        units.extend(_plain_pdf_units("内容深度审查结果", result.ai_review.strip()))
    return units


def _issue_pdf_units(issue: Issue, index: int) -> list[str]:
    body = f"{issue.location}，{issue.message}建议：{issue.suggestion}"
    pieces = _split_pdf_text(body)
    if len(pieces) == 1:
        return [f"<b>问题{index}（{_esc(issue.priority)}）</b>：{_esc(pieces[0])}"]
    units = [f"<b>问题{index}（{_esc(issue.priority)}）</b>：{_esc(pieces[0])}"]
    for piece in pieces[1:]:
        units.append(f"<b>问题{index}（续）</b>：{_esc(piece)}")
    return units


def _plain_pdf_units(title: str, value: str) -> list[str]:
    pieces = _split_pdf_text(value)
    if not pieces:
        return []
    units = [f"<b>{_esc(title)}：</b>{_esc(pieces[0])}"]
    for piece in pieces[1:]:
        units.append(f"<b>{_esc(title)}（续）：</b>{_esc(piece)}")
    return units


def _split_pdf_text(value: str) -> list[str]:
    text = re.sub(r"\s+", " ", value or "").strip()
    if not text:
        return []
    if len(text) <= MAX_PDF_OPINION_CHARS:
        return [text]
    parts: list[str] = []
    current = ""
    segments = re.split(r"(?<=[。！？；;])", text)
    for segment in segments:
        if not segment:
            continue
        if len(segment) > MAX_PDF_OPINION_CHARS:
            if current:
                parts.append(current)
                current = ""
            parts.extend(segment[i : i + MAX_PDF_OPINION_CHARS] for i in range(0, len(segment), MAX_PDF_OPINION_CHARS))
            continue
        if current and len(current) + len(segment) > MAX_PDF_OPINION_CHARS:
            parts.append(current)
            current = segment
        else:
            current += segment
    if current:
        parts.append(current)
    return parts


def _plain_length(value: str) -> int:
    return len(re.sub(r"<[^>]+>", "", html.unescape(value or "")))


def _compliance_issues(result: ReviewResult) -> list[Issue]:
    return [issue for issue in result.compliance_issues if not _is_format_issue(issue)]


def _format_issues(result: ReviewResult) -> list[Issue]:
    return [issue for issue in result.compliance_issues if _is_format_issue(issue)]


def _is_format_issue(issue: Issue) -> bool:
    return issue.rule_id.startswith("R-FORMAT")


def _report_issues(issues: list[Issue]) -> list[Issue]:
    high_priority = [issue for issue in issues if issue.priority == "高"]
    mergeable = [issue for issue in issues if issue.priority != "高"]
    grouped: dict[str, list[Issue]] = {}
    for issue in mergeable:
        grouped.setdefault(_issue_group_key(issue), []).append(issue)
    report_items: list[Issue] = [
        Issue(
            issue.rule_id,
            issue.priority,
            issue.location,
            _short_issue_message(issue.message),
            issue.suggestion,
        )
        for issue in high_priority
    ]
    for group in grouped.values():
        first = group[0]
        priority = max((issue.priority for issue in group), key=_priority_rank)
        if len(group) == 1:
            report_items.append(
                Issue(
                    first.rule_id,
                    priority,
                    first.location,
                    _short_issue_message(first.message),
                    first.suggestion,
                )
            )
            continue
        report_items.append(
            Issue(
                first.rule_id,
                priority,
                first.location,
                f"同类问题共 {len(group)} 处。示例：{first.location}，{_short_issue_message(first.message)}请自行校对其他部分存在的相似问题。",
                first.suggestion,
            )
        )
    return report_items


def _issue_group_key(issue: Issue) -> str:
    if issue.rule_id.startswith("R-BASIC-CATALOG"):
        return "R-BASIC-CATALOG"
    if issue.rule_id.startswith("R-FORMAT"):
        return issue.rule_id
    return issue.rule_id


def _priority_rank(priority: str) -> int:
    return {"低": 1, "中": 2, "高": 3}.get(priority, 0)


def _short_issue_message(message: str) -> str:
    text = re.sub(r"\s+", " ", message or "").strip()
    text = _keep_first_list_example(text)
    if len(text) <= 180:
        return _ensure_sentence(text)
    cut = max(text.rfind("。", 0, 180), text.rfind("；", 0, 180), text.rfind("，", 0, 180))
    if cut >= 40:
        text = text[: cut + 1]
    else:
        text = text[:180].rstrip("，；、 ")
    return _ensure_sentence(text)


def _keep_first_list_example(text: str) -> str:
    for marker in ["：", ":"]:
        if marker not in text:
            continue
        prefix, rest = text.split(marker, 1)
        if "；" in rest:
            return f"{prefix}{marker}{rest.split('；', 1)[0]}。请自行校对其他部分存在的相似问题。"
    return text


def _ensure_sentence(text: str) -> str:
    return text if not text or text.endswith(("。", "！", "？")) else text + "。"


def _summary_text(result: ReviewResult) -> str:
    if not result.compliance_issues:
        return "经系统初审，未发现自动规则可识别的突出问题。"

    summaries: list[str] = []
    issues = result.compliance_issues
    if any(issue.rule_id == "R-ASSESS-007" for issue in issues):
        summaries.append("表4课程目标考核环节评定方式计算有误，需要严格按照公式进行计算。")
    if any(issue.rule_id in {"R-ASSESS-005", "R-ASSESS-006", "R-ASSESS-008"} for issue in issues):
        summaries.append("表4课程目标考核环节评定方式填写不规范，需要核对总评占比、合计行和各考核环节权重。")
    if any(issue.rule_id == "R-RESOURCE-001" for issue in issues):
        summaries.append("课程资源部分要素不完整，请严格按照模板中规定的课程资源部分要素要求撰写，不得有缺项。")
    if any(issue.rule_id.startswith("R-BASIC-CATALOG") for issue in issues):
        summaries.append("课程基本信息与本专业课程基本信息文档及人才培养方案不一致，需要逐项核对修正。")
    if any(issue.rule_id == "R-BASIC-007" for issue in issues):
        summaries.append("课程代码需要核查。")
    if any(issue.rule_id.startswith("R-GRAD") or issue.rule_id.startswith("R-PLAN") for issue in issues):
        summaries.append("课程目标与毕业要求支撑关系需要以本专业人才培养方案为准进行核对。")
    if any(issue.rule_id.startswith("R-TABLE") for issue in issues):
        summaries.append("核心表格名称、表头或撰写方式不符合模板要求，需要按新版模板规范填写。")
    if any(issue.rule_id == "R-HOUR-004" for issue in issues):
        summaries.append("教学内容学时安排表述不规范，建议用具体学时数体现，不宜使用“周”或“天”作为教学内容拆分单位。")
    if any(issue.rule_id == "R-HOUR-005" for issue in issues):
        summaries.append("表2教学模块学时安排过大，需要按较小教学单元进一步细化。")
    if any(issue.rule_id == "R-HOUR-006" for issue in issues):
        summaries.append("表2教学内容与支撑课程目标对应关系不够清晰，建议尽量一项教学内容对应一个课程目标。")
    if any(issue.rule_id in {"R-HOUR-001", "R-HOUR-003"} for issue in issues):
        summaries.append("教学内容与学时安排存在不规范项，需要按课程教学要求进一步完善。")
    if any(issue.rule_id.startswith("R-FORMAT") for issue in issues):
        summaries.append("文档清洁度或格式存在问题，提交前应删除批注、修订痕迹或模板残留。")
    if any(issue.rule_id.startswith("R-ASSESS") and issue.rule_id not in {"R-ASSESS-005", "R-ASSESS-006", "R-ASSESS-007", "R-ASSESS-008"} for issue in issues):
        summaries.append("课程考核方式或考核内容表述存在不规范项，需要按 OBE 理念和模板要求修正。")
    if any(issue.rule_id.startswith("R-PROGRAM") for issue in issues):
        summaries.append("系统未能自动识别适用专业，需要检查制定依据或手动指定专业后重新审查。")

    covered_prefixes = ("R-ASSESS", "R-RESOURCE", "R-BASIC", "R-GRAD", "R-PLAN", "R-TABLE", "R-HOUR", "R-FORMAT", "R-PROGRAM")
    if any(not issue.rule_id.startswith(covered_prefixes) for issue in issues):
        summaries.append("其余问题请结合明细逐项核对修正。")
    if not summaries:
        summaries.append("本课程存在若干合规性问题，请结合下方明细逐项核对修正。")
    return _join_summary(_dedupe(summaries))


def _join_summary(values: list[str]) -> str:
    cleaned = [re.sub(r"[。；;]+$", "", value.strip()) for value in values if value.strip()]
    return "；".join(cleaned) + "。"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _render_story(story: list, font_name: str | None = None) -> bytes:
    buffer = io.BytesIO()
    font_name = font_name or _register_chinese_font()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=24 * mm,
        bottomMargin=13 * mm,
    )
    doc.build(story, onFirstPage=lambda c, d: _draw_page_header(c, d, font_name), onLaterPages=lambda c, d: _draw_page_header(c, d, font_name))
    return buffer.getvalue()


def _draw_page_header(canvas, doc, font_name: str) -> None:
    canvas.saveState()
    canvas.setFont(font_name, 15)
    canvas.drawCentredString(doc.pagesize[0] / 2, doc.pagesize[1] - 14 * mm, "西京学院课程教学大纲专家审核意见")
    canvas.restoreState()


def _pdf_styles(font_name: str) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "CoverTitle": ParagraphStyle(
            "CoverTitle",
            parent=base["Title"],
            fontName=font_name,
            fontSize=22,
            leading=30,
            alignment=TA_CENTER,
            textColor=colors.white,
        ),
        "Title": ParagraphStyle("TitleCN", parent=base["Heading1"], fontName=font_name, fontSize=18, leading=24, spaceAfter=12, textColor=colors.HexColor("#0f172a")),
        "SectionTitle": ParagraphStyle("SectionTitleCN", parent=base["Heading2"], fontName=font_name, fontSize=13.5, leading=19, spaceBefore=8, spaceAfter=8, textColor=colors.HexColor("#1e3a8a")),
        "IssueTitle": ParagraphStyle("IssueTitleCN", parent=base["Heading3"], fontName=font_name, fontSize=12.5, leading=18, spaceAfter=0, textColor=colors.HexColor("#111827")),
        "Body": ParagraphStyle("BodyCN", parent=base["BodyText"], fontName=font_name, fontSize=10.5, leading=16, spaceAfter=4, textColor=colors.HexColor("#1f2937")),
        "MetaLabel": ParagraphStyle("MetaLabelCN", parent=base["BodyText"], fontName=font_name, fontSize=10, leading=14, textColor=colors.HexColor("#334155")),
        "MetaValue": ParagraphStyle("MetaValueCN", parent=base["BodyText"], fontName=font_name, fontSize=10, leading=14, textColor=colors.HexColor("#0f172a")),
        "TableHeader": ParagraphStyle("TableHeaderCN", parent=base["BodyText"], fontName=font_name, fontSize=10.5, leading=14, alignment=TA_CENTER, textColor=colors.black),
        "TableCell": ParagraphStyle("TableCellCN", parent=base["BodyText"], fontName=font_name, fontSize=9.5, leading=14, alignment=TA_CENTER, textColor=colors.black),
        "OpinionCell": ParagraphStyle("OpinionCellCN", parent=base["BodyText"], fontName=font_name, fontSize=8.0, leading=10.2, alignment=TA_LEFT, textColor=colors.black),
        "Heading1": ParagraphStyle("MarkdownH1CN", parent=base["Heading1"], fontName=font_name, fontSize=17, leading=23, spaceAfter=10),
        "Heading2": ParagraphStyle("MarkdownH2CN", parent=base["Heading2"], fontName=font_name, fontSize=13, leading=18, spaceAfter=7),
    }


def _register_chinese_font() -> str:
    candidates = [
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont("ReportCN", path, subfontIndex=0))
                return "ReportCN"
            except Exception:
                pass
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


def _markdown_line_to_text(line: str) -> str:
    line = re.sub(r"^#{1,6}\s*", "", line)
    line = re.sub(r"^\-\s*", "• ", line)
    line = re.sub(r"\*\*(.*?)\*\*", r"\1", line)
    return _esc(line)


def _safe_filename(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]+", "_", value)
    value = re.sub(r"\s+", "", value)
    return value[:180]


def _clean_hours(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^总学时\s*[:：]?\s*", "", text)
    return text.strip()


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=False).replace("\n", "<br/>")
