from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATUSES = {"PASS", "FAIL", "HUMAN_REVIEW", "BLOCKED"}
SECTIONS = [
    "一、产品简介",
    "二、功能介绍",
    "三、产品优势",
    "四、应用场景",
    "五、技术参数",
    "六、产品资质",
]
PLACEHOLDERS = ["XXX", "XX", "待核实", "TBD", "TODO", "请输入", "此处文件为产品示例图片"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def stable_id(kind: str, location: str, text: str = "", digest: str = "") -> str:
    value = f"{kind}|{location}|{text}|{digest}".encode("utf-8")
    return f"{kind.upper()}-{hashlib.sha256(value).hexdigest()[:12].upper()}"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def result(status: str, stage: str, *, findings: list[dict[str, Any]] | None = None, **facts: Any) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status}")
    return {
        "status": status,
        "stage": stage,
        "generated_at": utc_now(),
        "findings": findings or [],
        **facts,
    }


def finish(payload: dict[str, Any], output: Path | None = None) -> int:
    if output:
        write_json(output, payload)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return {"PASS": 0, "HUMAN_REVIEW": 3, "FAIL": 2, "BLOCKED": 2}[payload["status"]]


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def executable(name: str) -> str | None:
    return shutil.which(name)


def run(command: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout)

