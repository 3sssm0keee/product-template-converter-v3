from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pipeline_common import sha256_file


# A-ABL-KEEP：V3 计划构造器跨模块导入此常量；实验误删会失败，必须保留。
V3_GENERATOR_VERSION = "3.0.0"


def canonical_json_bytes(payload: Any) -> bytes:
    """返回用于制品寻址的稳定 JSON 字节。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest().upper()





