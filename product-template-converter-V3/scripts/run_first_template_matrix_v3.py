from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from decision_bundle_v3 import validate_content_decision
from dependency_bindings_v3 import (
    dependency_bindings_load_blocked_result,
    dependency_bindings_reference,
    load_dependency_bindings,
    missing_dependency_findings,
)
from pipeline_common import finish, read_json, result, sha256_file, write_json
from review_v3 import validate_review_receipt
from run_release_matrix_v3 import _verify_ref, build_pipeline_command, collect_case_evidence
from schema_validation_v3 import load_schema, validate_instance


ROOT = Path(__file__).resolve().parents[1]
SPEC_SCHEMA = ROOT / "references" / "schemas" / "first-template-validation-run-spec-v3.schema.json"
DRAFT_SPEC_SCHEMA = ROOT / "references" / "schemas" / "first-template-validation-run-spec-draft-v3.schema.json"
SOURCE_FORMATS = ("DOC", "DOCX", "PPT", "PPTX", "PDF")
DELIVERY_TARGETS = ("desktop", "mobile")
EXPECTED_DIMENSIONS = {(source_format, target) for source_format in SOURCE_FORMATS for target in DELIVERY_TARGETS}
EXPECTED_TEMPLATE_PACK = ("zxty-fixed-v1", "1.0.0")

_HUMAN_REVIEW_REASONS = {
    "CONTENT_DECISION_NOT_APPROVED",
    "CURRENT_REVIEW_QUEUE_MISSING",
    "APPROVED_RECEIPT_MISSING",
    "RECEIPT_NOT_APPROVED",
    "RECEIPT_NOT_VALID_FOR_CURRENT_QUEUE",
}
_BLOCKED_REASONS = {
    "TEMPLATE_MISSING_OR_SHA_MISMATCH",
    "SOURCE_MISSING_OR_SHA_MISMATCH",
    "CONTENT_DECISION_MISSING_OR_SHA_MISMATCH",
}
_RECEIPT_RESOLVABLE_DECISION_CODES = {
    "DECISION_LOW_CONFIDENCE": "CONTENT-LOW-CONFIDENCE",
    "IDENTITY_REVIEW_REQUIRED": "CONTENT-IDENTITY-REVIEW",
}


def _dimension_hold_status(reasons: list[str]) -> str:
    """Map an unready dimension onto the public fail-closed status contract."""
    if any(reason not in _HUMAN_REVIEW_REASONS | _BLOCKED_REASONS for reason in reasons):
        return "FAIL"
    if any(reason in _BLOCKED_REASONS for reason in reasons):
        return "BLOCKED"
    return "HUMAN_REVIEW"


def _read_optional_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    try:
        value = read_json(path)
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _receipt_binding(receipt: dict[str, Any] | None) -> dict[str, Any]:
    binding = receipt.get("binding") if isinstance(receipt, dict) else None
    return binding if isinstance(binding, dict) else {}


def _decision_ready(decision: dict[str, Any] | None) -> bool:
    if not decision:
        return False
    # ContentDecisionV3 does not carry an approval flag; a non-review identity
    # conclusion plus no unresolved items is the machine-readable usable shape.
    identity = decision.get("identity_review")
    identity_status = identity.get("status") if isinstance(identity, dict) else None
    return identity_status != "REVIEW_REQUIRED" and not decision.get("unresolved_items")


def _decision_resolved_by_receipt(
    decision: dict[str, Any] | None,
    receipt: dict[str, Any] | None,
    review_validation: dict[str, Any] | None,
) -> bool:
    if not decision or not receipt or not review_validation or review_validation.get("status") != "PASS":
        return False
    decision_validation = validate_content_decision(decision, task_bundles=None)
    if decision_validation.get("status") == "PASS":
        return True
    if decision_validation.get("status") != "HUMAN_REVIEW":
        return False
    codes = {
        str(value.get("code") or "")
        for value in decision_validation.get("findings", [])
        if isinstance(value, dict)
    }
    if not codes or not codes <= set(_RECEIPT_RESOLVABLE_DECISION_CODES):
        return False
    approved_review_items = {
        str(value.get("review_item_id") or "")
        for value in receipt.get("decisions", [])
        if isinstance(value, dict) and value.get("action") == "APPROVE"
    }
    required_review_items = {
        _RECEIPT_RESOLVABLE_DECISION_CODES[code]
        for code in codes
    }
    return required_review_items <= approved_review_items


def _template_sha_from_manifest(base: Path, manifest_ref: dict[str, Any]) -> str | None:
    manifest_path = _resolve_ref(base, manifest_ref)
    if manifest_path is None:
        return None
    manifest = _read_optional_json(manifest_path)
    value = manifest.get("template_sha256") if manifest else None
    return str(value).upper() if value else None


def _resolve_ref(base: Path, reference: dict[str, Any] | None) -> Path | None:
    if not isinstance(reference, dict) or not reference.get("path"):
        return None
    path = Path(str(reference["path"]))
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def build_readiness_report(payload: dict[str, Any], *, base: Path, phase_evidence: Path | None = None) -> dict[str, Any]:
    """Read-only gate for the ten-cell first-template phase.

    This function never creates an output root or invokes Office. It deliberately
    treats null/missing receipts and review-gated decisions as not input-ready.
    """
    schema_errors = validate_instance(payload, load_schema(DRAFT_SPEC_SCHEMA))
    findings: list[dict[str, Any]] = []
    if schema_errors:
        return result(
            "FAIL",
            "first_template_matrix_readiness_v3",
            findings=[{"code": "FIRST_TEMPLATE_SPEC_SCHEMA_INVALID", "errors": schema_errors}],
            dimensions_total=10,
            input_ready_count=0,
            run_complete_count=0,
            readiness_status="RELEASE_HOLD",
            office_started=False,
        )

    manifest_path = _verify_ref(base, payload["manifest"], "manifest", findings)
    manifest_sha = str(payload["manifest"]["sha256"]).upper()
    manifest_template_sha = _template_sha_from_manifest(base, payload["manifest"])
    actual_dimensions = {
        (str(case["source"]["format"]).upper(), str(case["delivery_target"]).lower())
        for case in payload["cases"]
    }
    if actual_dimensions != EXPECTED_DIMENSIONS:
        findings.append({"code": "FIRST_TEMPLATE_DIMENSION_COVERAGE_MISMATCH"})

    phase_path = phase_evidence or (base / "first-template-validation-evidence-v3.json")
    phase_exists = phase_path.is_file()
    phase_case_count = 0
    if phase_exists:
        evidence = _read_optional_json(phase_path)
        phase_case_count = len(evidence.get("cases", [])) if evidence else 0
        if phase_case_count != 10:
            findings.append({"code": "FIRST_TEMPLATE_PHASE_EVIDENCE_COVERAGE_MISMATCH", "actual": phase_case_count, "expected": 10})

    dimensions: list[dict[str, Any]] = []
    input_ready_count = 0
    run_complete_count = 0
    for case in payload["cases"]:
        case_id = str(case["case_id"])
        source = case["source"]
        decision_path = _verify_ref(base, case["content_decision"], f"{case_id}:content_decision", findings)
        source_path = _verify_ref(base, source, f"{case_id}:source", findings)
        receipt_ref = case.get("review_receipt")
        receipt_path = _verify_ref(base, receipt_ref, f"{case_id}:review_receipt", findings) if receipt_ref else None
        queue_ref = case.get("review_queue")
        queue_path = _verify_ref(base, queue_ref, f"{case_id}:review_queue", findings) if queue_ref else None
        decision = _read_optional_json(decision_path)
        receipt = _read_optional_json(receipt_path)
        queue = _read_optional_json(queue_path)
        binding = _receipt_binding(receipt)
        review_validation: dict[str, Any] | None = None
        reasons: list[str] = []
        if manifest_template_sha and str(case["template_pack"]["template_sha256"]).upper() != manifest_template_sha:
            reasons.append("TEMPLATE_SHA_MANIFEST_MISMATCH")
        template_path_value = case["template_pack"].get("template_path")
        if template_path_value:
            template_path = _verify_ref(
                base,
                {"path": template_path_value, "sha256": case["template_pack"]["template_sha256"]},
                f"{case_id}:template",
                findings,
            )
            if template_path is None:
                reasons.append("TEMPLATE_MISSING_OR_SHA_MISMATCH")
        if tuple((str(case["template_pack"]["id"]), str(case["template_pack"]["version"]))) != EXPECTED_TEMPLATE_PACK:
            reasons.append("TEMPLATE_PACK_NOT_PRIMARY")
        if source_path is None:
            reasons.append("SOURCE_MISSING_OR_SHA_MISMATCH")
        if decision_path is None:
            reasons.append("CONTENT_DECISION_MISSING_OR_SHA_MISMATCH")
        if queue_path is None:
            reasons.append("CURRENT_REVIEW_QUEUE_MISSING")
        if receipt_path is None:
            reasons.append("APPROVED_RECEIPT_MISSING")
        else:
            if receipt.get("overall_action") != "APPROVED":
                reasons.append("RECEIPT_NOT_APPROVED")
            if str(binding.get("source_sha256") or "").upper() != str(source["sha256"]).upper():
                reasons.append("RECEIPT_SOURCE_BINDING_MISMATCH")
            if str(binding.get("template_sha256") or "").upper() != str(case["template_pack"]["template_sha256"]).upper():
                reasons.append("RECEIPT_TEMPLATE_BINDING_MISMATCH")
            if queue is not None:
                review_validation = validate_review_receipt(queue, receipt)
                if review_validation.get("status") != "PASS":
                    reasons.append("RECEIPT_NOT_VALID_FOR_CURRENT_QUEUE")
        if decision_path is not None and not _decision_ready(decision) and not _decision_resolved_by_receipt(decision, receipt, review_validation):
            reasons.append("CONTENT_DECISION_NOT_APPROVED")
        input_ready = not reasons
        if input_ready:
            input_ready_count += 1
        run_complete = input_ready and phase_exists and phase_case_count == 10
        if run_complete:
            run_complete_count += 1
        if not input_ready:
            findings.append({
                "code": "FIRST_TEMPLATE_DIMENSION_NOT_INPUT_READY",
                "case_id": case_id,
                "reasons": reasons,
            })
        dimension_status = "READY" if run_complete else ("INPUT_READY" if input_ready else _dimension_hold_status(reasons))
        dimensions.append({
            "case_id": case_id,
            "format": str(source["format"]).upper(),
            "target": str(case["delivery_target"]).lower(),
            "input_ready": input_ready,
            "run_complete": run_complete,
            "status": dimension_status,
            "blocking_reasons": reasons,
            "references": {
                "source": {"path": str(source["path"]), "sha256": str(source["sha256"]).upper()},
                "content_decision": {"path": str(case["content_decision"]["path"]), "sha256": str(case["content_decision"]["sha256"]).upper()},
                "review_queue": None if queue_ref is None else {"path": str(queue_ref["path"]), "sha256": str(queue_ref["sha256"]).upper()},
                "review_receipt": None if receipt_ref is None else {"path": str(receipt_ref["path"]), "sha256": str(receipt_ref["sha256"]).upper()},
                "template": None if not template_path_value else {"path": str(template_path_value), "sha256": str(case["template_pack"]["template_sha256"]).upper()},
            },
        })

    if input_ready_count == 10 and run_complete_count == 10 and not findings:
        readiness_status = "PHASE_COMPLETE"
    elif input_ready_count == 10 and run_complete_count == 0 and not findings:
        readiness_status = "READY_TO_RUN"
    else:
        readiness_status = "RELEASE_HOLD"
    dimension_hold_statuses = {
        item["status"] for item in dimensions if item["status"] in {"FAIL", "BLOCKED", "HUMAN_REVIEW"}
    }
    non_dimension_codes = {
        str(item.get("code"))
        for item in findings
        if item.get("code") != "FIRST_TEMPLATE_DIMENSION_NOT_INPUT_READY"
    }
    if not findings:
        overall_status = "PASS"
    elif "FAIL" in dimension_hold_statuses or any(code != "MATRIX_RUN_INPUT_MISSING" for code in non_dimension_codes):
        overall_status = "FAIL"
    elif "BLOCKED" in dimension_hold_statuses or "MATRIX_RUN_INPUT_MISSING" in non_dimension_codes:
        overall_status = "BLOCKED"
    else:
        overall_status = "HUMAN_REVIEW"
    return result(
        overall_status,
        "first_template_matrix_readiness_v3",
        findings=findings,
        manifest={"path": str(manifest_path) if manifest_path else str(payload["manifest"]["path"]), "sha256": manifest_sha},
        dimensions_total=10,
        input_ready_count=input_ready_count,
        run_complete_count=run_complete_count,
        readiness_status=readiness_status,
        office_started=False,
        phase_evidence={"path": str(phase_path), "exists": phase_exists, "case_count": phase_case_count},
        dimensions=dimensions,
    )


def validate_phase_spec(payload: dict[str, Any], *, base: Path) -> dict[str, Any]:
    errors = validate_instance(payload, load_schema(SPEC_SCHEMA))
    if errors:
        return result(
            "FAIL",
            "validate_first_template_matrix_spec_v3",
            findings=[{"code": "FIRST_TEMPLATE_SPEC_SCHEMA_INVALID", "errors": errors}],
            release_claim_allowed=False,
        )
    findings: list[dict[str, Any]] = []
    _verify_ref(base, payload["manifest"], "manifest", findings)
    case_ids = [str(case["case_id"]) for case in payload["cases"]]
    if len(case_ids) != len(set(case_ids)):
        findings.append({"code": "FIRST_TEMPLATE_CASE_ID_DUPLICATE"})
    actual_dimensions = {
        (str(case["source"]["format"]).upper(), str(case["delivery_target"]).lower())
        for case in payload["cases"]
    }
    if actual_dimensions != EXPECTED_DIMENSIONS:
        findings.append({
            "code": "FIRST_TEMPLATE_DIMENSION_COVERAGE_MISMATCH",
            "expected": [list(value) for value in sorted(EXPECTED_DIMENSIONS)],
            "actual": [list(value) for value in sorted(actual_dimensions)],
        })
    template_hashes = {str(case["template_pack"]["template_sha256"]).upper() for case in payload["cases"]}
    if len(template_hashes) != 1:
        findings.append({"code": "FIRST_TEMPLATE_SHA_NOT_UNIFORM", "actual": sorted(template_hashes)})
    for case in payload["cases"]:
        case_id = str(case["case_id"])
        _verify_ref(base, case["source"], f"{case_id}:source", findings)
        _verify_ref(base, case["content_decision"], f"{case_id}:content_decision", findings)
        if case.get("review_receipt") is None:
            findings.append({"code": "FIRST_TEMPLATE_APPROVED_RECEIPT_REQUIRED", "case_id": case_id})
        else:
            _verify_ref(base, case["review_receipt"], f"{case_id}:review_receipt", findings)
    return result(
        "PASS" if not findings else "FAIL",
        "validate_first_template_matrix_spec_v3",
        findings=findings,
        case_count=len(payload["cases"]),
        template_pack="zxty-fixed-v1@1.0.0",
        release_claim_allowed=False,
    )


def run_phase_matrix(
    payload: dict[str, Any],
    *,
    base: Path,
    output_root: Path,
    python: Path,
    workers: int,
    continue_on_failure: bool,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    office_settle_seconds: float = 0,
    sleeper: Callable[[float], Any] = time.sleep,
    dependency_bindings_path: Path | None = None,
    dependency_bindings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dependency_bindings_fact = None
    if dependency_bindings_path is not None:
        try:
            dependency_bindings = dependency_bindings or load_dependency_bindings(dependency_bindings_path)
        except (OSError, ValueError) as exc:
            return dependency_bindings_load_blocked_result(
                dependency_bindings_path,
                "run_first_template_matrix_v3",
                exc,
                release_claim_allowed=False,
            )
        dependency_bindings_fact = dependency_bindings_reference(dependency_bindings_path)
        missing = missing_dependency_findings(dependency_bindings)
        if missing:
            return result(
                "BLOCKED",
                "run_first_template_matrix_v3",
                findings=missing,
                release_claim_allowed=False,
                dependency_bindings=dependency_bindings_fact,
            )
    validation = validate_phase_spec(payload, base=base)
    if validation["status"] != "PASS":
        return result(
            "FAIL",
            "run_first_template_matrix_v3",
            findings=validation["findings"],
            validation=validation,
            release_claim_allowed=False,
        )
    if output_root.exists() and any(output_root.iterdir()):
        return result(
            "BLOCKED",
            "run_first_template_matrix_v3",
            findings=[{
                "code": "FIRST_TEMPLATE_OUTPUT_ROOT_NOT_EMPTY",
                "path": str(output_root),
                "message": "Use a new empty directory so stale reports cannot enter phase evidence.",
            }],
            release_claim_allowed=False,
        )
    output_root.mkdir(parents=True, exist_ok=True)
    cache_dir = output_root / "cache"
    run_summaries: list[dict[str, Any]] = []
    evidence_cases: list[dict[str, Any]] = []
    office_settles: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    for index, case in enumerate(payload["cases"], 1):
        case_id = str(case["case_id"])
        case_dir = output_root / "cases" / f"{index:02d}-{case_id}"
        case_dir.mkdir(parents=True, exist_ok=False)
        command = build_pipeline_command(
            case,
            base=base,
            output_dir=case_dir,
            cache_dir=cache_dir,
            python=python,
            workers=workers,
            dependency_bindings_path=dependency_bindings_path,
            dependency_bindings=dependency_bindings,
        )
        completed = command_runner(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=7200,
        )
        report_path = case_dir / "reports" / "pipeline_report.json"
        if not report_path.is_file():
            summary = {"case_id": case_id, "status": "BLOCKED", "returncode": completed.returncode, "report": str(report_path)}
            findings.append({"code": "FIRST_TEMPLATE_PIPELINE_REPORT_MISSING", **summary})
        else:
            report = read_json(report_path)
            summary = {
                "case_id": case_id,
                "status": report.get("status"),
                "deliverable": report.get("deliverable"),
                "returncode": completed.returncode,
                "report": str(report_path),
                "report_sha256": sha256_file(report_path),
            }
            if report.get("status") == "PASS" and report.get("deliverable") is True and completed.returncode == 0:
                try:
                    evidence_cases.append(collect_case_evidence(case, base=base, report_path=report_path))
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    summary["status"] = "FAIL"
                    findings.append({"code": "FIRST_TEMPLATE_CASE_EVIDENCE_COLLECTION_FAILED", "case_id": case_id, "message": str(exc)})
            else:
                findings.append({
                    "code": "FIRST_TEMPLATE_PIPELINE_NOT_DELIVERABLE",
                    "case_id": case_id,
                    "status": report.get("status"),
                    "deliverable": report.get("deliverable"),
                    "returncode": completed.returncode,
                })
        run_summaries.append(summary)
        if findings and not continue_on_failure:
            break
        if office_settle_seconds > 0 and index < len(payload["cases"]):
            settle = {
                "after_case_id": case_id,
                "after_case_index": index,
                "seconds": office_settle_seconds,
            }
            office_settles.append(settle)
            sleeper(office_settle_seconds)

    evidence_path = output_root / "first-template-validation-evidence-v3.json"
    if not findings and len(evidence_cases) == 10:
        evidence = {
            "schema_version": "first-template-validation-evidence-v3",
            "scope": "zxty-fixed-v1-only",
            "release_claim_allowed": False,
            "manifest": {
                "path": str(_verify_ref(base, payload["manifest"], "manifest", [])),
                "sha256": str(payload["manifest"]["sha256"]).upper(),
            },
            "coverage": {
                "source_formats": list(SOURCE_FORMATS),
                "delivery_targets": list(DELIVERY_TARGETS),
                "template_packs": ["zxty-fixed-v1@1.0.0"],
            },
            "cases": evidence_cases,
        }
        if dependency_bindings_fact is not None:
            evidence["dependency_bindings"] = dependency_bindings_fact
        write_json(evidence_path, evidence)

    passed = not findings and len(evidence_cases) == 10 and evidence_path.is_file()
    payload = result(
        "PASS" if passed else "FAIL",
        "run_first_template_matrix_v3",
        findings=findings,
        release_claim_allowed=False,
        release_scope_note="阶段验证仅覆盖 zxty-fixed-v1，不能替代两模板 16 条正式发布矩阵。",
        executed_case_count=len(run_summaries),
        verified_case_count=len(evidence_cases),
        office_settle_seconds=office_settle_seconds,
        office_settles=office_settles,
        cases=run_summaries,
        phase_evidence=str(evidence_path) if evidence_path.is_file() else None,
        phase_evidence_sha256=sha256_file(evidence_path) if evidence_path.is_file() else None,
    )
    if dependency_bindings_fact is not None:
        payload["dependency_bindings"] = dependency_bindings_fact
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="串行执行 V3 第一模板五格式×双目标 10 条阶段验证；结果不能用于多模板正式发布声明")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, help="阶段运行输出目录；readiness/preflight 不需要此参数")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--office-settle-seconds", type=float, default=0.0, help="可选诊断项：真实 Office/WPS 矩阵 case 之间的 COM 状态恢复等待秒数")
    parser.add_argument("--continue-on-failure", action="store_true")
    parser.add_argument("--readiness", action="store_true", help="仅检查十格输入和阶段证据，不运行 Office、不创建输出制品")
    parser.add_argument("--preflight", action="store_true", help="--readiness 的别名")
    parser.add_argument("--phase-evidence", type=Path, help="可选的阶段证据 JSON，仅用于只读 readiness")
    parser.add_argument("--dependency-bindings", type=Path, help="DependencyBindingsV3 JSON discovered once and passed to every pipeline child")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.workers < 1:
        return finish(result("FAIL", "run_first_template_matrix_v3", findings=[{"code": "WORKER_COUNT_INVALID"}], release_claim_allowed=False), args.report)
    payload = read_json(args.spec)
    if args.readiness or args.preflight:
        report = build_readiness_report(
            payload,
            base=args.spec.resolve().parent,
            phase_evidence=args.phase_evidence.resolve() if args.phase_evidence else None,
        )
        return finish(report, args.report)
    if args.output_root is None:
        return finish(result("FAIL", "run_first_template_matrix_v3", findings=[{"code": "OUTPUT_ROOT_REQUIRED_FOR_RUN"}], release_claim_allowed=False), args.report)
    dependency_bindings_path = args.dependency_bindings.resolve() if args.dependency_bindings else None
    try:
        dependency_bindings = load_dependency_bindings(dependency_bindings_path) if dependency_bindings_path else None
    except (OSError, ValueError) as exc:
        return finish(
            dependency_bindings_load_blocked_result(
                dependency_bindings_path,
                "run_first_template_matrix_v3",
                exc,
                release_claim_allowed=False,
            ),
            args.report,
        )
    report = run_phase_matrix(
        payload,
        base=args.spec.resolve().parent,
        output_root=args.output_root.resolve(),
        python=args.python.resolve(),
        workers=args.workers,
        continue_on_failure=args.continue_on_failure,
        office_settle_seconds=max(0.0, float(args.office_settle_seconds)),
        dependency_bindings_path=dependency_bindings_path,
        dependency_bindings=dependency_bindings,
    )
    return finish(report, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
