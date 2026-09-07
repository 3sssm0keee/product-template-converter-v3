from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from dependency_bindings_v3 import (
    dependency_bindings_load_blocked_result,
    dependency_bindings_reference,
    load_dependency_bindings,
    missing_dependency_findings,
)
from pipeline_common import finish, read_json, result, sha256_file, write_json
from run_release_matrix_v3 import collect_case_evidence
from verify_release_matrix_v3 import _resolve, _verify_case, _verify_ref, case_sha256, verify_matrix


FIRST_DIMENSIONS = {
    ("zxty-fixed-v1", "1.0.0", source_format, target)
    for source_format in ("DOC", "DOCX", "PPT", "PPTX", "PDF")
    for target in ("desktop", "mobile")
}
SECOND_FORMATS = ("DOCX", "PPTX", "PDF")
TARGETS = ("desktop", "mobile")
COMPATIBILITY_STATES = {"REUSABLE", "REBIND_ONLY", "STALE_REVERIFY", "INVALID"}


def _expected_phase_header(phase: str) -> tuple[str, str, int]:
    if phase == "first":
        return "first-template-validation-evidence-v3", "zxty-fixed-v1-only", 10
    if phase == "second":
        return "second-template-validation-evidence-v3", "second-template-only", 6
    raise ValueError(f"unsupported phase: {phase}")


def _phase_dimensions(cases: list[dict[str, Any]]) -> set[tuple[str, str, str, str]]:
    return {
        (
            str(case.get("template_pack", {}).get("id") or ""),
            str(case.get("template_pack", {}).get("version") or ""),
            str(case.get("source", {}).get("format") or "").upper(),
            str(case.get("delivery_target") or "").lower(),
        )
        for case in cases
    }


def _validate_phase_dimensions(phase: str, cases: list[dict[str, Any]], findings: list[dict[str, Any]]) -> None:
    actual = _phase_dimensions(cases)
    if phase == "first":
        expected = FIRST_DIMENSIONS
    else:
        packs = sorted({(item[0], item[1]) for item in actual})
        if len(packs) != 1 or packs[0][0] == "zxty-fixed-v1":
            findings.append({
                "code": "PHASE_TEMPLATE_COVERAGE_INVALID",
                "phase": phase,
                "actual": [f"{pack_id}@{version}" for pack_id, version in packs],
            })
            return
        pack_id, version = packs[0]
        expected = {
            (pack_id, version, source_format, target)
            for source_format in SECOND_FORMATS
            for target in TARGETS
        }
    if actual != expected:
        findings.append({
            "code": "PHASE_DIMENSION_COVERAGE_MISMATCH",
            "phase": phase,
            "missing": sorted(expected - actual),
            "extra": sorted(actual - expected),
        })


def _verify_historical_case_refs(case: dict[str, Any], *, base: Path, findings: list[dict[str, Any]]) -> None:
    case_id = str(case.get("case_id") or "")
    expected_case_sha = str(case.get("case_sha256") or "").upper()
    actual_case_sha = case_sha256(case)
    if expected_case_sha != actual_case_sha:
        findings.append({
            "code": "MATRIX_CASE_SHA_MISMATCH",
            "case_id": case_id,
            "expected": expected_case_sha,
            "actual": actual_case_sha,
        })
    source = case.get("source")
    if isinstance(source, dict):
        _verify_ref(base, source, f"{case_id}:source", findings)
    else:
        findings.append({"code": "PHASE_SOURCE_REF_INVALID", "case_id": case_id})
    artifacts = case.get("artifacts")
    if not isinstance(artifacts, dict):
        findings.append({"code": "PHASE_ARTIFACTS_INVALID", "case_id": case_id})
        return
    for name, reference in artifacts.items():
        if isinstance(reference, dict):
            _verify_ref(base, reference, f"{case_id}:{name}", findings)
        else:
            findings.append({"code": "PHASE_ARTIFACT_REF_INVALID", "case_id": case_id, "artifact": name})


def _normalize_case(case: dict[str, Any], *, base: Path) -> dict[str, Any]:
    report_path = _resolve(base, case["artifacts"]["pipeline_report"]["path"])
    report = read_json(report_path)
    content_plan_path = _resolve(report_path.parent, report.get("content_plan"))
    content_plan = read_json(content_plan_path)
    decision_path = _resolve(report_path.parent, report.get("content_decision"))
    receipt_ref = content_plan.get("upstream", {}).get("review_receipt")
    if not isinstance(receipt_ref, dict) or not receipt_ref.get("path"):
        raise ValueError("content plan does not expose its bound review receipt")
    receipt_path = _resolve(content_plan_path.parent, receipt_ref["path"])
    if sha256_file(receipt_path) != str(receipt_ref.get("sha256") or "").upper():
        raise ValueError("content plan review receipt hash mismatch")
    historical_decision_sha = str(case["artifacts"]["content_decision"]["sha256"]).upper()
    if sha256_file(decision_path) != historical_decision_sha:
        raise ValueError("runtime content decision differs from the approved phase input")
    run_case = {
        "case_id": case["case_id"],
        "source": case["source"],
        "template_pack": case["template_pack"],
        "content_decision": {"path": str(decision_path), "sha256": sha256_file(decision_path)},
        "review_receipt": {"path": str(receipt_path), "sha256": sha256_file(receipt_path)},
        "delivery_target": case["delivery_target"],
    }
    return collect_case_evidence(run_case, base=base, report_path=report_path)


def _ref_summary(reference: dict[str, Any], *, base: Path) -> dict[str, Any]:
    path_value = str(reference.get("path") or "")
    summary = {
        "path": path_value,
        "resolved_path": "",
        "sha256": str(reference.get("sha256") or "").upper(),
        "actual_sha256": None,
        "exists": False,
    }
    if path_value:
        try:
            resolved = _resolve(base, path_value)
            summary["resolved_path"] = str(resolved)
            summary["exists"] = resolved.is_file()
            if resolved.is_file():
                summary["actual_sha256"] = sha256_file(resolved)
        except (OSError, ValueError):
            summary["resolved_path"] = path_value
    return summary


def _manifest_compatibility(
    historical: dict[str, Any],
    current: dict[str, Any],
    *,
    historical_base: Path,
    current_base: Path,
) -> tuple[str, list[dict[str, Any]]]:
    historical_summary = _ref_summary(historical, base=historical_base)
    current_summary = _ref_summary(current, base=current_base)
    reasons: list[dict[str, Any]] = []
    if current_summary["actual_sha256"] != current_summary["sha256"]:
        reasons.append({
            "code": "CURRENT_MANIFEST_REF_INVALID",
            "expected": current_summary["sha256"],
            "actual": current_summary["actual_sha256"],
            "path": current_summary["resolved_path"] or current_summary["path"],
        })
        return "STALE_REVERIFY", reasons
    if historical_summary["sha256"] != current_summary["sha256"]:
        reasons.append({
            "code": "MANIFEST_SHA_CHANGED",
            "historical": historical_summary["sha256"],
            "current": current_summary["sha256"],
        })
        return "STALE_REVERIFY", reasons
    if historical_summary["resolved_path"] != current_summary["resolved_path"]:
        reasons.append({
            "code": "MANIFEST_PATH_REBIND_ONLY",
            "historical_path": historical_summary["resolved_path"] or historical_summary["path"],
            "current_path": current_summary["resolved_path"] or current_summary["path"],
        })
        return "REBIND_ONLY", reasons
    reasons.append({"code": "HISTORICAL_MANIFEST_REUSABLE"})
    return "REUSABLE", reasons


def _dependency_binding_summary(
    dependency_bindings: dict[str, Any] | None,
    dependency_bindings_ref: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if dependency_bindings is None:
        return None
    summary: dict[str, Any] = {
        "status": dependency_bindings.get("status"),
    }
    if dependency_bindings_ref is not None:
        summary.update(dependency_bindings_ref)
    return summary


def _manifest_entries_by_path(manifest_ref: dict[str, Any], *, base: Path, findings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    manifest_path = _resolve(base, str(manifest_ref.get("path") or ""))
    try:
        manifest = read_json(manifest_path)
    except (OSError, ValueError) as exc:
        findings.append({"code": "CURRENT_MANIFEST_JSON_INVALID", "path": str(manifest_path), "message": str(exc)})
        return {}
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list):
        findings.append({"code": "CURRENT_MANIFEST_FILES_INVALID", "path": str(manifest_path)})
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for entry in files:
        if not isinstance(entry, dict):
            findings.append({"code": "CURRENT_MANIFEST_FILE_ENTRY_INVALID", "path": str(manifest_path)})
            continue
        path = str(entry.get("path") or "").replace("\\", "/")
        if path:
            entries[path] = entry
    return entries


def _consumer_findings(consumers: list[str], *, current_manifest_ref: dict[str, Any], current_manifest_base: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not consumers:
        return findings
    entries = _manifest_entries_by_path(current_manifest_ref, base=current_manifest_base, findings=findings)
    for raw_path in consumers:
        relative_path = str(raw_path or "").replace("\\", "/")
        if not relative_path or Path(relative_path).is_absolute() or ".." in Path(relative_path).parts:
            findings.append({"code": "CONSUMER_PATH_INVALID", "path": raw_path})
            continue
        entry = entries.get(relative_path)
        actual_path = current_manifest_base / Path(relative_path)
        if entry is None:
            findings.append({"code": "CONSUMER_MANIFEST_ENTRY_MISSING", "path": relative_path, "actual_path": str(actual_path)})
            continue
        expected_sha = str(entry.get("sha256") or "").upper()
        if not actual_path.is_file():
            findings.append({"code": "CONSUMER_FILE_MISSING", "path": relative_path, "actual_path": str(actual_path), "expected": expected_sha})
            continue
        actual_sha = sha256_file(actual_path)
        if actual_sha != expected_sha:
            findings.append({
                "code": "CONSUMER_SHA_MISMATCH",
                "path": relative_path,
                "actual_path": str(actual_path),
                "expected": expected_sha,
                "actual": actual_sha,
            })
    return findings


def compatibility_audit_phase_evidence(
    payload: dict[str, Any],
    *,
    base: Path,
    phase: str,
    current_manifest_ref: dict[str, Any],
    current_manifest_base: Path | None = None,
    dependency_bindings: dict[str, Any] | None = None,
    dependency_bindings_ref: dict[str, Any] | None = None,
    consumers: list[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    # 消融 C-03：结构校验与 assess_phase_evidence 重复，但删去会使坏 schema 测试失败。
    # 标注为必要行为、可合并实现；本次保留，不削弱合同及 Word 符号/分页保护。
    expected_schema, expected_scope, expected_count = _expected_phase_header(phase)
    findings: list[dict[str, Any]] = []
    if payload.get("schema_version") != expected_schema:
        findings.append({"code": "PHASE_SCHEMA_VERSION_INVALID", "phase": phase, "actual": payload.get("schema_version"), "expected": expected_schema})
    if payload.get("scope") != expected_scope:
        findings.append({"code": "PHASE_SCOPE_INVALID", "phase": phase, "actual": payload.get("scope"), "expected": expected_scope})
    if payload.get("release_claim_allowed") is not False:
        findings.append({"code": "PHASE_RELEASE_CLAIM_FLAG_INVALID", "phase": phase})
    cases = payload.get("cases")
    if not isinstance(cases, list):
        findings.append({"code": "PHASE_CASES_INVALID", "phase": phase})
        cases = []
    if len(cases) != expected_count:
        findings.append({"code": "PHASE_CASE_COUNT_INVALID", "phase": phase, "actual": len(cases), "expected": expected_count})
    case_ids = [str(case.get("case_id") or "") for case in cases if isinstance(case, dict)]
    if len(case_ids) != len(set(case_ids)):
        findings.append({"code": "MATRIX_CASE_ID_DUPLICATE", "phase": phase})
    valid_cases = [case for case in cases if isinstance(case, dict)]
    if len(valid_cases) != len(cases):
        findings.append({"code": "PHASE_CASE_INVALID", "phase": phase})
    _validate_phase_dimensions(phase, valid_cases, findings)
    structural_findings = list(findings)

    current_manifest_base = current_manifest_base or base
    historical_manifest = payload.get("manifest") if isinstance(payload.get("manifest"), dict) else {}
    manifest_state, manifest_reasons = _manifest_compatibility(
        historical_manifest,
        current_manifest_ref,
        historical_base=base,
        current_base=current_manifest_base,
    )
    dependency_findings = missing_dependency_findings(dependency_bindings) if dependency_bindings is not None else []
    dependency_summary = _dependency_binding_summary(dependency_bindings, dependency_bindings_ref)
    current_manifest_summary = _ref_summary(current_manifest_ref, base=current_manifest_base)
    # 审查候选 C-04：默认标签仅作展示，不等于已校验消费者；实际审计须显式传入文件。
    consumer_values = list(consumers or ["merge_release_matrix_evidence_v3.compatibility_audit_phase_evidence"])
    consumer_findings = _consumer_findings(list(consumers or []), current_manifest_ref=current_manifest_ref, current_manifest_base=current_manifest_base)

    compatibility_cases: list[dict[str, Any]] = []
    for case in valid_cases:
        case_findings: list[dict[str, Any]] = []
        _verify_historical_case_refs(case, base=base, findings=case_findings)
        if structural_findings or case_findings:
            classification = "INVALID"
            reasons = [*structural_findings, *case_findings]
        elif dependency_findings or consumer_findings:
            classification = "STALE_REVERIFY"
            # 审查候选 C-05：公共原因在各 case 重复；未经独立消融，不宣称可删。
            reasons = [*manifest_reasons, *dependency_findings, *consumer_findings]
        else:
            classification = manifest_state
            reasons = manifest_reasons
        # 消融 C-02：既往隔离副本移除此防御分支后测试通过，当前仅标注、保留实现。
        if classification not in COMPATIBILITY_STATES:
            raise ValueError(f"invalid compatibility classification: {classification}")
        # 消融 C-01：consumers/current_binding 为公共重复字段；既往上移至顶层的实验通过。
        # 当前保持输出合同；下游采纳前须核对字段消费者，不能只凭测试通过直接删除。
        compatibility_cases.append({
            "case_id": str(case.get("case_id") or ""),
            "classification": classification,
            "phase": phase,
            "consumers": consumer_values,
            "case_path": str(_resolve(base, case["artifacts"]["pipeline_report"]["path"])) if isinstance(case.get("artifacts"), dict) and isinstance(case["artifacts"].get("pipeline_report"), dict) else "",
            "case_sha256": str(case.get("case_sha256") or "").upper(),
            "current_binding": {
                "manifest": {
                    "path": current_manifest_summary["resolved_path"] or current_manifest_summary["path"],
                    "sha256": current_manifest_summary["sha256"],
                    "actual_sha256": current_manifest_summary["actual_sha256"],
                },
                **({"dependency_bindings": dependency_summary} if dependency_summary is not None else {}),
            },
            "reasons": reasons,
        })

    classifications = {case["classification"] for case in compatibility_cases}
    if findings or "INVALID" in classifications:
        status = "FAIL"
    elif dependency_findings:
        status = "BLOCKED"
    elif consumer_findings or "STALE_REVERIFY" in classifications:
        status = "HUMAN_REVIEW"
    else:
        status = "PASS"
    return result(
        status,
        "compatibility_audit_release_matrix_phase_evidence_v3",
        findings=findings,
        phase=phase,
        case_count=len(compatibility_cases),
        release_claim_allowed=False,
        merge_ready=False,
        evidence_copy_created=False,
        current_manifest={
            "path": current_manifest_summary["resolved_path"] or current_manifest_summary["path"],
            "sha256": current_manifest_summary["sha256"],
            "actual_sha256": current_manifest_summary["actual_sha256"],
        },
        dependency_bindings=dependency_summary,
        dependency_findings=dependency_findings,
        consumer_findings=consumer_findings,
        compatibility_summary={state: sum(1 for case in compatibility_cases if case["classification"] == state) for state in sorted(COMPATIBILITY_STATES)},
        compatibility_cases=compatibility_cases,
    ), compatibility_cases


def assess_phase_evidence(
    payload: dict[str, Any],
    *,
    base: Path,
    phase: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    expected_schema, expected_scope, expected_count = _expected_phase_header(phase)
    findings: list[dict[str, Any]] = []
    if payload.get("schema_version") != expected_schema:
        findings.append({"code": "PHASE_SCHEMA_VERSION_INVALID", "phase": phase, "actual": payload.get("schema_version"), "expected": expected_schema})
    if payload.get("scope") != expected_scope:
        findings.append({"code": "PHASE_SCOPE_INVALID", "phase": phase, "actual": payload.get("scope"), "expected": expected_scope})
    if payload.get("release_claim_allowed") is not False:
        findings.append({"code": "PHASE_RELEASE_CLAIM_FLAG_INVALID", "phase": phase})
    manifest = payload.get("manifest")
    if isinstance(manifest, dict):
        _verify_ref(base, manifest, f"{phase}:manifest", findings)
    else:
        findings.append({"code": "PHASE_MANIFEST_REF_INVALID", "phase": phase})
    cases = payload.get("cases")
    if not isinstance(cases, list):
        findings.append({"code": "PHASE_CASES_INVALID", "phase": phase})
        cases = []
    if len(cases) != expected_count:
        findings.append({"code": "PHASE_CASE_COUNT_INVALID", "phase": phase, "actual": len(cases), "expected": expected_count})
    case_ids = [str(case.get("case_id") or "") for case in cases if isinstance(case, dict)]
    if len(case_ids) != len(set(case_ids)):
        findings.append({"code": "MATRIX_CASE_ID_DUPLICATE", "phase": phase})
    valid_cases = [case for case in cases if isinstance(case, dict)]
    if len(valid_cases) != len(cases):
        findings.append({"code": "PHASE_CASE_INVALID", "phase": phase})
    _validate_phase_dimensions(phase, valid_cases, findings)
    for case in valid_cases:
        _verify_historical_case_refs(case, base=base, findings=findings)
    if findings:
        return result(
            "FAIL",
            "assess_release_matrix_phase_evidence_v3",
            findings=findings,
            phase=phase,
            case_count=len(cases),
            release_claim_allowed=False,
            merge_ready=False,
        ), []

    normalized: list[dict[str, Any]] = []
    for case in valid_cases:
        try:
            normalized.append(_normalize_case(case, base=base))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            findings.append({"code": "PHASE_CASE_NORMALIZATION_FAILED", "phase": phase, "case_id": case.get("case_id"), "message": str(exc)})
    if not findings:
        for case in normalized:
            _verify_case(base, case, findings)
    return result(
        "PASS" if not findings else "FAIL",
        "assess_release_matrix_phase_evidence_v3",
        findings=findings,
        phase=phase,
        case_count=len(normalized),
        release_claim_allowed=False,
        merge_ready=not findings,
        canonical_case_sha256s=[case["case_sha256"] for case in normalized] if not findings else [],
    ), normalized if not findings else []


def merge_phase_evidence(
    first: dict[str, Any],
    second: dict[str, Any],
    *,
    first_base: Path,
    second_base: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    first_report, first_cases = assess_phase_evidence(first, base=first_base, phase="first")
    second_report, second_cases = assess_phase_evidence(second, base=second_base, phase="second")
    findings: list[dict[str, Any]] = []
    if first_report["status"] != "PASS":
        findings.append({"code": "FIRST_PHASE_EVIDENCE_INVALID", "findings": first_report["findings"]})
    if second_report["status"] != "PASS":
        findings.append({"code": "SECOND_PHASE_EVIDENCE_INVALID", "findings": second_report["findings"]})
    first_manifest = first.get("manifest") if isinstance(first.get("manifest"), dict) else {}
    second_manifest = second.get("manifest") if isinstance(second.get("manifest"), dict) else {}
    if str(first_manifest.get("sha256") or "").upper() != str(second_manifest.get("sha256") or "").upper():
        findings.append({"code": "PHASE_MANIFEST_SHA_MISMATCH"})
    if findings:
        return result("FAIL", "merge_release_matrix_evidence_v3", findings=findings, merge_ready=False, release_claim_allowed=False), {}

    matrix = {
        "schema_version": "release-matrix-evidence-v3",
        "manifest": {
            "path": str(_resolve(first_base, first_manifest["path"])),
            "sha256": str(first_manifest["sha256"]).upper(),
        },
        "cases": first_cases + second_cases,
    }
    verification = verify_matrix(matrix, base=first_base)
    if verification.get("status") != "PASS":
        findings.append({"code": "MERGED_MATRIX_VERIFICATION_FAILED", "findings": verification.get("findings", [])})
    return result(
        "PASS" if not findings else "FAIL",
        "merge_release_matrix_evidence_v3",
        findings=findings,
        merge_ready=not findings,
        release_claim_allowed=False,
        release_scope_note="合并矩阵通过不替代性能、黄金案例、独立盲验、verify_release 与正式发布门禁。",
        case_count=len(matrix["cases"]),
        verification=verification,
    ), matrix if not findings else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="只读核验既有阶段证据，并在两阶段齐备后合并为 16 案例正式矩阵")
    parser.add_argument("--first-template-evidence", type=Path, required=True)
    parser.add_argument("--second-template-evidence", type=Path)
    parser.add_argument("--compatibility-current-manifest", type=Path, help="只读兼容审计使用的当前 manifest.json；不会写入正式 evidence")
    parser.add_argument("--consumer", action="append", help="兼容审计报告中的当前消费者/阶段标识，可重复传入")
    parser.add_argument("--dependency-bindings", type=Path, help="可选 DependencyBindingsV3；缺失必需依赖时兼容审计保持 BLOCKED")
    parser.add_argument("--output", type=Path, help="仅在两份阶段证据均通过完整验证时写入合并矩阵")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    first_path = args.first_template_evidence.resolve()
    first = read_json(first_path)
    # 审查候选 C-06：兼容入口优先返回，混用 --output/第二模板参数时这些参数不生效。
    # 本次仅标注，不改 CLI 行为，也不将静态兼容审计升级为正式合并。
    if args.compatibility_current_manifest is not None:
        current_manifest_path = args.compatibility_current_manifest.resolve()
        dependency_bindings = None
        dependency_bindings_fact = None
        if args.dependency_bindings is not None:
            dependency_bindings_path = args.dependency_bindings.resolve()
            try:
                dependency_bindings = load_dependency_bindings(dependency_bindings_path)
            except (OSError, ValueError) as exc:
                return finish(
                    dependency_bindings_load_blocked_result(
                        dependency_bindings_path,
                        "compatibility_audit_release_matrix_phase_evidence_v3",
                        exc,
                        release_claim_allowed=False,
                        merge_ready=False,
                    ),
                    args.report,
                )
            dependency_bindings_fact = dependency_bindings_reference(dependency_bindings_path)
        if not current_manifest_path.is_file():
            report = result(
                "BLOCKED",
                "compatibility_audit_release_matrix_phase_evidence_v3",
                findings=[{"code": "CURRENT_MANIFEST_MISSING", "path": str(current_manifest_path)}],
                release_claim_allowed=False,
                merge_ready=False,
            )
            return finish(report, args.report)
        report, _ = compatibility_audit_phase_evidence(
            first,
            base=first_path.parent,
            phase="first",
            current_manifest_ref={"path": str(current_manifest_path), "sha256": sha256_file(current_manifest_path)},
            current_manifest_base=current_manifest_path.parent,
            dependency_bindings=dependency_bindings,
            dependency_bindings_ref=dependency_bindings_fact,
            consumers=args.consumer,
        )
        return finish(report, args.report)
    if args.second_template_evidence is None:
        report, _ = assess_phase_evidence(first, base=first_path.parent, phase="first")
        report["pending_phase"] = "second"
        return finish(report, args.report)
    second_path = args.second_template_evidence.resolve()
    second = read_json(second_path)
    report, matrix = merge_phase_evidence(first, second, first_base=first_path.parent, second_base=second_path.parent)
    if report["status"] == "PASS":
        if args.output is None:
            return finish(result("FAIL", "merge_release_matrix_evidence_v3", findings=[{"code": "MERGED_MATRIX_OUTPUT_REQUIRED"}], merge_ready=False, release_claim_allowed=False), args.report)
        write_json(args.output.resolve(), matrix)
        report["output"] = str(args.output.resolve())
        report["output_sha256"] = sha256_file(args.output.resolve())
    return finish(report, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
