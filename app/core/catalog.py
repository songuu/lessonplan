"""教师字典和模块配置校验，草稿创建与用户偏好共用。"""

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.errors import AppError

_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "module_registry.json"
MODULE_DEFINITIONS = {item["module_type"]: item for item in json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))["modules"]}
MODULE_CATALOG = [
    {"module_type": item["module_type"], "display_name": item["name"], "default": item["default"],
     "module_summary": item["summary"], "depends_on": item["depends_on"]}
    for item in MODULE_DEFINITIONS.values() if item["module_type"] != "custom_module"
]

_BY_TYPE = {item["module_type"]: item for item in MODULE_CATALOG}
_BY_NAME = {item["display_name"]: item for item in MODULE_CATALOG}
_DEFAULT_ALIASES = {"重难点", "教学重点", "教学难点", "教学重点与难点", "重点难点", "教学目标设计", "板书", "备课准备", "教学准备工作", "教法", "教材解读"}

STAGES = [
    {"value": "primary", "label": "小学"},
    {"value": "junior", "label": "初中"},
    {"value": "senior", "label": "高中"},
]
SUBJECTS = [{"value": value, "label": label} for value, label in [
    ("chinese", "语文"), ("math", "数学"), ("english", "英语"), ("physics", "物理"),
    ("chemistry", "化学"), ("biology", "生物"), ("history", "历史"), ("geography", "地理"),
    ("politics", "道德与法治/思想政治"), ("science", "科学"), ("music", "音乐"),
    ("art", "美术"), ("physical_education", "体育与健康"), ("information_technology", "信息科技"), ("labor", "劳动"),
]]
GRADES = [
    {"value": f"grade_{number}", "label": f"{['一', '二', '三', '四', '五', '六'][number-1]}年级", "school_stage": "primary"}
    for number in range(1, 7)
] + [
    {"value": f"grade_{number}", "label": label, "school_stage": "junior" if number < 10 else "senior"}
    for number, label in [(7, "七年级"), (8, "八年级"), (9, "九年级"), (10, "高一"), (11, "高二"), (12, "高三")]
]
LESSON_TYPES = [{"value": value, "label": label} for value, label in [
    ("new_lesson", "新授课"), ("review_lesson", "复习课"), ("practice_lesson", "练习课"),
    ("experiment_lesson", "实验课"), ("reading_lesson", "阅读课"), ("writing_lesson", "写作课"),
    ("activity_lesson", "活动课"), ("assessment_lesson", "讲评课"),
]]


def default_modules() -> list[dict[str, Any]]:
    return normalize_modules([{"module_type": item["module_type"]} for item in MODULE_CATALOG if item["default"]])


def normalize_modules(modules: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if modules is None:
        return default_modules()
    if not isinstance(modules, list) or not 1 <= len(modules) <= 20:
        raise AppError("INVALID_MODULE_CONFIG", "请选择 1 至 20 个教学模块", 422)
    normalized = []
    seen_types: set[str] = set()
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for index, original in enumerate(modules):
        if not isinstance(original, dict):
            raise AppError("INVALID_MODULE_CONFIG", "模块配置必须为对象", 422)
        module = deepcopy(original)
        module_type = str(module.get("module_type") or "custom_module").strip()
        name = str(module.get("display_name") or "").strip()
        summary = module.get("module_summary")
        if summary is not None and (not isinstance(summary, str) or len(summary) > 2000):
            raise AppError("INVALID_MODULE_CONFIG", "模块生成要求最长 2000 字", 422)
        if module_type == "custom_module":
            if not name or len(name) > 20:
                raise AppError("INVALID_MODULE_NAME", "自定义模块名称需为 1 至 20 字", 422)
            preset = _BY_NAME.get(name)
            if (preset and preset["default"]) or name in _DEFAULT_ALIASES:
                raise AppError("RESERVED_MODULE_NAME", "自定义模块不能使用默认模块或别名名称", 422)
            if preset:
                module_type = preset["module_type"]
        elif module_type not in _BY_TYPE:
            raise AppError("INVALID_MODULE_TYPE", "模块类型不受支持", 422)
        preset = _BY_TYPE.get(module_type)
        if preset:
            if preset["default"] and name and name != preset["display_name"]:
                raise AppError("RESERVED_MODULE_NAME", "默认模块名称不可修改", 422)
            name = preset["display_name"]
        if name in seen_names or (module_type != "custom_module" and module_type in seen_types):
            raise AppError("DUPLICATE_MODULE", "同一教案不能包含重复模块", 422)
        module_id = str(module.get("module_id") or uuid4())
        if not re.fullmatch(r"[\w-]{1,80}", module_id) or module_id in seen_ids:
            raise AppError("INVALID_MODULE_ID", "模块标识不合法或重复", 422)
        seen_names.add(name)
        seen_types.add(module_type)
        seen_ids.add(module_id)
        normalized.append({
            "module_id": module_id,
            "client_module_key": str(module.get("client_module_key") or f"module_{index+1}"),
            "module_type": module_type,
            "display_name": name,
            "display_order": index + 1,
            "enabled": bool(module.get("enabled", True)),
            "criticality": "core" if preset and preset["default"] else "optional",
            "is_custom": module_type == "custom_module",
            "module_summary": summary if summary is not None else (preset["module_summary"] if preset else ""),
        })
    if not any(item["enabled"] for item in normalized):
        raise AppError("INVALID_MODULE_CONFIG", "至少启用一个模块", 422)
    return normalized


def dictionaries() -> dict[str, Any]:
    return {"modules": deepcopy(MODULE_CATALOG), "subjects": deepcopy(SUBJECTS), "stages": deepcopy(STAGES), "school_stages": deepcopy(STAGES), "grades": deepcopy(GRADES), "lesson_types": deepcopy(LESSON_TYPES)}


def validate_lesson_input(values: dict[str, Any]) -> dict[str, Any]:
    """Accept labels for integration compatibility, persist only dictionary identifiers."""
    normalized = dict(values)
    for field, options in [("school_stage", STAGES), ("grade", GRADES), ("subject", SUBJECTS), ("lesson_type", LESSON_TYPES)]:
        value = normalized[field]
        selected = next((item for item in options if value in {item["value"], item["label"]}), None)
        if selected is None:
            raise AppError("INVALID_LESSON_DICTIONARY", "学段、年级、学科或课型不在支持范围内", 422)
        normalized[field] = selected["value"]
    grade = next(item for item in GRADES if item["value"] == normalized["grade"])
    if grade["school_stage"] != normalized["school_stage"]:
        raise AppError("GRADE_STAGE_MISMATCH", "年级与学段不匹配", 422)
    if not str(normalized.get("topic", "")).strip():
        raise AppError("TOPIC_REQUIRED", "请填写课题", 422)
    return normalized
