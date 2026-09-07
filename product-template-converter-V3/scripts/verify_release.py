from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from delivery_contract import target_values
from pipeline_common import read_json, sha256_file, write_json
from refresh_manifest import collect_manifest_entries


TEMPLATE_RELATIVE_PATH = Path("assets") / "ZXTY-XX_产品全称-产品介绍-模板.docx"
REQUIRED_PIPELINE_STAGES = (
    "verify_document_structure",
    "export_word_wps",
    "analyze_rendered_pages",
    "scan_source_identity_rendered_word",
    "scan_source_identity_rendered_wps",
    "final_delivery",
)
EXPECTED_VALIDATION_PROFILE = {
    "desktop": "desktop_docx",
    "mobile": "mobile_pdf",
}
EXPECTED_DELIVERY_FORMAT = {
    "desktop": "DOCX",
    "mobile": "PDF",
}
EXPECTED_RENDERERS = {"word", "wps"}
V3_GOLDEN_CASE_COUNT = 5
V3_MATRIX_CASE_COUNT = 16
V3_REQUIRED_SOURCE_FORMATS = {"DOC", "DOCX", "PPT", "PPTX", "PDF"}
V3_REQUIRED_DELIVERY_TARGETS = {"desktop", "mobile"}
EXPECTED_ENGINE_CONTRACT = {
    "wps": {
        "role": "primary",
        "required_identity": "wps-writer",
    },
    "word": {
        "role": "backup_compatibility",
        "required_identity": "microsoft-word",
    },
}


def _normalize_status(value: Any) -> str:
    return str(value or "").strip().upper()


def _normalize_target(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalize_source_format(value: Any) -> str:
    return str(value or "").strip().upper().lstrip(".")


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip()
    return len(text) == 64 and all(character in "0123456789abcdefABCDEF" for character in text)


def _resolve_path(base: Path, value: Any) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else (base / path).resolve()


def _stage_map(stages: Any) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    if not isinstance(stages, list):
        return mapping
    for stage in stages:
        if isinstance(stage, dict):
            name = stage.get("stage")
            if isinstance(name, str) and name and name not in mapping:
                mapping[name] = stage
    return mapping


def _entry_map(entries: Any, *, source: str, findings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    if not isinstance(entries, list):
        findings.append({"code": f"{source.upper()}_FILES_INVALID", "message": "files must be a list"})
        return mapping
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            findings.append({"code": f"{source.upper()}_ENTRY_INVALID", "index": index, "message": "entry must be an object"})
            continue
        path = entry.get("path")
        bytes_value = entry.get("bytes")
        sha_value = entry.get("sha256")
        if not isinstance(path, str) or not path:
            findings.append({"code": f"{source.upper()}_ENTRY_INVALID", "index": index, "message": "entry path must be a non-empty string"})
            continue
        if not isinstance(bytes_value, int):
            findings.append({"code": f"{source.upper()}_ENTRY_INVALID", "index": index, "path": path, "message": "entry bytes must be an integer"})
            continue
        if not isinstance(sha_value, str) or not sha_value.strip():
            findings.append({"code": f"{source.upper()}_ENTRY_INVALID", "index": index, "path": path, "message": "entry sha256 must be a non-empty string"})
            continue
        normalized = dict(entry)
        normalized["sha256"] = sha_value.strip().upper()
        mapping[path] = normalized
    return mapping




def _record_file_result(
    findings: list[dict[str, Any]],
    *,
    code: str,
    path: Path,
    expected_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
    }
    if not path.is_file():
        findings.append({"code": code, **facts, "message": "file is missing"})
        return facts
    actual_bytes = path.stat().st_size
    actual_sha = sha256_file(path)
    facts["bytes"] = actual_bytes
    facts["sha256"] = actual_sha
    if expected_bytes is not None and actual_bytes != expected_bytes:
        findings.append(
            {
                "code": code,
                **facts,
                "expected_bytes": expected_bytes,
                "message": "byte size mismatch",
            }
        )
    if expected_sha256 is not None and actual_sha.upper() != expected_sha256.upper():
        findings.append(
            {
                "code": code,
                **facts,
                "expected_sha256": expected_sha256.upper(),
                "message": "sha256 mismatch",
            }
        )
    return facts


def _collect_manifest_evidence(manifest_path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    if not manifest_path.is_file():
        findings.append({"code": "MANIFEST_MISSING", "path": str(manifest_path)})
        return None, findings
    try:
        manifest = read_json(manifest_path)
    except Exception as exc:
        findings.append({"code": "MANIFEST_INVALID", "path": str(manifest_path), "message": str(exc)})
        return None, findings
    if not isinstance(manifest, dict):
        findings.append({"code": "MANIFEST_INVALID", "path": str(manifest_path), "message": "manifest must be an object"})
        return None, findings

    root = manifest_path.parent
    actual_entries = collect_manifest_entries(root)
    manifest_entries = manifest.get("files", [])
    actual_map = _entry_map(actual_entries, source="actual", findings=findings)
    manifest_map = _entry_map(manifest_entries, source="manifest", findings=findings)
    manifest_sha256 = sha256_file(manifest_path)

    actual_paths = set(actual_map)
    manifest_paths = set(manifest_map)
    missing_paths = sorted(actual_paths - manifest_paths)
    extra_paths = sorted(manifest_paths - actual_paths)
    if missing_paths or extra_paths:
        findings.append(
            {
                "code": "MANIFEST_PUBLISHABLE_MISMATCH",
                "missing_paths": missing_paths,
                "extra_paths": extra_paths,
            }
        )

    mismatched_entries = []
    for path in sorted(actual_paths & manifest_paths):
        actual_entry = actual_map[path]
        manifest_entry = manifest_map[path]
        if actual_entry["bytes"] != manifest_entry["bytes"]:
            mismatched_entries.append(
                {
                    "path": path,
                    "field": "bytes",
                    "expected": actual_entry["bytes"],
                    "actual": manifest_entry["bytes"],
                }
            )
        if actual_entry["sha256"].upper() != manifest_entry["sha256"].upper():
            mismatched_entries.append(
                {
                    "path": path,
                    "field": "sha256",
                    "expected": actual_entry["sha256"].upper(),
                    "actual": manifest_entry["sha256"].upper(),
                }
            )
    if mismatched_entries:
        findings.append({"code": "MANIFEST_ENTRY_MISMATCH", "items": mismatched_entries})

    template_path = root / TEMPLATE_RELATIVE_PATH
    template_entry = manifest_map.get(TEMPLATE_RELATIVE_PATH.as_posix())
    template_sha256 = manifest.get("template_sha256")
    template_facts = _record_file_result(
        findings,
        code="TEMPLATE_FILE_MISSING",
        path=template_path,
        expected_sha256=template_sha256 if isinstance(template_sha256, str) and template_sha256.strip() else None,
    )
    if isinstance(template_sha256, str) and template_sha256.strip() and template_facts.get("sha256") and template_facts["sha256"].upper() != template_sha256.upper():
        findings.append(
            {
                "code": "TEMPLATE_HASH_MISMATCH",
                "path": str(template_path),
                "expected": template_sha256.upper(),
                "actual": template_facts["sha256"],
            }
        )
    if template_entry and template_facts.get("sha256") and template_entry.get("sha256", "").upper() != template_facts["sha256"].upper():
        findings.append(
            {
                "code": "TEMPLATE_ENTRY_HASH_MISMATCH",
                "path": template_entry["path"],
                "expected": template_facts["sha256"],
                "actual": template_entry.get("sha256"),
            }
        )

    manifest_entry = manifest_map.get(TEMPLATE_RELATIVE_PATH.as_posix())
    manifest_summary = {
        "path": str(manifest_path),
        "sha256": manifest_sha256,
        "project": manifest.get("project", manifest.get("skill")),
        "generated_at": manifest.get("generated_at"),
        "template_sha256": manifest.get("template_sha256"),
        "expected_file_count": len(actual_entries),
        "manifest_file_count": len(manifest_map),
        "publishable_match": not any(item["code"] in {"MANIFEST_PUBLISHABLE_MISMATCH", "MANIFEST_ENTRY_MISMATCH"} for item in findings),
        "files": actual_entries,
        "template": {
            "path": template_entry["path"] if template_entry else TEMPLATE_RELATIVE_PATH.as_posix(),
            "bytes": template_facts.get("bytes"),
            "sha256": template_facts.get("sha256"),
            "manifest_entry": manifest_entry,
        },
    }
    return manifest_summary, findings


def _verify_report_file(
    path: Path,
    *,
    label: str,
    expected_status: str | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    if not path.is_file():
        findings.append({"code": f"{label.upper()}_MISSING", "path": str(path)})
        return None, findings
    try:
        payload = read_json(path)
    except Exception as exc:
        findings.append({"code": f"{label.upper()}_INVALID", "path": str(path), "message": str(exc)})
        return None, findings
    if not isinstance(payload, dict):
        findings.append({"code": f"{label.upper()}_INVALID", "path": str(path), "message": "report must be an object"})
        return None, findings
    status = _normalize_status(payload.get("status"))
    if expected_status and status != expected_status:
        findings.append(
            {
                "code": f"{label.upper()}_STATUS_NOT_{expected_status}",
                "path": str(path),
                "expected": expected_status,
                "actual": status or None,
            }
        )
    summary = {
        "path": str(path),
        "sha256": sha256_file(path),
        "status": status or None,
    }
    return summary | payload, findings


def _verify_post_install_report(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    summary, findings = _verify_report_file(path, label="post_install_report", expected_status="PASS")
    if summary is None:
        return None, findings
    if _normalize_status(summary.get("status")) != "PASS":
        findings.append(
            {
                "code": "POST_INSTALL_NOT_PASS",
                "path": str(path),
                "expected": "PASS",
                "actual": summary.get("status"),
            }
        )
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "status": _normalize_status(summary.get("status")),
    }, findings


def _extract_manifest_sha(payload: dict[str, Any]) -> str | None:
    value = payload.get("manifest_sha256")
    if isinstance(value, str) and value.strip():
        return value.strip().upper()
    return None


def _verify_blind_report(path: Path, manifest_sha256: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    summary, findings = _verify_report_file(path, label="blind_report", expected_status="PASS")
    if summary is None:
        return None, findings
    blind_sha = _extract_manifest_sha(summary)
    if not blind_sha:
        findings.append({"code": "BLIND_REPORT_MANIFEST_SHA_MISSING", "path": str(path)})
    elif blind_sha.upper() != manifest_sha256.upper():
        findings.append(
            {
                "code": "BLIND_REPORT_MANIFEST_SHA_MISMATCH",
                "path": str(path),
                "expected": manifest_sha256.upper(),
                "actual": blind_sha.upper(),
            }
        )
    if _normalize_status(summary.get("status")) != "PASS":
        findings.append(
            {
                "code": "BLIND_REPORT_NOT_PASS",
                "path": str(path),
                "expected": "PASS",
                "actual": summary.get("status"),
            }
        )
    scope = summary.get("scope") if isinstance(summary.get("scope"), dict) else {}
    evidence = summary.get("evidence") if isinstance(summary.get("evidence"), dict) else {}
    cases = evidence.get("cases") if isinstance(evidence.get("cases"), list) else []
    visual_review = summary.get("visual_review") if isinstance(summary.get("visual_review"), dict) else {}
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "status": _normalize_status(summary.get("status")),
        "manifest_sha256": blind_sha,
        "scope": scope,
        "cases": cases,
        "evidence": evidence,
        "visual_review": visual_review,
    }, findings


def _verify_release_matrix_report(path: Path, manifest_sha256: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    summary, findings = _verify_report_file(path, label="release_matrix_report", expected_status="PASS")
    if summary is None:
        return None, findings
    if summary.get("stage") != "verify_release_matrix_v3":
        findings.append(
            {
                "code": "V3_RELEASE_MATRIX_STAGE_INVALID",
                "path": str(path),
                "expected": "verify_release_matrix_v3",
                "actual": summary.get("stage"),
            }
        )
    matrix_manifest_sha = _extract_manifest_sha(summary)
    if not matrix_manifest_sha:
        findings.append({"code": "V3_RELEASE_MATRIX_MANIFEST_SHA_MISSING", "path": str(path)})
    elif matrix_manifest_sha != manifest_sha256.upper():
        findings.append(
            {
                "code": "V3_RELEASE_MATRIX_MANIFEST_SHA_MISMATCH",
                "path": str(path),
                "expected": manifest_sha256.upper(),
                "actual": matrix_manifest_sha,
            }
        )
    matrix_findings = summary.get("findings")
    if not isinstance(matrix_findings, list) or matrix_findings:
        findings.append({"code": "V3_RELEASE_MATRIX_FINDINGS_NOT_EMPTY", "path": str(path), "actual": matrix_findings})
    cases = summary.get("cases") if isinstance(summary.get("cases"), list) else []
    if summary.get("case_count") != V3_MATRIX_CASE_COUNT or len(cases) != V3_MATRIX_CASE_COUNT:
        findings.append(
            {
                "code": "V3_RELEASE_MATRIX_CASE_COUNT_INVALID",
                "path": str(path),
                "expected": V3_MATRIX_CASE_COUNT,
                "reported": summary.get("case_count"),
                "actual": len(cases),
            }
        )
    required_case_fields = {
        "case_id",
        "case_sha256",
        "source_format",
        "template_pack",
        "delivery_target",
        "pipeline_report_sha256",
        "output_sha256",
    }
    normalized_cases: list[dict[str, str]] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or any(not str(case.get(field) or "").strip() for field in required_case_fields):
            findings.append({"code": "V3_RELEASE_MATRIX_CASE_INVALID", "path": str(path), "index": index})
            continue
        if any(not _is_sha256(case.get(field)) for field in ("case_sha256", "pipeline_report_sha256", "output_sha256")):
            findings.append({"code": "V3_RELEASE_MATRIX_CASE_SHA_INVALID", "path": str(path), "index": index})
            continue
        normalized_cases.append(
            {
                "case_id": str(case["case_id"]),
                "case_sha256": str(case["case_sha256"]).upper(),
                "source_format": _normalize_source_format(case["source_format"]),
                "template_pack": str(case["template_pack"]),
                "delivery_target": _normalize_target(case["delivery_target"]),
                "pipeline_report_sha256": str(case["pipeline_report_sha256"]).upper(),
                "output_sha256": str(case["output_sha256"]).upper(),
            }
        )
    case_ids = [case["case_id"] for case in normalized_cases]
    if len(case_ids) != len(set(case_ids)):
        findings.append({"code": "V3_RELEASE_MATRIX_CASE_ID_DUPLICATE", "path": str(path)})
    coverage = summary.get("coverage") if isinstance(summary.get("coverage"), dict) else {}
    source_formats = {_normalize_source_format(value) for value in coverage.get("source_formats", []) if str(value).strip()}
    delivery_targets = {_normalize_target(value) for value in coverage.get("delivery_targets", []) if str(value).strip()}
    template_packs = {str(value).strip() for value in coverage.get("template_packs", []) if str(value).strip()}
    actual_source_formats = {case["source_format"] for case in normalized_cases}
    actual_delivery_targets = {case["delivery_target"] for case in normalized_cases}
    actual_template_packs = {case["template_pack"] for case in normalized_cases}
    if source_formats != V3_REQUIRED_SOURCE_FORMATS:
        findings.append({"code": "V3_RELEASE_MATRIX_SOURCE_FORMAT_COVERAGE_INVALID", "expected": sorted(V3_REQUIRED_SOURCE_FORMATS), "actual": sorted(source_formats)})
    if delivery_targets != V3_REQUIRED_DELIVERY_TARGETS:
        findings.append({"code": "V3_RELEASE_MATRIX_TARGET_COVERAGE_INVALID", "expected": sorted(V3_REQUIRED_DELIVERY_TARGETS), "actual": sorted(delivery_targets)})
    if (
        source_formats != actual_source_formats
        or delivery_targets != actual_delivery_targets
        or template_packs != actual_template_packs
    ):
        findings.append(
            {
                "code": "V3_RELEASE_MATRIX_DECLARED_COVERAGE_MISMATCH",
                "declared": {
                    "source_formats": sorted(source_formats),
                    "delivery_targets": sorted(delivery_targets),
                    "template_packs": sorted(template_packs),
                },
                "actual": {
                    "source_formats": sorted(actual_source_formats),
                    "delivery_targets": sorted(actual_delivery_targets),
                    "template_packs": sorted(actual_template_packs),
                },
            }
        )
    if len(actual_template_packs) != 2 or not any(pack.split("@", 1)[0] == "zxty-fixed-v1" for pack in actual_template_packs):
        findings.append({"code": "V3_RELEASE_MATRIX_TEMPLATE_DIMENSION_INVALID", "actual": sorted(actual_template_packs)})
    else:
        primary = next(pack for pack in actual_template_packs if pack.split("@", 1)[0] == "zxty-fixed-v1")
        secondary = next(pack for pack in actual_template_packs if pack != primary)
        actual_dimensions = {
            (case["template_pack"], case["source_format"], case["delivery_target"])
            for case in normalized_cases
        }
        required_dimensions = {
            (primary, source_format, target)
            for source_format in ("DOC", "DOCX", "PPT", "PPTX", "PDF")
            for target in ("desktop", "mobile")
        } | {
            (secondary, source_format, target)
            for source_format in ("DOCX", "PPTX", "PDF")
            for target in ("desktop", "mobile")
        }
        if actual_dimensions != required_dimensions:
            findings.append(
                {
                    "code": "V3_RELEASE_MATRIX_DIMENSION_COVERAGE_MISMATCH",
                    "missing": sorted(required_dimensions - actual_dimensions),
                    "extra": sorted(actual_dimensions - required_dimensions),
                }
            )
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "status": _normalize_status(summary.get("status")),
        "stage": summary.get("stage"),
        "manifest_sha256": matrix_manifest_sha,
        "case_count": len(normalized_cases),
        "cases": normalized_cases,
        "coverage": {
            "source_formats": sorted(source_formats),
            "delivery_targets": sorted(delivery_targets),
            "template_packs": sorted(template_packs),
        },
    }, findings


def blind_case_binding(
    *,
    case_id: str,
    source_format: str,
    source_sha256: str,
    template_pack_id: str,
    template_pack_version: str,
    delivery_target: str,
    pipeline_report_sha256: str,
    output_sha256: str,
) -> dict[str, str]:
    return {
        "case_id": str(case_id),
        "source_format": _normalize_source_format(source_format),
        "source_sha256": str(source_sha256).upper(),
        "template_pack_id": str(template_pack_id),
        "template_pack_version": str(template_pack_version),
        "delivery_target": _normalize_target(delivery_target),
        "pipeline_report_sha256": str(pipeline_report_sha256).upper(),
        "output_sha256": str(output_sha256).upper(),
    }


def blind_case_sha256(binding: dict[str, Any]) -> str:
    canonical = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest().upper()


def _template_pack_from_pipeline(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    value = payload.get("template_pack")
    if isinstance(value, dict):
        pack_id = str(value.get("id") or "").strip() or None
        version = str(value.get("version") or "").strip() or None
        return pack_id, version
    if isinstance(value, str) and value.strip():
        return value.strip(), None
    return None, None


def _blind_case_records(summary: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(summary, dict):
        return []
    cases = summary.get("cases")
    return [item for item in cases if isinstance(item, dict)] if isinstance(cases, list) else []


def _case_binding_from_blind(case: dict[str, Any]) -> dict[str, str] | None:
    source = case.get("source") if isinstance(case.get("source"), dict) else {}
    pack = case.get("template_pack") if isinstance(case.get("template_pack"), dict) else {}
    pipeline = case.get("pipeline_report") if isinstance(case.get("pipeline_report"), dict) else {}
    output = case.get("output") if isinstance(case.get("output"), dict) else {}
    case_id = str(case.get("id") or case.get("case_id") or "").strip()
    values = {
        "case_id": case_id,
        "source_format": source.get("format") or case.get("source_format"),
        "source_sha256": source.get("actual_sha256") or source.get("sha256") or case.get("source_sha256"),
        "template_pack_id": pack.get("id") or case.get("template_pack_id"),
        "template_pack_version": pack.get("version") or case.get("template_pack_version"),
        "delivery_target": pipeline.get("delivery_target") or case.get("delivery_target"),
        "pipeline_report_sha256": pipeline.get("sha256") or case.get("pipeline_report_sha256"),
        "output_sha256": output.get("actual_sha256") or output.get("sha256") or case.get("output_sha256"),
    }
    if any(value is None or not str(value).strip() for value in values.values()):
        return None
    return blind_case_binding(**{key: str(value) for key, value in values.items()})


def _page_list(value: Any) -> list[int] | None:
    if not isinstance(value, list) or not value or any(not isinstance(item, int) or item < 1 for item in value):
        return None
    pages = list(value)
    if len(pages) != len(set(pages)) or pages != list(range(1, len(pages) + 1)):
        return None
    return pages


def _verify_v3_responsible_visual_review(
    blind_summary: dict[str, Any],
    blind_bindings_by_id: dict[str, dict[str, str]],
    expected_total_pages: int,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    evidence = blind_summary.get("evidence") if isinstance(blind_summary.get("evidence"), dict) else {}
    reference = evidence.get("responsible_engineer_visual_review")
    if not isinstance(reference, dict):
        return None, [{"code": "V3_RESPONSIBLE_VISUAL_REVIEW_REQUIRED"}]
    blind_path = Path(str(blind_summary.get("path")))
    review_path = _resolve_path(blind_path.parent, reference.get("path"))
    if review_path is None:
        return None, [{"code": "V3_RESPONSIBLE_VISUAL_REVIEW_PATH_MISSING"}]
    expected_bytes = reference.get("bytes") if isinstance(reference.get("bytes"), int) else None
    expected_sha = str(reference.get("sha256") or "").strip().upper() or None
    if expected_bytes is None or expected_bytes < 1:
        findings.append({"code": "V3_RESPONSIBLE_VISUAL_REVIEW_BYTES_MISSING", "path": str(review_path)})
    if not _is_sha256(expected_sha):
        findings.append({"code": "V3_RESPONSIBLE_VISUAL_REVIEW_SHA_MISSING", "path": str(review_path)})
    file_facts = _record_file_result(
        findings,
        code="V3_RESPONSIBLE_VISUAL_REVIEW_FILE_INVALID",
        path=review_path,
        expected_bytes=expected_bytes,
        expected_sha256=expected_sha,
    )
    if _normalize_status(reference.get("status")) != "PASS":
        findings.append({"code": "V3_RESPONSIBLE_VISUAL_REVIEW_REFERENCE_NOT_PASS", "path": str(review_path)})
    if reference.get("used_as_substitute_for_blind_visual_review") is not False:
        findings.append({"code": "V3_RESPONSIBLE_REVIEW_SUBSTITUTION_NOT_FORBIDDEN", "path": str(review_path)})
    if not review_path.is_file():
        return file_facts, findings
    try:
        payload = read_json(review_path)
    except Exception as exc:
        findings.append({"code": "V3_RESPONSIBLE_VISUAL_REVIEW_JSON_INVALID", "path": str(review_path), "message": str(exc)})
        return file_facts, findings
    if not isinstance(payload, dict):
        findings.append({"code": "V3_RESPONSIBLE_VISUAL_REVIEW_JSON_INVALID", "path": str(review_path), "message": "review must be an object"})
        return file_facts, findings
    if _normalize_status(payload.get("status")) != "PASS" or payload.get("stage") != "responsible_engineer_visual_review":
        findings.append({"code": "V3_RESPONSIBLE_VISUAL_REVIEW_NOT_PASS", "path": str(review_path)})
    if not str(payload.get("reviewer_id") or "").strip() or not str(payload.get("reviewer_role") or "").strip():
        findings.append({"code": "V3_RESPONSIBLE_VISUAL_REVIEWER_MISSING", "path": str(review_path)})
    cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    actual_ids = {str(case.get("id") or "") for case in cases if isinstance(case, dict)}
    if len(cases) != V3_GOLDEN_CASE_COUNT or actual_ids != set(blind_bindings_by_id):
        findings.append(
            {
                "code": "V3_RESPONSIBLE_VISUAL_REVIEW_CASE_COVERAGE_INVALID",
                "expected": sorted(blind_bindings_by_id),
                "actual": sorted(actual_ids),
            }
        )
    reviewed_pages = 0
    for case in cases:
        if not isinstance(case, dict):
            findings.append({"code": "V3_RESPONSIBLE_VISUAL_REVIEW_CASE_INVALID"})
            continue
        case_id = str(case.get("id") or "")
        binding = blind_bindings_by_id.get(case_id)
        wps_pages = _page_list(case.get("wps_pages_reviewed"))
        word_pages = _page_list(case.get("word_pages_reviewed"))
        expected_per_engine = case.get("expected_pages_per_engine")
        valid = (
            binding is not None
            and _normalize_status(case.get("decision")) == "PASS"
            and case.get("findings") == []
            and wps_pages is not None
            and word_pages is not None
            and isinstance(expected_per_engine, int)
            and expected_per_engine > 0
            and len(wps_pages) == expected_per_engine
            and len(word_pages) == expected_per_engine
            and str(case.get("pipeline_report_sha256") or "").upper() == binding["pipeline_report_sha256"]
        )
        if not valid:
            findings.append({"code": "V3_RESPONSIBLE_VISUAL_REVIEW_CASE_INVALID", "case_id": case_id})
            continue
        reviewed_pages += len(wps_pages) + len(word_pages)
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    if (
        coverage.get("complete") is not True
        or coverage.get("output_pages_expected") != expected_total_pages
        or coverage.get("output_pages_reviewed") != expected_total_pages
        or reviewed_pages != expected_total_pages
        or expected_total_pages <= 0
    ):
        findings.append(
            {
                "code": "V3_RESPONSIBLE_VISUAL_REVIEW_PAGE_COVERAGE_INVALID",
                "expected": expected_total_pages,
                "reviewed_from_cases": reviewed_pages,
                "coverage": coverage,
            }
        )
    if reference.get("reported_page_coverage") != expected_total_pages:
        findings.append(
            {
                "code": "V3_RESPONSIBLE_VISUAL_REVIEW_REFERENCE_COVERAGE_MISMATCH",
                "expected": expected_total_pages,
                "actual": reference.get("reported_page_coverage"),
            }
        )
    return {
        **file_facts,
        "status": _normalize_status(payload.get("status")),
        "case_count": len(cases),
        "pages_reviewed": reviewed_pages,
    }, findings


def _verify_v3_blind_visual_review(
    blind_summary: dict[str, Any],
    blind_bindings_by_id: dict[str, dict[str, str]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    visual = blind_summary.get("visual_review")
    if not isinstance(visual, dict) or not visual:
        return None, [{"code": "V3_BLIND_VISUAL_REVIEW_REQUIRED"}]
    cases = visual.get("cases") if isinstance(visual.get("cases"), list) else []
    actual_ids = {str(case.get("id") or "") for case in cases if isinstance(case, dict)}
    if len(cases) != V3_GOLDEN_CASE_COUNT or actual_ids != set(blind_bindings_by_id):
        findings.append(
            {
                "code": "V3_BLIND_VISUAL_REVIEW_CASE_COVERAGE_INVALID",
                "expected": sorted(blind_bindings_by_id),
                "actual": sorted(actual_ids),
            }
        )
    total_expected = 0
    total_reviewed = 0
    for case in cases:
        if not isinstance(case, dict):
            findings.append({"code": "V3_BLIND_VISUAL_REVIEW_CASE_INVALID"})
            continue
        case_id = str(case.get("id") or "")
        wps_pages = _page_list(case.get("wps_pages_reviewed"))
        word_pages = _page_list(case.get("word_pages_reviewed"))
        expected = case.get("pages_expected")
        reviewed = case.get("pages_reviewed")
        valid = (
            case_id in blind_bindings_by_id
            and case.get("complete") is True
            and _normalize_status(case.get("decision")) == "PASS"
            and case.get("findings") == []
            and case.get("source_brand_residual_found") is False
            and case.get("substantive_engine_difference_found") is False
            and wps_pages is not None
            and word_pages is not None
            and expected == len(wps_pages) + len(word_pages)
            and reviewed == expected
        )
        if not valid:
            findings.append({"code": "V3_BLIND_VISUAL_REVIEW_CASE_INVALID", "case_id": case_id})
            continue
        total_expected += expected
        total_reviewed += reviewed
    if (
        visual.get("complete") is not True
        or _normalize_status(visual.get("decision")) != "PASS"
        or visual.get("findings") != []
        or visual.get("total_pages_expected") != total_expected
        or visual.get("total_pages_reviewed") != total_reviewed
        or total_expected <= 0
        or total_reviewed != total_expected
    ):
        findings.append(
            {
                "code": "V3_BLIND_VISUAL_REVIEW_PAGE_COVERAGE_INVALID",
                "expected_from_cases": total_expected,
                "reviewed_from_cases": total_reviewed,
            }
        )
    return {
        "reviewer": visual.get("reviewer"),
        "case_count": len(cases),
        "total_pages_expected": total_expected,
        "total_pages_reviewed": total_reviewed,
        "complete": visual.get("complete") is True,
        "decision": _normalize_status(visual.get("decision")),
    }, findings


def _source_format_from_report(report: dict[str, Any], base: Path) -> str | None:
    explicit = report.get("source_format")
    if isinstance(explicit, str) and explicit.strip():
        return _normalize_source_format(explicit)
    source_path = _resolve_path(base, report.get("source"))
    if source_path and source_path.suffix:
        return _normalize_source_format(source_path.suffix)
    source_document = report.get("source_document")
    if isinstance(source_document, str) and source_document.strip():
        path = _resolve_path(base, source_document)
        if path and path.suffix:
            return _normalize_source_format(path.suffix)
    return None


def _verify_pipeline_report(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    summary, findings = _verify_report_file(path, label="pipeline_report", expected_status="PASS")
    if summary is None:
        return None, findings

    payload = dict(summary)
    report_sha256 = sha256_file(path)
    pipeline_status = _normalize_status(payload.get("status"))
    if pipeline_status != "PASS":
        findings.append({"code": "PIPELINE_REPORT_NOT_PASS", "path": str(path), "expected": "PASS", "actual": payload.get("status")})
    if not bool(payload.get("deliverable")):
        findings.append({"code": "PIPELINE_REPORT_NOT_DELIVERABLE", "path": str(path), "actual": payload.get("deliverable")})

    delivery_target = _normalize_target(payload.get("delivery_target") or payload.get("delivery", {}).get("target") if isinstance(payload.get("delivery"), dict) else None)
    if delivery_target and delivery_target not in target_values():
        findings.append({"code": "PIPELINE_REPORT_TARGET_INVALID", "path": str(path), "actual": delivery_target})
    delivery_format = _normalize_status(payload.get("delivery_format") or payload.get("delivery", {}).get("delivery_format") if isinstance(payload.get("delivery"), dict) else None)
    expected_format = EXPECTED_DELIVERY_FORMAT.get(delivery_target)
    if expected_format and delivery_format and delivery_format != expected_format:
        findings.append(
            {
                "code": "PIPELINE_REPORT_DELIVERY_FORMAT_MISMATCH",
                "path": str(path),
                "target": delivery_target,
                "expected": expected_format,
                "actual": delivery_format,
            }
        )
    validation_profile = payload.get("validation_profile")
    expected_profile = EXPECTED_VALIDATION_PROFILE.get(delivery_target)
    if expected_profile and validation_profile != expected_profile:
        findings.append(
            {
                "code": "PIPELINE_REPORT_VALIDATION_PROFILE_MISMATCH",
                "path": str(path),
                "target": delivery_target,
                "expected": expected_profile,
                "actual": validation_profile,
            }
        )

    delivery = payload.get("delivery")
    if not isinstance(delivery, dict):
        findings.append({"code": "PIPELINE_DELIVERY_SECTION_MISSING", "path": str(path)})
        delivery = {}
    required_renderers = {str(item).strip().lower() for item in delivery.get("required_renderers", []) if str(item).strip()}
    if required_renderers != EXPECTED_RENDERERS:
        findings.append(
            {
                "code": "PIPELINE_DELIVERY_RENDERERS_MISMATCH",
                "path": str(path),
                "expected": sorted(EXPECTED_RENDERERS),
                "actual": sorted(required_renderers),
            }
        )
    if delivery_target and delivery.get("target") and _normalize_target(delivery.get("target")) != delivery_target:
        findings.append(
            {
                "code": "PIPELINE_DELIVERY_TARGET_MISMATCH",
                "path": str(path),
                "expected": delivery_target,
                "actual": delivery.get("target"),
            }
        )
    if delivery_format and delivery.get("delivery_format") and _normalize_status(delivery.get("delivery_format")) != delivery_format:
        findings.append(
            {
                "code": "PIPELINE_DELIVERY_FORMAT_MISMATCH",
                "path": str(path),
                "expected": delivery_format,
                "actual": delivery.get("delivery_format"),
            }
        )
    if expected_profile and delivery.get("validation_profile") and delivery.get("validation_profile") != expected_profile:
        findings.append(
            {
                "code": "PIPELINE_DELIVERY_PROFILE_MISMATCH",
                "path": str(path),
                "expected": expected_profile,
                "actual": delivery.get("validation_profile"),
            }
        )

    output_candidates = [payload.get("output"), payload.get("deliverable_path"), delivery.get("path"), delivery.get("output")]
    resolved_outputs = [str(_resolve_path(path.parent, candidate)) for candidate in output_candidates if candidate]
    if not resolved_outputs:
        findings.append({"code": "PIPELINE_OUTPUT_MISSING", "path": str(path)})
        output_path = None
    else:
        output_path = Path(resolved_outputs[0])
        if len(set(resolved_outputs)) > 1:
            findings.append(
                {
                    "code": "PIPELINE_OUTPUT_PATH_MISMATCH",
                    "path": str(path),
                    "candidates": resolved_outputs,
                }
            )
    output_sha256 = str(payload.get("output_sha256") or "").strip().upper()
    if output_path is not None:
        if not output_path.is_file():
            findings.append({"code": "PIPELINE_OUTPUT_MISSING", "path": str(path), "output": str(output_path)})
        else:
            actual_output_sha = sha256_file(output_path)
            if output_sha256 and actual_output_sha.upper() != output_sha256:
                findings.append(
                    {
                        "code": "PIPELINE_OUTPUT_SHA_MISMATCH",
                        "path": str(path),
                        "output": str(output_path),
                        "expected": output_sha256,
                        "actual": actual_output_sha,
                    }
                )
            output_sha256 = actual_output_sha

    stages = _stage_map(payload.get("stages"))
    for required_stage in REQUIRED_PIPELINE_STAGES:
        if required_stage not in stages:
            findings.append({"code": "PIPELINE_STAGE_MISSING", "path": str(path), "stage": required_stage})

    structure_stage = stages.get("verify_document_structure")
    if structure_stage and _normalize_status(structure_stage.get("status")) != "PASS":
        findings.append({"code": "PIPELINE_STRUCTURE_NOT_PASS", "path": str(path), "actual": structure_stage.get("status")})

    export_engine_map: dict[str, dict[str, Any]] = {}
    export_stage = stages.get("export_word_wps")
    if export_stage:
        if _normalize_status(export_stage.get("status")) != "PASS":
            findings.append({"code": "PIPELINE_EXPORT_NOT_PASS", "path": str(path), "actual": export_stage.get("status")})
        engines = export_stage.get("engines")
        if not isinstance(engines, list):
            findings.append({"code": "PIPELINE_EXPORT_ENGINES_INVALID", "path": str(path)})
        else:
            engine_names: list[str] = []
            engine_entries: dict[str, dict[str, Any]] = {}
            for engine in engines:
                if not isinstance(engine, dict) or not isinstance(engine.get("engine"), str) or not engine["engine"].strip():
                    findings.append({"code": "PIPELINE_EXPORT_ENGINE_ENTRY_INVALID", "path": str(path), "entry": engine})
                    continue
                engine_name = engine["engine"].strip().lower()
                engine_names.append(engine_name)
                if engine_name in engine_entries:
                    findings.append({"code": "PIPELINE_EXPORT_ENGINE_DUPLICATE", "path": str(path), "engine": engine_name})
                    continue
                engine_entries[engine_name] = engine
            export_engine_map = engine_entries
            if len(engine_names) != len(set(engine_names)):
                findings.append({"code": "PIPELINE_EXPORT_ENGINE_DUPLICATE", "path": str(path), "engines": engine_names})
            if set(engine_names) != EXPECTED_RENDERERS or len(engine_names) != len(EXPECTED_RENDERERS):
                findings.append({"code": "PIPELINE_EXPORT_ENGINE_SET_MISMATCH", "path": str(path), "expected": sorted(EXPECTED_RENDERERS), "actual": sorted(engine_names)})
            resolved_pdfs: dict[str, dict[str, Any]] = {}
            for engine_name, contract in EXPECTED_ENGINE_CONTRACT.items():
                engine = engine_entries.get(engine_name)
                if engine is None:
                    continue
                for field, expected in contract.items():
                    if engine.get(field) != expected:
                        findings.append(
                            {
                                "code": "PIPELINE_EXPORT_ENGINE_CONTRACT_MISMATCH",
                                "path": str(path),
                                "engine": engine_name,
                                "field": field,
                                "expected": expected,
                                "actual": engine.get(field),
                            }
                        )
                for field in ("identity_verified", "ownership_verified"):
                    if engine.get(field) is not True:
                        findings.append(
                            {
                                "code": "PIPELINE_EXPORT_ENGINE_VERIFICATION_MISSING",
                                "path": str(path),
                                "engine": engine_name,
                                "field": field,
                                "expected": True,
                                "actual": engine.get(field),
                            }
                        )
            for engine_name in sorted(EXPECTED_RENDERERS & set(export_engine_map)):
                engine = export_engine_map[engine_name]
                if _normalize_status(engine.get("status")) != "PASS":
                    findings.append(
                        {
                            "code": "PIPELINE_EXPORT_ENGINE_NOT_PASS",
                            "path": str(path),
                            "engine": engine_name,
                            "actual": engine.get("status"),
                        }
                    )
                pdf_path = _resolve_path(path.parent, engine.get("pdf"))
                if pdf_path is None or not pdf_path.is_file():
                    findings.append(
                        {
                            "code": "PIPELINE_EXPORT_ENGINE_PDF_MISSING",
                            "path": str(path),
                            "engine": engine_name,
                            "pdf": str(pdf_path) if pdf_path else None,
                        }
                    )
                    continue
                resolved_pdfs[engine_name] = {
                    "path": pdf_path.resolve(),
                    "bytes": pdf_path.stat().st_size,
                    "sha256": sha256_file(pdf_path).upper(),
                }
                if resolved_pdfs[engine_name]["bytes"] <= 0:
                    findings.append({"code": "PIPELINE_EXPORT_ENGINE_PDF_EMPTY", "path": str(path), "engine": engine_name, "pdf": str(pdf_path)})
                actual_bytes = pdf_path.stat().st_size
                if engine.get("bytes") != actual_bytes:
                    findings.append({"code": "PIPELINE_EXPORT_ENGINE_BYTES_MISMATCH", "path": str(path), "engine": engine_name, "expected": engine.get("bytes"), "actual": actual_bytes})
                actual_sha256 = resolved_pdfs[engine_name]["sha256"]
                if str(engine.get("sha256") or "").upper() != actual_sha256.upper():
                    findings.append({"code": "PIPELINE_EXPORT_ENGINE_SHA_MISMATCH", "path": str(path), "engine": engine_name, "expected": engine.get("sha256"), "actual": actual_sha256})
            if len(resolved_pdfs) == 2:
                word_pdf = resolved_pdfs["word"]
                wps_pdf = resolved_pdfs["wps"]
                if word_pdf["path"] == wps_pdf["path"]:
                    findings.append({"code": "PIPELINE_EXPORT_PDFS_NOT_DISTINCT", "path": str(path), "pdf": str(word_pdf["path"])})
                if word_pdf["bytes"] == wps_pdf["bytes"]:
                    findings.append({"code": "PIPELINE_EXPORT_PDF_BYTES_NOT_DISTINCT", "path": str(path), "bytes": word_pdf["bytes"]})
                if word_pdf["sha256"] == wps_pdf["sha256"]:
                    findings.append({"code": "PIPELINE_EXPORT_PDF_HASHES_NOT_DISTINCT", "path": str(path), "sha256": word_pdf["sha256"]})
    analyze_stage = stages.get("analyze_rendered_pages")
    if analyze_stage:
        if _normalize_status(analyze_stage.get("status")) != "PASS":
            findings.append({"code": "PIPELINE_ANALYSIS_NOT_PASS", "path": str(path), "actual": analyze_stage.get("status")})
        render_data = [(engine_name, analyze_stage.get(engine_name)) for engine_name in sorted(EXPECTED_RENDERERS)]
        if any(not isinstance(render, dict) for _, render in render_data):
            findings.append({"code": "PIPELINE_ANALYSIS_RENDER_DATA_MISSING", "path": str(path), "engines": sorted(EXPECTED_RENDERERS)})
        else:
            for engine_name, render in render_data:
                export_pdf = _resolve_path(path.parent, export_engine_map.get(engine_name, {}).get("pdf"))
                analysis_pdf = _resolve_path(path.parent, render.get("pdf"))
                if export_pdf is None or analysis_pdf is None or export_pdf.resolve() != analysis_pdf.resolve():
                    findings.append({"code": "PIPELINE_ANALYSIS_EXPORT_PDF_MISMATCH", "path": str(path), "engine": engine_name, "export_pdf": str(export_pdf) if export_pdf else None, "analysis_pdf": str(analysis_pdf) if analysis_pdf else None})
                page_count = render.get("page_count")
                pages = render.get("pages")
                if not isinstance(page_count, int) or page_count <= 0:
                    findings.append(
                        {
                            "code": "PIPELINE_ANALYSIS_PAGE_COUNT_INVALID",
                            "path": str(path),
                            "engine": engine_name,
                            "actual": page_count,
                        }
                    )
                if not isinstance(pages, list) or not pages:
                    findings.append(
                        {
                            "code": "PIPELINE_ANALYSIS_PAGES_MISSING",
                            "path": str(path),
                            "engine": engine_name,
                        }
                    )
                else:
                    for page in pages:
                        if not isinstance(page, dict):
                            findings.append(
                                {
                                    "code": "PIPELINE_ANALYSIS_PAGE_INVALID",
                                    "path": str(path),
                                    "engine": engine_name,
                                }
                            )
                            continue
                        png_path = _resolve_path(path.parent, page.get("png"))
                        if png_path is None or not png_path.is_file():
                            findings.append(
                                {
                                    "code": "PIPELINE_ANALYSIS_RENDER_IMAGE_MISSING",
                                    "path": str(path),
                                    "engine": engine_name,
                                    "png": str(png_path) if png_path else None,
                                }
                            )
            page_counts = {engine_name: render.get("page_count") for engine_name, render in render_data}
            if page_counts.get("word") != page_counts.get("wps"):
                findings.append({"code": "PIPELINE_ANALYSIS_ENGINE_PAGE_COUNT_MISMATCH", "path": str(path), **page_counts})
            expected_comparison_pages = list(range(1, page_counts.get("word", 0) + 1)) if isinstance(page_counts.get("word"), int) and page_counts.get("word", 0) > 0 else []
            comparisons = analyze_stage.get("comparisons")
            comparison_pages = [item.get("page") for item in comparisons if isinstance(item, dict)] if isinstance(comparisons, list) else []
            if comparison_pages != expected_comparison_pages:
                findings.append({"code": "PIPELINE_ANALYSIS_COMPARISON_COVERAGE_MISMATCH", "path": str(path), "expected": expected_comparison_pages, "actual": comparison_pages})
    for engine_name in sorted(EXPECTED_RENDERERS):
        stage_name = f"scan_source_identity_rendered_{engine_name}"
        identity_stage = stages.get(stage_name)
        if not identity_stage:
            continue
        if _normalize_status(identity_stage.get("status")) != "PASS":
            findings.append(
                {
                    "code": "PIPELINE_RENDERED_IDENTITY_SCAN_NOT_PASS",
                    "path": str(path),
                    "engine": engine_name,
                    "actual": identity_stage.get("status"),
                }
            )
            continue

        term_count = identity_stage.get("term_count")
        if not isinstance(term_count, int) or term_count < 0:
            findings.append(
                {
                    "code": "PIPELINE_RENDERED_IDENTITY_TERM_COUNT_INVALID",
                    "path": str(path),
                    "engine": engine_name,
                    "actual": term_count,
                }
            )
            continue

        if term_count == 0:
            review = identity_stage.get("identity_review")
            reviewer = review.get("reviewer") if isinstance(review, dict) else None
            required_review_fields = ("source_sha256", "reviewed_at", "scope")
            review_valid = (
                isinstance(review, dict)
                and review.get("status") == "NO_SOURCE_IDENTITY_FOUND"
                and all(str(review.get(field) or "").strip() for field in required_review_fields)
                and isinstance(reviewer, dict)
                and bool(str(reviewer.get("id") or "").strip())
                and bool(str(reviewer.get("role") or "").strip())
            )
            if not review_valid:
                findings.append(
                    {
                        "code": "PIPELINE_RENDERED_IDENTITY_REVIEW_INVALID",
                        "path": str(path),
                        "engine": engine_name,
                    }
                )
            continue

        analysis_render = analyze_stage.get(engine_name) if isinstance(analyze_stage, dict) else None
        page_count = analysis_render.get("page_count") if isinstance(analysis_render, dict) else None
        expected_pages = list(range(1, page_count + 1)) if isinstance(page_count, int) and page_count > 0 else []
        reported_expected = identity_stage.get("ocr_expected_pages")
        scanned_pages = identity_stage.get("ocr_scanned_pages")
        failed_pages = identity_stage.get("ocr_failed_pages")
        if identity_stage.get("ocr_scanned") is not True:
            findings.append(
                {
                    "code": "PIPELINE_RENDERED_IDENTITY_OCR_NOT_RUN",
                    "path": str(path),
                    "engine": engine_name,
                }
            )
        if not isinstance(identity_stage.get("ocr_backends"), list) or not identity_stage.get("ocr_backends"):
            findings.append(
                {
                    "code": "PIPELINE_RENDERED_IDENTITY_OCR_BACKEND_MISSING",
                    "path": str(path),
                    "engine": engine_name,
                }
            )
        if reported_expected != expected_pages or scanned_pages != expected_pages:
            findings.append(
                {
                    "code": "PIPELINE_RENDERED_IDENTITY_PAGE_COVERAGE_MISMATCH",
                    "path": str(path),
                    "engine": engine_name,
                    "expected": expected_pages,
                    "reported_expected": reported_expected,
                    "scanned": scanned_pages,
                }
            )
        if failed_pages != []:
            findings.append(
                {
                    "code": "PIPELINE_RENDERED_IDENTITY_OCR_FAILURES_PRESENT",
                    "path": str(path),
                    "engine": engine_name,
                    "failed_pages": failed_pages,
                }
            )
    final_stage = stages.get("final_delivery")
    if final_stage:
        if _normalize_status(final_stage.get("status")) != "PASS":
            findings.append({"code": "PIPELINE_FINAL_DELIVERY_NOT_PASS", "path": str(path), "actual": final_stage.get("status")})
        final_output = _resolve_path(path.parent, final_stage.get("output"))
        if output_path and final_output and final_output.resolve() != output_path.resolve():
            findings.append(
                {
                    "code": "PIPELINE_FINAL_DELIVERY_OUTPUT_MISMATCH",
                    "path": str(path),
                    "expected": str(output_path),
                    "actual": str(final_output),
                }
            )
    if delivery and output_path:
        delivery_output = _resolve_path(path.parent, delivery.get("path"))
        if delivery_output and output_path.resolve() != delivery_output.resolve():
            findings.append(
                {
                    "code": "PIPELINE_DELIVERY_PATH_MISMATCH",
                    "path": str(path),
                    "expected": str(output_path),
                    "actual": str(delivery_output),
                }
            )
    if delivery_target == "mobile" and output_path and output_path.is_file():
        expected_renderer = "wps_pdf_export"
        renderer_values = {
            "top": payload.get("delivery_renderer"),
            "delivery": delivery.get("renderer") if isinstance(delivery, dict) else None,
            "final_delivery": final_stage.get("renderer") if isinstance(final_stage, dict) else None,
        }
        if any(value != expected_renderer for value in renderer_values.values()):
            findings.append({"code": "PIPELINE_MOBILE_RENDERER_MISMATCH", "path": str(path), "expected": expected_renderer, "actual": renderer_values})
        wps_pdf = _resolve_path(path.parent, export_engine_map.get("wps", {}).get("pdf"))
        if wps_pdf is None or not wps_pdf.is_file() or sha256_file(wps_pdf) != sha256_file(output_path):
            findings.append({"code": "PIPELINE_MOBILE_OUTPUT_NOT_WPS_EXPORT", "path": str(path), "output": str(output_path), "wps_pdf": str(wps_pdf) if wps_pdf else None})

    source_format = _source_format_from_report(payload, path.parent)
    template_pack_id, template_pack_version = _template_pack_from_pipeline(payload)
    summary.update(
        {
            "path": str(path),
            "sha256": report_sha256,
            "status": pipeline_status or None,
            "deliverable": bool(payload.get("deliverable")),
            "delivery_target": delivery_target or None,
            "delivery_format": delivery_format or None,
            "validation_profile": validation_profile,
            "output": str(output_path) if output_path else None,
            "output_sha256": output_sha256 or None,
            "source_format": source_format,
            "source_sha256": str(payload.get("source_sha256") or "").upper() or None,
            "template_pack_id": template_pack_id,
            "template_pack_version": template_pack_version,
            "case_id": payload.get("case_id"),
            "case_sha256": str(payload.get("case_sha256") or "").upper() or None,
            "stages": {
                name: {
                    "status": _normalize_status(stage.get("status")) or None,
                }
                | (
                    {
                        "engines": stage.get("engines")
                    }
                    if name == "export_word_wps"
                    else {}
                )
                | (
                    {
                        "word": stage.get("word"),
                        "wps": stage.get("wps"),
                        "comparisons": stage.get("comparisons"),
                    }
                    if name == "analyze_rendered_pages"
                    else {}
                )
                | (
                    {
                        "output": stage.get("output"),
                        "renderer": stage.get("renderer"),
                        "validation_profile": stage.get("validation_profile"),
                    }
                    if name == "final_delivery"
                    else {}
                )
                for name, stage in stages.items()
            },
            "delivery": delivery,
        }
    )
    return summary, findings


def evaluate_release(
    manifest_path: Path,
    post_install_report: Path,
    pipeline_reports: Sequence[Path],
    blind_report: Path,
    *,
    release_matrix_report: Path | None = None,
    required_targets: Sequence[str] = (),
    required_source_formats: Sequence[str] = (),
    required_template_packs: Sequence[str] = (),
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    manifest_summary, manifest_findings = _collect_manifest_evidence(manifest_path)
    findings.extend(manifest_findings)
    manifest_sha256 = manifest_summary["sha256"] if manifest_summary else ""

    post_install_summary, post_install_findings = _verify_post_install_report(post_install_report)
    findings.extend(post_install_findings)

    blind_summary, blind_findings = (None, [])
    if manifest_summary is not None:
        blind_summary, blind_findings = _verify_blind_report(blind_report, manifest_sha256)
    else:
        blind_findings.append({"code": "BLIND_REPORT_SKIPPED", "path": str(blind_report), "message": "manifest evidence unavailable"})
    findings.extend(blind_findings)

    pipeline_summaries: list[dict[str, Any]] = []
    covered_targets: set[str] = set()
    covered_source_formats: set[str] = set()
    covered_template_packs: set[str] = set()
    for index, report_path in enumerate(pipeline_reports):
        summary, report_findings = _verify_pipeline_report(report_path)
        findings.extend(report_findings)
        if summary is None:
            continue
        pipeline_summaries.append(summary)
        if summary.get("delivery_target"):
            covered_targets.add(summary["delivery_target"])
        if summary.get("source_format"):
            covered_source_formats.add(summary["source_format"])
        if summary.get("template_pack_id"):
            pack_key = summary["template_pack_id"]
            if summary.get("template_pack_version"):
                pack_key = f"{pack_key}@{summary['template_pack_version']}"
            covered_template_packs.add(pack_key)

    required_targets_normalized = {_normalize_target(value) for value in required_targets if str(value).strip()}
    required_source_formats_normalized = {_normalize_source_format(value) for value in required_source_formats if str(value).strip()}
    missing_targets = sorted(required_targets_normalized - covered_targets)
    if missing_targets:
        findings.append(
            {
                "code": "REQUIRED_TARGET_UNMET",
                "expected": sorted(required_targets_normalized),
                "actual": sorted(covered_targets),
                "missing": missing_targets,
            }
        )
    missing_source_formats = sorted(required_source_formats_normalized - covered_source_formats)
    if missing_source_formats:
        findings.append(
            {
                "code": "REQUIRED_SOURCE_FORMAT_UNMET",
                "expected": sorted(required_source_formats_normalized),
                "actual": sorted(covered_source_formats),
                "missing": missing_source_formats,
            }
        )

    required_template_packs_normalized = {str(value).strip() for value in required_template_packs if str(value).strip()}
    missing_template_packs = sorted(required_template_packs_normalized - covered_template_packs)
    if missing_template_packs:
        findings.append(
            {
                "code": "REQUIRED_TEMPLATE_PACK_UNMET",
                "expected": sorted(required_template_packs_normalized),
                "actual": sorted(covered_template_packs),
                "missing": missing_template_packs,
            }
        )

    # V3 的流水线必须与盲验案例逐项绑定，不能只依赖 blind status 和 manifest SHA。
    v3_case_binding_required = bool(required_template_packs_normalized) or any(
        summary.get("template_pack_version") for summary in pipeline_summaries
    )
    matrix_summary: dict[str, Any] | None = None
    if v3_case_binding_required:
        if release_matrix_report is None:
            findings.append({"code": "V3_RELEASE_MATRIX_REPORT_REQUIRED"})
        elif manifest_summary is None:
            findings.append({"code": "V3_RELEASE_MATRIX_REPORT_SKIPPED", "path": str(release_matrix_report), "message": "manifest evidence unavailable"})
        else:
            matrix_summary, matrix_findings = _verify_release_matrix_report(release_matrix_report, manifest_sha256)
            findings.extend(matrix_findings)
        if len(required_template_packs_normalized) != 2:
            findings.append(
                {
                    "code": "V3_REQUIRED_TEMPLATE_PACKS_INVALID",
                    "expected_count": 2,
                    "actual": sorted(required_template_packs_normalized),
                }
            )
        if len(pipeline_summaries) != V3_GOLDEN_CASE_COUNT:
            findings.append(
                {
                    "code": "V3_GOLDEN_PIPELINE_COUNT_INVALID",
                    "expected": V3_GOLDEN_CASE_COUNT,
                    "actual": len(pipeline_summaries),
                }
            )
    blind_cases = _blind_case_records(blind_summary) if v3_case_binding_required else []
    if v3_case_binding_required and len(blind_cases) != V3_GOLDEN_CASE_COUNT:
        findings.append(
            {
                "code": "V3_GOLDEN_BLIND_CASE_COUNT_INVALID",
                "expected": V3_GOLDEN_CASE_COUNT,
                "actual": len(blind_cases),
            }
        )
    blind_bindings: dict[str, dict[str, str]] = {}
    blind_bindings_by_id: dict[str, dict[str, str]] = {}
    for case in blind_cases:
        binding = _case_binding_from_blind(case)
        if binding is None:
            findings.append({"code": "BLIND_CASE_BINDING_INVALID", "case_id": case.get("id") or case.get("case_id")})
            continue
        declared_sha = str(case.get("case_sha256") or "").strip().upper()
        computed_sha = blind_case_sha256(binding)
        if not declared_sha:
            findings.append({"code": "BLIND_CASE_SHA256_MISSING", "case_id": binding["case_id"]})
            continue
        if declared_sha != computed_sha:
            findings.append(
                {
                    "code": "BLIND_CASE_SHA256_MISMATCH",
                    "case_id": binding["case_id"],
                    "expected": computed_sha,
                    "actual": declared_sha,
                }
            )
            continue
        if declared_sha in blind_bindings or binding["case_id"] in blind_bindings_by_id:
            findings.append({"code": "BLIND_CASE_DUPLICATE", "case_id": binding["case_id"], "case_sha256": declared_sha})
            continue
        blind_bindings[declared_sha] = binding
        blind_bindings_by_id[binding["case_id"]] = binding

    responsible_visual_summary: dict[str, Any] | None = None
    blind_visual_summary: dict[str, Any] | None = None
    if v3_case_binding_required:
        blind_formats = {binding["source_format"] for binding in blind_bindings.values()}
        blind_targets = {binding["delivery_target"] for binding in blind_bindings.values()}
        blind_packs = {f"{binding['template_pack_id']}@{binding['template_pack_version']}" for binding in blind_bindings.values()}
        if blind_formats != V3_REQUIRED_SOURCE_FORMATS:
            findings.append(
                {
                    "code": "V3_GOLDEN_SOURCE_FORMAT_COVERAGE_INVALID",
                    "expected": sorted(V3_REQUIRED_SOURCE_FORMATS),
                    "actual": sorted(blind_formats),
                }
            )
        if blind_targets != V3_REQUIRED_DELIVERY_TARGETS:
            findings.append(
                {
                    "code": "V3_GOLDEN_TARGET_COVERAGE_INVALID",
                    "expected": sorted(V3_REQUIRED_DELIVERY_TARGETS),
                    "actual": sorted(blind_targets),
                }
            )
        if blind_packs != required_template_packs_normalized:
            findings.append(
                {
                    "code": "V3_GOLDEN_TEMPLATE_COVERAGE_INVALID",
                    "expected": sorted(required_template_packs_normalized),
                    "actual": sorted(blind_packs),
                }
            )
        blind_visual_summary, visual_findings = _verify_v3_blind_visual_review(blind_summary or {}, blind_bindings_by_id)
        findings.extend(visual_findings)
        expected_visual_pages = blind_visual_summary.get("total_pages_expected", 0) if blind_visual_summary else 0
        responsible_visual_summary, responsible_findings = _verify_v3_responsible_visual_review(
            blind_summary or {},
            blind_bindings_by_id,
            int(expected_visual_pages),
        )
        findings.extend(responsible_findings)
        matrix_cases = matrix_summary.get("cases", []) if isinstance(matrix_summary, dict) else []
        matrix_packs = set(matrix_summary.get("coverage", {}).get("template_packs", [])) if isinstance(matrix_summary, dict) else set()
        if matrix_summary is not None and matrix_packs != required_template_packs_normalized:
            findings.append(
                {
                    "code": "V3_RELEASE_MATRIX_TEMPLATE_COVERAGE_INVALID",
                    "expected": sorted(required_template_packs_normalized),
                    "actual": sorted(matrix_packs),
                }
            )
        for binding in blind_bindings.values():
            pack = f"{binding['template_pack_id']}@{binding['template_pack_version']}"
            matches = [
                case
                for case in matrix_cases
                if case.get("source_format") == binding["source_format"]
                and case.get("delivery_target") == binding["delivery_target"]
                and case.get("template_pack") == pack
                and case.get("pipeline_report_sha256") == binding["pipeline_report_sha256"]
                and case.get("output_sha256") == binding["output_sha256"]
            ]
            if len(matches) != 1:
                findings.append(
                    {
                        "code": "V3_GOLDEN_CASE_NOT_IN_RELEASE_MATRIX",
                        "case_id": binding["case_id"],
                        "matching_case_count": len(matches),
                    }
                )

    for summary in pipeline_summaries:
        # 只有声明 pack version 的报告才属于 V3；V2 历史报告保持兼容读取。
        if not summary.get("template_pack_version"):
            continue
        case_sha = str(summary.get("case_sha256") or "").upper()
        candidates = [
            (blind_sha, blind_binding)
            for blind_sha, blind_binding in blind_bindings.items()
            if blind_binding["source_format"] == summary.get("source_format")
            and blind_binding["delivery_target"] == summary.get("delivery_target")
            and blind_binding["template_pack_id"] == summary.get("template_pack_id")
            and blind_binding["template_pack_version"] == summary.get("template_pack_version")
            and blind_binding["pipeline_report_sha256"] == summary.get("sha256")
            and blind_binding["output_sha256"] == summary.get("output_sha256")
            and (
                not summary.get("source_sha256")
                or blind_binding["source_sha256"] == summary.get("source_sha256")
            )
        ]
        if len(candidates) != 1:
            findings.append(
                {
                    "code": "PIPELINE_BLIND_CASE_NOT_COVERED",
                    "path": summary.get("path"),
                    "case_sha256": case_sha or None,
                    "matching_case_count": len(candidates),
                }
            )
            continue
        matched_sha, binding = candidates[0]
        if case_sha and case_sha != matched_sha:
            findings.append(
                {
                    "code": "PIPELINE_CASE_SHA256_MISMATCH",
                    "path": summary.get("path"),
                    "expected": matched_sha,
                    "actual": case_sha,
                }
            )
        expected_pack = f"{binding['template_pack_id']}@{binding['template_pack_version']}"
        actual_pack = f"{summary.get('template_pack_id')}@{summary.get('template_pack_version')}"
        comparisons = {
            "source_format": (binding["source_format"], summary.get("source_format")),
            "delivery_target": (binding["delivery_target"], summary.get("delivery_target")),
            "template_pack": (expected_pack, actual_pack),
            "pipeline_report_sha256": (binding["pipeline_report_sha256"], summary.get("sha256")),
            "output_sha256": (binding["output_sha256"], summary.get("output_sha256")),
        }
        for field, (expected, actual) in comparisons.items():
            if str(expected or "").upper() != str(actual or "").upper():
                findings.append(
                    {
                        "code": "BLIND_CASE_PIPELINE_BINDING_MISMATCH",
                        "path": summary.get("path"),
                        "field": field,
                        "expected": expected,
                        "actual": actual,
                    }
                )

    status = "PASS" if not findings else "RELEASE_HOLD"
    report = {
        "status": status,
        "stage": "verify_release",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "findings": findings,
        "manifest": manifest_summary,
        "post_install": post_install_summary,
        "blind_report": blind_summary,
        "release_matrix_report": matrix_summary,
        "responsible_engineer_visual_review": responsible_visual_summary,
        "independent_blind_visual_review": blind_visual_summary,
        "pipeline_reports": pipeline_summaries,
        "coverage": {
            "required_targets": sorted(required_targets_normalized),
            "required_source_formats": sorted(required_source_formats_normalized),
            "covered_targets": sorted(covered_targets),
            "covered_source_formats": sorted(covered_source_formats),
            "required_template_packs": sorted(required_template_packs_normalized),
            "covered_template_packs": sorted(covered_template_packs),
            "blind_case_sha256": sorted(blind_bindings),
        },
    }
    return report


def emit_report(payload: dict[str, Any], output: Path | None = None) -> int:
    if output is not None:
        write_json(output, payload)
    try:
        import sys

        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("status") == "PASS" else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布门禁：校验 manifest、安装后自检、流水线报告和独立盲验")
    parser.add_argument("pipeline_reports", nargs="*", type=Path, help="一个或多个 pipeline_report.json 路径")
    parser.add_argument("--manifest", type=Path, required=True, help="manifest.json 路径")
    parser.add_argument("--post-install-report", type=Path, required=True, help="post_install 报告路径")
    parser.add_argument("--blind-report", type=Path, required=True, help="独立盲验报告路径")
    parser.add_argument("--release-matrix-report", type=Path, help="V3 的 16 案例矩阵验证报告；V3 发布必需")
    parser.add_argument("--pipeline-report", dest="pipeline_reports_opt", action="append", type=Path, default=[], help="可重复传入的 pipeline_report.json 路径")
    parser.add_argument("--require-target", action="append", choices=target_values(), default=[], help="要求覆盖的交付目标，可重复")
    parser.add_argument("--require-source-format", action="append", default=[], help="要求覆盖的源文件格式，可重复，例如 PDF、DOCX")
    parser.add_argument("--require-template-pack", action="append", default=[], help="要求覆盖的模板包，可重复，例如 zxty-fixed-v1@1")
    parser.add_argument("--report", type=Path, help="把结构化 JSON 报告写入该路径")
    args = parser.parse_args(argv)

    reports = [*args.pipeline_reports, *args.pipeline_reports_opt]
    if not reports:
        parser.error("至少需要一个 pipeline_report")

    payload = evaluate_release(
        args.manifest,
        args.post_install_report,
        reports,
        args.blind_report,
        release_matrix_report=args.release_matrix_report,
        required_targets=args.require_target,
        required_source_formats=args.require_source_format,
        required_template_packs=args.require_template_pack,
    )
    return emit_report(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
