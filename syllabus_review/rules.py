from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd
from openai import OpenAI

from syllabus_review.config import ProgramConfig
from syllabus_review.parser import ParsedDocument
from syllabus_review.training_plan import TrainingPlan, find_plan_course


Priority = Literal["高", "中", "低"]


@dataclass(frozen=True)
class Issue:
    rule_id: str
    priority: Priority
    location: str
    message: str
    suggestion: str


@dataclass(frozen=True)
class ReviewContext:
    program: ProgramConfig
    course_catalog: pd.DataFrame
    parsed: ParsedDocument
    training_plan: TrainingPlan | None = None
    training_plans: dict[str, TrainingPlan] = field(default_factory=dict)
    program_names: dict[str, str] = field(default_factory=dict)
    platform_related_programs: tuple[str, ...] = ()
    enable_ai_review: bool = False


@dataclass
class ReviewResult:
    file_name: str
    program: ProgramConfig
    parser_name: str
    course_name: str = ""
    course_code: str = ""
    course_total_hours: str = ""
    compliance_issues: list[Issue] = field(default_factory=list)
    ai_review: str = ""


COURSE_CATEGORIES = {"通识教育课程", "公共基础课程", "专业教育课程", "个性化课程", "实践教育课程"}
COURSE_NATURES = {"必修", "选修"}
TERMS = {f"第{i}学期" for i in range(1, 9)}
HOURS_PER_PRACTICE_WEEK = 30
REQUIREMENT_NAME_TO_ID = {
    "思想品德": "毕业要求1",
    "思想道德": "毕业要求1",
    "学科知识": "毕业要求2",
    "应用能力": "毕业要求3",
    "创新能力": "毕业要求4",
    "信息能力": "毕业要求5",
    "沟通表达": "毕业要求6",
    "团队合作": "毕业要求7",
    "国际视野": "毕业要求8",
    "学习发展": "毕业要求9",
}
AI_REVIEW_SYSTEM_PROMPT = """你是高校教学大纲语义质检专家。请只做内容深度审查，不审查格式、页边距、学时计算、表格边框等可由程序规则判断的问题。

请重点关注以下六类问题：
1. 是否存在明显错别字、病句、语句不通顺、表达重复；
2. 课程目标是否可评价，是否过多使用“了解、熟悉、掌握”等笼统表述而缺少可观察学习产出；
3. 课程目标、教学内容、考核方式之间是否存在明显脱节；
4. 课程思政、劳育、美育等融入是否生硬、空泛或与课程内容脱节；
5. 实践类课程是否明确实践任务、过程、成果物和评价证据；
6. 章节标题、模块标题、考核内容标题是否过于笼统、不规范或逻辑不清。

输出要求：
- 只输出一段中文意见，不要分点，不要表格，不要 Markdown 标题；
- 控制在 120-220 字左右；
- 只指出明显、重要、可复核的问题，不要泛泛而谈，不要编造文档中没有的信息；
- 如果没有发现明显突出问题，请输出：内容深度审查未发现明显突出问题，建议人工复核时继续关注课程目标、教学内容与考核证据的一致性。"""


def run_compliance_review(context: ReviewContext) -> ReviewResult:
    result = ReviewResult(
        file_name=context.parsed.file_name,
        program=context.program,
        parser_name=context.parsed.parser_name,
    )
    text = _normalize(context.parsed.text)
    result.course_name = _find_field(text, ["课程名称"]) or ""
    result.course_code = _find_field(text, ["课程代码", "课程编号"]) or ""
    result.course_total_hours = _find_field(text, ["总学时"]) or ""

    _check_syllabus_title(text, result)
    _check_core_tables(text, result)
    _check_basic_info(text, context, result)
    _check_assessment(text, context, result)
    _check_graduation_support(text, context, result)
    _check_teaching_hours(text, context, result)
    _check_course_resources(text, result)
    _check_document_cleanliness(text, context.parsed, result)

    if context.enable_ai_review:
        try:
            result.ai_review = _run_ai_review(text)
        except Exception as exc:
            result.ai_review = f"内容深度审查暂不可用：{exc}"
    return result


def _normalize(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text or "")


def _add(result: ReviewResult, rule_id: str, priority: Priority, location: str, message: str, suggestion: str) -> None:
    result.compliance_issues.append(Issue(rule_id, priority, location, message, suggestion))


def _check_syllabus_title(text: str, result: ReviewResult) -> None:
    title = _find_syllabus_title_line(text)
    if title and _is_standard_syllabus_title(title):
        return
    if title:
        message = f"大纲标题“{title}”不符合模板要求。"
    else:
        message = "未识别到符合模板要求的大纲标题。"
    _add(
        result,
        "R-FORMAT-113",
        "高",
        "大纲标题",
        message,
        "大纲标题应严格写为“西京学院《课程名称》本科课程教学大纲（2026版）”。",
    )


def _find_syllabus_title_line(text: str) -> str:
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("|").strip()
        line = re.sub(r"^#+\s*", "", line).strip("* ")
        if not line or set(line) <= {"-", ":", "：", " "}:
            continue
        if "课程基本信息" in line or re.match(r"^一、", line):
            break
        lines.append(line)
        if len(lines) >= 20:
            break
    for line in lines:
        if "西京学院" in line and "教学大纲" in line:
            return line
    for line in lines:
        if "教学大纲" in line:
            return line
    return ""


def _is_standard_syllabus_title(value: str) -> bool:
    compact = re.sub(r"[*#]", "", re.sub(r"\s+", "", value or ""))
    return bool(re.fullmatch(r"西京学院《[^《》]+》本科课程教学大纲（2026版）", compact))


def _find_field(text: str, names: list[str]) -> str | None:
    table_value = _find_field_in_tables(text, names)
    if table_value:
        return table_value
    for name in names:
        match = re.search(rf"{name}\s*[:：]?\s*([^\n\r|，,。;；]+)", text)
        if match:
            return match.group(1).strip()
    return None


def _number(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r"\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def _is_blank(value: str | None) -> bool:
    return value is None or not re.sub(r"\s+", "", str(value))


def _parse_formulated_year_month(value: str | None) -> tuple[int, int] | None:
    if _is_blank(value):
        return None
    text = str(value).strip()
    normalized = re.sub(r"\s+", "", text)
    match = re.search(r"(20\d{2})\s*(?:年|[./\-])\s*(\d{1,2})\s*(?:月)?", normalized)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2))
    if month < 1 or month > 12:
        return None
    return year, month


def _hour_number(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value)
    hour_match = re.search(r"(\d+(?:\.\d+)?)\s*学时", text)
    if hour_match:
        return float(hour_match.group(1))
    week_match = re.search(r"(\d+(?:\.\d+)?)\s*周", text)
    if week_match:
        return float(week_match.group(1)) * HOURS_PER_PRACTICE_WEEK
    return _number(value)


def _contains_week_unit(value: str | None) -> bool:
    if not value:
        return False
    text = str(value)
    return bool(re.search(r"\d+(?:\.\d+)?\s*周", text)) and not bool(re.search(r"\d+(?:\.\d+)?\s*学时", text))


def _find_hour_per_week_fields(fields: dict[str, str | None]) -> list[str]:
    values: list[str] = []
    for label, value in fields.items():
        text = _safe_cell_text(value)
        if re.search(r"学时\s*/\s*周", text):
            values.append(f"{label}“{text}”")
    return values


def _check_core_tables(text: str, result: ReviewResult) -> None:
    required = [
        ("R-TABLE-001", ["课程基本信息"], ["课程名称", "课程代码"]),
        ("R-TABLE-002", ["课程目标与毕业要求"], ["课程目标", "支撑的毕业要求", "支撑强度"]),
        ("R-TABLE-003", ["教学内容与课程目标"], ["教学内容", "支撑的课程目标"]),
        ("R-TABLE-004", ["课程目标与考核内容", "课程目标与课程考核内容"], ["考核内容", "考核环节"]),
        ("R-TABLE-005", ["课程目标考核环节评定方式"], ["课程目标", "设计权重"]),
        ("R-TABLE-006", ["课程目标评分标准", "课程目标1的评分标准", "课程目标1评分标准"], ["评分标准", "优秀（90"]),
    ]
    for rule_id, titles, body_keywords in required:
        if rule_id == "R-TABLE-005" and _has_renamed_assessment_weight_title(text):
            _add(result, rule_id, "高", "表4 课程目标考核环节评定方式", "表4名称被修改为“课程目标达成度评价方式及课程成绩评定方式”，与模板要求的“课程目标考核环节评定方式”不一致。", "请保留模板中的规范表名，并按模板填写各考核环节、设计权重和合计行。")
            continue
        if rule_id == "R-TABLE-006":
            if _has_course_target_score_standard(text):
                continue
            score_standard_problem = _score_standard_problem(text)
            if score_standard_problem:
                _add(result, rule_id, "高", "课程目标评分标准", score_standard_problem, "请按照新模板要求，分别撰写课程目标1、课程目标2等对应的评分标准。")
                continue
        status = _core_table_status(text, titles, body_keywords)
        title = titles[0]
        if status == "missing_title":
            suggestion = "请保留模板中的完整表名和表头，不要删除表格标题。"
            if rule_id == "R-TABLE-006":
                suggestion = "请分课程目标分别撰写考核方式的评分标准，表格命名为“课程目标X的评分标准”。请保留模板中的完整表名和表头，不要删除表格标题。"
            _add(result, rule_id, "高", "全文", f"识别到“{title}”相关表格内容，但缺少规范表名或表头。", suggestion)
        elif status == "missing":
            _add(result, rule_id, "高", "全文", f"未识别到“{title}”相关核心表格。", "请补充该表格，或检查表格标题是否与模板一致。")


def _has_renamed_assessment_weight_title(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return "课程目标考核环节评定方式" not in compact and "课程目标达成度评价方式及课程成绩评定方式" in compact


def _has_generic_score_standard(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return "课程成绩评分标准" in compact or "考核形式|评分标准" in compact


def _score_standard_problem(text: str) -> str:
    assessment_items = _assessment_item_score_standard_names(text)
    if assessment_items:
        return f"评分标准未按课程目标分别撰写：当前按“{'、'.join(assessment_items[:5])}”等考核环节撰写，未识别到“课程目标1的评分标准”等分课程目标评分标准。"
    if _has_generic_score_standard(text):
        return "评分标准未按课程目标分别撰写：当前仅识别到总的课程成绩评分标准，未识别到“课程目标1的评分标准”等分课程目标评分标准。"
    return ""


def _assessment_item_score_standard_names(text: str) -> list[str]:
    names: list[str] = []
    compact = re.sub(r"\s+", "", text)
    for name in ["课堂表现", "模块作业", "各模块实训作业", "综合实训报告", "综合报告", "期末考试", "期末考核"]:
        if f"{name}评分标准" in compact or re.search(rf"{re.escape(name)}\|[^|\n]*优秀", text):
            names.append(name)
    if not names:
        for row in [line for line in text.splitlines() if "|" in line]:
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            row_text = "".join(cells)
            if "考核环节" in row_text and "优秀" in row_text and "合格" in row_text:
                for next_row in text.splitlines()[text.splitlines().index(row) + 1 : text.splitlines().index(row) + 5]:
                    next_cells = [cell.strip() for cell in next_row.strip().strip("|").split("|")]
                    if next_cells and next_cells[0] and "课程目标" not in next_cells[0]:
                        names.append(next_cells[0])
                break
    return list(dict.fromkeys(names))


def _has_course_target_score_standard(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if re.search(r"课程目标\d+的?评分标准", compact):
        return True
    for line in text.splitlines():
        if "课程目标" in line and "评分标准" in line:
            return True
    return False


def _check_basic_info(text: str, context: ReviewContext, result: ReviewResult) -> None:
    name = _find_field(text, ["课程名称"])
    code = _find_field(text, ["课程代码", "课程编号"])
    category = _find_field(text, ["课程类别", "课程分类"])
    nature = _find_field(text, ["课程性质"])
    term = _normalize_term_value(_find_field(text, ["开课学期"]))
    formulated_time = _find_field(text, ["制定时间"])
    credit = _number(_find_field(text, ["学分"]))
    total_hours_text = _find_field(text, ["总学时"])
    theory_hours_text = _find_field(text, ["理论学时"])
    practice_hours_text = _find_field(text, ["实践学时", "实验学时"])
    total_hours = _hour_number(total_hours_text)
    theory_hours = _hour_number(theory_hours_text)
    practice_hours = _hour_number(practice_hours_text)
    preferred_catalog_values = {
        "课程名称": name,
        "课程类别": category,
        "课程性质": nature,
        "开课学期": term,
        "学分": credit,
        "总学时": total_hours_text,
        "理论学时": theory_hours_text,
        "实践学时": practice_hours_text,
    }
    catalog_row = _find_catalog_row(context.course_catalog, code, name, preferred_catalog_values)
    has_catalog_standards = catalog_row is not None
    expected_category = _safe_cell_text(catalog_row.get("课程类别", "")) if catalog_row is not None else ""
    expected_nature = _safe_cell_text(catalog_row.get("课程性质", "")) if catalog_row is not None else ""
    prerequisite_book_titles = _find_prerequisite_book_title_courses(text)

    if category and _canonical_compare_text(category) not in COURSE_CATEGORIES and not expected_category:
        _add(result, "R-BASIC-001", "高", "课程基本信息表", f"课程类别“{category}”不在允许范围内。", "课程类别应为五类之一：通识教育课程、公共基础课程、专业教育课程、个性化课程、实践教育课程。")
    if nature and _canonical_compare_text(nature) not in COURSE_NATURES and not expected_nature:
        _add(result, "R-BASIC-002", "高", "课程基本信息表", f"课程性质“{nature}”不在允许范围内。", "课程性质只能填写“必修”或“选修”。")
    if prerequisite_book_titles:
        _add(
            result,
            "R-BASIC-008",
            "中",
            "课程基本信息表",
            "先修课程、后修课程中的课程名称不应添加书名号：" + "；".join(prerequisite_book_titles[:6]) + "。",
            "请删除先修课程、后修课程名称外的《》书名号，直接填写课程名称。",
        )
    if term and term not in TERMS:
        _add(result, "R-BASIC-003", "中", "课程基本信息表", f"开课学期“{term}”不符合规范。", "请使用“第1学期”至“第8学期”的标准写法。")
    formulated_date = _parse_formulated_year_month(formulated_time)
    if _is_blank(formulated_time):
        _add(result, "R-BASIC-010", "高", "课程基本信息表", "制定时间未填写。", "请填写 2026年4月及之后的制定时间，例如“2026年4月”或“2026.4”。")
    elif formulated_date is None:
        _add(result, "R-BASIC-010", "高", "课程基本信息表", f"制定时间“{formulated_time}”格式不规范。", "请填写到年月，且时间不得早于 2026年4月，例如“2026年4月”或“2026.4”。")
    elif formulated_date < (2026, 4):
        _add(result, "R-BASIC-010", "高", "课程基本信息表", f"制定时间“{formulated_time}”早于学校启动 2026 版教学大纲制定工作的时间。", "制定时间应为 2026年4月及之后，不得填写 2025.9 等早于 2026年4月的时间。")
    hour_unit_problems = _find_hour_per_week_fields(
        {
            "总学时": total_hours_text,
            "理论学时": theory_hours_text,
            "实践学时": practice_hours_text,
        }
    )
    if hour_unit_problems:
        _add(
            result,
            "R-BASIC-009",
            "高",
            "课程基本信息表",
            "学时字段不应写成“学时/周”：" + "；".join(hour_unit_problems) + "。",
            "请删除“/周”，直接填写总学时、理论学时、实践学时，例如“48学时”。",
        )
    if credit and total_hours and not has_catalog_standards and not _contains_week_unit(total_hours_text) and abs(total_hours - credit * 16) > 0.01:
        _add(result, "R-BASIC-004", "高", "课程基本信息表", f"学分与总学时不匹配：{credit} 学分对应应为 {credit * 16:g} 学时，文档为 {total_hours:g}。", "除集中实践课程外，请按 1 学分=16 学时修正。")
    if total_hours is not None and theory_hours is not None and practice_hours is not None and abs(total_hours - theory_hours - practice_hours) > 0.01:
        _add(result, "R-BASIC-005", "高", "课程基本信息表", "总学时不等于理论学时与实践学时之和。", "请核对总学时、理论学时、实践学时三项。")

    plan, plan_course = _find_plan_course_for_context(context, code, name)
    plan_candidates = _available_training_plans(context)
    if context.training_plan and not context.training_plan.available and not plan_candidates:
        _add(result, "R-PLAN-001", "低", "人才培养方案", "未找到该专业的 training_plan.pdf，跳过人才培养方案一致性审查。", "请将专业人才培养方案 PDF 放在 data/programs/<专业代码>/training_plan.pdf。")
    elif plan_candidates and not plan_course:
        _add(result, "R-PLAN-002", "中", "人才培养方案", "未能在人才培养方案中匹配到该课程。", "请按照本专业课程基本信息文档及人才培养方案修正文档。")
    elif plan_course and term and plan_course.term and term != plan_course.term:
        _add(result, "R-PLAN-003", "高", "开课学期", f"开课学期与人才培养方案不一致：大纲为“{term}”，培养方案为“{plan_course.term}”。", "请按照本专业课程基本信息文档及人才培养方案修正文档。")

    if context.course_catalog.empty:
        _add(result, "R-DATA-001", "低", "课程总表", "未找到全院课程总表，跳过课程代码和课程基本信息精确比对。", "请将全院课程总表放在 data/course_codes.xlsx。")
        return

    if code:
        if catalog_row is None:
            _add(result, "R-BASIC-007", "高", "课程基本信息表", f"课程代码“{code}”未出现在课程编码表中。", "建议核查课程代码。")
        else:
            expected = catalog_row
            comparisons = dict(preferred_catalog_values)
            for column, actual in comparisons.items():
                if _should_skip_week_hour_catalog_compare(column, actual, category, name, expected_category):
                    continue
                expected_value = _safe_cell_text(expected.get(column, ""))
                if _values_differ(actual, expected_value):
                    _add(
                        result,
                        f"R-BASIC-CATALOG-{column}",
                        "高",
                        "课程基本信息表",
                        f"{column}与本专业课程基本信息文档及人才培养方案不一致：文档为“{_display_value(actual)}”，标准为“{expected_value}”。",
                        "请按照本专业课程基本信息文档及人才培养方案修正文档。",
                    )

            expected_assessment = _safe_cell_text(expected.get("考核方式", ""))
            actual_assessment = _find_assessment_mode(text)
            if actual_assessment and expected_assessment and _canonical_compare_text(actual_assessment) != _canonical_compare_text(expected_assessment):
                _add(
                    result,
                    "R-BASIC-CATALOG-考核方式",
                    "高",
                    "课程基本信息表",
                    f"考核方式与本专业课程基本信息文档及人才培养方案不一致：文档为“{actual_assessment}”，标准为“{expected_assessment}”。",
                    "请按照本专业课程基本信息文档及人才培养方案修正文档。",
                )


def _check_assessment(text: str, context: ReviewContext, result: ReviewResult) -> None:
    attendance_context = _find_attendance_assessment_context(text)
    if attendance_context:
        _add(result, "R-ASSESS-001", "高", "考核方式", "考核环节中出现“出勤/考勤”作为评分相关表述。", "OBE 理念下不建议将出勤或考勤作为评分项，请改为可评价的学习产出证据。")
    vague_modules = _find_vague_module_mentions(context.parsed.tables)
    if vague_modules:
        _add(result, "R-ASSESS-002", "中", "考核内容", "存在仅以“模块一/模块二”等编号代替考核内容的表述。", "请写明具体考核内容。")
    if "成绩评定" in text and "考试备注" in text and text.find("考试备注") < text.find("成绩评定"):
        _add(result, "R-ASSESS-003", "中", "考核方式", "“考试备注”位于“成绩评定”之前。", "请将“考试备注”放在“成绩评定”之后。")
    assessment_mode = _find_assessment_mode(text)
    if assessment_mode is None:
        _add(result, "R-ASSESS-004", "高", "考核方式", "未识别到课程考核方式为“考试”“考查”或“考核”。", "请明确填写考核方式，并与人才培养方案保持一致。")
    _check_assessment_weight_table(context.parsed, result)


def _find_prerequisite_book_title_courses(text: str) -> list[str]:
    values: list[str] = []
    for line in text.splitlines():
        if not re.search(r"先修课程|后修课程", line):
            continue
        compact_line = re.sub(r"\s+", "", line)
        if "《" not in compact_line or "》" not in compact_line:
            continue
        for label in ["先修课程", "后修课程"]:
            if label not in compact_line:
                continue
            segment = _segment_after_label(compact_line, label)
            courses = re.findall(r"《([^》]+)》", segment)
            for course in courses:
                item = f"{label}“《{course}》”"
                if item not in values:
                    values.append(item)
    return values


def _segment_after_label(line: str, label: str) -> str:
    start = line.find(label)
    if start < 0:
        return ""
    segment = line[start + len(label) :]
    stop_positions = [pos for next_label in ["先修课程", "后修课程", "课程性质", "课程类别", "学分", "总学时"] if (pos := segment.find(next_label)) > 0]
    if stop_positions:
        segment = segment[: min(stop_positions)]
    return segment


def _check_assessment_weight_table(parsed: ParsedDocument, result: ReviewResult) -> None:
    table = _find_assessment_weight_table(parsed.tables)
    if not table:
        return
    if not _table_has_total_row(table):
        _add(
            result,
            "R-ASSESS-008",
            "高",
            "表4 课程目标考核环节评定方式",
            "表4缺少最后一行“合计”，无法核对每个考核环节在各课程目标中的权重和是否为 1。",
            "请按模板保留“合计”行，并核对各考核环节列的课程目标权重合计均为 1。",
        )
        return

    analysis = _analyze_assessment_weight_table(table)
    if not analysis:
        return

    ratios = analysis["ratios"]
    ratio_sum = sum(ratios)
    if ratios and abs(ratio_sum - 1.0) > 0.001:
        _add(
            result,
            "R-ASSESS-005",
            "高",
            "表4 课程目标考核环节评定方式",
            f"各考核成绩所占比例之和为 {ratio_sum * 100:.1f}%，不等于 100%。",
            "请核对表头中各考核环节在总评中的占比，确保合计为 100%。",
        )

    for column_name, total in analysis["column_totals"]:
        if abs(total - 1.0) > 0.001:
            _add(
                result,
                "R-ASSESS-006",
                "高",
                "表4 课程目标考核环节评定方式",
                f"“{column_name}”列各课程目标权重合计为 {total:.3f}，不等于 1。",
                "请核对该考核环节下各课程目标权重，确保每个考核方式的课程目标权重之和为 1。",
            )

    for row_check in analysis["row_checks"]:
        calculated = row_check["calculated"]
        documented = row_check["documented"]
        if documented is None:
            _add(
                result,
                "R-ASSESS-007",
                "高",
                "表4 课程目标考核环节评定方式",
                f"{row_check['target']} 未填写设计权重，系统按表4计算应为 {calculated:.3f}。",
                "请按“∑（考核环节在课程目标中权重 × 考核环节在总评中占比）”计算并填写设计权重，结果保留三位小数。",
            )
        elif abs(round(calculated, 3) - documented) > 0.001:
            _add(
                result,
                "R-ASSESS-007",
                "高",
                "表4 课程目标考核环节评定方式",
                f"{row_check['target']} 设计权重计算不一致：系统计算为 {calculated:.3f}，文档填写为 {documented:.3f}。计算式：{row_check['formula']}。",
                "请按“∑（考核环节在课程目标中权重 × 考核环节在总评中占比）”重新计算，结果保留三位小数。",
            )


def _find_assessment_weight_table(tables: tuple[tuple[tuple[str, ...], ...], ...]) -> list[list[str]] | None:
    for table in tables:
        rows = [list(row) for row in table]
        joined = " ".join(" ".join(row) for row in rows)
        if "课程目标" in joined and "设计权重" in joined:
            return rows
    return None


def _table_has_total_row(table: list[list[str]]) -> bool:
    return any(any("合计" in cell for cell in row) for row in table)


def _analyze_assessment_weight_table(table: list[list[str]]) -> dict[str, object] | None:
    if len(table) < 3:
        return None
    width = max(len(row) for row in table)
    rows = [row + [""] * (width - len(row)) for row in table]
    design_idx = _find_column_index(rows, "设计权重")
    target_idx = _find_column_index(rows, "课程目标")
    if design_idx is None or target_idx is None or design_idx <= target_idx + 1:
        return None

    assessment_indices = list(range(target_idx + 1, design_idx))
    header_rows = _assessment_header_rows(rows, target_idx, design_idx)
    ratios = [_assessment_column_ratio(rows, idx, header_rows, assessment_indices) for idx in assessment_indices]
    if any(ratio is None for ratio in ratios):
        return None
    ratio_values = [float(ratio) for ratio in ratios if ratio is not None]
    column_names = [_assessment_column_name(rows, idx, header_rows) for idx in assessment_indices]

    data_rows = [row for row in rows if _is_course_target_row(row[target_idx])]
    if not data_rows:
        return None

    column_totals = []
    for idx, name in zip(assessment_indices, column_names):
        total = sum(_number(row[idx]) or 0 for row in data_rows)
        column_totals.append((name, total))

    row_checks = []
    for row in data_rows:
        values = [_number(row[idx]) or 0 for idx in assessment_indices]
        calculated = sum(value * ratio for value, ratio in zip(values, ratio_values))
        documented = _number(row[design_idx])
        formula = "+".join(f"{value:g}×{ratio * 100:g}%" for value, ratio in zip(values, ratio_values) if value)
        row_checks.append(
            {
                "target": _safe_cell_text(row[target_idx]),
                "calculated": calculated,
                "documented": documented,
                "formula": formula or "0",
            }
        )
    return {"ratios": ratio_values, "column_totals": column_totals, "row_checks": row_checks}


def _find_column_index(rows: list[list[str]], keyword: str) -> int | None:
    for row in rows[:3]:
        for idx, cell in enumerate(row):
            if keyword in cell:
                return idx
    return None


def _assessment_header_rows(rows: list[list[str]], target_idx: int, design_idx: int) -> list[list[str]]:
    header_rows = []
    for row in rows:
        if _is_course_target_row(row[target_idx]) or _clean_label(row[target_idx]) == "合计":
            break
        if any(row[idx].strip() for idx in range(target_idx + 1, design_idx + 1)):
            header_rows.append(row)
    return header_rows[:3]


def _assessment_column_ratio(rows: list[list[str]], idx: int, header_rows: list[list[str]], assessment_indices: list[int]) -> float | None:
    for row in reversed(header_rows):
        percent = _percent(row[idx])
        if percent is not None:
            same_value_count = sum(1 for col_idx in assessment_indices if row[col_idx] == row[idx])
            if same_value_count == 1 or _has_specific_percent_below(header_rows, idx, row):
                return percent
    return None


def _has_specific_percent_below(header_rows: list[list[str]], idx: int, row: list[str]) -> bool:
    start = header_rows.index(row) + 1
    return any(_percent(later_row[idx]) is not None for later_row in header_rows[start:])


def _assessment_column_name(rows: list[list[str]], idx: int, header_rows: list[list[str]]) -> str:
    parts = []
    for row in header_rows:
        text = _safe_cell_text(row[idx])
        if text and text not in parts and text != "课程目标":
            parts.append(text)
    return " / ".join(parts) or f"第{idx + 1}列"


def _percent(value: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", value or "")
    return float(match.group(1)) / 100 if match else None


def _is_course_target_row(value: str) -> bool:
    return bool(re.search(r"课程目标\s*\d+", value or ""))


def _check_graduation_support(text: str, context: ReviewContext, result: ReviewResult) -> None:
    if context.program.is_platform:
        _check_platform_graduation_support(text, context, result)
        return

    for column in ["课程目标", "支撑的毕业要求", "支撑强度", "毕业要求指标点内容"]:
        if _clean_label(column) not in _clean_label(text):
            _add(result, "R-GRAD-001", "高", "课程目标与毕业要求的关系表", f"缺少“{column}”列或字段。", "请补齐支撑关系表的四个必需字段。")
    invalid = re.findall(r"支撑强度\s*[:：]?\s*([^HML\s，,。;；|]+)", text)
    if invalid:
        _add(result, "R-GRAD-002", "高", "支撑强度", f"存在非法支撑强度：{', '.join(invalid[:5])}。", "支撑强度只能填写 H、M、L。")

    code = _find_field(text, ["课程代码", "课程编号"])
    name = _find_field(text, ["课程名称"])
    plan_course = find_plan_course(context.training_plan, code, name)
    if not plan_course:
        return

    graduation_support_tables = _extract_graduation_support_tables(context.parsed.tables)
    syllabus_support_details = graduation_support_tables[0] if graduation_support_tables else _extract_syllabus_support_details(text)
    syllabus_support = {key: value["strength"] for key, value in syllabus_support_details.items() if value.get("strength")}
    if not syllabus_support:
        _add(result, "R-GRAD-003", "中", "课程目标与毕业要求的关系表", "未能从大纲中识别出明确的毕业要求支撑关系。", "请确认表格中包含毕业要求编号和 H/M/L 支撑强度。")
        return
    compact_rows = _compact_graduation_support_rows(text)
    if compact_rows:
        _add(result, "R-GRAD-006", "中", "课程目标与毕业要求的关系表", _format_compact_support_message(compact_rows, len(syllabus_support)), "原则上建议课程目标数量与支撑毕业要求数量相匹配，由每个课程目标分别对应一条毕业要求；如确需一项课程目标支撑多个毕业要求，也应使用规范的合并单元格并保留每个毕业要求及支撑强度的独立行。")
    duplicate_requirement_content = _find_requirement_name_repeated_in_content_table(context.parsed.tables)
    if duplicate_requirement_content:
        _add(
            result,
            "R-GRAD-008",
            "中",
            "课程目标与毕业要求的关系表",
            "“毕业要求指标点内容”列重复填写了毕业要求编号或名称：" + "；".join(duplicate_requirement_content[:5]) + "。",
            "“支撑的毕业要求”列填写毕业要求编号和名称即可，“毕业要求指标点内容”列建议直接填写指标点具体内容。",
        )

    if plan_course.support:
        expected = {key: value for key, value in plan_course.support.items() if value}
        actual = {key: value for key, value in syllabus_support.items() if value}
        if expected and actual != expected:
            _add(
                result,
                "R-GRAD-004",
                "高",
                "课程目标与毕业要求的关系表",
                _format_support_mismatch(actual, expected, syllabus_support_details, context.training_plan),
                "请以本专业人才培养方案课程支撑矩阵为准修正毕业要求编号和 H/M/L 支撑强度。",
            )

    for requirement_id, content in _extract_requirement_content_from_syllabus(text).items():
        expected_content = _find_expected_requirement_content(context.training_plan, requirement_id)
        if expected_content and not _is_content_equivalent(content, expected_content):
            _add(
                result,
                "R-GRAD-005",
                "中",
                "毕业要求指标点内容",
                f"{requirement_id} 的指标点内容疑似与人才培养方案不一致或存在摘录缩写。",
                "请完整引用人才培养方案中的毕业要求指标点内容；强支撑部分可加粗，但不应改写或缩写。",
            )


def _check_platform_graduation_support(text: str, context: ReviewContext, result: ReviewResult) -> None:
    for column in ["课程目标", "支撑的毕业要求", "支撑强度", "毕业要求指标点内容"]:
        if _clean_label(column) not in _clean_label(text):
            _add(result, "R-GRAD-001", "高", "课程目标与毕业要求的关系表", f"缺少“{column}”列或字段。", "请补齐支撑关系表的四个必需字段。")

    related_codes = list(context.platform_related_programs or context.program.related_programs)
    support_tables = _extract_graduation_support_tables(context.parsed.tables)
    if len(support_tables) < len(related_codes):
        _add(
            result,
            "R-GRAD-007",
            "高",
            "平台课程毕业要求关系表",
            f"商学院平台课程应包含 {len(related_codes)} 个专业对应的课程目标与毕业要求关系表，当前仅识别到 {len(support_tables)} 个。",
            "请按电子商务、国际经济与贸易、城市管理、大数据管理与应用、金融科技等专业分别保留课程目标与毕业要求关系表。",
        )

    code = _find_field(text, ["课程代码", "课程编号"])
    name = _find_field(text, ["课程名称"])
    plan_courses: dict[str, object] = {}
    expected_supports: dict[str, dict[str, str]] = {}
    for index, program_code in enumerate(related_codes):
        program_name = context.program_names.get(program_code, program_code)
        plan = context.training_plans.get(program_code)
        if not plan or not plan.available:
            _add(result, "R-PLAN-001", "低", f"{program_name}人才培养方案", f"未找到{program_name}的人才培养方案 PDF，跳过该专业一致性审查。", "请将对应专业人才培养方案 PDF 放在 data/programs/<专业代码>/training_plan.pdf。")
            continue
        plan_course = find_plan_course(plan, code, name)
        if not plan_course:
            _add(result, "R-PLAN-004", "中", f"{program_name}人才培养方案", f"未能在{program_name}人才培养方案中匹配到该平台课程。", "请核对课程代码、课程名称是否与该专业人才培养方案一致。")
            continue
        plan_courses[program_code] = plan_course
        expected_supports[program_code] = {key: value for key, value in plan_course.support.items() if value}

    support_assignments = _match_platform_support_tables(support_tables, expected_supports)
    has_missing_platform_table = len(support_tables) < len(related_codes)

    for program_code in related_codes:
        if program_code not in plan_courses:
            continue
        program_name = context.program_names.get(program_code, program_code)
        table_support_details = support_assignments.get(program_code, {})
        plan = context.training_plans.get(program_code)
        if not table_support_details:
            if not has_missing_platform_table:
                _add(result, "R-GRAD-003", "中", f"{program_name}课程目标与毕业要求的关系表", f"未能从大纲中识别出{program_name}对应的毕业要求支撑关系。", "请确认平台课程中该专业对应表格包含毕业要求编号和 H/M/L 支撑强度。")
            continue
        actual = {key: value["strength"] for key, value in table_support_details.items() if value.get("strength")}
        expected = expected_supports.get(program_code, {})
        if expected and actual != expected:
            _add(
                result,
                "R-GRAD-004",
                "高",
                f"{program_name}课程目标与毕业要求的关系表",
                _format_support_mismatch(actual, expected, table_support_details, plan),
                f"请以{program_name}人才培养方案课程支撑矩阵为准修正该专业对应表格中的毕业要求编号和 H/M/L 支撑强度。",
            )


def _available_training_plans(context: ReviewContext) -> list[TrainingPlan]:
    if context.training_plans:
        return [plan for plan in context.training_plans.values() if plan.available]
    return [context.training_plan] if context.training_plan and context.training_plan.available else []


def _find_plan_course_for_context(context: ReviewContext, code: str | None, name: str | None) -> tuple[TrainingPlan | None, object | None]:
    for plan in _available_training_plans(context):
        course = find_plan_course(plan, code, name)
        if course:
            return plan, course
    return None, None


def _extract_graduation_support_tables(
    tables: tuple[tuple[tuple[str, ...], ...], ...],
) -> list[dict[str, dict[str, str]]]:
    support_tables: list[dict[str, dict[str, str]]] = []
    for table in tables:
        support = _extract_support_details_from_table(table)
        if support:
            support_tables.append(support)
    return support_tables


def _match_platform_support_tables(
    support_tables: list[dict[str, dict[str, str]]],
    expected_supports: dict[str, dict[str, str]],
) -> dict[str, dict[str, dict[str, str]]]:
    candidates: list[tuple[int, str, int]] = []
    for program_code, expected in expected_supports.items():
        if not expected:
            continue
        for index, support in enumerate(support_tables):
            score = _support_table_match_score(support, expected)
            if score > 0:
                candidates.append((score, program_code, index))
    candidates.sort(reverse=True, key=lambda item: item[0])

    assignments: dict[str, dict[str, dict[str, str]]] = {}
    used_tables: set[int] = set()
    used_programs: set[str] = set()
    for _, program_code, index in candidates:
        if program_code in used_programs or index in used_tables:
            continue
        assignments[program_code] = support_tables[index]
        used_programs.add(program_code)
        used_tables.add(index)
    return assignments


def _support_table_match_score(support: dict[str, dict[str, str]], expected: dict[str, str]) -> int:
    actual = {key: value.get("strength", "") for key, value in support.items() if value.get("strength")}
    if not actual or not expected:
        return 0
    matching = sum(1 for key, value in actual.items() if expected.get(key) == value)
    wrong = sum(1 for key, value in actual.items() if key in expected and expected.get(key) != value)
    extra = len(set(actual) - set(expected))
    missing = len(set(expected) - set(actual))
    exact_bonus = 100 if actual == expected else 0
    return exact_bonus + matching * 4 - wrong * 3 - extra - missing


def _extract_support_details_from_table(table: tuple[tuple[str, ...], ...]) -> dict[str, dict[str, str]]:
    if not table:
        return {}
    rows = [list(row) for row in table]
    header_idx = None
    requirement_idx = None
    strength_idx = None
    for idx, row in enumerate(rows[:3]):
        for col_idx, cell in enumerate(row):
            clean = _clean_label(cell)
            if "支撑的毕业要求" in clean or clean == "毕业要求":
                requirement_idx = col_idx
            if "支撑强度" in clean:
                strength_idx = col_idx
        if requirement_idx is not None and strength_idx is not None:
            header_idx = idx
            break
    if header_idx is None or requirement_idx is None or strength_idx is None:
        return {}
    support: dict[str, dict[str, str]] = {}
    for row in rows[header_idx + 1 :]:
        requirement_cell = _safe_cell_text(row[requirement_idx] if requirement_idx < len(row) else "")
        strength_cell = _safe_cell_text(row[strength_idx] if strength_idx < len(row) else "")
        details = _extract_requirement_details(requirement_cell)
        strengths = _extract_strengths(strength_cell)
        if details and strengths:
            if len(strengths) == 1:
                strengths = strengths * len(details)
            for detail, strength in zip(details, strengths):
                support[detail["id"]] = {"name": detail.get("name", ""), "strength": strength}
    return support


def _find_requirement_name_repeated_in_content_table(tables: tuple[tuple[tuple[str, ...], ...], ...]) -> list[str]:
    values: list[str] = []
    for table in tables:
        rows = [list(row) for row in table]
        header_idx = None
        requirement_idx = None
        content_idx = None
        for idx, row in enumerate(rows[:3]):
            for col_idx, cell in enumerate(row):
                clean = _clean_label(cell)
                if "支撑的毕业要求" in clean or clean == "毕业要求":
                    requirement_idx = col_idx
                if "毕业要求指标点内容" in clean or "指标点内容" in clean:
                    content_idx = col_idx
            if requirement_idx is not None and content_idx is not None:
                header_idx = idx
                break
        if header_idx is None or requirement_idx is None or content_idx is None:
            continue
        for row in rows[header_idx + 1 :]:
            requirement = _safe_cell_text(row[requirement_idx] if requirement_idx < len(row) else "")
            content = _safe_cell_text(row[content_idx] if content_idx < len(row) else "")
            if not requirement or not content:
                continue
            req_id = _normalize_bare_requirement_id(requirement)
            req_name = _extract_requirement_name(requirement)
            content_prefix = content[:24]
            number = re.escape(req_id.replace("毕业要求", "")) if req_id else ""
            name = re.escape(req_name) if req_name else ""
            repeated_id = bool(number and re.match(rf"^\s*{number}\s*[.．、]", content_prefix))
            repeated_name = bool(name and re.match(rf"^\s*(?:{number}\s*[.．、]\s*)?{name}\s*[。；;：:，,、]", content_prefix))
            if repeated_id or repeated_name:
                item = f"{requirement}在指标点内容列中重复出现"
                if item not in values:
                    values.append(item)
    return values


def _check_teaching_hours(text: str, context: ReviewContext, result: ReviewResult) -> None:
    is_practice_course = _is_practice_education_course(text, context)
    non_hour_units = _find_non_hour_teaching_units(text, context.parsed.tables)
    if non_hour_units:
        _add(
            result,
            "R-HOUR-004",
            "中",
            "教学内容与学时",
            f"教学内容的学时安排使用了“周/天”作为单位：{', '.join(non_hour_units[:8])}。",
            "建议每一个具体教学内容按照每周 30 学时的总标准进行拆分，以具体学时数体现，不建议用“周”或“天”表述。",
        )
    if not is_practice_course:
        large_modules = _find_large_teaching_hour_modules(context.parsed.tables)
        if large_modules:
            _add(
                result,
                "R-HOUR-005",
                "中",
                "表2 教学内容与课程目标的支撑关系",
                "存在单个教学模块学时安排过大：" + "；".join(large_modules[:8]) + "。",
                "建议按 2 学时左右的教学单元细化拆分，避免将 6 学时、10 学时等内容合并为笼统模块。",
            )
        multi_goal_mappings = _find_multi_goal_teaching_content_mappings(context.parsed.tables)
        if multi_goal_mappings:
            _add(
                result,
                "R-HOUR-006",
                "中",
                "表2 教学内容与课程目标的支撑关系",
                "存在一个教学内容对应多个课程目标的情况：" + "；".join(multi_goal_mappings[:8]) + "。",
                "建议教学内容与支撑课程目标一一对应，方便后续课程目标达成计算，尽量不要出现一个教学内容对应多个课程目标的情况。",
            )
    repetitive_methods = _find_repetitive_teaching_methods(context.parsed.tables)
    if repetitive_methods:
        _add(
            result,
            "R-HOUR-007",
            "中",
            "表2 教学内容与课程目标的支撑关系",
            repetitive_methods,
            "建议结合每个教学内容的实际需要选择教学方法，避免所有教学内容机械套用同一组教学方法。",
        )
    hour_values = re.findall(r"(?:理论学时|实践学时|实验学时)\s*[:：]?\s*(\d+)(?!\s*[周天])", text)
    odd_values = [value for value in hour_values if int(value) % 2 != 0]
    if odd_values and not _is_relaxed_practice_hour_course(text, context):
        _add(result, "R-HOUR-001", "中", "教学内容与学时", f"存在非 2 的倍数学时：{', '.join(odd_values[:8])}。", "建议理论学时和实践学时均按 2 学时单元划分。")
    if not re.search(r"课程思政|劳育|美育", text):
        _add(result, "R-HOUR-003", "中", "教学内容", "未识别到课程思政、劳育或美育相关内容。", "每个知识模块建议至少体现课程思政、劳育、美育三者之一。")


def _is_practice_education_course(text: str, context: ReviewContext) -> bool:
    category = _find_field(text, ["课程类别", "课程分类"])
    if _canonical_compare_text(category) == "实践教育课程":
        return True
    code = _find_field(text, ["课程代码", "课程编号"])
    catalog_row = _find_catalog_row(context.course_catalog, code)
    if catalog_row is not None and _canonical_compare_text(_safe_cell_text(catalog_row.get("课程类别", ""))) == "实践教育课程":
        return True
    return False


def _is_relaxed_practice_hour_course(text: str, context: ReviewContext) -> bool:
    if not _is_practice_education_course(text, context):
        return False
    name = _canonical_compare_text(_find_field(text, ["课程名称"]) or "")
    return any(keyword in name for keyword in ["毕业设计", "毕业论文", "毕业实习", "认知实习"])


def _find_non_hour_teaching_units(text: str, tables: tuple[tuple[tuple[str, ...], ...], ...]) -> list[str]:
    values: list[str] = []
    found_teaching_hours_table = False
    for table in tables:
        rows = [list(row) for row in table]
        if not _looks_like_teaching_hours_table(rows):
            continue
        found_teaching_hours_table = True
        _, theory_idx, practice_idx = _teaching_hours_table_indices(rows)
        target_indices = [idx for idx in (theory_idx, practice_idx) if idx is not None]
        start_row = _teaching_hours_data_start(rows)
        for row in rows[start_row:]:
            for column_index in target_indices:
                if column_index >= len(row):
                    continue
                for value in _extract_week_day_units(_safe_cell_text(row[column_index])):
                    if value not in values:
                        values.append(value)
    if found_teaching_hours_table:
        return values

    for line in text.splitlines():
        if not _looks_like_teaching_content_hour_line(line):
            continue
        for value in _extract_week_day_units(line):
            if value not in values:
                values.append(value)
    return values


def _extract_week_day_units(text: str) -> list[str]:
    return [re.sub(r"\s+", "", match.group(0)) for match in re.finditer(r"\d+(?:\.\d+)?\s*[周天]", text)]


def _find_large_teaching_hour_modules(tables: tuple[tuple[tuple[str, ...], ...], ...]) -> list[str]:
    values: list[str] = []
    for table in tables:
        rows = [list(row) for row in table]
        if not _looks_like_teaching_hours_table(rows):
            continue
        module_idx, theory_idx, practice_idx = _teaching_hours_table_indices(rows)
        if module_idx is None or (theory_idx is None and practice_idx is None):
            continue
        start_row = _teaching_hours_data_start(rows)
        for row in rows[start_row:]:
            module = _safe_cell_text(row[module_idx] if module_idx < len(row) else "")
            if not module or re.search(r"合计|小计", module):
                continue
            for label, column_index in [("理论", theory_idx), ("实践", practice_idx)]:
                if column_index is None or column_index >= len(row):
                    continue
                hours = _number(_safe_cell_text(row[column_index]))
                if hours is not None and hours > 4:
                    item = f"{module[:24]}（{label}{hours:g}学时）"
                    if item not in values:
                        values.append(item)
    return values


def _looks_like_teaching_content_goal_table(rows: list[list[str]]) -> bool:
    sample = "".join("".join(row) for row in rows[:3])
    return bool(re.search(r"教学内容|教学模块|教学单元|实践项目", sample) and re.search(r"支撑的课程目标|课程目标", sample))


def _find_multi_goal_teaching_content_mappings(tables: tuple[tuple[tuple[str, ...], ...], ...]) -> list[str]:
    values: list[str] = []
    for table in tables:
        rows = [list(row) for row in table]
        if not _looks_like_teaching_content_goal_table(rows):
            continue
        content_idx, goal_idx = _teaching_content_goal_indices(rows)
        if goal_idx is None:
            continue
        start_row = _teaching_hours_data_start(rows)
        for row in rows[start_row:]:
            content = _safe_cell_text(row[content_idx] if content_idx is not None and content_idx < len(row) else "")
            goal_text = _safe_cell_text(row[goal_idx] if goal_idx < len(row) else "")
            goals = _extract_course_target_numbers(goal_text)
            if len(goals) <= 1:
                continue
            label = content[:24] if content else "未识别教学内容"
            item = f"{label}（对应课程目标{ '、'.join(goals) }）"
            if item not in values:
                values.append(item)
    return values


def _find_repetitive_teaching_methods(tables: tuple[tuple[tuple[str, ...], ...], ...]) -> str:
    for table in tables:
        rows = [list(row) for row in table]
        if not _looks_like_teaching_content_goal_table(rows):
            continue
        method_idx = _teaching_method_index(rows)
        if method_idx is None:
            continue
        start_row = _teaching_hours_data_start(rows)
        methods = [
            _normalize_teaching_method_text(_safe_cell_text(row[method_idx] if method_idx < len(row) else ""))
            for row in rows[start_row:]
        ]
        methods = [method for method in methods if method and not re.search(r"合计|小计", method)]
        if len(methods) < 4:
            continue
        counts = Counter(methods)
        method_text, count = counts.most_common(1)[0]
        method_count = len(_split_teaching_methods(method_text))
        if method_count >= 4 and count == len(methods):
            return f"教学方法列共 {len(methods)} 处均填写“{method_text}”，且单处包含 {method_count} 种教学方法，存在模板化填写倾向。"
        if method_count >= 4 and count / len(methods) >= 0.85:
            return f"教学方法列有 {count}/{len(methods)} 处重复填写“{method_text}”，且单处包含 {method_count} 种教学方法，存在模板化填写倾向。"
    return ""


def _teaching_method_index(rows: list[list[str]]) -> int | None:
    for row in rows[:3]:
        for index, cell in enumerate(row):
            if re.search(r"教学方法|教学方式", _clean_label(cell)):
                return index
    return None


def _normalize_teaching_method_text(value: str) -> str:
    return re.sub(r"\s+", "", value or "").strip("，,；;、 ")


def _split_teaching_methods(value: str) -> list[str]:
    return [item for item in re.split(r"[、,，；;+/＋和及]", value or "") if item.strip()]


def _teaching_content_goal_indices(rows: list[list[str]]) -> tuple[int | None, int | None]:
    content_idx: int | None = None
    goal_idx: int | None = None
    for row in rows[:3]:
        for index, cell in enumerate(row):
            clean = _clean_label(cell)
            if content_idx is None and re.search(r"教学内容|教学模块|教学单元|实践项目", clean):
                content_idx = index
            if goal_idx is None and re.search(r"支撑.*课程目标|课程目标", clean):
                goal_idx = index
    return content_idx, goal_idx


def _extract_course_target_numbers(value: str) -> list[str]:
    text = re.sub(r"\s+", "", value or "")
    if "课程目标" not in text:
        return []
    tail = text[text.find("课程目标") :]
    tail = tail.replace("课程目标", "、")
    tokens = re.findall(r"\d+|[一二三四五六七八九十]", tail)
    goals: list[str] = []
    for token in tokens:
        goal = _chinese_digit_to_int(token)
        if goal and goal not in goals:
            goals.append(goal)
    return goals


def _looks_like_teaching_hours_table(rows: list[list[str]]) -> bool:
    sample = "".join("".join(row) for row in rows[:3])
    return bool(re.search(r"教学模块|教学内容|支撑的课程目标", sample) and re.search(r"理论|实践|学时安排", sample))


def _teaching_hours_table_indices(rows: list[list[str]]) -> tuple[int | None, int | None, int | None]:
    module_idx: int | None = None
    theory_idx: int | None = None
    practice_idx: int | None = None
    for row in rows[:3]:
        for index, cell in enumerate(row):
            clean = _clean_label(cell)
            if module_idx is None and re.search(r"教学模块|教学单元|模块", clean):
                module_idx = index
            if theory_idx is None and clean == "理论":
                theory_idx = index
            if practice_idx is None and clean == "实践":
                practice_idx = index
    return module_idx, theory_idx, practice_idx


def _teaching_hours_data_start(rows: list[list[str]]) -> int:
    for index, row in enumerate(rows[:4]):
        if any(_clean_label(cell) in {"理论", "实践"} for cell in row):
            return index + 1
    return 1


def _looks_like_teaching_content_hour_line(line: str) -> bool:
    clean = line.strip()
    if not clean:
        return False
    if re.search(r"课程基本信息|总学时|理论学时|实践学时|实验学时|学\s*分|开课学期", clean):
        return False
    if "|" in clean:
        cells = [cell.strip() for cell in clean.strip("|").split("|")]
        row_text = "".join(cells)
        return bool(re.search(r"教学内容|教学模块|实践项目|课程目标|支撑的课程目标", row_text))
    return bool(re.search(r"教学内容|教学模块|实践项目|学时安排|支撑的课程目标", clean))


def _check_course_resources(text: str, result: ReviewResult) -> None:
    required = ["五、课程资源", "教材", "主要参考书目", "数字教学资源"]
    for keyword in required:
        if keyword not in text:
            _add(result, "R-RESOURCE-001", "中", "课程资源", f"未识别到“{keyword}”。", "请按“五、课程资源”“(一)教材”“(二)主要参考书目”“(三)数字教学资源”的结构填写。")
    years = [int(year) for year in re.findall(r"(20\d{2})\s*年?", text)]
    if years and max(years) < 2023:
        _add(result, "R-RESOURCE-002", "低", "教材", f"识别到最新出版年份为 {max(years)} 年。", "教材建议使用近 3 年版本，优先国家级规划教材或自编教材。")


def _check_document_cleanliness(text: str, parsed: ParsedDocument, result: ReviewResult) -> None:
    for finding in parsed.format_rule_findings:
        priority = finding.priority if finding.priority in {"高", "中", "低"} else "低"
        _add(result, finding.rule_id, priority, finding.location, finding.message, finding.suggestion)
    if parsed.format_findings:
        _add(result, "R-FORMAT-001", "高", "文档清洁度", "文档中存在批注或修订痕迹：" + "；".join(parsed.format_findings[:5]) + "。", "请接受修订、删除批注后再提交。")
    if re.search(r"绿色模板|模板残留|请在此处填写", text):
        _add(result, "R-FORMAT-002", "中", "文档清洁度", "文档中疑似存在模板残留文字。", "请删除模板提示语和残留文字。")


def _run_ai_review(text: str) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return "未配置 DEEPSEEK_API_KEY，已跳过内容深度审查。"
    client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": AI_REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": "请根据以下教学大纲文本输出一段简要内容深度审查意见：\n\n" + text[:60000]},
        ],
        temperature=0.2,
        max_tokens=360,
    )
    return _clean_ai_review_output(response.choices[0].message.content or "")


def _clean_ai_review_output(content: str) -> str:
    lines: list[str] = []
    for raw_line in (content or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^#+\s*", "", line).strip()
        if re.fullmatch(r"(内容深度审查结果|审查意见)[:：]?", line):
            continue
        line = re.sub(r"^(?:[-*•]\s*|\d+\s*[.、)]\s*)", "", line).strip()
        line = re.sub(r"^(内容深度审查结果|审查意见)\s*[:：]\s*", "", line).strip()
        if line:
            lines.append(line)
    text = "；".join(lines)
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"^#+\s*", "", text)
    text = re.sub(r"^(内容深度审查结果|审查意见)\s*[:：]\s*", "", text)
    text = re.sub(r"[。！？]\s*；", "；", text)
    text = re.sub(r"；\s*[。！？]", "。", text)
    if len(text) <= 260:
        return text
    punctuation_positions = [match.end() for match in re.finditer(r"[。！？；]", text[:260])]
    if punctuation_positions:
        return text[:punctuation_positions[-1]].strip()
    return text[:260].rstrip("，,；;、 ") + "。"


def _extract_syllabus_support(text: str) -> dict[str, str]:
    return {key: value["strength"] for key, value in _extract_syllabus_support_details(text).items() if value.get("strength")}


def _extract_syllabus_support_details(text: str) -> dict[str, dict[str, str]]:
    support: dict[str, str] = {}
    pipe_rows = [line for line in text.splitlines() if "|" in line]
    for row in pipe_rows:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        requirement_details = []
        strengths = []
        for cell in cells:
            requirement_details.extend(_extract_requirement_details(cell))
            strengths.extend(_extract_strengths(cell))
        if requirement_details and strengths:
            if len(strengths) == 1:
                strengths = strengths * len(requirement_details)
            for detail, strength in zip(requirement_details, strengths):
                support[detail["id"]] = {"name": detail["name"], "strength": strength}

    inline_matches = re.findall(r"(?:毕业要求|指标点)\s*(\d+(?:\.\d+)?)[^HML\n\r]{0,30}([HML])", text, flags=re.IGNORECASE)
    for number, strength in inline_matches:
        key = f"毕业要求{number}"
        support.setdefault(key, {"name": "", "strength": strength.upper()})
    return support


def _has_compact_graduation_support_rows(text: str) -> bool:
    return bool(_compact_graduation_support_rows(text))


def _compact_graduation_support_rows(text: str) -> list[dict[str, object]]:
    compact_rows: list[dict[str, object]] = []
    for row in [line for line in text.splitlines() if "|" in line]:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        requirements = _unique_requirement_details([detail for cell in cells for detail in _extract_requirement_details(cell)])
        strengths = [strength for cell in cells for strength in _extract_strengths(cell)]
        if not strengths:
            continue
        if len(requirements) > 1 or len(set(strengths)) > 1:
            if any("课程目标" in cell for cell in cells):
                compact_rows.append(
                    {
                        "target": next((cell for cell in cells if "课程目标" in cell), "课程目标"),
                        "requirements": requirements,
                        "strengths": strengths,
                    }
                )
    return compact_rows


def _unique_requirement_details(details: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for detail in details:
        key = detail.get("id", "")
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(detail)
    return unique


def _format_compact_support_message(rows: list[dict[str, object]], support_count: int) -> str:
    details = []
    for row in rows[:3]:
        requirements = row.get("requirements", [])
        strengths = row.get("strengths", [])
        if not isinstance(requirements, list) or not isinstance(strengths, list):
            continue
        req_text = "、".join(_format_support_item(detail["id"], detail.get("name", ""), strengths[idx] if idx < len(strengths) else "未填写") for idx, detail in enumerate(requirements))
        details.append(f"{row.get('target', '课程目标')}同时对应{req_text}")
    prefix = "；".join(details) if details else "同一课程目标同时对应多个毕业要求或多个支撑强度"
    return f"{prefix}，表格结构不规范。该课程共识别到 {support_count} 条毕业要求支撑关系，原则上建议设置相应数量的课程目标分别对应撰写。"


def _extract_requirement_content_from_syllabus(text: str) -> dict[str, str]:
    contents: dict[str, str] = {}
    pattern = re.compile(r"((?:毕业要求|指标点)\s*\d+(?:\.\d+)?)\s*[:：]?\s*([^\n\r|]{8,})")
    for raw_id, content in pattern.findall(text):
        requirement_id = _normalize_requirement_id(raw_id)
        if requirement_id:
            contents[requirement_id] = content.strip()
    return contents


def _find_expected_requirement_content(plan: TrainingPlan | None, requirement_id: str) -> str:
    if not plan:
        return ""
    if requirement_id in plan.graduation_requirements:
        return plan.graduation_requirements[requirement_id]
    short_id = requirement_id.replace("毕业要求", "")
    for key, content in plan.graduation_requirements.items():
        if key.replace("毕业要求", "") == short_id:
            return content
    return ""


def _is_content_equivalent(actual: str, expected: str) -> bool:
    actual_clean = re.sub(r"\s+", "", actual)
    expected_clean = re.sub(r"\s+", "", expected)
    if not actual_clean or not expected_clean:
        return True
    return actual_clean in expected_clean or expected_clean in actual_clean


def _normalize_requirement_id(value: str) -> str:
    match = re.search(r"(?:毕业要求|指标点)\s*(\d+(?:\.\d+)?)", value)
    return f"毕业要求{match.group(1)}" if match else ""


def _format_support(support: dict[str, str]) -> str:
    return "、".join(f"{key}:{value}" for key, value in sorted(support.items())) or "未识别"


def _format_support_mismatch(
    actual: dict[str, str],
    expected: dict[str, str],
    actual_details: dict[str, dict[str, str]],
    plan: TrainingPlan | None,
) -> str:
    differences: list[str] = []
    all_ids = sorted(set(actual) | set(expected), key=_requirement_sort_key)
    for requirement_id in all_ids:
        actual_strength = actual.get(requirement_id)
        expected_strength = expected.get(requirement_id)
        if actual_strength == expected_strength:
            continue
        actual_item = _format_support_item(requirement_id, _actual_requirement_name(actual_details, requirement_id), actual_strength or "未填写")
        expected_item = _format_support_item(requirement_id, _find_plan_requirement_name(plan, requirement_id), expected_strength or "不应填写")
        if actual_strength and expected_strength:
            differences.append(f"{requirement_id}支撑强度不一致：大纲为“{actual_item}”，培养方案为“{expected_item}”，大纲应改为“{expected_item}”。")
        elif actual_strength and not expected_strength:
            differences.append(f"{requirement_id}不应填写：大纲为“{actual_item}”，培养方案未要求该毕业要求，大纲应删除该项。")
        elif expected_strength and not actual_strength:
            differences.append(f"{requirement_id}缺失：大纲未填写，培养方案为“{expected_item}”，大纲应补充“{expected_item}”。")
    return "课程支撑毕业要求与人才培养方案不一致。仅列出需修改项：" + " ".join(differences)


def _actual_requirement_name(details: dict[str, dict[str, str]], requirement_id: str) -> str:
    return details.get(requirement_id, {}).get("name", "")


def _format_actual_support(actual: dict[str, str], details: dict[str, dict[str, str]]) -> str:
    items = []
    for requirement_id, strength in sorted(actual.items(), key=lambda item: _requirement_sort_key(item[0])):
        requirement_name = details.get(requirement_id, {}).get("name", "")
        items.append(_format_support_item(requirement_id, requirement_name, strength))
    return "；".join(items) or "未识别"


def _format_expected_support(expected: dict[str, str], plan: TrainingPlan | None) -> str:
    items = []
    for requirement_id, strength in sorted(expected.items(), key=lambda item: _requirement_sort_key(item[0])):
        items.append(_format_support_item(requirement_id, _find_plan_requirement_name(plan, requirement_id), strength))
    return "；".join(items) or "未识别"


def _format_support_item(requirement_id: str, requirement_name: str, strength: str) -> str:
    name_part = f".{requirement_name}" if requirement_name else ""
    return f"{requirement_id}{name_part}，支撑强度{strength}"


def _find_plan_requirement_name(plan: TrainingPlan | None, requirement_id: str) -> str:
    if not plan or not plan.parsed:
        return ""
    number = re.escape(requirement_id.replace("毕业要求", ""))
    text = plan.parsed.markdown or plan.parsed.text
    match = re.search(rf"(?:^|\n)\s*{number}\s*[.．、]\s*([\u4e00-\u9fa5]{{2,12}})\s*[。.]", text)
    return match.group(1) if match else ""


def _extract_requirement_name(value: str) -> str:
    match = re.search(r"(?:毕业要求|指标点)?\s*\d+(?:\.\d+)?\s*[.．、]?\s*([\u4e00-\u9fa5]{2,12})", value)
    return match.group(1) if match else ""


def _extract_requirement_details(value: str) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    clean_value = _clean_label(value)
    pattern = re.compile(r"(?:毕业要求|指标点)?\s*(\d+(?:\.\d+)?)\s*[.．、]?\s*([\u4e00-\u9fa5]{2,12})?")
    for match in pattern.finditer(value):
        name = (match.group(2) or "").strip()
        if not name and not re.search(r"毕业要求|指标点|[\u4e00-\u9fa5]", match.group(0)):
            continue
        if name and re.search(r"课程目标|学时|分数|优秀|良好|合格|不合格", name):
            continue
        details.append({"id": f"毕业要求{match.group(1)}", "name": name})
    if details:
        return details
    if 2 <= len(clean_value) <= 12:
        for name, requirement_id in REQUIREMENT_NAME_TO_ID.items():
            if name in clean_value:
                details.append({"id": requirement_id, "name": name})
                break
    return details


def _extract_strengths(value: str) -> list[str]:
    return [match.group(1).upper() for match in re.finditer(r"(?<![A-Za-z])([HML])(?![A-Za-z])", value)]


def _requirement_sort_key(requirement_id: str) -> tuple[float, str]:
    match = re.search(r"(\d+(?:\.\d+)?)", requirement_id)
    return (float(match.group(1)) if match else 999.0, requirement_id)


def _safe_cell_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def _find_catalog_row(
    course_catalog: pd.DataFrame,
    code: str | None,
    name: str | None = None,
    preferred_values: dict[str, object] | None = None,
) -> pd.Series | None:
    if course_catalog.empty or not code:
        return None
    clean_code = _normalize_course_code(code)
    rows = course_catalog[course_catalog["课程代码"].map(_normalize_course_code) == clean_code]
    if rows.empty:
        return None
    selected = rows.iloc[0]
    clean_name = _canonical_compare_text(name)
    if clean_name:
        same_name_rows = course_catalog[course_catalog["课程名称"].map(_canonical_compare_text) == clean_name]
        if len(same_name_rows) > 1 and _canonical_compare_text(selected.get("课程名称", "")) == clean_name:
            best_row = _best_catalog_row(same_name_rows, preferred_values)
            return best_row if best_row is not None else selected
    return selected


def _best_catalog_row(rows: pd.DataFrame, preferred_values: dict[str, object] | None) -> pd.Series | None:
    if rows.empty:
        return None
    if not preferred_values:
        return rows.iloc[0]
    best_row: pd.Series | None = None
    best_score = -1
    for _, row in rows.iterrows():
        score = 0
        for column, actual in preferred_values.items():
            expected = _safe_cell_text(row.get(column, ""))
            if expected and actual is not None and not _values_differ(actual, expected):
                score += 1
        if score > best_score:
            best_score = score
            best_row = row
    return best_row


def _should_skip_week_hour_catalog_compare(
    column: str,
    actual: object,
    category: str | None,
    course_name: str | None,
    expected_category: str | None,
) -> bool:
    if column not in {"总学时", "实践学时"} or not _contains_week_unit(_safe_cell_text(actual)):
        return False
    category_text = _canonical_compare_text(category or expected_category or "")
    name_text = _canonical_compare_text(course_name or "")
    return category_text == "实践教育课程" or any(keyword in name_text for keyword in ["毕业设计", "毕业论文", "毕业实习"])


def _values_differ(actual: object, expected: str) -> bool:
    if actual is None or not expected:
        return False
    if isinstance(actual, (int, float)):
        expected_number = _number(expected)
        return expected_number is not None and abs(float(actual) - expected_number) > 0.001
    actual_text = _safe_cell_text(actual)
    if not actual_text:
        return False
    expected_number = _number(expected)
    actual_number = _number(actual_text)
    if expected_number is not None and actual_number is not None and _looks_numeric(expected):
        if re.search(r"学时|周", actual_text):
            actual_number = _hour_number(actual_text)
            if actual_number is None:
                return False
        return abs(actual_number - expected_number) > 0.001
    return _canonical_compare_text(actual_text) != _canonical_compare_text(expected)


def _display_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return _safe_cell_text(value)


def _looks_numeric(value: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", value.strip()))


def _canonical_compare_text(value: object) -> str:
    return re.sub(r"\s+", "", _safe_cell_text(value))


def _normalize_course_code(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = re.sub(r"\s+", "", str(value))
    if text.endswith(".0"):
        text = text[:-2]
    return re.sub(r"[^0-9A-Za-z]", "", text).upper()


def _normalize_term_value(value: str | None) -> str | None:
    if not value:
        return value
    match = re.search(r"第\s*([1-8一二三四五六七八])\s*学期", value)
    if match:
        return f"第{_chinese_digit_to_int(match.group(1))}学期"
    return value.strip()


def _chinese_digit_to_int(value: str) -> str:
    mapping = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}
    return mapping.get(value, value)


def _normalize_bare_requirement_id(value: str) -> str:
    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*[.．、]?\s*[\u4e00-\u9fa5A-Za-z]", value)
    return f"毕业要求{match.group(1)}" if match else ""


def _find_field_in_tables(text: str, names: list[str]) -> str | None:
    wanted = {_clean_label(name) for name in names}
    label_aliases = {
        "学分": {"学分", "学分"},
        "总学时": {"总学时", "学时"},
        "理论学时": {"理论学时"},
        "实践学时": {"实践学时", "实验学时"},
    }
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        for idx, cell in enumerate(cells):
            clean = _clean_label(cell)
            expanded = set(wanted)
            for name in wanted:
                expanded.update(label_aliases.get(name, set()))
            if clean not in expanded:
                continue
            value = _next_table_value(cells, idx, expanded)
            if value:
                return value
    return None


def _next_table_value(cells: list[str], start: int, labels: set[str]) -> str | None:
    for cell in cells[start + 1 :]:
        value = _strip_embedded_table_label(cell.strip())
        clean = _clean_label(value)
        if not value or clean in labels:
            continue
        if clean in {"开课单位", "课程性质", "学分", "课程类别", "制定时间"}:
            break
        return value
    return None


def _strip_embedded_table_label(value: str) -> str:
    if not value:
        return ""
    match = re.match(r"^[^：:|]{1,20}[：:]\s*(.+)$", value)
    return match.group(1).strip() if match else value


def _clean_label(value: str) -> str:
    return re.sub(r"\s+", "", value or "").replace("：", "").replace(":", "")


def _core_table_status(text: str, titles: list[str] | str, body_keywords: list[str]) -> str:
    title_options = [titles] if isinstance(titles, str) else titles
    if any(title in text for title in title_options):
        return "present"
    if all(keyword in text for keyword in body_keywords):
        return "missing_title"
    return "missing"


def _find_assessment_mode(text: str) -> str | None:
    compact_text = re.sub(r"\s+", "", text)
    match = re.search(r"考核方式为(考试|考查|考核)", compact_text)
    if match:
        return match.group(1)
    for mode in ["考试", "考查", "考核"]:
        if f"考核方式为{mode}" in compact_text:
            return mode
    return None


def _find_attendance_assessment_context(text: str) -> str | None:
    for line in text.splitlines():
        if not re.search(r"出勤|考勤", line):
            continue
        if re.search(r"评分|成绩|考核|权重|%|优秀|良好|合格|不合格|全勤|缺勤|迟到", line):
            return line
    return None


def _find_vague_module_mentions(tables: tuple[tuple[tuple[str, ...], ...], ...]) -> list[str]:
    vague: list[str] = []
    for table in tables:
        rows = [list(row) for row in table]
        content_idx = _assessment_content_index(rows)
        if content_idx is None:
            continue
        for row in rows[1:]:
            content = _safe_cell_text(row[content_idx] if content_idx < len(row) else "")
            if re.fullmatch(r"模块[一二三四五六七八九十\d]+", _canonical_compare_text(content)):
                if content not in vague:
                    vague.append(content)
    return vague


def _assessment_content_index(rows: list[list[str]]) -> int | None:
    for row in rows[:3]:
        for index, cell in enumerate(row):
            clean = _clean_label(cell)
            if clean == "考核内容" or "课程考核内容" in clean:
                return index
    return None


def _count_unique_teaching_modules(text: str) -> int:
    teaching_rows = set()
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        first, second = cells[0], cells[1]
        if re.search(r"模块[一二三四五六七八九十\d]+", first) and re.search(r"第[一二三四五六七八九十\d]+章", second):
            teaching_rows.add(second)
    if teaching_rows:
        return len(teaching_rows)

    knowledge_modules = set(re.findall(r"知识模块[一二三四五六七八九十\d]+", text))
    if knowledge_modules:
        return len(knowledge_modules)

    modules = set()
    for match in re.finditer(r"(?:^|\|)\s*(模块[一二三四五六七八九十\d]+)\s*([^|\n\r]*)", text, flags=re.MULTILINE):
        title = re.sub(r"\s+", "", match.group(1) + match.group(2))
        title = re.sub(r"(讲授|第一章|第二章|第三章|第四章|第五章|第六章|第七章|第八章|第九章|第十章).*", "", title)
        if len(title) > 3:
            modules.add(title)
    if modules:
        return len(modules)
    return len(set(re.findall(r"模块[一二三四五六七八九十\d]+", text)))
