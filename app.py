from __future__ import annotations

import os
import re
import time
from pathlib import Path

import streamlit as st
import streamlit

from syllabus_review.config import AppConfig, load_app_config, load_programs
from syllabus_review.data_loader import load_course_catalog
from syllabus_review.parser import parse_document
from syllabus_review.report import (
    build_combined_pdf_report,
    build_markdown_report,
    build_pdf_report,
    combined_report_pdf_filename,
    report_pdf_filename,
)
from syllabus_review.rules import Issue, ReviewContext, ReviewResult, run_compliance_review
from syllabus_review.training_plan import load_training_plan


ROOT = Path(__file__).parent
LOCAL_REVIEW_MAX_FILES = 100
AI_REVIEW_MAX_FILES = 10
SPECIAL_PLATFORM_COURSE_PROGRAMS = {
    "习近平经济思想概论": ("jinrong", "guomao"),
}


def patch_streamlit_uploader_frontend() -> None:
    static_root = Path(streamlit.__file__).parent / "static" / "static" / "js"
    if not static_root.exists():
        return
    for path in static_root.glob("*.js"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        updated = text
        updated = updated.replace(
            'children:"Showing page ".concat(i," of ").concat(s)',
            'children:"第 ".concat(i," 页，共 ").concat(s," 页")',
        )
        updated = updated.replace(
            "a.length>0&&(0,O.jsx)(pe,{items:a,pageSize:3,onDelete:this.deleteFile,resetOnAdd:!0})",
            "a.length>0&&(0,O.jsx)(pe,{items:a,pageSize:10,onDelete:this.deleteFile,resetOnAdd:!0})",
        )
        if updated != text:
            try:
                path.write_text(updated, encoding="utf-8")
            except OSError:
                return


patch_streamlit_uploader_frontend()


st.set_page_config(
    page_title="西京学院课程教学大纲自动化审查系统",
    page_icon="📄",
    layout="wide",
)


@st.cache_data(show_spinner=False)
def cached_config() -> AppConfig:
    return load_app_config(ROOT / "config" / "app.yaml")


def programs_fingerprint() -> tuple[tuple[str, float], ...]:
    root = ROOT / "data" / "programs"
    return tuple(sorted((str(path), path.stat().st_mtime) for path in root.glob("*/program.yaml")))


def catalog_fingerprint() -> tuple[tuple[str, float], ...]:
    candidates = [ROOT / "data" / "course_codes.xlsx", ROOT / "data" / "course_code.xlsx"]
    return tuple((str(path), path.stat().st_mtime) for path in candidates if path.exists())


def training_plan_fingerprint(program_code: str) -> tuple[tuple[str, float], ...]:
    candidates = [
        ROOT / "data" / "programs" / program_code / "training_plan.pdf",
        ROOT / "syllabus_review" / "training_plan.py",
        ROOT / "syllabus_review" / "parser.py",
    ]
    return tuple((str(path), path.stat().st_mtime) for path in candidates if path.exists())


def training_plans_fingerprint(program_codes: tuple[str, ...]) -> tuple[tuple[str, float], ...]:
    candidates = [
        ROOT / "syllabus_review" / "training_plan.py",
        ROOT / "syllabus_review" / "parser.py",
    ]
    for code in program_codes:
        candidates.append(ROOT / "data" / "programs" / code / "training_plan.pdf")
    return tuple((str(path), path.stat().st_mtime) for path in candidates if path.exists())


@st.cache_data(show_spinner=False)
def cached_programs(fingerprint: tuple[tuple[str, float], ...]):
    return load_programs(ROOT / "data" / "programs")


@st.cache_data(show_spinner=False)
def cached_catalog(fingerprint: tuple[tuple[str, float], ...]):
    return load_course_catalog(ROOT / "data")


@st.cache_data(show_spinner=False)
def cached_training_plan(program_code: str, fingerprint: tuple[tuple[str, float], ...]):
    return load_training_plan(program_code, ROOT / "data" / "programs")


@st.cache_data(show_spinner=False)
def cached_training_plans(program_codes: tuple[str, ...], fingerprint: tuple[tuple[str, float], ...]):
    return {code: load_training_plan(code, ROOT / "data" / "programs") for code in program_codes}


def review_program_codes(program) -> tuple[str, ...]:
    return program.related_programs if program.is_platform else (program.code,)


def review_program_codes_for_document(program, text: str, programs: dict[str, object]) -> tuple[str, ...]:
    if not program.is_platform:
        return (program.code,)
    configured_codes = tuple(program.related_programs)
    applicable_codes = extract_applicable_program_codes(text, programs, configured_codes)
    return applicable_codes or configured_codes


def extract_applicable_program_codes(text: str, programs: dict[str, object], allowed_codes: tuple[str, ...]) -> tuple[str, ...]:
    normalized_text = normalize_program_detection_text(text)
    for course_name, program_codes in SPECIAL_PLATFORM_COURSE_PROGRAMS.items():
        if normalize_program_detection_text(course_name) in normalized_text:
            return tuple(code for code in program_codes if code in allowed_codes)

    scope = extract_applicable_program_scope(text)
    if not scope:
        return ()
    normalized_scope = normalize_program_detection_text(scope)
    matches: list[str] = []
    for code in allowed_codes:
        program = programs.get(code)
        if not program:
            continue
        normalized_name = normalize_program_detection_text(program.name)
        if normalized_name and normalized_name in normalized_scope:
            matches.append(code)
    return tuple(dict.fromkeys(matches))


def extract_applicable_program_scope(text: str) -> str:
    table_scope = extract_table_field_values(text, ["适用专业", "制定依据"])
    if table_scope:
        return table_scope
    normalized = re.sub(r"[ \t]+", " ", text or "")
    patterns = [
        r"适用专业\s*[:：]?\s*(.{0,500}?)(?:\n\s*(?:制定依据|二、|三、|课程目标|课程基本信息|表\s*\d)|$)",
        r"制定依据\s*[:：]?\s*(.{0,800}?)(?:\n\s*(?:二、|三、|课程目标|课程基本信息|表\s*\d)|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.S)
        if match:
            return match.group(1)
    return ""


def extract_table_field_values(text: str, labels: list[str]) -> str:
    wanted = {normalize_program_detection_text(label) for label in labels}
    stop_labels = {
        normalize_program_detection_text(label)
        for label in [
            "课程名称",
            "课程代码",
            "课程类别",
            "课程性质",
            "开课学期",
            "学分",
            "总学时",
            "理论学时",
            "实践学时",
            "先修课程",
            "后续课程",
            "后修课程",
            "制定时间",
        ]
    }
    values: list[str] = []
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        for index, cell in enumerate(cells):
            if normalize_program_detection_text(cell) not in wanted:
                continue
            for value in cells[index + 1 :]:
                clean_value = value.strip()
                normalized_value = normalize_program_detection_text(clean_value)
                if not clean_value or normalized_value in wanted:
                    continue
                if normalized_value in stop_labels:
                    break
                if clean_value not in values:
                    values.append(clean_value)
            if values:
                return "；".join(values)
    return ""


def detect_program_code_from_document(text: str, programs: dict[str, object]) -> str | None:
    basis = extract_training_basis_text(text)
    search_text = basis or text
    matched_codes: list[str] = []
    normalized_search = normalize_program_detection_text(search_text)
    for code, program in programs.items():
        if program.is_platform:
            continue
        normalized_name = normalize_program_detection_text(program.name)
        if normalized_name and normalized_name in normalized_search:
            matched_codes.append(code)

    matched_codes = list(dict.fromkeys(matched_codes))
    if not matched_codes:
        return None
    if len(matched_codes) == 1:
        return matched_codes[0]

    platform_code = find_platform_program_for_matches(matched_codes, programs)
    if platform_code:
        return platform_code
    return None


def extract_training_basis_text(text: str) -> str:
    normalized = re.sub(r"[ \t]+", " ", text or "")
    patterns = [
        r"制定依据\s*[:：]?\s*(.{0,500}?)(?:\n\s*(?:二、|三、|课程目标|课程基本信息|表\s*\d)|$)",
        r"依据\s*[:：]?\s*(.{0,300}?人才培养方案.{0,120})",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.S)
        if match:
            return match.group(1)
    plan_mentions = re.findall(r"20\d{2}\s*版[^。\n\r；;]{0,30}?专业人才培养方案", normalized)
    return "；".join(plan_mentions)


def normalize_program_detection_text(value: str) -> str:
    value = value or ""
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[《》（）()、，,。；;：:\"“”'‘’\-—_]", "", value)
    value = value.replace("管理应用", "管理与应用")
    return value


def find_platform_program_for_matches(matched_codes: list[str], programs: dict[str, object]) -> str | None:
    matched = set(matched_codes)
    for code, program in programs.items():
        if not program.is_platform:
            continue
        related = set(program.related_programs)
        if matched and matched.issubset(related):
            return code
    return None


def configure_api_keys() -> None:
    for key in ["LLAMA_CLOUD_API_KEY", "DEEPSEEK_API_KEY"]:
        if not os.getenv(key) and key in st.secrets:
            os.environ[key] = str(st.secrets[key])


def show_ai_review_notice_dialog() -> None:
    dialog = getattr(st, "dialog", None) or getattr(st, "experimental_dialog", None)
    if dialog is None:
        st.warning("内容深度审查目前处于测试阶段，反馈意见仅供参考。")
        st.session_state["ai_review_notice_ack"] = True
        return

    @dialog("内容深度审查提示")
    def _notice() -> None:
        st.write("该功能目前处于测试阶段，反馈意见仅供参考。开启后系统会调用 DeepSeek API，对错别字与语句质量、课程目标可评价性、目标-内容-考核匹配度、课程思政融入质量、实践类课程产出设计、标题与内容具体性进行简要语义审查，单次最多上传 10 个文件。")
        if st.button("我知道了", type="primary", use_container_width=True):
            st.session_state["ai_review_notice_ack"] = True
            st.rerun()

    _notice()


def render_review_reports(reports: list[tuple[str, ReviewResult, str]], program_name: str) -> None:
    if len(reports) > 1:
        combined_pdf = build_combined_pdf_report([result for _, result, _ in reports], program_name)
        st.download_button(
            "下载全部审查意见 PDF",
            data=combined_pdf,
            file_name=combined_report_pdf_filename(program_name),
            mime="application/pdf",
            key="combined-pdf",
            use_container_width=True,
        )

    for file_name, result, markdown in reports:
        with st.expander(file_name, expanded=len(reports) == 1):
            st.markdown(markdown)
            pdf = build_pdf_report(result)
            st.download_button(
                "下载 PDF 报告",
                data=pdf,
                file_name=report_pdf_filename(result),
                mime="application/pdf",
                key=f"pdf-{file_name}",
            )


def main() -> None:
    configure_api_keys()
    config = cached_config()
    programs = cached_programs(programs_fingerprint())

    inject_styles()

    st.markdown(
        """
        <section class="hero">
          <div>
            <p class="eyebrow">Xijing University Business School</p>
            <h1>西京学院课程教学大纲自动化审查系统</h1>
            <p class="subtitle">本系统面向西京学院2026版教学大纲初审，集成人工智能技术，旨在实现格式校对、综合意见生成、合规性审查和深度审查。作为一个正式的系统说明，本系统致力于通过自动化的流程，为教学大纲的审核提供全方位的技术支撑，确保大纲内容的规范性与专业性。</p>
          </div>
        </section>
        """,
        unsafe_allow_html=True,
    )

    metrics = st.columns(4)
    metrics[0].metric("已配置专业", f"{len(programs)} 个")
    metrics[1].metric("上传上限", f"{LOCAL_REVIEW_MAX_FILES} 份 / 深度审查 {AI_REVIEW_MAX_FILES} 份")
    metrics[2].metric("单文件上限", f"{config.max_file_mb} MB")
    metrics[3].metric("支持格式", "PDF/DOCX/DOC")
    st.markdown('<footer class="app-footer">© 西京学院商学院本科教学中心 2026 powerby Streamlit</footer>', unsafe_allow_html=True)

    left, right = st.columns([0.35, 0.65], gap="large")
    with left:
        st.markdown("### 审查设置")
        if "uploader_version" not in st.session_state:
            st.session_state["uploader_version"] = 0
        if st.button("刷新专业和配置", use_container_width=True):
            st.cache_data.clear()
            st.session_state["uploader_version"] += 1
            st.session_state.pop("last_reports", None)
            st.session_state.pop("last_reports_program", None)
            st.rerun()

        program_options = ["auto", *programs.keys()]
        selected_program_code = st.selectbox(
            "审查专业",
            options=program_options,
            format_func=lambda code: "自动识别（推荐）" if code == "auto" else programs[code].name,
            help="默认根据教学大纲“一、课程基本信息”中的“制定依据”自动匹配专业；识别失败时可手动指定专业。",
        )

        enable_ai = st.toggle("启用内容深度审查", value=False, help="开启后将调用 DeepSeek API。")
        previous_enable_ai = st.session_state.get("previous_ai_review_enabled", False)
        if enable_ai and not previous_enable_ai:
            st.session_state["ai_review_notice_ack"] = False
        if not enable_ai:
            st.session_state["ai_review_notice_ack"] = False
        st.session_state["previous_ai_review_enabled"] = enable_ai

        if enable_ai and not st.session_state.get("ai_review_notice_ack", False):
            show_ai_review_notice_dialog()

        max_files = AI_REVIEW_MAX_FILES if enable_ai else LOCAL_REVIEW_MAX_FILES
        st.caption("开启后会聚焦错别字与语句质量、课程目标可评价性、目标-内容-考核匹配度、课程思政融入质量、实践类课程产出设计、标题与内容具体性，并输出一段简要意见；关闭时仅执行本地规则审查，不消耗 API 额度。")

        files = st.file_uploader(
            "上传教学大纲文件",
            type=["pdf", "docx", "doc"],
            accept_multiple_files=True,
            help=f"支持 PDF、DOCX、DOC。当前模式最多 {max_files} 个文件，单个文件不超过 {config.max_file_mb}MB。",
            key=f"syllabus_uploader_{st.session_state['uploader_version']}",
        )

        start = st.button("开始审查", type="primary", use_container_width=True)

    with right:
        st.markdown("### 审查输出")
        if not start:
            saved_reports = st.session_state.get("last_reports")
            if saved_reports:
                render_review_reports(saved_reports, st.session_state.get("last_reports_program", "自动识别"))
                return
            st.info("上传教学大纲文件后，系统会根据“制定依据”自动识别专业并开始审查。")
            return

        if not files:
            st.error("请至少上传 1 个教学大纲文件。")
            return
        if len(files) > max_files:
            st.error(f"当前模式一次最多支持 {max_files} 个文件。关闭内容深度审查时最多 {LOCAL_REVIEW_MAX_FILES} 个，开启内容深度审查时最多 {AI_REVIEW_MAX_FILES} 个。")
            return

        invalid = [f.name for f in files if f.size > config.max_file_mb * 1024 * 1024]
        if invalid:
            st.error("以下文件超过大小限制：" + "、".join(invalid))
            return

        with st.spinner("正在加载课程总表..."):
            course_catalog = cached_catalog(catalog_fingerprint())
        if course_catalog.empty:
            st.warning("未找到全院课程总表，将跳过课程代码和课程基本信息的精确比对。请将课程总表放在 data/course_codes.xlsx。")
        progress = st.progress(0)
        status = st.empty()
        reports: list[tuple[str, ReviewResult, str]] = []
        program_load_messages: set[str] = set()
        started_at = time.time()

        with st.spinner("正在审查教学大纲，请稍候..."):
            for index, uploaded in enumerate(files, start=1):
                status.markdown(f'<div class="review-status"><span></span>正在解析与审查：{uploaded.name}</div>', unsafe_allow_html=True)
                try:
                    parsed = parse_document(uploaded.name, uploaded.getvalue())
                except Exception as exc:
                    fallback_program = programs[next(iter(programs))]
                    result = ReviewResult(
                        file_name=uploaded.name,
                        program=fallback_program,
                        parser_name="解析失败",
                        compliance_issues=[],
                        ai_review=f"文件解析失败：{exc}",
                    )
                    reports.append((uploaded.name, result, build_markdown_report(result)))
                    progress.progress(index / len(files), text=f"已完成 {index}/{len(files)}")
                    continue

                detected_program_code = detect_program_code_from_document(parsed.text, programs)
                if selected_program_code != "auto":
                    program_code = selected_program_code
                    if detected_program_code and detected_program_code != selected_program_code:
                        st.info(f"{uploaded.name}：系统从制定依据识别为“{programs[detected_program_code].name}”，本次按手动指定的“{programs[program_code].name}”审查。")
                else:
                    program_code = detected_program_code or ""
                if not program_code or program_code not in programs:
                    fallback_program = programs[next(iter(programs))]
                    result = ReviewResult(
                        file_name=uploaded.name,
                        program=fallback_program,
                        parser_name=parsed.parser_name,
                        compliance_issues=[
                            Issue(
                                "R-PROGRAM-001",
                                "高",
                                "制定依据",
                                "未能从“制定依据”中识别出适用专业。",
                                "请检查大纲是否写明“2026版××专业人才培养方案”，或在审查设置中手动指定专业后重新审查。",
                            )
                        ],
                    )
                    reports.append((uploaded.name, result, build_markdown_report(result)))
                    progress.progress(index / len(files), text=f"已完成 {index}/{len(files)}")
                    continue

                program = programs[program_code]
                related_program_codes = review_program_codes_for_document(program, parsed.text, programs)
                training_plans = cached_training_plans(related_program_codes, training_plans_fingerprint(related_program_codes))
                training_plan = training_plans.get(program_code) if not program.is_platform else None

                load_message_key = ",".join(related_program_codes)
                if load_message_key not in program_load_messages:
                    program_load_messages.add(load_message_key)
                    available_plans = [plan for plan in training_plans.values() if plan.available]
                    if program.is_platform:
                        missing = [programs[code].name if code in programs else code for code, plan in training_plans.items() if not plan.available]
                        if missing:
                            st.warning("以下关联专业的人才培养方案未找到，将跳过对应专业审查：" + "、".join(missing))
                        elif len(available_plans) == len(related_program_codes):
                            st.success(f"{program.name}已加载 {len(available_plans)} 个人才培养方案。")
                        else:
                            st.warning("部分关联专业人才培养方案未能加载。")
                    elif not training_plan or not training_plan.available:
                        st.warning(f"{program.name}人才培养方案 PDF 未找到，将跳过人才培养方案一致性审查。")
                    elif not training_plan.courses or not training_plan.graduation_requirements:
                        st.warning(f"{program.name}人才培养方案 PDF 已读取，但部分表格尚未稳定识别为结构化数据。系统会继续完成本次审查，并保留解析文本用于后续优化。")
                    else:
                        st.success(f"已自动匹配专业：{program.name}。人才培养方案已加载。")

                context = ReviewContext(
                    program=program,
                    course_catalog=course_catalog,
                    parsed=parsed,
                    training_plan=training_plan,
                    training_plans=training_plans,
                    program_names={code: config.name for code, config in programs.items()},
                    platform_related_programs=related_program_codes if program.is_platform else (),
                    enable_ai_review=enable_ai,
                )
                result = run_compliance_review(context)
                markdown = build_markdown_report(result)
                reports.append((uploaded.name, result, markdown))

                elapsed = time.time() - started_at
                remaining = (elapsed / index) * (len(files) - index) if index else 0
                progress.progress(index / len(files), text=f"已完成 {index}/{len(files)}，预计剩余 {remaining:.0f} 秒")

        status.success("审查完成")
        st.session_state["last_reports"] = reports
        st.session_state["last_reports_program"] = "自动识别"
        render_review_reports(reports, "自动识别")


def inject_styles() -> None:
    st.markdown(
        """
        <style>
        :root {
            --app-bg: #f8fafc;
            --surface: #ffffff;
            --surface-soft: #f1f5f9;
            --surface-hover: #e2e8f0;
            --border: #e5e7eb;
            --text: #0f172a;
            --muted: #475569;
            --accent-soft: #eff6ff;
            --accent-border: #dbeafe;
            --accent-text: #1e3a8a;
        }
        @media (prefers-color-scheme: dark) {
            :root {
                --app-bg: #0f1117;
                --surface: #171b24;
                --surface-soft: #242936;
                --surface-hover: #303747;
                --border: #3b4252;
                --text: #f8fafc;
                --muted: #cbd5e1;
                --accent-soft: #172b45;
                --accent-border: #31537a;
                --accent-text: #dbeafe;
            }
        }
        .stApp {
            background: var(--app-bg);
            color: var(--text);
            padding-bottom: 110px;
        }
        .app-footer {
            position: fixed;
            left: 0;
            right: 0;
            bottom: 0;
            z-index: 999;
            padding: 7px 16px;
            border-top: 1px solid var(--border);
            background: var(--surface);
            color: var(--muted);
            font-size: .82rem;
            text-align: center;
        }
        .block-container {
            padding-top: 1.6rem;
            padding-bottom: 120px !important;
            max-width: 1240px;
        }
        .hero {
            min-height: 210px;
            padding: 34px 38px;
            margin-bottom: 22px;
            color: #f8fafc;
            background:
                linear-gradient(120deg, rgba(15, 23, 42, .92), rgba(30, 64, 175, .70)),
                url("https://images.unsplash.com/photo-1497366754035-f200968a6e72?auto=format&fit=crop&w=1800&q=80");
            background-size: cover;
            background-position: center;
            border-radius: 8px;
            display: flex;
            align-items: end;
        }
        .hero h1 {
            font-size: 2.25rem;
            line-height: 1.15;
            margin: .15rem 0 .55rem;
            letter-spacing: 0;
            color: #ffffff !important;
            text-shadow: 0 2px 12px rgba(0,0,0,.35);
        }
        .eyebrow {
            margin: 0;
            color: #bfdbfe;
            font-size: .82rem;
            text-transform: uppercase;
        }
        .subtitle {
            max-width: 820px;
            margin: 0;
            color: #e5e7eb;
            font-size: 1.02rem;
            line-height: 1.7;
        }
        div[data-testid="stMetric"] {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px 14px;
            box-shadow: 0 1px 2px rgba(15, 23, 42, .06);
        }
        div[data-testid="stMetric"] * {
            color: var(--text) !important;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.16rem;
            line-height: 1.2;
        }
        div[data-testid="stMetricLabel"] p {
            color: var(--muted) !important;
            font-size: .88rem;
        }
        section[data-testid="stSidebar"] {
            background: var(--app-bg);
        }
        h1, h2, h3, h4, h5, h6,
        label, p, span,
        [data-testid="stMarkdownContainer"] {
            color: var(--text);
        }
        .stButton > button {
            border-radius: 8px;
            border-color: var(--border);
            color: var(--text);
            background: var(--surface);
        }
        .review-status {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 12px 14px;
            margin: 8px 0 12px;
            border: 1px solid var(--accent-border);
            border-radius: 8px;
            color: var(--accent-text);
            background: var(--accent-soft);
            font-size: .96rem;
        }
        .review-status span {
            width: 14px;
            height: 14px;
            border: 2px solid #93c5fd;
            border-top-color: #1d4ed8;
            border-radius: 50%;
            animation: spin .8s linear infinite;
            flex: 0 0 auto;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        [data-testid="stFileUploaderDropzoneInstructions"] > div > span {
            visibility: hidden;
            position: relative;
        }
        [data-testid="stFileUploaderDropzoneInstructions"] > div > span::after {
            visibility: visible;
            position: absolute;
            left: 0;
            top: 0;
            white-space: nowrap;
            color: var(--muted);
        }
        [data-testid="stFileUploaderDropzoneInstructions"] > div:first-child > span::after {
            content: "将文件拖到这里";
        }
        [data-testid="stFileUploaderDropzoneInstructions"] > div:nth-child(2),
        [data-testid="stFileUploaderDropzoneInstructions"] > div:nth-child(2) > span {
            display: none;
        }
        [data-testid="stFileUploaderDropzone"]::after {
            content: "单个文件不超过 5MB，支持 PDF、DOCX、DOC";
            color: var(--muted);
            font-size: .92rem;
            margin-left: 12px;
            margin-right: auto;
        }
        [data-testid="stFileUploaderDropzone"] {
            background: var(--surface-soft);
            border-color: var(--border);
        }
        [data-testid="stFileUploaderDropzone"] svg {
            color: var(--muted);
            stroke: var(--muted);
        }
        [data-testid="stFileUploaderDropzone"] button {
            font-size: 0;
            color: var(--text);
            border-color: var(--border);
            background: var(--surface);
        }
        [data-testid="stFileUploaderDropzone"] button::after {
            content: "选择文件";
            font-size: .9rem;
        }
        div[data-baseweb="select"] > div,
        div[data-baseweb="select"] span {
            background: var(--surface-soft) !important;
            color: var(--text) !important;
            border-color: var(--border) !important;
        }
        [data-testid="stAlert"] {
            border-radius: 8px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
