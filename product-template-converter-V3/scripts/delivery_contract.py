from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class DeliveryTarget(str, Enum):
    DESKTOP = "desktop"
    MOBILE = "mobile"


@dataclass(frozen=True)
class DeliveryProfile:
    target: DeliveryTarget
    delivery_format: str
    suffix: str
    intended_use: str
    validation_profile: str
    formal_renderer: str
    required_renderers: tuple[str, ...]

    def report(self, path: Path, renderer: str) -> dict[str, Any]:
        payload = asdict(self)
        payload["target"] = self.target.value
        payload["required_renderers"] = list(self.required_renderers)
        payload["path"] = str(path)
        payload["renderer"] = renderer
        return payload


PROFILES = {
    DeliveryTarget.DESKTOP: DeliveryProfile(DeliveryTarget.DESKTOP, "DOCX", ".docx", "电脑端 WPS 主用、Microsoft Word 备用兼容性编辑与打印", "desktop_docx", "fixed_docx_builder", ("wps", "word")),
    DeliveryTarget.MOBILE: DeliveryProfile(DeliveryTarget.MOBILE, "PDF", ".pdf", "微信 Android/iOS 手机阅读", "mobile_pdf", "wps_pdf_export", ("wps", "word")),
}


def target_values() -> tuple[str, ...]:
    return tuple(target.value for target in DeliveryTarget)


def get_profile(value: str) -> DeliveryProfile:
    return PROFILES[DeliveryTarget(value)]


def clean_filename(value: str) -> str:
    invalid = '\\/:*?"<>|'
    cleaned = "".join("-" if char in invalid else char for char in value.strip())
    return " ".join(cleaned.split()).rstrip(". ")


def artifact_stem(model: str, full_name: str) -> str:
    return f"{clean_filename(model)}_{clean_filename(full_name)}-产品介绍"


def final_path(output_dir: Path, model: str, full_name: str, profile: DeliveryProfile) -> Path:
    return output_dir / f"{artifact_stem(model, full_name)}{profile.suffix}"


def intermediate_path(work_dir: Path, model: str, full_name: str, change: str, suffix: str) -> Path:
    base = work_dir / f"{artifact_stem(model, full_name)}-{clean_filename(change)}{suffix}"
    if not base.exists():
        return base
    version = 2
    while True:
        candidate = work_dir / f"{artifact_stem(model, full_name)}-{clean_filename(change)}-v{version}{suffix}"
        if not candidate.exists():
            return candidate
        version += 1


def select_primary_pdf(engines: dict[str, dict[str, Any]], primary_engine: str = "wps") -> Path | None:
    """Return the existing PDF produced by the selected primary engine."""
    candidate = engines.get(primary_engine, {}).get("pdf")
    path = Path(candidate) if candidate else None
    return path if path and path.is_file() else None


def select_word_pdf(engines: dict[str, dict[str, Any]]) -> Path | None:
    """Compatibility wrapper for callers that explicitly request Word output."""
    return select_primary_pdf(engines, primary_engine="word")
