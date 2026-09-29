"""选址会签输入契约：空间边界、证据批次注册与角色声明。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")

# 三个专业域与职责顺序（数组顺序即会签顺序）。
SIGNING_STEPS: tuple[str, ...] = ("forestry", "ecology", "engineering")
STEP_LABELS: Mapping[str, str] = {
    "forestry": "林业（林下植被）",
    "ecology": "生态（鸟类繁殖地）",
    "engineering": "工程（雨季排水）",
}
ROLE_FOR_STEP: Mapping[str, str] = {
    "forestry": "forester",
    "ecology": "ecologist",
    "engineering": "engineer",
}
STEP_FOR_ROLE: Mapping[str, str] = {role: step for step, role in ROLE_FOR_STEP.items()}
ROLES = frozenset({"coordinator", "forester", "ecologist", "engineer", "auditor"})

DRAFT_STATES = frozenset({"draft", "blocked"})


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def sha256_hex(value: object, field: str) -> str:
    result = required_text(value, field, 64).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", result):
        raise ValidationFailed(f"{field} 必须是 64 位十六进制 SHA-256")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationFailed(f"{field} 必须是对象")
    return value


def _sequence(value: object, field: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationFailed(f"{field} 必须是数组")
    return value


def validate_role(role: object) -> str:
    result = required_text(role, "role", 32)
    if result not in ROLES:
        raise ValidationFailed(f"未知角色: {result}")
    return result


def validate_geometry(geometry: object) -> Mapping[str, Any]:
    """轻量校验空间边界 GeoJSON Geometry，坐标必须有限。"""

    data = _mapping(geometry, "boundary.geometry")
    geometry_type = required_text(data.get("type"), "boundary.geometry.type", 32)
    if geometry_type not in {"Polygon", "MultiPolygon"}:
        raise ValidationFailed("空间边界只支持 Polygon 或 MultiPolygon")
    coordinates = data.get("coordinates")
    _validate_coordinates(coordinates, "boundary.geometry.coordinates", depth=3 if geometry_type == "Polygon" else 4)
    return data


def _validate_coordinates(value: object, field: str, *, depth: int) -> None:
    if depth == 1:
        point = _sequence(value, field)
        if len(point) < 2:
            raise ValidationFailed(f"{field} 坐标至少包含经度和纬度")
        for axis in point[:2]:
            if isinstance(axis, bool) or not isinstance(axis, (int, float, str, Decimal)):
                raise ValidationFailed(f"{field} 坐标分量必须是数值")
            text = str(axis)
            try:
                number = float(text)
            except ValueError as exc:
                raise ValidationFailed(f"{field} 坐标分量必须是数值") from exc
            if number != number or number in (float("inf"), float("-inf")):
                raise ValidationFailed(f"{field} 坐标分量必须有限")
        return
    items = _sequence(value, field)
    if not items:
        raise ValidationFailed(f"{field} 不能为空")
    for index, item in enumerate(items):
        _validate_coordinates(item, f"{field}[{index}]", depth=depth - 1)


@dataclass(frozen=True, slots=True)
class BoundarySpec:
    """创建结论版本时冻结的空间边界。"""

    boundary_id: str
    label: str
    geometry: Mapping[str, Any]
    source_text: str

    @classmethod
    def from_dict(cls, raw: object) -> "BoundarySpec":
        data = _mapping(raw, "boundary")
        return cls(
            boundary_id=identifier(data.get("boundary_id"), "boundary.boundary_id"),
            label=required_text(data.get("label"), "boundary.label"),
            geometry=validate_geometry(data.get("geometry")),
            source_text=required_text(data.get("source_text"), "boundary.source_text", 1024),
        )


@dataclass(frozen=True, slots=True)
class EvidenceSelection:
    """结论版本对一个专业域证据批次的冻结引用。"""

    domain: str
    batch_id: str

    @classmethod
    def from_dict(cls, raw: object, index: int) -> "EvidenceSelection":
        data = _mapping(raw, f"evidence_batches[{index}]")
        domain = required_text(data.get("domain"), f"evidence_batches[{index}].domain", 32)
        if domain not in SIGNING_STEPS:
            raise ValidationFailed(f"evidence_batches[{index}].domain 必须是 {', '.join(SIGNING_STEPS)} 之一")
        return cls(domain=domain, batch_id=identifier(data.get("batch_id"), f"evidence_batches[{index}].batch_id"))
