from __future__ import annotations

import argparse
import copy
from pathlib import Path
from typing import Any

from pipeline_common import finish, read_json, result, sha256_file
from schema_validation_v3 import load_schema, validate_instance
from v3_common import canonical_json_sha256


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "references" / "schemas" / "release-matrix-evidence-v3.schema.json"
REVIEW_RECEIPT_SCHEMA = ROOT / "references" / "schemas" / "review-receipt-v3.schema.json"
REQUIRED_PASS_STAGES = {
    "fixed_template_compiler_v3",
    "validate_content_plan_v3",
    "verify_template_invariants",
    "verify_document_structure",
    "verify_tables",
    "scan_source_identity",
    "export_word_wps",
    "analyze_rendered_pages",
    "scan_source_identity_rendered_word",
    "scan_source_identity_rendered_wps",
    "final_delivery",
}
PUBLIC_REVIEW_BINDING_FIELDS = (
    "source_sha256",
    "template_sha256",
    "template_ir_sha256",
    "template_dsl_sha256",
    "compiler_sha256",
    "candidate_output_sha256",
    "diff_report_sha256",
)


def case_sha256(case: dict[str, Any]) -> str:
    payload = copy.deepcopy(case)
    payload.pop("case_sha256", None)
    return canonical_json_sha256(payload)


def _resolve(base: Path, value: Any) -> Path:
    path = Path(str(value))
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _verify_ref(base: Path, reference: dict[str, Any], label: str, findings: list[dict[str, Any]]) -> Path | None:
    path = _resolve(base, reference.get("path"))
    expected = str(reference.get("sha256") or "").upper()
    if not path.is_file():
        findings.append({"code": "MATRIX_ARTIFACT_MISSING", "artifact": label, "path": str(path)})
        return None
    actual = sha256_file(path)
    if actual != expected:
        findings.append({"code": "MATRIX_ARTIFACT_SHA_MISMATCH", "artifact": label, "path": str(path), "expected": expected, "actual": actual})
    return path


def _stage_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(stage.get("stage")): stage
        for stage in report.get("stages", [])
        if isinstance(stage, dict) and stage.get("stage")
    }


def _same_path(base: Path, actual: Any, expected: Path) -> bool:
    if not actual:
        return False
    return _resolve(base, actual) == expected.resolve()


def _public_review_binding(value: Any) -> dict[str, Any]:
    binding = value if isinstance(value, dict) else {}
    return {field: binding.get(field) for field in PUBLIC_REVIEW_BINDING_FIELDS}


def _decision_validation_path(report_path: Path, report: dict[str, Any]) -> Path | None:
    explicit = report.get("decision_validation")
    if explicit:
        return _resolve(report_path.parent, explicit)
    # r16 及更早的真实流水线将本阶段报告固定写在 reports 目录，
    # 但尚未把路径提升到 pipeline_report 顶层。只接受这个精确文件名，
    # 最终仍由回执中的 diff_report_sha256 校验内容身份。
    fallback = report_path.parent / "content_decision_v3.json"
    return fallback.resolve() if fallback.is_file() else None


def _expect(condition: bool, findings: list[dict[str, Any]], code: str, case_id: str, **facts: Any) -> None:
    if not condition:
        findings.append({"code": code, "case_id": case_id, **facts})


def _verify_case(base: Path, case: dict[str, Any], findings: list[dict[str, Any]]) -> dict[str, Any]:
    case_id = str(case["case_id"])
    expected_case_sha = str(case["case_sha256"]).upper()
    actual_case_sha = case_sha256(case)
    _expect(actual_case_sha == expected_case_sha, findings, "MATRIX_CASE_SHA_MISMATCH", case_id, expected=expected_case_sha, actual=actual_case_sha)

    source = _verify_ref(base, case["source"], f"{case_id}:source", findings)
    artifact_paths = {
        name: _verify_ref(base, reference, f"{case_id}:{name}", findings)
        for name, reference in case["artifacts"].items()
    }
    report_path = artifact_paths.get("pipeline_report")
    if report_path is None:
        return {"case_id": case_id, "case_sha256": actual_case_sha}
    try:
        report = read_json(report_path)
        content_plan = read_json(artifact_paths["content_plan"]) if artifact_paths.get("content_plan") else {}
        render_plan = read_json(artifact_paths["render_plan"]) if artifact_paths.get("render_plan") else {}
        receipt = read_json(artifact_paths["review_receipt"]) if artifact_paths.get("review_receipt") else {}
        template_program = read_json(artifact_paths["template_program"]) if artifact_paths.get("template_program") else {}
    except Exception as exc:
        findings.append({"code": "MATRIX_ARTIFACT_JSON_INVALID", "case_id": case_id, "message": str(exc)})
        return {"case_id": case_id, "case_sha256": actual_case_sha}

    expected_format = str(case["source"]["format"]).upper()
    expected_target = str(case["delivery_target"]).lower()
    expected_pack = case["template_pack"]
    expected_output = artifact_paths.get("output")
    stages = _stage_map(report)
    resolver = stages.get("template_resolver_v3", {}).get("template_pack", {})

    _expect(report.get("status") == "PASS", findings, "MATRIX_PIPELINE_NOT_PASS", case_id, actual=report.get("status"))
    _expect(report.get("deliverable") is True, findings, "MATRIX_PIPELINE_NOT_DELIVERABLE", case_id, actual=report.get("deliverable"))
    _expect(str(report.get("source_format") or "").upper() == expected_format, findings, "MATRIX_SOURCE_FORMAT_MISMATCH", case_id)
    _expect(str(report.get("source_sha256") or "").upper() == str(case["source"]["sha256"]).upper(), findings, "MATRIX_SOURCE_SHA_MISMATCH", case_id)
    _expect(source is not None and _same_path(base, report.get("source"), source), findings, "MATRIX_SOURCE_PATH_MISMATCH", case_id)
    _expect(str(report.get("delivery_target") or "").lower() == expected_target, findings, "MATRIX_DELIVERY_TARGET_MISMATCH", case_id)
    _expect(report.get("delivery_format") == ("DOCX" if expected_target == "desktop" else "PDF"), findings, "MATRIX_DELIVERY_FORMAT_MISMATCH", case_id)
    _expect(expected_output is not None and _same_path(base, report.get("output"), expected_output), findings, "MATRIX_OUTPUT_PATH_MISMATCH", case_id)
    _expect(str(report.get("output_sha256") or "").upper() == str(case["artifacts"]["output"]["sha256"]).upper(), findings, "MATRIX_OUTPUT_SHA_MISMATCH", case_id)

    _expect(resolver.get("id") == expected_pack["id"], findings, "MATRIX_TEMPLATE_ID_MISMATCH", case_id)
    _expect(str(resolver.get("version")) == str(expected_pack["version"]), findings, "MATRIX_TEMPLATE_VERSION_MISMATCH", case_id)
    _expect(str(resolver.get("template_sha256") or "").upper() == str(expected_pack["template_sha256"]).upper(), findings, "MATRIX_TEMPLATE_SHA_MISMATCH", case_id)
    _expect(artifact_paths.get("template_program") is not None and _same_path(base, resolver.get("program_path"), artifact_paths["template_program"]), findings, "MATRIX_TEMPLATE_PROGRAM_PATH_MISMATCH", case_id)

    for name in ("content_decision", "content_plan", "render_plan"):
        _expect(artifact_paths.get(name) is not None and _same_path(base, report.get(name), artifact_paths[name]), findings, "MATRIX_REPORT_ARTIFACT_PATH_MISMATCH", case_id, artifact=name)

    upstream = content_plan.get("upstream", {})
    _expect(str(upstream.get("content_decision", {}).get("sha256") or "").upper() == str(case["artifacts"]["content_decision"]["sha256"]).upper(), findings, "MATRIX_CONTENT_DECISION_BINDING_MISMATCH", case_id)
    _expect(str(upstream.get("review_receipt", {}).get("sha256") or "").upper() == str(case["artifacts"]["review_receipt"]["sha256"]).upper(), findings, "MATRIX_REVIEW_RECEIPT_BINDING_MISMATCH", case_id)
    _expect(receipt.get("overall_action") == "APPROVED", findings, "MATRIX_REVIEW_NOT_APPROVED", case_id)

    receipt_errors = validate_instance(receipt, load_schema(REVIEW_RECEIPT_SCHEMA))
    if receipt_errors:
        findings.append({"code": "MATRIX_REVIEW_RECEIPT_SCHEMA_INVALID", "case_id": case_id, "errors": receipt_errors})
    else:
        binding = receipt["binding"]
        _expect(str(binding["source_sha256"]).upper() == str(case["source"]["sha256"]).upper(), findings, "MATRIX_REVIEW_BINDING_SOURCE_MISMATCH", case_id)
        _expect(str(binding["template_sha256"]).upper() == str(expected_pack["template_sha256"]).upper(), findings, "MATRIX_REVIEW_BINDING_TEMPLATE_MISMATCH", case_id)
        _expect(str(binding["template_ir_sha256"]).upper() == str(template_program.get("template_ir_sha256") or "").upper(), findings, "MATRIX_REVIEW_BINDING_TEMPLATE_IR_MISMATCH", case_id)
        _expect(str(binding["template_dsl_sha256"]).upper() == str(template_program.get("program_sha256") or "").upper(), findings, "MATRIX_REVIEW_BINDING_TEMPLATE_DSL_MISMATCH", case_id)
        _expect(
            str(binding["compiler_sha256"]).upper() == sha256_file(ROOT / "scripts" / "fixed_template_compiler_v3.py"),
            findings,
            "MATRIX_REVIEW_BINDING_COMPILER_MISMATCH",
            case_id,
        )
        _expect(str(binding["candidate_output_sha256"]).upper() == str(case["artifacts"]["content_decision"]["sha256"]).upper(), findings, "MATRIX_REVIEW_BINDING_CANDIDATE_MISMATCH", case_id)
        decision_validation_path = _decision_validation_path(report_path, report)
        if decision_validation_path is None or not decision_validation_path.is_file():
            findings.append({"code": "MATRIX_DECISION_VALIDATION_MISSING", "case_id": case_id})
        else:
            _expect(str(binding["diff_report_sha256"]).upper() == sha256_file(decision_validation_path), findings, "MATRIX_REVIEW_BINDING_DIFF_REPORT_MISMATCH", case_id)
        approval = content_plan.get("review_approval") if isinstance(content_plan.get("review_approval"), dict) else {}
        _expect(approval.get("overall_action") == "APPROVED", findings, "MATRIX_CONTENT_PLAN_REVIEW_NOT_APPROVED", case_id)
        _expect(
            _public_review_binding(approval.get("binding")) == _public_review_binding(binding),
            findings,
            "MATRIX_CONTENT_PLAN_REVIEW_BINDING_MISMATCH",
            case_id,
        )

    render_upstream = render_plan.get("upstream", {})
    _expect(str(render_upstream.get("content_plan", {}).get("sha256") or "").upper() == str(case["artifacts"]["content_plan"]["sha256"]).upper(), findings, "MATRIX_CONTENT_PLAN_BINDING_MISMATCH", case_id)
    _expect(str(render_upstream.get("template_program", {}).get("sha256") or "").upper() == str(case["artifacts"]["template_program"]["sha256"]).upper(), findings, "MATRIX_TEMPLATE_PROGRAM_BINDING_MISMATCH", case_id)
    _expect(render_plan.get("delivery_target") == expected_target, findings, "MATRIX_RENDER_TARGET_MISMATCH", case_id)
    _expect(render_plan.get("template_pack", {}).get("id") == expected_pack["id"], findings, "MATRIX_RENDER_TEMPLATE_ID_MISMATCH", case_id)
    _expect(str(render_plan.get("template_pack", {}).get("version")) == str(expected_pack["version"]), findings, "MATRIX_RENDER_TEMPLATE_VERSION_MISMATCH", case_id)
    _expect(str(render_plan.get("template_pack", {}).get("template_sha256") or "").upper() == str(expected_pack["template_sha256"]).upper(), findings, "MATRIX_RENDER_TEMPLATE_SHA_MISMATCH", case_id)

    for stage_name in sorted(REQUIRED_PASS_STAGES):
        _expect(stages.get(stage_name, {}).get("status") == "PASS", findings, "MATRIX_REQUIRED_STAGE_NOT_PASS", case_id, required_stage=stage_name, actual=stages.get(stage_name, {}).get("status"))
    return {
        "case_id": case_id,
        "case_sha256": actual_case_sha,
        "source_format": expected_format,
        "template_pack": f"{expected_pack['id']}@{expected_pack['version']}",
        "delivery_target": expected_target,
        "pipeline_report_sha256": case["artifacts"]["pipeline_report"]["sha256"],
        "output_sha256": case["artifacts"]["output"]["sha256"],
    }


def matrix_dimension_findings(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    findings: list[dict[str, Any]] = []
    packs = sorted({(case["template_pack"]["id"], str(case["template_pack"]["version"])) for case in cases})
    if len(packs) != 2 or not any(pack[0] == "zxty-fixed-v1" for pack in packs):
        findings.append({"code": "MATRIX_TEMPLATE_COVERAGE_INVALID", "actual": [f"{pack[0]}@{pack[1]}" for pack in packs]})
        return findings, packs
    primary = next(pack for pack in packs if pack[0] == "zxty-fixed-v1")
    secondary = next(pack for pack in packs if pack != primary)
    actual = {(case["template_pack"]["id"], str(case["template_pack"]["version"]), case["source"]["format"], case["delivery_target"]) for case in cases}
    required = {
        (primary[0], primary[1], source_format, target)
        for source_format in ("DOC", "DOCX", "PPT", "PPTX", "PDF")
        for target in ("desktop", "mobile")
    } | {
        (secondary[0], secondary[1], source_format, target)
        for source_format in ("DOCX", "PPTX", "PDF")
        for target in ("desktop", "mobile")
    }
    if actual != required:
        findings.append({"code": "MATRIX_DIMENSION_COVERAGE_MISMATCH", "missing": sorted(required - actual), "extra": sorted(actual - required)})
    return findings, packs


def verify_matrix(payload: dict[str, Any], *, base: Path) -> dict[str, Any]:
    errors = validate_instance(payload, load_schema(SCHEMA))
    if errors:
        return result("FAIL", "verify_release_matrix_v3", findings=[{"code": "MATRIX_SCHEMA_INVALID", "errors": errors}])
    findings: list[dict[str, Any]] = []
    _verify_ref(base, payload["manifest"], "manifest", findings)
    cases = payload["cases"]
    case_ids = [str(case["case_id"]) for case in cases]
    if len(case_ids) != len(set(case_ids)):
        findings.append({"code": "MATRIX_CASE_ID_DUPLICATE"})
    summaries = [_verify_case(base, case, findings) for case in cases]

    dimension_findings, packs = matrix_dimension_findings(cases)
    findings.extend(dimension_findings)

    return result(
        "PASS" if not findings else "FAIL",
        "verify_release_matrix_v3",
        findings=findings,
        manifest_sha256=payload["manifest"]["sha256"],
        case_count=len(cases),
        cases=summaries,
        coverage={
            "source_formats": sorted({case["source"]["format"] for case in cases}),
            "delivery_targets": sorted({case["delivery_target"] for case in cases}),
            "template_packs": [f"{pack[0]}@{pack[1]}" for pack in packs],
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 V3 两模板 16 案例正式矩阵及全链制品 SHA 绑定")
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload = read_json(args.matrix)
    report = verify_matrix(payload, base=args.matrix.resolve().parent)
    return finish(report, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
