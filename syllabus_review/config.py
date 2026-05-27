from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AppConfig:
    max_files: int
    max_file_mb: int
    current_academic_term: str


@dataclass(frozen=True)
class ProgramConfig:
    code: str
    name: str
    practice_hour_ratio: float
    current_academic_term: str
    type: str = "program"
    related_programs: tuple[str, ...] = ()

    @property
    def is_platform(self) -> bool:
        return self.type == "platform" and bool(self.related_programs)


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def load_app_config(path: Path) -> AppConfig:
    data = read_yaml(path)
    return AppConfig(
        max_files=int(data.get("max_files", 50)),
        max_file_mb=int(data.get("max_file_mb", 5)),
        current_academic_term=str(data.get("current_academic_term", "2026-2027学年第一学期")),
    )


def load_programs(root: Path) -> dict[str, ProgramConfig]:
    programs: dict[str, ProgramConfig] = {}
    for program_yaml in sorted(root.glob("*/program.yaml")):
        data = read_yaml(program_yaml)
        code = str(data.get("code") or program_yaml.parent.name)
        programs[code] = ProgramConfig(
            code=code,
            name=str(data.get("name", code)),
            practice_hour_ratio=float(data.get("practice_hour_ratio", 0.3)),
            current_academic_term=str(data.get("current_academic_term", "2026-2027学年第一学期")),
            type=str(data.get("type", "program")),
            related_programs=tuple(str(item) for item in data.get("related_programs", []) or []),
        )
    if not programs:
        programs["template"] = ProgramConfig(
            code="template",
            name="模板专业",
            practice_hour_ratio=0.3,
            current_academic_term="2026-2027学年第一学期",
        )
    return programs
