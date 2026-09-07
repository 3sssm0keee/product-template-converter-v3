from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from dependency_bindings_v3 import (
    dependency_bindings_load_blocked_result,
    dependency_bindings_reference,
    load_dependency_bindings,
    missing_dependency_findings,
    resolve_dependency,
)
from pipeline_common import finish, read_json, result, sha256_file, write_json
from schema_validation_v3 import load_schema, validate_instance
from verify_release_matrix_v3 import case_sha256, matrix_dimension_findings, verify_matrix


ROOT = Path(__file__).resolve().parents[1]
RUN_PIPELINE = ROOT / "scripts" / "run_pipeline.py"
SPEC_SCHEMA = ROOT / "references" / "schemas" / "release-matrix-run-spec-v3.schema.json"


def _resolve(base: Path, value: Any) -> Path:
    path = Path(str(value))
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _verify_ref(base: Path, reference: dict[str, Any], label: str, findings: list[dict[str, Any]]) -> Path | None:
    path = _resolve(base, reference.get("path"))
    expected = str(reference.get("sha256") or "").upper()
    if not path.is_file():
        findings.append({"code": "MATRIX_RUN_INPUT_MISSING", "input": label, "path": str(path)})
        return None
    actual = sha256_file(path)
    if actual != expected:
        findings.append({"code": "MATRIX_RUN_INPUT_SHA_MISMATCH", "input": label, "path": str(path), "expected": expected, "actual": actual})
    return path


def validate_run_spec(payload: dict[str, Any], *, base: Path) -> dict[str, Any]:
    errors = validate_instance(payload, load_schema(SPEC_SCHEMA))
    if errors:
        return result("FAIL", "validate_release_matrix_run_spec_v3", findings=[{"code": "MATRIX_RUN_SPEC_SCHEMA_INVALID", "errors": errors}])
    findings: list[dict[str, Any]] = []
    _verify_ref(base, payload["manifest"], "manifest", findings)
    case_ids = [str(case["case_id"]) for case in payload["cases"]]
    if len(case_ids) != len(set(case_ids)):
        findings.append({"code": "MATRIX_CASE_ID_DUPLICATE"})
    dimension_findings, packs = matrix_dimension_findings(payload["cases"])
    findings.extend(dimension_findings)
    for case in payload["cases"]:
        case_id = str(case["case_id"])
        _verify_ref(base, case["source"], f"{case_id}:source", findings)
        _verify_ref(base, case["content_decision"], f"{case_id}:content_decision", findings)
        _verify_ref(base, case["review_receipt"], f"{case_id}:review_receipt", findings)
    return result(
        "PASS" if not findings else "FAIL",
        "validate_release_matrix_run_spec_v3",
        findings=findings,
        case_count=len(payload["cases"]),
        template_packs=[f"{pack[0]}@{pack[1]}" for pack in packs],
    )


def build_pipeline_command(
    case: dict[str, Any],
    *,
    base: Path,
    output_dir: Path,
    cache_dir: Path,
    python: Path,
    workers: int,
    dependency_bindings_path: Path | None = None,
    dependency_bindings: dict[str, Any] | None = None,
) -> list[str]:
    pack = case["template_pack"]
    runtime_python = Path(resolve_dependency(dependency_bindings, "python")) if dependency_bindings else python
    command = [
        str(runtime_python),
        str(RUN_PIPELINE),
        "--source", str(_resolve(base, case["source"]["path"])),
        "--output-dir", str(output_dir),
        "--template-pack", f"{pack['id']}@{pack['version']}",
        "--decision-bundle", str(_resolve(base, case["content_decision"]["path"])),
        "--review-receipt", str(_resolve(base, case["review_receipt"]["path"])),
        "--delivery-target", str(case["delivery_target"]),
        "--cache-dir", str(cache_dir),
        "--workers", str(workers),
        "--python", str(runtime_python),
        "--report", str(output_dir / "reports" / "pipeline_report.json"),
    ]
    if dependency_bindings_path is not None:
        command.extend(["--dependency-bindings", str(dependency_bindings_path.resolve())])
    return command


def _stage_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(stage.get("stage")): stage
        for stage in report.get("stages", [])
        if isinstance(stage, dict) and stage.get("stage")
    }


def _artifact(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {"path": str(resolved), "sha256": sha256_file(resolved)}


def collect_case_evidence(case: dict[str, Any], *, base: Path, report_path: Path) -> dict[str, Any]:
    report = read_json(report_path)
    stages = _stage_map(report)
    resolver = stages.get("template_resolver_v3", {}).get("template_pack", {})
    expected_pack = case["template_pack"]
    if resolver.get("id") != expected_pack["id"]:
        raise ValueError(f"template pack id mismatch: expected={expected_pack['id']} actual={resolver.get('id')}")
    if str(resolver.get("version")) != str(expected_pack["version"]):
        raise ValueError(f"template pack version mismatch: expected={expected_pack['version']} actual={resolver.get('version')}")
    resolved_template_sha = str(resolver.get("template_sha256") or "").upper()
    expected_template_sha = str(expected_pack.get("template_sha256") or "").upper()
    if resolved_template_sha != expected_template_sha:
        raise ValueError(f"template sha256 mismatch: expected={expected_template_sha} actual={resolved_template_sha}")
    if str(report.get("source_sha256") or "").upper() != str(case["source"]["sha256"]).upper():
        raise ValueError("pipeline source sha256 does not match the run specification")
    if str(report.get("source_format") or "").upper() != str(case["source"]["format"]).upper():
        raise ValueError("pipeline source format does not match the run specification")
    if str(report.get("delivery_target") or "").lower() != str(case["delivery_target"]).lower():
        raise ValueError("pipeline delivery target does not match the run specification")
    content_plan_path = _resolve(report_path.parent, report.get("content_plan"))
    render_plan_path = _resolve(report_path.parent, report.get("render_plan"))
    program_path = _resolve(report_path.parent, resolver.get("program_path"))
    output_path = _resolve(report_path.parent, report.get("output"))
    # 证据必须引用内容计划实际绑定的运行副本，外部 JSON 的换行可能不同。
    content_plan = read_json(content_plan_path)
    runtime_paths = {}
    for name in ("content_decision", "review_receipt"):
        reference = content_plan.get("upstream", {}).get(name)
        if not isinstance(reference, dict) or not reference.get("path"):
            raise ValueError(f"runtime {name} reference missing")
        path = _resolve(content_plan_path.parent, reference["path"])
        if sha256_file(path) != str(reference.get("sha256") or "").upper():
            raise ValueError(f"runtime {name} hash mismatch")
        runtime_paths[name] = path
    decision_path = runtime_paths["content_decision"]
    receipt_path = runtime_paths["review_receipt"]
    if decision_path.resolve() != _resolve(report_path.parent, report.get("content_decision")).resolve():
        raise ValueError("runtime content_decision report path mismatch")
    evidence = {
        "case_id": case["case_id"],
        "source": {
            **_artifact(_resolve(base, case["source"]["path"])),
            "format": case["source"]["format"],
        },
        "template_pack": {
            "id": case["template_pack"]["id"],
            "version": str(case["template_pack"]["version"]),
            "template_sha256": resolved_template_sha,
        },
        "delivery_target": case["delivery_target"],
        "artifacts": {
            "content_decision": _artifact(decision_path),
            "review_receipt": _artifact(receipt_path),
            "content_plan": _artifact(content_plan_path),
            "render_plan": _artifact(render_plan_path),
            "template_program": _artifact(program_path),
            "output": _artifact(output_path),
            "pipeline_report": _artifact(report_path),
        },
    }
    evidence["case_sha256"] = case_sha256(evidence)
    return evidence


def run_matrix(
    payload: dict[str, Any],
    *,
    base: Path,
    output_root: Path,
    python: Path,
    workers: int,
    continue_on_failure: bool,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    dependency_bindings_path: Path | None = None,
    dependency_bindings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    dependency_bindings_fact = None
    if dependency_bindings_path is not None:
        try:
            dependency_bindings = dependency_bindings or load_dependency_bindings(dependency_bindings_path)
        except (OSError, ValueError) as exc:
            return dependency_bindings_load_blocked_result(dependency_bindings_path, "run_release_matrix_v3", exc)
        dependency_bindings_fact = dependency_bindings_reference(dependency_bindings_path)
        missing = missing_dependency_findings(dependency_bindings)
        if missing:
            return result(
                "BLOCKED",
                "run_release_matrix_v3",
                findings=missing,
                dependency_bindings=dependency_bindings_fact,
            )
    validation = validate_run_spec(payload, base=base)
    if validation["status"] != "PASS":
        return result("FAIL", "run_release_matrix_v3", findings=validation["findings"], validation=validation)
    if output_root.exists() and any(output_root.iterdir()):
        return result("BLOCKED", "run_release_matrix_v3", findings=[{
            "code": "MATRIX_OUTPUT_ROOT_NOT_EMPTY",
            "path": str(output_root),
            "message": "Use a new empty directory so stale reports cannot enter formal evidence.",
        }])
    output_root.mkdir(parents=True, exist_ok=True)
    cache_dir = output_root / "cache"
    run_summaries: list[dict[str, Any]] = []
    evidence_cases: list[dict[str, Any]] = []
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
            findings.append({"code": "MATRIX_PIPELINE_REPORT_MISSING", **summary})
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
                    findings.append({"code": "MATRIX_CASE_EVIDENCE_COLLECTION_FAILED", "case_id": case_id, "message": str(exc)})
            else:
                findings.append({
                    "code": "MATRIX_PIPELINE_NOT_DELIVERABLE",
                    "case_id": case_id,
                    "status": report.get("status"),
                    "deliverable": report.get("deliverable"),
                    "returncode": completed.returncode,
                })
        run_summaries.append(summary)
        if findings and not continue_on_failure:
            break

    evidence_path = output_root / "release-matrix-evidence-v3.json"
    verify_path = output_root / "verify-release-matrix-v3.json"
    verification: dict[str, Any] | None = None
    if not findings and len(evidence_cases) == 16:
        evidence = {
            "schema_version": "release-matrix-evidence-v3",
            "manifest": {
                "path": str(_resolve(base, payload["manifest"]["path"])),
                "sha256": str(payload["manifest"]["sha256"]).upper(),
            },
            "cases": evidence_cases,
        }
        write_json(evidence_path, evidence)
        verification = verify_matrix(evidence, base=evidence_path.parent)
        write_json(verify_path, verification)
        if verification.get("status") != "PASS":
            findings.append({"code": "MATRIX_EVIDENCE_VERIFICATION_FAILED", "report": str(verify_path)})

    payload = result(
        "PASS" if not findings and verification and verification.get("status") == "PASS" else "FAIL",
        "run_release_matrix_v3",
        findings=findings,
        executed_case_count=len(run_summaries),
        verified_case_count=len(evidence_cases),
        cases=run_summaries,
        matrix_evidence=str(evidence_path) if evidence_path.is_file() else None,
        matrix_verification=str(verify_path) if verify_path.is_file() else None,
    )
    if dependency_bindings_fact is not None:
        payload["dependency_bindings"] = dependency_bindings_fact
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="串行执行 V3 两模板 16 条正式流水线并自动生成全链 SHA 证据")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--continue-on-failure", action="store_true")
    parser.add_argument("--dependency-bindings", type=Path, help="DependencyBindingsV3 JSON discovered once and passed to every pipeline child")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.workers < 1:
        return finish(result("FAIL", "run_release_matrix_v3", findings=[{"code": "WORKER_COUNT_INVALID"}]), args.report)
    payload = read_json(args.spec)
    dependency_bindings_path = args.dependency_bindings.resolve() if args.dependency_bindings else None
    try:
        dependency_bindings = load_dependency_bindings(dependency_bindings_path) if dependency_bindings_path else None
    except (OSError, ValueError) as exc:
        return finish(dependency_bindings_load_blocked_result(dependency_bindings_path, "run_release_matrix_v3", exc), args.report)
    report = run_matrix(
        payload,
        base=args.spec.resolve().parent,
        output_root=args.output_root.resolve(),
        python=args.python.resolve(),
        workers=args.workers,
        continue_on_failure=args.continue_on_failure,
        dependency_bindings_path=dependency_bindings_path,
        dependency_bindings=dependency_bindings,
    )
    return finish(report, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
