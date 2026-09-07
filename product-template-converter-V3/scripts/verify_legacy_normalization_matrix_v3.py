from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from typing import Any

from pipeline_common import finish, read_json, result, sha256_file


def _stage(payload: dict[str, Any], name: str) -> dict[str, Any]:
    for stage in payload.get("stages", []):
        if isinstance(stage, dict) and stage.get("stage") == name:
            return stage
    return {}


def _expectations(values: list[str]) -> dict[str, int]:
    output: dict[str, int] = {}
    for value in values:
        name, separator, count = value.partition("=")
        name = name.strip().lower()
        if separator != "=" or name not in {"doc", "ppt"}:
            raise ValueError(f"invalid --expect value: {value}")
        output[name] = int(count)
    return output


def verify_reports(paths: list[Path], expected: dict[str, int]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for raw_path in paths:
        path = raw_path.resolve()
        if not path.is_file():
            findings.append({"code": "LEGACY_PIPELINE_REPORT_MISSING", "path": str(path)})
            continue
        pipeline = read_json(path)
        probe = _stage(pipeline, "format_probe")
        normalize = _stage(pipeline, "normalize_source")
        visual = _stage(pipeline, "legacy_visual_compare_v3")
        source_format = str(probe.get("detected_format") or "").lower()
        counts[source_format] += 1
        case_findings = []
        if source_format not in {"doc", "ppt"}:
            case_findings.append("LEGACY_SOURCE_FORMAT_INVALID")
        if normalize.get("status") != "PASS" or not normalize.get("format_changed"):
            case_findings.append("LEGACY_NORMALIZATION_NOT_PASS")
        if visual.get("status") != "PASS":
            case_findings.append("LEGACY_VISUAL_COMPARE_NOT_PASS")
        if visual.get("original_page_count") != visual.get("normalized_page_count"):
            case_findings.append("LEGACY_PAGE_COUNT_CHANGED")
        if pipeline.get("status") != "HUMAN_REVIEW" or pipeline.get("deliverable") is not False:
            case_findings.append("LEGACY_PIPELINE_DID_NOT_STOP_FOR_REVIEW")
        if case_findings:
            findings.append({
                "code": "LEGACY_NORMALIZATION_CASE_FAILED",
                "pipeline_report": str(path),
                "case_findings": case_findings,
            })
        cases.append({
            "source": probe.get("source", {}).get("path"),
            "source_format": source_format,
            "source_sha256": probe.get("source", {}).get("sha256"),
            "normalized": normalize.get("normalized"),
            "normalized_sha256": normalize.get("normalized_sha256"),
            "application": normalize.get("application"),
            "application_version": normalize.get("application_version"),
            "original_pdf_sha256": visual.get("original_pdf", {}).get("sha256"),
            "normalized_pdf_sha256": visual.get("normalized_pdf", {}).get("sha256"),
            "original_page_count": visual.get("original_page_count"),
            "normalized_page_count": visual.get("normalized_page_count"),
            "visual_status": visual.get("status"),
            "pipeline_status": pipeline.get("status"),
            "deliverable": pipeline.get("deliverable"),
            "pipeline_report": str(path),
            "pipeline_report_sha256": sha256_file(path),
            "task_bundle_count": len(pipeline.get("task_bundles", [])),
        })
    for source_format, expected_count in sorted(expected.items()):
        actual = counts.get(source_format, 0)
        if actual != expected_count:
            findings.append({
                "code": "LEGACY_NORMALIZATION_COUNT_MISMATCH",
                "source_format": source_format,
                "expected": expected_count,
                "actual": actual,
            })
    return result(
        "FAIL" if findings else "PASS",
        "legacy_normalization_matrix_v3",
        findings=findings,
        counts={name: counts.get(name, 0) for name in ("doc", "ppt")},
        cases=cases,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总 DOC/PPT 归一化、视觉证据和 fail-closed 状态")
    parser.add_argument("--pipeline-report", type=Path, action="append", required=True)
    parser.add_argument("--expect", action="append", default=[])
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = verify_reports(args.pipeline_report, _expectations(args.expect))
    except (OSError, ValueError, TypeError) as exc:
        payload = result("FAIL", "legacy_normalization_matrix_v3", findings=[{
            "code": "LEGACY_NORMALIZATION_MATRIX_INVALID",
            "message": str(exc),
        }])
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
