from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from format_probe import probe_format
from pipeline_common import finish, read_json, result, sha256_file, write_json


SCHEMA_VERSION = "3.0.0"
GENERATOR_NAME = "source_document_v3"
GENERATOR_VERSION = "3.0.0"
DEFAULT_POLICY_VERSION = "source-extraction-policy-v3.0.0"
KNOWN_ITEM_KINDS = {"text", "table", "image", "media", "relationship", "shape_description"}
CONTEXT_KEYS = {
    "page",
    "slide",
    "region",
    "part",
    "style",
    "relationship_id",
    "source_part",
    "extraction",
}


class SourceDocumentError(Exception):
    def __init__(self, code: str, message: str, **facts: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.facts = facts


def canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def _normalization_application(report: dict[str, Any]) -> dict[str, str]:
    raw_application = report.get("application")
    if isinstance(raw_application, dict):
        return {
            "name": str(raw_application.get("name") or ""),
            "version": str(raw_application.get("version") or ""),
            "identity": str(raw_application.get("identity") or raw_application.get("name") or ""),
        }
    return {
        "name": str(raw_application or ""),
        "version": str(report.get("application_version") or ""),
        "identity": str(report.get("application_identity") or raw_application or ""),
    }


def _normalization_evidence_digest(report: dict[str, Any], field: str) -> str | None:
    raw_path = str(report.get(field) or "")
    if not raw_path:
        return None
    path = Path(raw_path).resolve()
    return sha256_file(path) if path.is_file() else None


def _normalization_report_material(report: dict[str, Any]) -> dict[str, Any]:
    """Return only repeatable, approval-relevant normalization facts.

    Cache hit/miss labels, report timestamps and output-directory paths are
    execution metadata.  Binding them made an unchanged source invalidate a
    valid human receipt on every rerun.  Actual file hashes, format change,
    Office identity, visual evidence and findings remain bound.
    """

    source_format = str(report.get("source_format") or "").lower()
    normalized_format = str(report.get("normalized_format") or "").lower()
    return {
        "status": str(report.get("status") or "PASS"),
        "source_sha256": str(report.get("source_sha256") or "").upper(),
        "normalized_sha256": str(report.get("normalized_sha256") or "").upper(),
        "source_format": source_format,
        "normalized_format": normalized_format,
        "format_changed": bool(
            report.get("format_changed")
            if report.get("format_changed") is not None
            else source_format and normalized_format and source_format != normalized_format
        ),
        "application": _normalization_application(report),
        "visual_evidence": {
            "original_pdf_sha256": _normalization_evidence_digest(report, "original_render_pdf"),
            "normalized_pdf_sha256": _normalization_evidence_digest(report, "normalized_render_pdf"),
        },
        "findings": list(report.get("findings") or []),
    }


def _require_sha_match(actual: str, expected: str, code: str, label: str) -> None:
    if not expected or actual.upper() != expected.upper():
        raise SourceDocumentError(
            code,
            f"{label} SHA-256 与当前文件不一致",
            expected_sha256=expected,
            actual_sha256=actual,
        )


def _validate_probe(
    probe: dict[str, Any],
    path: Path,
    *,
    expected_format: str | None = None,
) -> None:
    if probe.get("artifact_type") != "FormatProbeResultV3":
        raise SourceDocumentError("FORMAT_PROBE_TYPE_INVALID", "上游格式探测制品类型无效")
    if probe.get("status") != "PASS":
        raise SourceDocumentError(
            "FORMAT_PROBE_BLOCKED",
            "上游格式探测未通过",
            upstream_status=probe.get("status"),
            upstream_findings=probe.get("findings", []),
        )
    source = probe.get("source")
    if not isinstance(source, dict):
        raise SourceDocumentError("FORMAT_PROBE_SOURCE_MISSING", "格式探测制品缺少 source")
    _require_sha_match(
        sha256_file(path),
        str(source.get("sha256") or ""),
        "FORMAT_PROBE_SHA_MISMATCH",
        "格式探测源文件",
    )
    detected_format = str(probe.get("detected_format") or "").lower()
    expected_family = {
        "doc": "word",
        "docx": "word",
        "ppt": "presentation",
        "pptx": "presentation",
        "pdf": "fixed_layout",
    }.get(detected_format)
    if expected_family and probe.get("family") != expected_family:
        raise SourceDocumentError(
            "FORMAT_PROBE_FAMILY_MISMATCH",
            "格式探测 family 与 detected_format 不一致",
            detected_format=detected_format,
            expected_family=expected_family,
            actual_family=probe.get("family"),
        )
    if expected_format and probe.get("detected_format") != expected_format:
        raise SourceDocumentError(
            "NORMALIZED_FORMAT_MISMATCH",
            "归一化文件实际格式与预期格式不一致",
            expected_format=expected_format,
            actual_format=probe.get("detected_format"),
        )


def _expected_normalized_format(source_format: str) -> str:
    expected = {
        "doc": "docx",
        "docx": "docx",
        "ppt": "pptx",
        "pptx": "pptx",
        "pdf": "pdf",
    }.get(source_format)
    if not expected:
        raise SourceDocumentError(
            "SOURCE_FORMAT_UNSUPPORTED",
            "SourceDocumentV3 收到不支持的源格式",
            source_format=source_format,
        )
    return expected


def _validate_inventory(inventory: dict[str, Any], normalized: Path, expected_format: str) -> None:
    status = str(inventory.get("status") or "PASS")
    if status != "PASS":
        raise SourceDocumentError(
            "INVENTORY_UPSTREAM_BLOCKED",
            "上游 inventory 未通过，不能生成 SourceDocumentV3",
            upstream_status=status,
            upstream_findings=inventory.get("findings", []),
        )
    inventory_sha = str(inventory.get("source_sha256") or "")
    if not inventory_sha:
        raise SourceDocumentError(
            "INVENTORY_SOURCE_SHA_MISSING",
            "inventory 缺少归一化文件 SHA-256",
        )
    _require_sha_match(
        sha256_file(normalized),
        inventory_sha,
        "INVENTORY_SHA_MISMATCH",
        "inventory 归一化文件",
    )
    if inventory.get("format") != expected_format:
        raise SourceDocumentError(
            "INVENTORY_FORMAT_MISMATCH",
            "inventory 格式与归一化格式不一致",
            expected_format=expected_format,
            actual_format=inventory.get("format"),
        )
    if not isinstance(inventory.get("items"), list):
        raise SourceDocumentError("INVENTORY_ITEMS_INVALID", "inventory.items 必须是数组")


def _normalization_provenance(
    source: Path,
    normalized: Path,
    source_format: str,
    normalized_format: str,
    normalization_report: dict[str, Any] | None,
) -> dict[str, Any]:
    source_sha = sha256_file(source)
    normalized_sha = sha256_file(normalized)
    report = normalization_report or {}
    if source_format in {"doc", "ppt"} and not report:
        raise SourceDocumentError(
            "NORMALIZATION_REPORT_REQUIRED",
            "DOC/PPT 正式支持必须绑定归一化报告与 Office 应用身份",
        )
    if report:
        reported_source_sha = str(report.get("source_sha256") or "")
        reported_normalized_sha = str(report.get("normalized_sha256") or "")
        if reported_source_sha:
            _require_sha_match(
                source_sha,
                reported_source_sha,
                "NORMALIZATION_SOURCE_SHA_MISMATCH",
                "归一化报告原件",
            )
        if reported_normalized_sha:
            _require_sha_match(
                normalized_sha,
                reported_normalized_sha,
                "NORMALIZATION_OUTPUT_SHA_MISMATCH",
                "归一化报告输出",
            )
        if report.get("status") not in {None, "PASS"}:
            raise SourceDocumentError(
                "NORMALIZATION_UPSTREAM_BLOCKED",
                "归一化报告未通过",
                upstream_status=report.get("status"),
                upstream_findings=report.get("findings", []),
            )
        reported_source_format = str(report.get("source_format") or "").lower()
        reported_normalized_format = str(report.get("normalized_format") or "").lower()
        if reported_source_format and reported_source_format != source_format:
            raise SourceDocumentError(
                "NORMALIZATION_SOURCE_FORMAT_MISMATCH",
                "归一化报告的源格式与 FormatProbe 不一致",
                expected_format=source_format,
                actual_format=reported_source_format,
            )
        if reported_normalized_format and reported_normalized_format != normalized_format:
            raise SourceDocumentError(
                "NORMALIZATION_OUTPUT_FORMAT_MISMATCH",
                "归一化报告的输出格式与实际归一化格式不一致",
                expected_format=normalized_format,
                actual_format=reported_normalized_format,
            )
        if source_format in {"doc", "ppt"}:
            format_findings = [
                finding
                for finding in report.get("findings") or []
                if isinstance(finding, dict) and finding.get("code") == "FORMAT_NORMALIZED"
            ]
            finding_matches = any(
                str(finding.get("source_format") or "").lower() == source_format
                and str(finding.get("normalized_format") or "").lower() == normalized_format
                and str(finding.get("source_sha256") or "").upper() == source_sha
                and str(finding.get("normalized_sha256") or "").upper() == normalized_sha
                for finding in format_findings
            )
            if not finding_matches:
                raise SourceDocumentError(
                    "NORMALIZATION_FORMAT_CHANGE_FINDING_REQUIRED",
                    "DOC/PPT 归一化报告缺少绑定当前格式和文件哈希的格式变化 finding",
                    source_format=source_format,
                    normalized_format=normalized_format,
                )
    application = _normalization_application(report)
    if source_format in {"doc", "ppt"} and not all(application.values()):
        raise SourceDocumentError(
            "NORMALIZATION_APPLICATION_IDENTITY_MISSING",
            "DOC/PPT 归一化报告缺少 Office 应用名称、版本或身份",
            application=application,
        )

    def evidence_file(field: str) -> dict[str, Any] | None:
        raw_path = str(report.get(field) or "")
        if not raw_path:
            return None
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise SourceDocumentError(
                "NORMALIZATION_VISUAL_EVIDENCE_MISSING",
                "归一化报告引用的视觉证据文件不存在",
                field=field,
                path=str(path),
            )
        return {"path": str(path), "sha256": sha256_file(path)}

    original_render = evidence_file("original_render_pdf")
    normalized_render = evidence_file("normalized_render_pdf")
    if source_format in {"doc", "ppt"} and (original_render is None or normalized_render is None):
        raise SourceDocumentError(
            "NORMALIZATION_VISUAL_EVIDENCE_REQUIRED",
            "DOC/PPT 归一化必须绑定原件与归一化文件的 PDF 渲染证据",
        )
    return {
        "required": source_format in {"doc", "ppt"},
        "format_changed": source_format != normalized_format,
        "method": "identity" if source_format == normalized_format else "office-normalization",
        "source_sha256": source_sha,
        "normalized_sha256": normalized_sha,
        "application": application,
        "visual_evidence": {
            "original_pdf": original_render,
            "normalized_pdf": normalized_render,
        },
        "report_sha256": canonical_json_sha256(_normalization_report_material(report)) if report else None,
        "findings": list(report.get("findings") or []),
    }


def _typed_payload(item: dict[str, Any]) -> dict[str, Any]:
    kind = str(item.get("kind") or "")
    if kind == "table":
        rows = item.get("rows")
        if not isinstance(rows, list) or any(not isinstance(row, list) for row in rows):
            raise SourceDocumentError(
                "INVENTORY_TABLE_INVALID",
                "表格 item 的 rows 必须是二维数组",
                source_id=item.get("source_id") or item.get("id"),
            )
        return {
            "type": "table",
            "rows": [[str(cell) for cell in row] for row in rows],
        }
    if kind in {"image", "media"}:
        sha256 = str(item.get("sha256") or "")
        if not re.fullmatch(r"[0-9A-Fa-f]{64}", sha256):
            raise SourceDocumentError(
                "INVENTORY_ASSET_SHA_INVALID",
                "媒体 item 缺少有效 SHA-256",
                source_id=item.get("source_id") or item.get("id"),
            )
        visual_sha = item.get("visual_sha256")
        return {
            "type": "asset",
            "name": str(item.get("name") or ""),
            "sha256": sha256.upper(),
            "visual_sha256": str(visual_sha).upper() if visual_sha else None,
            "bytes": int(item.get("bytes") or 0),
            "media_kind": kind,
        }
    if kind == "relationship":
        return {
            "type": "relationship",
            "source_part": str(item.get("source_part") or ""),
            "relationship_id": str(item.get("relationship_id") or ""),
            "relationship_type": str(item.get("relationship_type") or ""),
            "target": str(item.get("target") or ""),
            "target_mode": str(item.get("target_mode") or ""),
        }
    if kind == "shape_description":
        return {
            "type": "description",
            "text": str(item.get("text") or ""),
            "name": str(item.get("name") or ""),
            "title": str(item.get("title") or ""),
            "description": str(item.get("description") or ""),
        }
    if kind == "text":
        return {"type": "text", "text": str(item.get("text") or "")}
    return {
        "type": "generic",
        "value": {
            key: value
            for key, value in item.items()
            if key not in {"id", "source_id", "kind", "location"}
        },
    }


# ``notes`` is a real semantic source (speaker notes can contain instructions,
# data, or other user-authored content), so it must not be treated as template
# metadata merely because it is outside the visible slide canvas.
PRESENTATION_NON_SLIDE_REGIONS = {
    "slide_master",
    "slide_layout",
    "notes_master",
    "notes",
}

# These parts are template infrastructure rather than article content.  Keep
# their inventory items in the source document for traceability, but attach an
# explicit deterministic removal contract so later consumers can distinguish
# them from unresolved user-authored content.
PRESENTATION_TEMPLATE_REGIONS = {
    "slide_master",
    "slide_layout",
    "notes_master",
}
PRESENTATION_TEMPLATE_ITEM_KINDS = {
    "text",
    "shape_description",
    "image",
    "media",
    "table",
}

_DETERMINISTIC_REMOVAL_ATTRIBUTES = {
    "safe_remove_action",
    "deterministic_remove_action",
    "deterministic_remove_approved",
}


def _presentation_region(raw_item: dict[str, Any]) -> str | None:
    """Infer a stable presentation region from all OOXML location hints.

    Inventory producers have historically put the part name in ``location``,
    ``part`` or ``path`` (and, for relationships, ``source_part``). Shape
    descriptions are collected before the inventory code assigns a region, so
    SourceDocumentV3 must derive it from immutable part/path markers rather
    than treating an omitted region as slide content.
    """

    hints = [
        str(raw_item.get(key) or "")
        for key in ("location", "part", "path", "source_part")
    ]
    for raw_hint in hints:
        hint = raw_hint.replace("\\", "/").casefold()
        if "notesmasters/" in hint or hint.startswith("notes_master:"):
            return "notes_master"
        if "notesslides/" in hint or hint.startswith("notes:"):
            return "notes"
        if "slidemasters/" in hint or hint.startswith("slide_master:"):
            return "slide_master"
        if "slidelayouts/" in hint or hint.startswith("slide_layout:"):
            return "slide_layout"
        if hint.startswith("slide:") or re.search(r"(?:^|/)slides/slide\d+\.xml(?:/|$)", hint):
            return "slide"

    explicit_region = str(raw_item.get("region") or "").strip()
    return explicit_region or None


def _presentation_metadata_attributes(
    attributes: dict[str, Any],
    *,
    shape_description: bool,
) -> None:
    """Attach the explicit, traceable contract for template metadata.

    ``structural_placeholder`` remains present for shape descriptions because
    the deterministic consumer uses it as a typed presentation marker.  The
    more general ``structural_metadata`` flag also covers master/layout text,
    tables, and assets, which are retained as evidence but are not article
    content.
    """

    attributes.update(
        {
            "semantic_role": "structural_metadata",
            "structural_metadata": True,
            "role_source": "deterministic",
            "safe_remove_action": "remove_template_background",
            "deterministic_remove_action": "remove_template_background",
            "deterministic_remove_approved": True,
        }
    )
    if shape_description:
        attributes["structural_placeholder"] = True


def _shape_description_has_authored_metadata(raw_item: dict[str, Any]) -> bool:
    """Return whether OOXML title/description contains authored metadata."""

    return bool(
        str(raw_item.get("title") or "").strip()
        or str(raw_item.get("description") or "").strip()
    )


def _typed_items(
    items: list[Any],
    *,
    presentation: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    typed: list[dict[str, Any]] = []
    source_ids: set[str] = set()
    unsupported: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            raise SourceDocumentError(
                "INVENTORY_ITEM_INVALID",
                "inventory item 必须是对象",
                item_index=index,
            )
        explicit_id = str(raw_item.get("source_id") or "")
        legacy_id = str(raw_item.get("id") or "")
        if explicit_id and legacy_id and explicit_id != legacy_id:
            raise SourceDocumentError(
                "SOURCE_ID_MISMATCH",
                "inventory item 的 id 与 source_id 不一致",
                item_index=index,
                item_id=legacy_id,
                source_id=explicit_id,
            )
        source_id = explicit_id or legacy_id
        if not source_id:
            raise SourceDocumentError(
                "SOURCE_ID_MISSING",
                "inventory item 缺少稳定 source_id",
                item_index=index,
            )
        if source_id in source_ids:
            raise SourceDocumentError(
                "SOURCE_ID_DUPLICATE",
                "inventory 中存在重复 source_id",
                source_id=source_id,
            )
        source_ids.add(source_id)
        kind = str(raw_item.get("kind") or "")
        location = str(raw_item.get("location") or "")
        if not kind or not location:
            raise SourceDocumentError(
                "INVENTORY_ITEM_LOCATION_INVALID",
                "inventory item 缺少 kind 或 location",
                source_id=source_id,
            )
        context = {
            key: raw_item[key]
            for key in CONTEXT_KEYS
            if key in raw_item and raw_item[key] is not None and raw_item[key] != ""
        }
        if presentation:
            inferred_region = _presentation_region(raw_item)
            if inferred_region:
                context["region"] = inferred_region
        consumed = {
            "id", "source_id", "kind", "location", *CONTEXT_KEYS,
            "text", "rows", "name", "sha256", "visual_sha256", "bytes",
            "relationship_type", "target", "target_mode", "title", "description",
        }
        attributes = {
            key: value for key, value in raw_item.items() if key not in consumed
        }
        if presentation:
            region = context.get("region")
            if region in PRESENTATION_TEMPLATE_REGIONS and kind in PRESENTATION_TEMPLATE_ITEM_KINDS:
                _presentation_metadata_attributes(
                    attributes,
                    shape_description=kind == "shape_description",
                )
            elif region in {"slide", "notes"} and kind == "shape_description":
                # A visible-slide shape description is only safely removable
                # when OOXML has no authored title/description.  The same
                # narrow rule applies to notesSlides shape metadata: a
                # shape's implementation name/text is not speaker-note prose,
                # while an authored title/description must remain unresolved.
                # Ordinary notes text/table/image items are intentionally not
                # covered by this branch.
                if not _shape_description_has_authored_metadata(raw_item):
                    _presentation_metadata_attributes(attributes, shape_description=True)
                else:
                    for key in _DETERMINISTIC_REMOVAL_ATTRIBUTES:
                        attributes.pop(key, None)
                    attributes.pop("structural_metadata", None)
                    attributes.pop("structural_placeholder", None)
        typed.append({
            "source_id": source_id,
            "kind": kind,
            "location": location,
            "payload": _typed_payload(raw_item),
            "context": context,
            "attributes": attributes,
        })
        if kind not in KNOWN_ITEM_KINDS:
            unsupported.append({
                "code": "UNSUPPORTED_INVENTORY_ITEM_KIND",
                "source_id": source_id,
                "location": location,
                "detail": kind,
            })
    return typed, unsupported


def _group_refs(items: list[dict[str, Any]], region: str) -> list[dict[str, Any]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for item in items:
        if item["context"].get("region") != region:
            continue
        part = str(item["context"].get("part") or "")
        groups[part].append(item["source_id"])
    return [
        {"part": part, "item_ids": item_ids}
        for part, item_ids in sorted(groups.items())
    ]


def _word_content(
    items: list[dict[str, Any]],
    inventory: dict[str, Any],
    unsupported: list[dict[str, Any]],
) -> dict[str, Any]:
    header_ids = {
        item["source_id"] for item in items if item["context"].get("region") == "header"
    }
    footer_ids = {
        item["source_id"] for item in items if item["context"].get("region") == "footer"
    }
    shape_ids = [
        item["source_id"]
        for item in items
        if item["kind"] == "shape_description"
        or item["context"].get("region") == "textbox"
        or "textbox:" in item["location"]
    ]
    paragraph_ids = [
        item["source_id"]
        for item in items
        if item["kind"] == "text"
        and item["source_id"] not in header_ids | footer_ids
        and item["source_id"] not in shape_ids
    ]
    table_ids = [
        item["source_id"]
        for item in items
        if item["kind"] == "table" and item["source_id"] not in header_ids | footer_ids
    ]
    return {
        "kind": "word",
        "items": items,
        "paragraph_ids": paragraph_ids,
        "table_ids": table_ids,
        "headers": _group_refs(items, "header"),
        "footers": _group_refs(items, "footer"),
        "shape_ids": shape_ids,
        "image_ids": [item["source_id"] for item in items if item["kind"] == "image"],
        "relationship_ids": [
            item["source_id"] for item in items if item["kind"] == "relationship"
        ],
        "metadata": dict(inventory.get("metadata") or {}),
        "counts": dict(inventory.get("counts") or {}),
        "unsupported_features": unsupported,
    }


def _part_groups(items: list[dict[str, Any]], region: str) -> list[dict[str, Any]]:
    return _group_refs(items, region)


def _slide_number(item: dict[str, Any]) -> int | None:
    raw_slide = item["context"].get("slide")
    if isinstance(raw_slide, int) and raw_slide > 0:
        return raw_slide
    match = re.search(r"(?:^|/)slide:(\d+)(?:/|$)", item["location"])
    return int(match.group(1)) if match else None


def _presentation_content(
    items: list[dict[str, Any]],
    inventory: dict[str, Any],
    unsupported: list[dict[str, Any]],
) -> dict[str, Any]:
    excluded_regions = PRESENTATION_NON_SLIDE_REGIONS
    slide_refs: dict[int, list[str]] = defaultdict(list)
    for item in items:
        if item["context"].get("region") in excluded_regions:
            continue
        slide_number = _slide_number(item)
        if slide_number is not None:
            slide_refs[slide_number].append(item["source_id"])
    declared_slide_count = int((inventory.get("counts") or {}).get("slides") or 0)
    if declared_slide_count < 0:
        raise SourceDocumentError("INVENTORY_COUNT_INVALID", "幻灯片数量不能为负数")
    slide_numbers = sorted(set(slide_refs) | set(range(1, declared_slide_count + 1)))
    shape_ids = [
        item["source_id"]
        for item in items
        if (
            "/shape:" in item["location"]
            or item["location"].startswith("slide:")
            or item["kind"] == "shape_description"
        )
        and item["context"].get("region") not in excluded_regions
    ]
    return {
        "kind": "presentation",
        "items": items,
        "slides": [
            {"number": number, "item_ids": slide_refs.get(number, [])}
            for number in slide_numbers
        ],
        "masters": _part_groups(items, "slide_master"),
        "layouts": _part_groups(items, "slide_layout"),
        "notes": _part_groups(items, "notes"),
        "shape_ids": shape_ids,
        "asset_ids": [
            item["source_id"]
            for item in items
            if item["kind"] in {"image", "media"}
            and item["context"].get("region") not in PRESENTATION_TEMPLATE_REGIONS
        ],
        "relationship_ids": [
            item["source_id"] for item in items if item["kind"] == "relationship"
        ],
        "metadata": dict(inventory.get("metadata") or {}),
        "counts": dict(inventory.get("counts") or {}),
        "unsupported_features": unsupported,
    }


def _page_number(item: dict[str, Any]) -> int | None:
    raw_page = item["context"].get("page")
    if isinstance(raw_page, int) and raw_page > 0:
        return raw_page
    match = re.search(r"(?:^|/)page:(\d+)(?:/|$)", item["location"])
    return int(match.group(1)) if match else None


def _pdf_content(
    items: list[dict[str, Any]],
    inventory: dict[str, Any],
    unsupported: list[dict[str, Any]],
) -> dict[str, Any]:
    pages: dict[int, dict[str, list[str]]] = defaultdict(
        lambda: {
            "native_text_ids": [],
            "ocr_text_ids": [],
            "image_ids": [],
            "other_item_ids": [],
        }
    )
    unattached: list[str] = []
    for item in items:
        page_number = _page_number(item)
        if page_number is None:
            unattached.append(item["source_id"])
            continue
        if item["kind"] == "text" and item["context"].get("extraction") == "ocr":
            pages[page_number]["ocr_text_ids"].append(item["source_id"])
        elif item["kind"] == "text":
            pages[page_number]["native_text_ids"].append(item["source_id"])
        elif item["kind"] == "image":
            pages[page_number]["image_ids"].append(item["source_id"])
        else:
            pages[page_number]["other_item_ids"].append(item["source_id"])
    counts = dict(inventory.get("counts") or {})
    declared_page_count = int(counts.get("pages") or 0)
    if declared_page_count < 0:
        raise SourceDocumentError("INVENTORY_COUNT_INVALID", "PDF 页数不能为负数")
    page_numbers = sorted(set(pages) | set(range(1, declared_page_count + 1)))
    return {
        "kind": "fixed_layout",
        "items": items,
        "pages": [
            {"number": number, **pages[number]}
            for number in page_numbers
        ],
        "unattached_item_ids": unattached,
        "metadata": dict(inventory.get("metadata") or {}),
        "counts": counts,
        "ocr": {
            "pages": sorted({int(page) for page in inventory.get("ocr_pages") or []}),
            "methods": {
                str(page): str(method)
                for page, method in (inventory.get("ocr_methods") or {}).items()
            },
            "required_pages": int(counts.get("ocr_required_pages") or 0),
            "failed_pages": int(counts.get("ocr_failed_pages") or 0),
        },
        "unsupported_features": unsupported,
    }


def _upstream_artifact(
    payload: dict[str, Any],
    artifact_type: str,
    schema_version: str,
) -> dict[str, Any]:
    if artifact_type == "NormalizationReport":
        stable_payload = _normalization_report_material(payload)
    else:
        stable_payload = dict(payload)
        stable_payload.pop("generated_at", None)
    return {
        "artifact_type": artifact_type,
        "schema_version": str(payload.get("schema_version") or schema_version),
        "artifact_sha256": canonical_json_sha256(stable_payload),
    }


def build_source_document_v3(
    source: Path,
    normalized: Path,
    inventory: dict[str, Any],
    format_probe_result: dict[str, Any],
    normalization_report: dict[str, Any] | None = None,
    *,
    policy_version: str = DEFAULT_POLICY_VERSION,
) -> dict[str, Any]:
    source = source.resolve()
    normalized = normalized.resolve()
    if not source.is_file():
        raise SourceDocumentError("SOURCE_MISSING", "源文件不存在", path=str(source))
    if not normalized.is_file():
        raise SourceDocumentError("NORMALIZED_SOURCE_MISSING", "归一化文件不存在", path=str(normalized))
    _validate_probe(format_probe_result, source)
    source_format = str(format_probe_result.get("detected_format") or "")
    family = str(format_probe_result.get("family") or "")
    normalized_format = _expected_normalized_format(source_format)
    normalized_probe = probe_format(normalized)
    _validate_probe(normalized_probe, normalized, expected_format=normalized_format)
    _validate_inventory(inventory, normalized, normalized_format)
    provenance = _normalization_provenance(
        source,
        normalized,
        source_format,
        normalized_format,
        normalization_report,
    )
    items, item_unsupported = _typed_items(
        list(inventory["items"]),
        presentation=family == "presentation",
    )
    declared_unsupported = inventory.get("unsupported_features") or []
    if not isinstance(declared_unsupported, list):
        raise SourceDocumentError(
            "INVENTORY_UNSUPPORTED_FEATURES_INVALID",
            "inventory.unsupported_features 必须是数组",
        )
    unsupported = [
        {
            "code": str(feature.get("code") or "UNSUPPORTED_FEATURE"),
            "source_id": str(feature.get("source_id") or ""),
            "location": str(feature.get("location") or ""),
            "detail": str(feature.get("detail") or feature.get("message") or ""),
        }
        if isinstance(feature, dict)
        else {
            "code": "UNSUPPORTED_FEATURE",
            "source_id": "",
            "location": "",
            "detail": str(feature),
        }
        for feature in declared_unsupported
    ] + item_unsupported

    if family == "word":
        content = _word_content(items, inventory, unsupported)
    elif family == "presentation":
        content = _presentation_content(items, inventory, unsupported)
    elif family == "fixed_layout":
        content = _pdf_content(items, inventory, unsupported)
    else:
        raise SourceDocumentError(
            "SOURCE_FAMILY_UNSUPPORTED",
            "格式探测返回未知源族",
            family=family,
        )

    review_reasons = [feature["code"] for feature in unsupported]
    if normalized_probe.get("findings"):
        review_reasons.extend(
            str(finding.get("code") or "FORMAT_FINDING")
            for finding in normalized_probe["findings"]
            if isinstance(finding, dict)
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "SourceDocumentV3",
        "policy_version": policy_version,
        "generator": {"name": GENERATOR_NAME, "version": GENERATOR_VERSION},
        "upstream": {
            "format_probe": _upstream_artifact(
                format_probe_result,
                "FormatProbeResultV3",
                SCHEMA_VERSION,
            ),
            "normalized_format_probe": _upstream_artifact(
                normalized_probe,
                "FormatProbeResultV3",
                SCHEMA_VERSION,
            ),
            "inventory": _upstream_artifact(inventory, "SourceInventoryV2", "v2"),
            "normalization_report": (
                _upstream_artifact(normalization_report, "NormalizationReport", "v2")
                if normalization_report
                else None
            ),
        },
        "source": {
            "path": str(source),
            "sha256": sha256_file(source),
            "size_bytes": source.stat().st_size,
            "format": source_format,
            "family": family,
        },
        "normalized": {
            "path": str(normalized),
            "sha256": sha256_file(normalized),
            "size_bytes": normalized.stat().st_size,
            "format": normalized_format,
        },
        "normalization": provenance,
        "content": content,
        "review": {
            "required": bool(review_reasons),
            "reasons": sorted(set(review_reasons)),
        },
    }




def main() -> int:
    parser = argparse.ArgumentParser(description="从 V2 inventory 生成 typed SourceDocumentV3")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--normalized", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--format-probe", type=Path, required=True)
    parser.add_argument("--normalization-report", type=Path)
    parser.add_argument("--policy-version", default=DEFAULT_POLICY_VERSION)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        inventory = read_json(args.inventory)
        format_probe_result = read_json(args.format_probe)
        normalization_report = read_json(args.normalization_report) if args.normalization_report else None
        if not isinstance(inventory, dict) or not isinstance(format_probe_result, dict):
            raise SourceDocumentError("UPSTREAM_JSON_INVALID", "上游 JSON 必须是对象")
        if normalization_report is not None and not isinstance(normalization_report, dict):
            raise SourceDocumentError("NORMALIZATION_REPORT_INVALID", "归一化报告必须是对象")
        artifact = build_source_document_v3(
            args.source,
            args.normalized,
            inventory,
            format_probe_result,
            normalization_report,
            policy_version=args.policy_version,
        )
        write_json(args.output, artifact)
        status = "HUMAN_REVIEW" if artifact["review"]["required"] else "PASS"
        payload = result(
            status,
            "source_document_v3",
            findings=[
                {
                    "code": reason,
                    "message": "SourceDocumentV3 包含需人工复核的特性",
                }
                for reason in artifact["review"]["reasons"]
            ],
            source_document=str(args.output.resolve()),
            source_document_sha256=sha256_file(args.output),
            schema_version=SCHEMA_VERSION,
            item_count=len(artifact["content"]["items"]),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, SourceDocumentError, ValueError, TypeError) as exc:
        if isinstance(exc, SourceDocumentError):
            finding = {"code": exc.code, "message": exc.message, **exc.facts}
        else:
            finding = {"code": "SOURCE_DOCUMENT_BUILD_FAILED", "message": str(exc)}
        payload = result(
            "BLOCKED",
            "source_document_v3",
            findings=[finding],
            schema_version=SCHEMA_VERSION,
        )
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
