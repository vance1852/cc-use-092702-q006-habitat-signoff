"""确定性的规范化 JSON 与内容摘要工具。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


class JsonDataError(ValueError):
    """JSON 文件缺失、损坏或不符合契约。"""


def load_json(path: str | Path) -> Any:
    """读取 UTF-8 JSON，并拒绝重复键和非标准常量。"""

    target = Path(path)

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise JsonDataError(f"{target} 含重复键 {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise JsonDataError(f"JSON 不允许非有限数值 {value}")

    try:
        text = target.read_text(encoding="utf-8")
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=pairs_hook,
        )
    except OSError as exc:
        raise JsonDataError(f"无法读取 {target}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise JsonDataError(f"{target} 不是有效 JSON: {exc.msg}") from exc


def canonical_json(value: object) -> str:
    """生成跨平台一致的紧凑 JSON 文本。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def content_digest(values: Iterable[object]) -> str:
    """按输入顺序计算规范化内容摘要。"""

    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
