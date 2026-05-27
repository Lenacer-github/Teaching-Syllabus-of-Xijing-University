from __future__ import annotations

from pathlib import Path

import pandas as pd


EXPECTED_COLUMNS = {
    "课程代码": ["课程代码", "代码", "课程编号"],
    "课程名称": ["课程名称（中文）", "课程名称", "课程名", "名称"],
    "课程名称（英文）": ["课程名称（英文）", "英文课程名", "英文名称", "课程英文名称"],
    "课程类别": ["课程类别", "课程分类", "类别"],
    "课程性质": ["课程性质", "性质"],
    "学分": ["学分"],
    "总学时": ["总学时", "学时"],
    "理论学时": ["理论学时"],
    "实践学时": ["实践学时", "实验学时"],
    "开课学期": ["开课学期", "学期"],
    "考核方式": ["考核方式（考试考查）", "考核方式", "考试方式", "考试类型"],
}


def load_course_catalog(data_root: Path) -> pd.DataFrame:
    path = _resolve_catalog_path(data_root)
    if path is None:
        return pd.DataFrame()
    table = pd.read_excel(path, dtype=str)
    rename: dict[str, str] = {}
    for canonical, candidates in EXPECTED_COLUMNS.items():
        for column in table.columns:
            if _is_matching_column(str(column).strip(), candidates):
                rename[column] = canonical
                break
    table = table.rename(columns=rename)
    for column in EXPECTED_COLUMNS:
        if column not in table.columns:
            table[column] = None
    table = table[list(EXPECTED_COLUMNS.keys())].copy()
    table["课程代码"] = table["课程代码"].map(_normalize_blank)
    table["课程名称"] = table["课程名称"].map(_normalize_blank)
    return table


def _resolve_catalog_path(data_root: Path) -> Path | None:
    candidates = [
        data_root / "course_codes.xlsx",
        data_root / "course_code.xlsx",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def _is_matching_column(column: str, candidates: list[str]) -> bool:
    if column in candidates:
        return True
    if any(column.startswith(f"{candidate}（") or column.startswith(f"{candidate}(") for candidate in candidates):
        return True
    descriptive_columns = {"课程类别", "课程性质", "考核方式"}
    return any(
        candidate in descriptive_columns and (column.startswith(f"{candidate}（") or column.startswith(f"{candidate}("))
        for candidate in candidates
    )


def _normalize_blank(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return None if text.lower() in {"", "nan", "none", "nat"} else text
