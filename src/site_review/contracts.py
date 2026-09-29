"""调查协议、证据批次、空间边界和结论草案的严格数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class ValidationError(ValueError):
    """输入不能满足领域契约。"""


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} 必须是对象")
    return value


def _require_sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{path} 必须是数组")
    return value


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是非空字符串")
    return value.strip()


def _optional_text(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, path)


def _positive_int(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{path} 必须是正整数")
    return value


@dataclass(frozen=True, slots=True)
class SpatialBoundary:
    """一次评估冻结的空间范围（GeoJSON 几何 + 坐标参考系）。"""

    geometry: Mapping[str, Any]
    crs: str

    @classmethod
    def from_dict(cls, raw: object, path: str = "spatial_boundary") -> "SpatialBoundary":
        data = _require_mapping(raw, path)
        geometry = _require_mapping(data.get("geometry"), f"{path}.geometry")
        geometry_type = _required_text(geometry.get("type"), f"{path}.geometry.type")
        if geometry_type not in {
            "Point", "MultiPoint", "LineString", "MultiLineString",
            "Polygon", "MultiPolygon", "GeometryCollection",
        }:
            raise ValidationError(f"{path}.geometry.type 不是受支持的 GeoJSON 类型")
        if "coordinates" not in geometry and geometry_type != "GeometryCollection":
            raise ValidationError(f"{path}.geometry.coordinates 缺失")
        crs = _required_text(data.get("crs"), f"{path}.crs")
        return cls(geometry=dict(geometry), crs=crs)


@dataclass(frozen=True, slots=True)
class SurveyProtocol:
    """被结论草案冻结引用的调查协议版本。"""

    protocol_id: str
    version: int
    discipline: str
    title: str
    canonical: Mapping[str, Any]

    @classmethod
    def from_dict(cls, raw: object) -> "SurveyProtocol":
        data = _require_mapping(raw, "survey_protocol")
        protocol_id = _required_text(data.get("protocol_id"), "survey_protocol.protocol_id")
        version = _positive_int(data.get("version"), "survey_protocol.version")
        discipline = _required_text(data.get("discipline"), "survey_protocol.discipline")
        title = _required_text(data.get("title"), "survey_protocol.title")
        return cls(
            protocol_id=protocol_id,
            version=version,
            discipline=discipline,
            title=title,
            canonical=dict(data),
        )


@dataclass(frozen=True, slots=True)
class EvidenceBatch:
    """证据批次登记输入：批次本身是冻结清单的容器。"""

    batch_id: str
    discipline: str
    protocol_id: str
    protocol_version: int
    collected_by: str
    collected_at: str
    note: str | None

    @classmethod
    def from_dict(cls, raw: object) -> "EvidenceBatch":
        data = _require_mapping(raw, "evidence_batch")
        return cls(
            batch_id=_required_text(data.get("batch_id"), "evidence_batch.batch_id"),
            discipline=_required_text(data.get("discipline"), "evidence_batch.discipline"),
            protocol_id=_required_text(data.get("protocol_id"), "evidence_batch.protocol_id"),
            protocol_version=_positive_int(data.get("protocol_version"), "evidence_batch.protocol_version"),
            collected_by=_required_text(data.get("collected_by"), "evidence_batch.collected_by"),
            collected_at=_required_text(data.get("collected_at"), "evidence_batch.collected_at"),
            note=_optional_text(data.get("note"), "evidence_batch.note"),
        )


@dataclass(frozen=True, slots=True)
class SignoffStep:
    """会签链条中的一个职责环节。"""

    sequence: int
    role: str
    title: str

    @classmethod
    def from_dict(cls, raw: object, index: int) -> "SignoffStep":
        data = _require_mapping(raw, f"signoff_chain[{index}]")
        sequence = _positive_int(data.get("sequence"), f"signoff_chain[{index}].sequence")
        role = _required_text(data.get("role"), f"signoff_chain[{index}].role")
        title = _required_text(data.get("title"), f"signoff_chain[{index}].title")
        return cls(sequence=sequence, role=role, title=title)
