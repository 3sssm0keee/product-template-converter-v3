from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from golden_visual_review_v3 import build_review_spec
from pipeline_common import finish, read_json, sha256_file, write_json
from schema_validation_v3 import load_schema, validate_instance
from verify_release import blind_case_binding, blind_case_sha256


ROOT = Path(__file__).resolve().parents[1]
RESPONSIBLE_SCHEMA = ROOT / "references" / "schemas" / "responsible-visual-review-v3.schema.json"
BLIND_SCHEMA = ROOT / "references" / "schemas" / "independent-blind-visual-review-v3.schema.json"


def _artifact(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {"path": str(resolved), "bytes": resolved.stat().st_size, "sha256": sha256_file(resolved)}


def _load_receipt(path: Path, schema_path: Path, label: str, findings: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        payload = read_json(path)
    except Exception as exc:
        findings.append({"code": f"{label}_INVALID", "path": str(path), "message": str(exc)})
        return {}
    if not isinstance(payload, dict):
        findings.append({"code": f"{label}_INVALID", "path": str(path), "message": "receipt must be an object"})
        return {}
    errors = validate_instance(payload, load_schema(schema_path))
    if errors:
        findings.append({"code": f"{label}_SCHEMA_INVALID", "path": str(path), "errors": errors})
    return payload


def _review_case_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    return {str(case.get("id")): case for case in cases if isinstance(case, dict) and case.get("id")}


def assemble_blind_report(
    *,
    matrix_path: Path,
    manifest_path: Path,
    case_ids: Sequence[str],
    responsible_review_path: Path,
    blind_review_path: Path,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    spec, spec_findings = build_review_spec(matrix_path.resolve(), case_ids, "responsible")
    findings.extend(spec_findings)
    if spec is None:
        return {
            "status": "FAIL",
            "stage": "assemble_blind_report_v3",
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "manifest_sha256": sha256_file(manifest_path) if manifest_path.is_file() else None,
            "deliverable": False,
            "findings": findings,
        }
    if not manifest_path.is_file():
        findings.append({"code": "BLIND_MANIFEST_MISSING", "path": str(manifest_path)})
        manifest_sha = ""
    else:
        manifest_sha = sha256_file(manifest_path)
        if manifest_sha != spec["manifest_sha256"]:
            findings.append(
                {
                    "code": "BLIND_MANIFEST_SHA_MISMATCH",
                    "expected": spec["manifest_sha256"],
                    "actual": manifest_sha,
                }
            )
    responsible = _load_receipt(responsible_review_path, RESPONSIBLE_SCHEMA, "RESPONSIBLE_REVIEW", findings)
    blind = _load_receipt(blind_review_path, BLIND_SCHEMA, "BLIND_VISUAL_REVIEW", findings)
    expected_ids = [str(case["id"]) for case in spec["cases"]]
    responsible_cases = _review_case_map(responsible)
    blind_cases = _review_case_map(blind)
    if set(responsible_cases) != set(expected_ids):
        findings.append({"code": "RESPONSIBLE_REVIEW_CASE_COVERAGE_INVALID", "expected": expected_ids, "actual": sorted(responsible_cases)})
    if set(blind_cases) != set(expected_ids):
        findings.append({"code": "BLIND_VISUAL_REVIEW_CASE_COVERAGE_INVALID", "expected": expected_ids, "actual": sorted(blind_cases)})
    expected_matrix_sha = spec["matrix"]["sha256"]
    for label, payload in (("RESPONSIBLE_REVIEW", responsible), ("BLIND_VISUAL_REVIEW", blind)):
        if str(payload.get("manifest_sha256") or "").upper() != manifest_sha:
            findings.append({"code": f"{label}_MANIFEST_SHA_MISMATCH"})
        if str(payload.get("matrix_sha256") or "").upper() != expected_matrix_sha:
            findings.append({"code": f"{label}_MATRIX_SHA_MISMATCH"})
        if payload.get("status") != "PASS":
            findings.append({"code": f"{label}_NOT_PASS", "actual": payload.get("status")})
    if responsible.get("reviewer_id") == blind.get("reviewer_id"):
        findings.append({"code": "BLIND_REVIEWER_NOT_INDEPENDENT", "reviewer_id": blind.get("reviewer_id")})

    total_pages = 0
    evidence_cases: list[dict[str, Any]] = []
    try:
        matrix = read_json(matrix_path)
    except Exception:
        matrix = {}
    matrix_by_id = {str(case.get("case_id")): case for case in matrix.get("cases", []) if isinstance(case, dict)}
    for expected in spec["cases"]:
        case_id = str(expected["id"])
        page_count = len(expected["engines"]["wps"])
        expected_pages = list(range(1, page_count + 1))
        total_pages += page_count * 2
        responsible_case = responsible_cases.get(case_id, {})
        blind_case = blind_cases.get(case_id, {})
        if (
            responsible_case.get("pipeline_report_sha256") != expected["pipeline_report"]["sha256"]
            or responsible_case.get("wps_pages_reviewed") != expected_pages
            or responsible_case.get("word_pages_reviewed") != expected_pages
            or responsible_case.get("expected_pages_per_engine") != page_count
            or responsible_case.get("decision") != "PASS"
            or responsible_case.get("findings") != []
        ):
            findings.append({"code": "RESPONSIBLE_REVIEW_CASE_BINDING_INVALID", "case_id": case_id})
        if (
            blind_case.get("wps_pages_reviewed") != expected_pages
            or blind_case.get("word_pages_reviewed") != expected_pages
            or blind_case.get("pages_expected") != page_count * 2
            or blind_case.get("pages_reviewed") != page_count * 2
            or blind_case.get("complete") is not True
            or blind_case.get("source_brand_residual_found") is not False
            or blind_case.get("substantive_engine_difference_found") is not False
            or blind_case.get("decision") != "PASS"
            or blind_case.get("findings") != []
        ):
            findings.append({"code": "BLIND_VISUAL_REVIEW_CASE_BINDING_INVALID", "case_id": case_id})
        matrix_case = matrix_by_id.get(case_id)
        if not matrix_case:
            findings.append({"code": "BLIND_MATRIX_CASE_MISSING", "case_id": case_id})
            continue
        binding = blind_case_binding(
            case_id=case_id,
            source_format=matrix_case["source"]["format"],
            source_sha256=matrix_case["source"]["sha256"],
            template_pack_id=matrix_case["template_pack"]["id"],
            template_pack_version=str(matrix_case["template_pack"]["version"]),
            delivery_target=matrix_case["delivery_target"],
            pipeline_report_sha256=matrix_case["artifacts"]["pipeline_report"]["sha256"],
            output_sha256=matrix_case["artifacts"]["output"]["sha256"],
        )
        evidence_cases.append(
            {
                "id": case_id,
                "source": {
                    "path": matrix_case["source"]["path"],
                    "format": binding["source_format"],
                    "actual_sha256": binding["source_sha256"],
                },
                "template_pack": {
                    "id": binding["template_pack_id"],
                    "version": binding["template_pack_version"],
                    "template_sha256": matrix_case["template_pack"]["template_sha256"],
                },
                "pipeline_report": {
                    "path": matrix_case["artifacts"]["pipeline_report"]["path"],
                    "delivery_target": binding["delivery_target"],
                    "sha256": binding["pipeline_report_sha256"],
                },
                "output": {
                    "path": matrix_case["artifacts"]["output"]["path"],
                    "actual_sha256": binding["output_sha256"],
                },
                "case_sha256": blind_case_sha256(binding),
            }
        )
    responsible_coverage = responsible.get("coverage") if isinstance(responsible.get("coverage"), dict) else {}
    if (
        responsible_coverage.get("complete") is not True
        or responsible_coverage.get("output_pages_expected") != total_pages
        or responsible_coverage.get("output_pages_reviewed") != total_pages
        or responsible.get("release_decision") != "PROCEED_TO_INDEPENDENT_BLIND_VALIDATION"
    ):
        findings.append({"code": "RESPONSIBLE_REVIEW_TOTAL_COVERAGE_INVALID"})
    if (
        blind.get("complete") is not True
        or blind.get("decision") != "PASS"
        or blind.get("findings") != []
        or blind.get("total_pages_expected") != total_pages
        or blind.get("total_pages_reviewed") != total_pages
    ):
        findings.append({"code": "BLIND_VISUAL_REVIEW_TOTAL_COVERAGE_INVALID"})

    status = "PASS" if not findings else "FAIL"
    responsible_ref = _artifact(responsible_review_path) if responsible_review_path.is_file() else {"path": str(responsible_review_path), "bytes": 0, "sha256": ""}
    responsible_ref.update(
        {
            "status": responsible.get("status"),
            "reported_page_coverage": responsible_coverage.get("output_pages_reviewed"),
            "used_as_substitute_for_blind_visual_review": False,
        }
    )
    visual_review = {
        "reviewer": blind.get("reviewer_id"),
        "reviewer_role": blind.get("reviewer_role"),
        "method": "独立审核人只依据冻结的 WPS/Word 页面逐页检查；未使用负责人结论替代盲验。",
        "cases": [blind_cases[case_id] for case_id in expected_ids if case_id in blind_cases],
        "total_pages_expected": blind.get("total_pages_expected"),
        "total_pages_reviewed": blind.get("total_pages_reviewed"),
        "complete": blind.get("complete"),
        "findings": blind.get("findings"),
        "decision": blind.get("decision"),
    }
    return {
        "status": status,
        "stage": "blind_validation",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "manifest_sha256": manifest_sha,
        "deliverable": status == "PASS",
        "summary": {
            "release_decision": "RELEASE" if status == "PASS" else "RELEASE_HOLD",
            "golden_cases_expected": 5,
            "golden_cases_verified": len(evidence_cases),
            "visual_pages_expected": total_pages,
            "visual_pages_reviewed": blind.get("total_pages_reviewed"),
        },
        "findings": findings,
        "scope": {
            "validation_mode": "v3_independent_blind_validation_on_frozen_evidence",
            "cases": expected_ids,
            "delivery_targets": sorted({case["delivery_target"] for case in spec["cases"]}),
            "source_formats": sorted({case["source_format"] for case in spec["cases"]}),
            "template_packs": sorted({case["template_pack"] for case in spec["cases"]}),
        },
        "evidence": {
            "manifest": _artifact(manifest_path) if manifest_path.is_file() else {"path": str(manifest_path), "bytes": 0, "sha256": ""},
            "matrix": _artifact(matrix_path),
            "responsible_engineer_visual_review": responsible_ref,
            "independent_blind_visual_review": _artifact(blind_review_path) if blind_review_path.is_file() else {"path": str(blind_review_path), "bytes": 0, "sha256": ""},
            "cases": evidence_cases,
        },
        "visual_review": visual_review,
        "limitations": [
            "负责人复核和独立盲验来自不同 reviewer ID。",
            "本报告只装配并校验冻结证据，不替代人工逐页查看。",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="装配并校验 V3 五条黄金案例的独立盲验报告")
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--case-id", action="append", required=True, help="与复核网页一致的 5 个黄金案例 ID")
    parser.add_argument("--responsible-review", type=Path, required=True)
    parser.add_argument("--blind-review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = assemble_blind_report(
        matrix_path=args.matrix,
        manifest_path=args.manifest,
        case_ids=args.case_id,
        responsible_review_path=args.responsible_review,
        blind_review_path=args.blind_review,
    )
    write_json(args.output, payload)
    return finish(payload)


if __name__ == "__main__":
    raise SystemExit(main())
