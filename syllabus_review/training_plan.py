from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pypdf import PdfReader

from syllabus_review.parser import ParsedDocument, parse_pdf


@dataclass(frozen=True)
class PlanCourse:
    code: str = ""
    name: str = ""
    term: str = ""
    support: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TrainingPlan:
    program_code: str
    file_path: Path | None
    parsed: ParsedDocument | None
    courses: list[PlanCourse] = field(default_factory=list)
    graduation_requirements: dict[str, str] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.parsed is not None


def load_training_plan(program_code: str, programs_root: Path) -> TrainingPlan:
    path = programs_root / program_code / "training_plan.pdf"
    if not path.exists():
        return TrainingPlan(program_code=program_code, file_path=None, parsed=None)

    parsed = parse_pdf(path.name, path.read_bytes(), parsing_instruction=_plan_parsing_instruction())
    extraction_text = _build_training_plan_extraction_text(path, parsed)
    _write_debug_parse(program_code, extraction_text, programs_root.parent.parent / ".cache" / "training_plans")
    return TrainingPlan(
        program_code=program_code,
        file_path=path,
        parsed=parsed,
        courses=_extract_courses(extraction_text),
        graduation_requirements=_extract_graduation_requirements(extraction_text),
    )


def find_plan_course(plan: TrainingPlan | None, code: str | None, name: str | None) -> PlanCourse | None:
    if not plan:
        return None
    clean_code = _normalize_course_code(code)
    clean_name = _clean(name)
    if clean_code:
        for course in plan.courses:
            if _normalize_course_code(course.code) == clean_code:
                return course
    if clean_name:
        for course in plan.courses:
            if _clean(course.name) == clean_name:
                return course
        for course in plan.courses:
            course_name = _clean(course.name)
            if _is_reasonable_fuzzy_course_match(clean_name, course_name):
                return course
    return None


def _is_reasonable_fuzzy_course_match(query_name: str, course_name: str) -> bool:
    if not query_name or not course_name:
        return False
    shorter = min(len(query_name), len(course_name))
    longer = max(len(query_name), len(course_name))
    if shorter < 6:
        return False
    return (query_name in course_name or course_name in query_name) and shorter / longer >= 0.65


def _plan_parsing_instruction() -> str:
    return (
        "请解析中文本科专业人才培养方案。重点保留：课程设置表、教学进程表、"
        "课程对毕业要求支撑矩阵、毕业要求及指标点内容。"
        "请尽量用 Markdown 表格输出，保留课程代码、课程名称、开课学期、毕业要求编号、"
        "指标点编号、H/M/L 支撑强度和完整指标点文本。"
    )


def _build_training_plan_extraction_text(path: Path, parsed: ParsedDocument) -> str:
    text = parsed.markdown or parsed.text
    layout_text = _extract_pdf_layout_text(path)
    if not layout_text.strip():
        return text
    return f"{text}\n\n<!-- PDF_LAYOUT_TEXT -->\n\n{layout_text}"


def _extract_pdf_layout_text(path: Path) -> str:
    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
    except Exception:
        return ""
    return "\n\n".join(page for page in pages if page.strip())


def _extract_courses(markdown: str) -> list[PlanCourse]:
    courses: dict[str, PlanCourse] = {}
    for table in _markdown_tables(markdown):
        headers, rows = table
        code_idx = _find_header(headers, ["课程代码", "课程编号", "代码"])
        name_idx = _find_header(headers, ["课程名称", "课程名"])
        term_idx = _find_header(headers, ["开课学期", "建议修读学期", "学期"])
        support_columns = _support_columns(headers)
        if code_idx is None and name_idx is None:
            continue

        for row in rows:
            code = _cell(row, code_idx)
            name = _cell(row, name_idx)
            if not code and not name:
                continue
            if not _looks_like_course(code, name):
                continue
            key = code or name
            existing = courses.get(key, PlanCourse(code=code, name=name))
            support = dict(existing.support)
            for idx, requirement_id in support_columns.items():
                strength = _normalize_strength(_cell(row, idx))
                if strength:
                    support[requirement_id] = strength
            term = _normalize_term(_cell(row, term_idx)) or existing.term
            _merge_course(
                courses,
                PlanCourse(
                    code=existing.code or code,
                    name=existing.name or name,
                    term=term,
                    support=support,
                ),
            )

    for course in _extract_courses_from_layout_text(markdown):
        _merge_course(courses, course)
    for course in _extract_support_matrix_from_layout_text(markdown):
        _merge_course(courses, course)
    for course in _extract_courses_from_text(markdown):
        _merge_course(courses, course)

    return list(courses.values())


def _merge_course(courses: dict[str, PlanCourse], course: PlanCourse) -> None:
    key = course.code or course.name
    if not key:
        return
    existing_key = _find_existing_course_key(courses, course)
    if existing_key is None:
        courses[key] = course
        return
    existing = courses[existing_key]
    support = dict(existing.support)
    support.update({k: v for k, v in course.support.items() if v})
    courses[existing_key] = PlanCourse(
        code=existing.code or course.code,
        name=existing.name or course.name,
        term=existing.term or course.term,
        support=support,
    )


def _find_existing_course_key(courses: dict[str, PlanCourse], course: PlanCourse) -> str | None:
    clean_code = _normalize_course_code(course.code)
    clean_name = _clean(course.name)
    for key, existing in courses.items():
        if clean_code and _normalize_course_code(existing.code) == clean_code:
            return key
        if clean_name and _clean(existing.name) == clean_name:
            return key
    return None


def _extract_graduation_requirements(markdown: str) -> dict[str, str]:
    requirements: dict[str, str] = {}

    for table in _markdown_tables(markdown):
        headers, rows = table
        id_idx = _find_header(headers, ["毕业要求", "指标点", "编号"])
        content_idx = _find_header(headers, ["毕业要求指标点内容", "指标点内容", "具体内容", "内容"])
        if id_idx is None or content_idx is None:
            continue
        for row in rows:
            requirement_id = _normalize_requirement_id(_cell(row, id_idx))
            content = _cell(row, content_idx)
            if requirement_id and content:
                requirements[requirement_id] = content

    pattern = re.compile(
        r"(毕业要求\s*\d+(?:\.\d+)?|指标点\s*\d+(?:\.\d+)?)\s*[:：]?\s*([^\n\r]+(?:\n(?!毕业要求\s*\d|指标点\s*\d)[^\n\r]+)*)"
    )
    for raw_id, raw_content in pattern.findall(markdown):
        requirement_id = _normalize_requirement_id(raw_id)
        content = re.sub(r"\s+", " ", raw_content).strip()
        if requirement_id and content and requirement_id not in requirements:
            requirements[requirement_id] = content

    fallback_pattern = re.compile(r"(?m)^\s*(\d+(?:\.\d+)?)\s+([^\n\r]{12,})$")
    for raw_id, raw_content in fallback_pattern.findall(markdown):
        if not _looks_like_requirement_content(raw_content):
            continue
        requirement_id = f"毕业要求{raw_id}"
        if requirement_id not in requirements:
            requirements[requirement_id] = re.sub(r"\s+", " ", raw_content).strip()

    return requirements


def _extract_courses_from_text(text: str) -> list[PlanCourse]:
    courses: list[PlanCourse] = []
    pattern = re.compile(
        r"(?P<code>[A-Z]{1,6}\d{2,8}[A-Z0-9-]*)\s+"
        r"(?P<name>[\u4e00-\u9fa5A-Za-z0-9（）()《》·、\-]{2,40})"
        r"(?P<trailing>[^\n\r]{0,120})"
    )
    for match in pattern.finditer(text):
        name = match.group("name").strip()
        trailing = match.group("trailing")
        if re.search(r"课程代码|课程名称|毕业要求|合计|小计", name):
            continue
        courses.append(
            PlanCourse(
                code=match.group("code").strip(),
                name=name,
                term=_extract_term_from_text(trailing),
                support=_extract_support_from_text(trailing),
            )
        )
    seen = {_clean(course.name) for course in courses}
    line_pattern = re.compile(
        r"(?m)^\s*(?:[\u4e00-\u9fa5]{0,10})?(?:必修|选修)?\s*\d{1,3}\s*"
        r"(?P<name>[\u4e00-\u9fa5A-Za-z（）()《》·、]{2,40}?)(?=\d|∆|√|考试|考查|[HML]\b)"
    )
    for match in line_pattern.finditer(text):
        name = match.group("name").strip()
        if not _looks_like_course("", name):
            continue
        clean_name = _clean(name)
        if clean_name in seen:
            continue
        seen.add(clean_name)
        courses.append(PlanCourse(name=name))
    return courses


def _extract_courses_from_layout_text(text: str) -> list[PlanCourse]:
    courses: list[PlanCourse] = []
    term_columns: list[tuple[int, str]] = []
    for line in text.splitlines():
        detected_columns = _detect_term_columns(line)
        if detected_columns:
            term_columns = detected_columns
            continue
        if not term_columns:
            continue
        course = _extract_layout_course_row(line, term_columns)
        if course:
            courses.append(course)
    return courses


def _extract_support_matrix_from_layout_text(text: str) -> list[PlanCourse]:
    courses: list[PlanCourse] = []
    support_columns: list[tuple[int, str]] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        detected_columns = _detect_support_columns(line)
        if detected_columns:
            support_columns = detected_columns
            continue
        if not support_columns:
            continue
        course = _extract_support_matrix_row(_support_matrix_candidate_line(lines, index), support_columns)
        if course:
            courses.append(course)
    return courses


def _detect_term_columns(line: str) -> list[tuple[int, str]]:
    matches = list(re.finditer(r"([1-8])\s*学期", line))
    if len(matches) < 4:
        return []
    return [(match.start(), f"第{match.group(1)}学期") for match in matches]


def _detect_support_columns(line: str) -> list[tuple[int, str]]:
    labels = "思想|学科|应用|创新|信息|沟通|团队|国际|学习|思|学|应|创|信|沟|团|国|习"
    matches = list(re.finditer(r"([1-9])\s*[.．]?\s*(?:" + labels + r")", line))
    if len(matches) < 5:
        return []
    return [(match.start(), f"毕业要求{match.group(1)}") for match in matches]


def _extract_layout_course_row(line: str, term_columns: list[tuple[int, str]]) -> PlanCourse | None:
    if "√" not in line and "∆" not in line and "△" not in line:
        return None
    if re.search(r"小计|合计|总计|注：|教学方式|考核方式", line):
        return None
    match = re.search(
        r"^\s*(?:[\u4e00-\u9fa5]{1,8})?\s*(?:必修|选修)?\s*\d{1,3}\s+"
        r"(?P<name>[\u4e00-\u9fa5A-Za-z0-9（）()《》“”·、+\-\s]+?)\s{2,}"
        r"\d+(?:\.\d+)?\s+",
        line,
    )
    if not match:
        return None
    name = match.group("name").strip()
    if not _looks_like_course("", name):
        return None
    marker_positions = [pos for pos in (line.rfind("√"), line.rfind("∆"), line.rfind("△")) if pos >= 0]
    marker_pos = max(marker_positions) if marker_positions else match.end()
    scheduled_numbers = [
        number_match
        for number_match in re.finditer(r"\d+(?:\.\d+)?", line)
        if number_match.start() > marker_pos
    ]
    term = _nearest_term_for_position(scheduled_numbers[-1].start(), term_columns) if scheduled_numbers else ""
    return PlanCourse(name=name, term=term)


def _extract_support_matrix_row(line: str, support_columns: list[tuple[int, str]]) -> PlanCourse | None:
    if not re.search(r"\b[HML]\b", line):
        return None
    if re.search(r"小计|合计|总计|注：|毕业要求|培养目标", line):
        return None
    match = re.search(
        r"^\s*\d{1,3}\s*"
        r"(?P<name>[\u4e00-\u9fa5A-Za-z0-9（）()《》“”·、+#*\-\s]+?)\s{2,}(?=[HML\s]+$)",
        line,
    )
    if not match:
        match = re.search(
            r"^\s*\d{1,3}\s*"
            r"(?P<name>[\u4e00-\u9fa5A-Za-z0-9（）()《》“”·、+#*\-\s]+?)\s{2,}",
            line,
        )
    if not match:
        return None
    name = _clean(match.group("name")).rstrip("#*")
    if not _looks_like_course("", name):
        return None
    support: dict[str, str] = {}
    for strength_match in re.finditer(r"\b([HML])\b", line):
        if strength_match.start() < match.end():
            continue
        requirement_id = _nearest_support_for_position(strength_match.start(), support_columns)
        if requirement_id:
            support[requirement_id] = strength_match.group(1)
    if not support:
        return None
    return PlanCourse(name=name, support=support)


def _support_matrix_candidate_line(lines: list[str], index: int) -> str:
    line = lines[index]
    if re.search(r"\b[HML]\b", line):
        return line
    if index + 1 >= len(lines) or not re.search(r"\b[HML]\b", lines[index + 1]):
        return line
    prefix = _clean_wrapped_course_fragment(line)
    if not prefix:
        return line
    suffix = _clean_wrapped_course_fragment(lines[index + 2], min_length=1) if index + 2 < len(lines) else ""
    name = prefix + suffix
    if not _looks_like_course("", name):
        return line
    support_line = lines[index + 1]
    strength_match = re.search(r"\b[HML]\b", support_line)
    number_match = re.match(r"^(\s*\d{1,3})", support_line)
    if not strength_match or not number_match:
        return line
    head = f"{number_match.group(1)}   {name}"
    strength_start = strength_match.start()
    if len(head) >= strength_start:
        return f"{head}  {support_line[strength_start:]}"
    return head + (" " * (strength_start - len(head))) + support_line[strength_start:]


def _clean_wrapped_course_fragment(line: str, *, min_length: int = 2) -> str:
    text = re.sub(r"\s+", "", line or "").strip()
    if not text or re.search(r"\b[HML]\b|毕业要求|课程名称|小计|合计|总计", text):
        return ""
    text = re.sub(r"^\d{1,3}", "", text)
    if len(text) < min_length:
        return ""
    return text if re.search(r"[\u4e00-\u9fa5A-Za-z]", text) else ""


def _nearest_term_for_position(position: int, term_columns: list[tuple[int, str]]) -> str:
    nearest_position, nearest_term = min(term_columns, key=lambda item: abs(item[0] - position))
    if abs(nearest_position - position) > 8:
        return ""
    return nearest_term


def _nearest_support_for_position(position: int, support_columns: list[tuple[int, str]]) -> str:
    nearest_position, nearest_requirement = min(support_columns, key=lambda item: abs(item[0] - position))
    if abs(nearest_position - position) > 5:
        return ""
    return nearest_requirement


def _extract_term_from_text(text: str) -> str:
    match = re.search(r"(?:第?\s*)?([1-8一二三四五六七八])\s*学期", text)
    if match:
        return f"第{_chinese_digit_to_int(match.group(1))}学期"
    tokens = re.findall(r"(?<!\d)([1-8])(?!\d)", text)
    return f"第{tokens[-1]}学期" if tokens else ""


def _extract_support_from_text(text: str) -> dict[str, str]:
    support: dict[str, str] = {}
    for number, strength in re.findall(r"(?:毕业要求)?\s*(\d+(?:\.\d+)?)\s*[:：]?\s*([HML])", text, flags=re.IGNORECASE):
        support[f"毕业要求{number}"] = strength.upper()
    return support


def _looks_like_requirement_content(content: str) -> bool:
    return bool(re.search(r"能力|素养|知识|掌握|具备|能够|理解|分析|应用|解决|评价|创新", content))


def _markdown_tables(markdown: str) -> list[tuple[list[str], list[list[str]]]]:
    tables: list[tuple[list[str], list[list[str]]]] = []
    current: list[str] = []
    for line in markdown.splitlines():
        if "|" in line and line.strip().startswith("|"):
            current.append(line.strip())
            continue
        if current:
            parsed = _parse_table(current)
            if parsed:
                tables.append(parsed)
            current = []
    if current:
        parsed = _parse_table(current)
        if parsed:
            tables.append(parsed)
    return tables


def _parse_table(lines: list[str]) -> tuple[list[str], list[list[str]]] | None:
    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]
    rows = [row for row in rows if row and not all(_is_separator_cell(cell) for cell in row)]
    if len(rows) < 2:
        return None
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    return normalized[0], normalized[1:]


def _is_separator_cell(cell: str) -> bool:
    return bool(re.fullmatch(r":?-{3,}:?", cell.strip()))


def _find_header(headers: list[str], keywords: list[str]) -> int | None:
    for idx, header in enumerate(headers):
        clean = _clean(header)
        if any(_clean(keyword) in clean for keyword in keywords):
            return idx
    return None


def _support_columns(headers: list[str]) -> dict[int, str]:
    columns: dict[int, str] = {}
    for idx, header in enumerate(headers):
        requirement_id = _normalize_requirement_id(header)
        if requirement_id:
            columns[idx] = requirement_id
    return columns


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def _looks_like_course(code: str, name: str) -> bool:
    if code and re.search(r"[A-Za-z0-9]{3,}", code):
        return True
    return bool(name and len(name) >= 2 and not re.search(r"合计|小计|课程名称|毕业要求", name))


def _normalize_term(value: str) -> str:
    value = _clean(value)
    if not value:
        return ""
    match = re.search(r"第?([1-8一二三四五六七八])学期", value)
    if match:
        return f"第{_chinese_digit_to_int(match.group(1))}学期"
    if re.fullmatch(r"[1-8一二三四五六七八]", value):
        return f"第{_chinese_digit_to_int(value)}学期"
    return value


def _normalize_strength(value: str) -> str:
    value = value.strip().upper()
    return value if value in {"H", "M", "L"} else ""


def _normalize_requirement_id(value: str) -> str:
    value = _clean(value)
    match = re.search(r"(?:毕业要求|指标点)?(\d+(?:\.\d+)?)", value)
    if not match:
        return ""
    number = match.group(1)
    return f"毕业要求{number}"


def _clean(value: str | None) -> str:
    text = re.sub(r"\s+", "", str(value or "")).strip()
    return re.sub(r"[“”‘’\"'《》#*]", "", text)


def _normalize_course_code(value: str | None) -> str:
    text = _clean(value)
    if text.endswith(".0"):
        text = text[:-2]
    return re.sub(r"[^0-9A-Za-z]", "", text).upper()


def _chinese_digit_to_int(value: str) -> str:
    mapping = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8"}
    return mapping.get(value, value)


def _write_debug_parse(program_code: str, text: str, cache_root: Path) -> None:
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        (cache_root / f"{program_code}.md").write_text(text, encoding="utf-8")
    except OSError:
        return
