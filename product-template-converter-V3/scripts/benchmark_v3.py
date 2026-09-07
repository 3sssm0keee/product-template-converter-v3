from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from dependency_bindings_v3 import (
    dependency_bindings_load_blocked_result,
    dependency_bindings_reference,
    load_dependency_bindings,
    missing_dependency_findings,
    rebind_runtime_spec_payload,
    resolve_dependency,
)
from pipeline_common import finish, read_json, result, sha256_file, write_json
from v3_common import canonical_json_sha256


_EXECUTABLE_DIGEST_CACHE: dict[tuple[str, int, int], str] = {}


def _stage_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(stage.get("stage")): stage
        for stage in report.get("stages", [])
        if isinstance(stage, dict) and stage.get("stage")
    }


def _server_executable(command: str) -> str:
    value = str(command or "").strip()
    if not value:
        return ""
    if value.startswith('"'):
        closing = value.find('"', 1)
        return value[1:closing] if closing > 1 else ""
    match = re.search(r"\.exe(?=\s|$)", value, flags=re.IGNORECASE)
    return value[: match.end()] if match else value


def _executable_fingerprint(value: str) -> dict[str, Any]:
    raw_path = _server_executable(value)
    path = Path(raw_path).resolve() if raw_path else None
    if path is None or not path.is_file():
        return {"path": str(path or raw_path), "exists": False}
    stat = path.stat()
    cache_key = (str(path).casefold(), stat.st_size, stat.st_mtime_ns)
    digest = _EXECUTABLE_DIGEST_CACHE.get(cache_key)
    if digest is None:
        digest = sha256_file(path)
        _EXECUTABLE_DIGEST_CACHE[cache_key] = digest
    return {
        "path": str(path),
        "exists": True,
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest,
    }


def environment_signature(report: dict[str, Any]) -> dict[str, Any]:
    """绑定性能样本的机器、Python 和实际 Office 二进制，不信任仅声明的版本号。"""
    preflight = _stage_map(report).get("preflight_dependencies", {})
    office = preflight.get("office", {}) if isinstance(preflight.get("office"), dict) else {}
    servers = office.get("servers", {}) if isinstance(office.get("servers"), dict) else {}
    word = office.get("word", {}) if isinstance(office.get("word"), dict) else {}
    candidates = word.get("machine_candidates") or office.get("word_machine_candidates") or []
    word_fingerprints = []
    for candidate in candidates if isinstance(candidates, list) else []:
        if not isinstance(candidate, dict):
            continue
        fingerprint = _executable_fingerprint(str(candidate.get("executable") or candidate.get("server") or ""))
        if fingerprint not in word_fingerprints:
            word_fingerprints.append(fingerprint)
    if not word_fingerprints and servers.get("word"):
        word_fingerprints.append(_executable_fingerprint(str(servers.get("word"))))
    word_fingerprints.sort(key=lambda item: str(item.get("path", "")).casefold())
    python_path = str(preflight.get("python") or "")
    return {
        "machine": {
            "node": platform.node(),
            "platform": platform.platform(),
        },
        "python": {
            "reported_version": preflight.get("python_version"),
            "executable": _executable_fingerprint(python_path),
        },
        "office": {
            "wps": _executable_fingerprint(str(servers.get("wps") or "")),
            "word_candidates": word_fingerprints,
        },
    }


def _environment_complete(signature: dict[str, Any]) -> bool:
    python_executable = signature.get("python", {}).get("executable", {})
    office = signature.get("office", {})
    wps = office.get("wps", {}) if isinstance(office, dict) else {}
    word_candidates = office.get("word_candidates", []) if isinstance(office, dict) else []
    return (
        bool(signature.get("machine", {}).get("node"))
        and python_executable.get("exists") is True
        and bool(python_executable.get("sha256"))
        and wps.get("exists") is True
        and bool(wps.get("sha256"))
        and bool(word_candidates)
        and all(item.get("exists") is True and item.get("sha256") for item in word_candidates if isinstance(item, dict))
    )


def quality_signature(report: dict[str, Any]) -> dict[str, Any]:
    stages = _stage_map(report)
    analysis = stages.get("analyze_rendered_pages", {})
    mapping = stages.get("validate_content_map", {}) or stages.get("validate_content_plan_v3", {})
    return {
        "status": report.get("status"),
        "deliverable": report.get("deliverable"),
        "delivery_target": report.get("delivery_target"),
        "delivery_format": report.get("delivery_format"),
        "source_count": mapping.get("source_count"),
        "mapped_count": mapping.get("mapped_count"),
        "word_pages": analysis.get("word", {}).get("page_count") if isinstance(analysis.get("word"), dict) else None,
        "wps_pages": analysis.get("wps", {}).get("page_count") if isinstance(analysis.get("wps"), dict) else None,
        "word_identity": stages.get("scan_source_identity_rendered_word", {}).get("status"),
        "wps_identity": stages.get("scan_source_identity_rendered_wps", {}).get("status"),
    }


def cache_signature(report: dict[str, Any]) -> dict[str, Any]:
    stages = _stage_map(report)
    final_delivery = stages.get("final_delivery", {})
    final_cache = report.get("final_artifact_cache") if isinstance(report.get("final_artifact_cache"), dict) else {}
    cache_stage = stages.get("final_artifact_cache", {})
    return {
        "status": str(final_cache.get("status") or cache_stage.get("cache_status") or "").upper() or None,
        "key": final_cache.get("key") or cache_stage.get("cache_key"),
        "final_delivery_cache_reused": final_delivery.get("cache_reused") is True,
    }


def provenance_signature(report: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable input contract carried by a pipeline report."""
    stages = _stage_map(report)
    decision_stage = stages.get("decision_task_bundle_v3", {})
    review_stage = stages.get("content_review_v3", {})
    return {
        "source_sha256": str(report.get("source_sha256") or "").upper() or None,
        "decision_sha256": str(decision_stage.get("decision_sha256") or report.get("decision_sha256") or "").upper() or None,
        "decision_id": decision_stage.get("decision_id") or report.get("decision_id"),
        "review_receipt_sha256": str(review_stage.get("receipt_sha256") or report.get("review_receipt_sha256") or "").upper() or None,
        "review_id": review_stage.get("review_id") or report.get("review_id"),
        "receipt_fresh": review_stage.get("receipt_fresh") is True or report.get("receipt_fresh") is True,
    }


def _command_path(command: list[Any], flag: str) -> Path | None:
    try:
        return Path(str(command[command.index(flag) + 1])).expanduser()
    except (ValueError, IndexError):
        return None


def _content_map_contract_sha(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    try:
        payload = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return canonical_json_sha256(payload) if isinstance(payload, dict) else None


def _pipeline_materialized_json_sha256(path: Path) -> str | None:
    """Hash JSON exactly as ``run_pipeline.py`` materializes it into ``work``.

    The V3 pipeline reads an external decision bundle and writes it with
    ``write_json`` before validating the receipt.  ``write_json`` uses a stable
    two-space, UTF-8 serialization without a trailing newline.  Hashing the
    source file bytes here made a harmless final-newline difference look like
    receipt drift even though the pipeline validated the materialized bytes.
    """
    try:
        payload = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    # ``Path.write_text`` uses the host text newline convention.  Reproduce
    # that conversion so this digest equals the bytes copied into ``work``.
    if os.linesep != "\n":
        serialized = serialized.replace("\n", os.linesep)
    encoded = serialized.encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def _receipt_is_fresh_for_command(
    command: list[Any],
    *,
    source_sha256: str | None,
    decision_sha256: str | None,
) -> bool:
    receipt_path = _command_path(command, "--review-receipt")
    if receipt_path is None or not receipt_path.is_file() or not source_sha256 or not decision_sha256:
        return False
    try:
        receipt = read_json(receipt_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    binding = receipt.get("binding") if isinstance(receipt.get("binding"), dict) else {}
    evidence = {
        str(item.get("evidence_id")): str(item.get("sha256") or "").upper()
        for item in receipt.get("evidence_bindings", [])
        if isinstance(item, dict) and item.get("evidence_id")
    }
    pipeline_path = Path(str(command[1])).expanduser() if len(command) > 1 else None
    compiler_path = pipeline_path.parent / "fixed_template_compiler_v3.py" if pipeline_path else None
    compiler_matches = (
        compiler_path is not None
        and compiler_path.is_file()
        and str(binding.get("compiler_sha256") or "").upper() == sha256_file(compiler_path)
    )
    return (
        receipt.get("overall_action") == "APPROVED"
        and bool(receipt.get("review_id"))
        and str(binding.get("source_sha256") or "").upper() == source_sha256
        and str(binding.get("candidate_output_sha256") or "").upper() == decision_sha256
        and evidence.get("CONTENT-DECISION") == decision_sha256
        and compiler_matches
    )


def _receipt_binding_diagnostics(
    command: list[Any],
    *,
    source_sha256: str | None,
    decision_sha256: str | None,
) -> dict[str, Any]:
    """Explain a stale receipt without weakening the freshness contract."""
    receipt_path = _command_path(command, "--review-receipt")
    pipeline_path = Path(str(command[1])).expanduser() if len(command) > 1 else None
    compiler_path = pipeline_path.parent / "fixed_template_compiler_v3.py" if pipeline_path else None
    current_compiler_sha256 = (
        sha256_file(compiler_path)
        if compiler_path is not None and compiler_path.is_file()
        else None
    )
    if receipt_path is None or not receipt_path.is_file():
        return {"receipt": "missing"}
    try:
        receipt = read_json(receipt_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"receipt": "unreadable"}
    binding = receipt.get("binding") if isinstance(receipt.get("binding"), dict) else {}
    evidence = {
        str(item.get("evidence_id")): str(item.get("sha256") or "").upper()
        for item in receipt.get("evidence_bindings", [])
        if isinstance(item, dict) and item.get("evidence_id")
    }
    expected = {
        "source_sha256": source_sha256,
        "candidate_output_sha256": decision_sha256,
        "content_decision_evidence_sha256": decision_sha256,
        "compiler_sha256": current_compiler_sha256,
    }
    actual = {
        "source_sha256": str(binding.get("source_sha256") or "").upper() or None,
        "candidate_output_sha256": str(binding.get("candidate_output_sha256") or "").upper() or None,
        "content_decision_evidence_sha256": evidence.get("CONTENT-DECISION"),
        "compiler_sha256": str(binding.get("compiler_sha256") or "").upper() or None,
    }
    return {
        "mismatches": [key for key in expected if expected[key] != actual[key]],
        "expected": expected,
        "actual": actual,
    }


def _input_provenance(command: list[Any]) -> dict[str, Any]:
    source = _command_path(command, "--source")
    source_sha = sha256_file(source) if source is not None and source.is_file() else None
    content_map = _command_path(command, "--content-map")
    decision = _command_path(command, "--decision-bundle")
    receipt = _command_path(command, "--review-receipt")
    out: dict[str, Any] = {
        "source_sha256": source_sha,
        "mapping_contract_sha256": _content_map_contract_sha(content_map),
        "decision_sha256": None,
        "decision_id": None,
        "review_receipt_sha256": None,
        "review_id": None,
        "receipt_fresh": None,
    }
    if decision is not None and decision.is_file():
        payload = read_json(decision)
        migration = payload.get("migration") if isinstance(payload.get("migration"), dict) else {}
        decision_sha = _pipeline_materialized_json_sha256(decision)
        out.update({
            "mapping_contract_sha256": str(migration.get("source_sha256") or "").upper() or None,
            "decision_sha256": decision_sha,
            "decision_id": payload.get("decision_id"),
        })
        if receipt is not None and receipt.is_file():
            receipt_payload = read_json(receipt)
            out.update({
                "review_receipt_sha256": sha256_file(receipt),
                "review_id": receipt_payload.get("review_id"),
                "receipt_fresh": _receipt_is_fresh_for_command(
                    command,
                    source_sha256=source_sha,
                    decision_sha256=decision_sha,
                ),
            })
    return out


def _contract_findings(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate literal case paths before the first child process is started."""
    findings: list[dict[str, Any]] = []
    cwd_value = case.get("cwd")
    cwd = Path(str(cwd_value)).expanduser() if cwd_value else None
    if cwd is None or not cwd.is_dir():
        findings.append({"code": "PERFORMANCE_CASE_CWD_INVALID", "path": str(cwd_value or "")})
    commands = {"v2": case.get("v2_command"), "v3": case.get("v3_command")}
    for label, command in commands.items():
        if not isinstance(command, list) or not command:
            findings.append({"code": "PERFORMANCE_CASE_COMMAND_INVALID", "group": label})
            continue
        executable = Path(_server_executable(str(command[0]))).expanduser()
        if not executable.is_file():
            findings.append({"code": "PERFORMANCE_CASE_EXECUTABLE_INVALID", "group": label, "path": str(executable)})
        required_paths = [("--source", "PERFORMANCE_CASE_SOURCE_INVALID")]
        required_paths.extend(
            [("--content-map", "PERFORMANCE_CASE_CONTENT_MAP_INVALID")]
            if label == "v2"
            else [
                ("--decision-bundle", "PERFORMANCE_CASE_DECISION_INVALID"),
                ("--review-receipt", "PERFORMANCE_CASE_RECEIPT_INVALID"),
            ]
        )
        for flag, code in required_paths:
            if flag not in command:
                findings.append({"code": code, "group": label, "path": ""})
                continue
            path = _command_path(command, flag)
            if path is None or not path.is_file():
                findings.append({"code": code, "group": label, "path": str(path or "")})
    if all(isinstance(command, list) for command in commands.values()):
        v2_provenance = _input_provenance(commands["v2"])
        v3_provenance = _input_provenance(commands["v3"])
        if v2_provenance.get("source_sha256") and v3_provenance.get("source_sha256") and (
            v2_provenance["source_sha256"] != v3_provenance["source_sha256"]
        ):
            findings.append({
                "code": "PERFORMANCE_CASE_SOURCE_MISMATCH",
                "v2": v2_provenance["source_sha256"],
                "v3": v3_provenance["source_sha256"],
            })
        v2_mapping = str(v2_provenance.get("mapping_contract_sha256") or "").upper()
        v3_mapping = str(v3_provenance.get("mapping_contract_sha256") or "").upper()
        if re.fullmatch(r"[A-F0-9]{64}", v2_mapping) is None:
            findings.append({"code": "PERFORMANCE_CASE_MAPPING_CONTRACT_MISSING", "group": "v2"})
        if re.fullmatch(r"[A-F0-9]{64}", v3_mapping) is None:
            findings.append({"code": "PERFORMANCE_CASE_MAPPING_CONTRACT_MISSING", "group": "v3"})
        if (
            re.fullmatch(r"[A-F0-9]{64}", v2_mapping)
            and re.fullmatch(r"[A-F0-9]{64}", v3_mapping)
            and v2_mapping != v3_mapping
        ):
            findings.append({
                "code": "PERFORMANCE_CASE_MAPPING_MISMATCH",
                "v2": v2_mapping,
                "v3": v3_mapping,
            })
        if v3_provenance.get("review_receipt_sha256") and v3_provenance.get("receipt_fresh") is not True:
            findings.append({
                "code": "PERFORMANCE_CASE_RECEIPT_STALE",
                "binding_diagnostics": _receipt_binding_diagnostics(
                    commands["v3"],
                    source_sha256=v3_provenance.get("source_sha256"),
                    decision_sha256=v3_provenance.get("decision_sha256"),
                ),
            })
    report_relative = str(case.get("report_relative") or "")
    report_path = Path(report_relative)
    if not report_relative or report_path.is_absolute() or ".." in report_path.parts:
        findings.append({"code": "PERFORMANCE_CASE_REPORT_PATH_INVALID", "path": report_relative})
    return findings


def _provenance_from_case(case: dict[str, Any]) -> dict[str, Any]:
    command = case.get("v3_command") if isinstance(case.get("v3_command"), list) else []
    return _input_provenance(command)


def benchmark_preflight(
    case: dict[str, Any],
    *,
    v2_runs: int = 3,
    v3_cold_runs: int = 3,
    v3_warm_runs: int = 5,
    dependency_bindings_path: Path | None = None,
    rebound_case_output: Path | None = None,
) -> dict[str, Any]:
    """Validate a benchmark case without launching a child process.

    This is an input/readiness gate only.  A PASS here means the literal case
    paths and immutable receipt bindings are internally consistent; it never
    counts samples, proves Office identity, or claims performance.
    """
    dependency_bindings_fact: dict[str, Any] | None = None
    if dependency_bindings_path is not None:
        try:
            dependency_bindings = load_dependency_bindings(dependency_bindings_path)
        except (OSError, ValueError) as exc:
            return dependency_bindings_load_blocked_result(
                dependency_bindings_path,
                "benchmark_preflight_v3",
                exc,
                case_id=case.get("case_id"),
                process_launch_performed=False,
                office_processes_started=False,
                formal_performance_pass_claim=False,
            )
        dependency_bindings_fact = dependency_bindings_reference(dependency_bindings_path)
        missing = missing_dependency_findings(dependency_bindings)
        if missing:
            return result(
                "BLOCKED",
                "benchmark_preflight_v3",
                findings=missing,
                case_id=case.get("case_id"),
                dependency_bindings=dependency_bindings_fact,
                process_launch_performed=False,
                office_processes_started=False,
                formal_performance_pass_claim=False,
            )
        if rebound_case_output is not None:
            case = rebind_runtime_spec_payload(
                case,
                rebound_case_output,
                dependency_bindings,
                replacements=[
                    {"field": "v2_command[0]", "dependency": "python"},
                    {"field": "v3_command[0]", "dependency": "python"},
                    {"field": "v3_command.--python", "dependency": "python"},
                ],
            )
        else:
            python_path = str(resolve_dependency(dependency_bindings, "python"))
            case = json.loads(json.dumps(case, ensure_ascii=False))
            for key in ("v2_command", "v3_command"):
                if isinstance(case.get(key), list) and case[key]:
                    case[key][0] = python_path
            if isinstance(case.get("v3_command"), list):
                command = case["v3_command"]
                if "--python" in command and command.index("--python") + 1 < len(command):
                    command[command.index("--python") + 1] = python_path
                else:
                    command.extend(["--python", python_path])
        if isinstance(case.get("v3_command"), list):
            command = case["v3_command"]
            bindings_text = str(dependency_bindings_path.resolve())
            if "--dependency-bindings" in command and command.index("--dependency-bindings") + 1 < len(command):
                command[command.index("--dependency-bindings") + 1] = bindings_text
            else:
                command.extend(["--dependency-bindings", bindings_text])
            if rebound_case_output is not None:
                write_json(rebound_case_output, case)

    findings = _contract_findings(case)
    minimums = {"v2": 3, "v3_cold": 3, "v3_warm": 5}
    configured = {"v2": v2_runs, "v3_cold": v3_cold_runs, "v3_warm": v3_warm_runs}
    for group, minimum in minimums.items():
        if configured[group] < minimum:
            findings.append({
                "code": "PERFORMANCE_REPEAT_COUNT_TOO_LOW",
                "group": group,
                "minimum": minimum,
                "actual": configured[group],
            })
    finding_codes = {str(item.get("code")) for item in findings}
    if not findings:
        preflight_status = "PASS"
    elif finding_codes == {"PERFORMANCE_CASE_RECEIPT_STALE"}:
        # An expired hash-bound approval is a human-review gate, not a broken
        # benchmark contract.  It remains fail-closed and launches no process.
        preflight_status = "HUMAN_REVIEW"
    elif finding_codes and finding_codes <= {
        "PERFORMANCE_CASE_CWD_INVALID",
        "PERFORMANCE_CASE_EXECUTABLE_INVALID",
        "PERFORMANCE_CASE_SOURCE_INVALID",
        "PERFORMANCE_CASE_DECISION_INVALID",
        "PERFORMANCE_CASE_RECEIPT_INVALID",
        "PERFORMANCE_CASE_CONTENT_MAP_INVALID",
    }:
        preflight_status = "BLOCKED"
    else:
        preflight_status = "FAIL"
    payload = result(
        preflight_status,
        "benchmark_preflight_v3",
        findings=findings,
        case_id=case.get("case_id"),
        configured_counts=configured,
        required_counts=minimums,
        expected_provenance=_provenance_from_case(case),
        process_launch_performed=False,
        office_processes_started=False,
        formal_performance_pass_claim=False,
    )
    if dependency_bindings_fact is not None:
        payload["dependency_bindings"] = dependency_bindings_fact
    if rebound_case_output is not None:
        payload["rebound_case"] = str(rebound_case_output.resolve())
    return payload


def evaluate_benchmark(
    v2_samples: list[dict[str, Any]],
    v3_cold_samples: list[dict[str, Any]],
    v3_warm_samples: list[dict[str, Any]],
    expected_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    groups = {"v2": v2_samples, "v3_cold": v3_cold_samples, "v3_warm": v3_warm_samples}
    minimum_counts = {"v2": 3, "v3_cold": 3, "v3_warm": 5}
    valid_durations: dict[str, list[float]] = {name: [] for name in groups}
    for group_name, samples in groups.items():
        if not samples:
            findings.append({"code": "PERFORMANCE_SAMPLE_GROUP_EMPTY", "group": group_name})
            continue
        if len(samples) < minimum_counts[group_name]:
            findings.append(
                {
                    "code": "PERFORMANCE_SAMPLE_COUNT_TOO_LOW",
                    "group": group_name,
                    "minimum": minimum_counts[group_name],
                    "actual": len(samples),
                }
            )
        for index, sample in enumerate(samples, 1):
            raw_duration = sample.get("duration_ms")
            try:
                duration = float(raw_duration)
            except (TypeError, ValueError):
                duration = None
            if isinstance(raw_duration, bool) or duration is None or not math.isfinite(duration) or duration < 0:
                actual = raw_duration
                if isinstance(raw_duration, float) and not math.isfinite(raw_duration):
                    actual = "NaN" if math.isnan(raw_duration) else ("Infinity" if raw_duration > 0 else "-Infinity")
                findings.append({
                    "code": "PERFORMANCE_SAMPLE_DURATION_INVALID",
                    "group": group_name,
                    "sample": index,
                    "actual": actual,
                })
            else:
                valid_durations[group_name].append(duration)
            if sample.get("returncode") not in (None, 0):
                findings.append({"code": "PERFORMANCE_SAMPLE_PROCESS_FAILED", "group": group_name, "sample": index, "returncode": sample.get("returncode")})
            signature = sample.get("quality_signature", {})
            if signature.get("status") != "PASS" or signature.get("deliverable") is not True:
                findings.append({"code": "PERFORMANCE_SAMPLE_QUALITY_FAILED", "group": group_name, "sample": index, "quality_signature": signature})
            environment = sample.get("environment_signature", {})
            if not _environment_complete(environment):
                findings.append({"code": "PERFORMANCE_SAMPLE_ENVIRONMENT_INCOMPLETE", "group": group_name, "sample": index})
            cache = sample.get("cache_signature", {})
            if group_name == "v3_warm" and (
                cache.get("status") != "HIT" or cache.get("final_delivery_cache_reused") is not True
            ):
                findings.append({"code": "PERFORMANCE_WARM_CACHE_HIT_NOT_PROVEN", "group": group_name, "sample": index, "cache_signature": cache})
            if group_name == "v3_cold" and cache.get("status") == "HIT":
                findings.append({"code": "PERFORMANCE_COLD_SAMPLE_USED_CACHE", "group": group_name, "sample": index, "cache_signature": cache})
            provenance = sample.get("provenance_signature", {})
            if expected_provenance is not None:
                expected = (
                    {key: expected_provenance.get(key) for key in ("source_sha256", "mapping_contract_sha256")}
                    if group_name == "v2"
                    else expected_provenance
                )
                actual = {key: provenance.get(key) for key in expected}
                if actual != expected:
                    findings.append({"code": "PERFORMANCE_SAMPLE_PROVENANCE_MISMATCH", "group": group_name, "sample": index, "expected": expected_provenance, "actual": provenance})
                if group_name != "v2" and not provenance.get("receipt_fresh"):
                    findings.append({"code": "PERFORMANCE_SAMPLE_RECEIPT_STALE", "group": group_name, "sample": index})

    reference = v2_samples[0].get("quality_signature") if v2_samples else None
    if reference:
        for group_name, samples in groups.items():
            for index, sample in enumerate(samples, 1):
                if sample.get("quality_signature") != reference:
                    findings.append({
                        "code": "PERFORMANCE_SAMPLE_QUALITY_MISMATCH",
                        "group": group_name,
                        "sample": index,
                        "expected": reference,
                        "actual": sample.get("quality_signature"),
                    })

    environment_reference = v2_samples[0].get("environment_signature") if v2_samples else None
    if environment_reference:
        for group_name, samples in groups.items():
            for index, sample in enumerate(samples, 1):
                if sample.get("environment_signature") != environment_reference:
                    findings.append({
                        "code": "PERFORMANCE_SAMPLE_ENVIRONMENT_MISMATCH",
                        "group": group_name,
                        "sample": index,
                        "expected": environment_reference,
                        "actual": sample.get("environment_signature"),
                    })

    medians = {name: statistics.median(durations) for name, durations in valid_durations.items() if durations}
    if {"v2", "v3_cold"}.issubset(medians):
        ratio = medians["v3_cold"] / medians["v2"] if medians["v2"] else float("inf")
        if ratio > 0.70:
            findings.append({"code": "V3_COLD_PERFORMANCE_TARGET_UNMET", "actual_ratio": round(ratio, 6), "maximum": 0.70})
    if {"v2", "v3_warm"}.issubset(medians):
        ratio = medians["v3_warm"] / medians["v2"] if medians["v2"] else float("inf")
        if ratio > 0.40:
            findings.append({"code": "V3_WARM_PERFORMANCE_TARGET_UNMET", "actual_ratio": round(ratio, 6), "maximum": 0.40})
    return result(
        "PASS" if not findings else "FAIL",
        "benchmark_v3",
        findings=findings,
        medians_ms=medians,
        sample_counts={name: len(samples) for name, samples in groups.items()},
        thresholds={"cold_v3_over_v2": 0.70, "warm_v3_over_v2": 0.40},
    )


def _expanded(command: list[str], **values: str) -> list[str]:
    return [str(value).format(**values) for value in command]


def _run_sample(command: list[str], report_path: Path, cwd: Path, expected_provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        report_path.unlink(missing_ok=True)
    except OSError as exc:
        return {
            "duration_ms": 0.0,
            "returncode": None,
            "quality_signature": {"status": "BLOCKED", "deliverable": False},
            "environment_signature": {},
            "cache_signature": {},
            "finding": f"stale report removal failed: {exc}",
        }
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=3600,
    )
    duration_ms = round((time.perf_counter() - started) * 1000, 3)
    if not report_path.is_file():
        return {
            "duration_ms": duration_ms,
            "returncode": completed.returncode,
            "quality_signature": {"status": "BLOCKED", "deliverable": False},
            "environment_signature": {},
            "cache_signature": {},
            "finding": "report missing",
        }
    report = read_json(report_path)
    observed = _input_provenance(command)
    reported = provenance_signature(report)
    for key, value in reported.items():
        if value is not None and key in observed and observed.get(key) is not None and value != observed[key]:
            observed["report_provenance_mismatch"] = True
    if observed.get("receipt_fresh") is not None:
        observed["receipt_fresh"] = bool(observed["receipt_fresh"]) and not any(
            "STALE" in str(item.get("code", "")).upper()
            for item in report.get("findings", [])
            if isinstance(item, dict)
        )
    return {
        "duration_ms": duration_ms,
        "returncode": completed.returncode,
        "report": str(report_path),
        "report_sha256": sha256_file(report_path),
        "quality_signature": quality_signature(report),
        "environment_signature": environment_signature(report),
        "cache_signature": cache_signature(report),
        "provenance_signature": observed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="同机比较 V2 与 V3 冷/热路径机器耗时，并强制质量签名一致")
    parser.add_argument("--case", type=Path, required=True, help="包含 v2_command、v3_command、report_relative 的 JSON")
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--v2-runs", type=int, default=3)
    parser.add_argument("--v3-cold-runs", type=int, default=3)
    parser.add_argument("--v3-warm-runs", type=int, default=5)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--preflight-only", action="store_true", help="仅验证 case 路径和回执绑定，不启动 V2/V3 子进程")
    parser.add_argument("--dependency-bindings", type=Path, help="DependencyBindingsV3 JSON discovered once and applied to benchmark commands")
    parser.add_argument("--rebound-case-output", type=Path, help="写入白名单重绑定后的 benchmark case 副本")
    args = parser.parse_args()
    case = read_json(args.case)
    if args.dependency_bindings and not args.preflight_only:
        rebound_output = args.rebound_case_output or (args.case.resolve().parent / f"{args.case.stem}.runtime-bound.json")
        rebound_preflight = benchmark_preflight(
            case,
            v2_runs=args.v2_runs,
            v3_cold_runs=args.v3_cold_runs,
            v3_warm_runs=args.v3_warm_runs,
            dependency_bindings_path=args.dependency_bindings.resolve(),
            rebound_case_output=rebound_output.resolve(),
        )
        if rebound_preflight["status"] in {"FAIL", "BLOCKED", "HUMAN_REVIEW"}:
            return finish(rebound_preflight, args.report)
        case = read_json(rebound_output.resolve())
    if args.preflight_only:
        return finish(benchmark_preflight(
            case,
            v2_runs=args.v2_runs,
            v3_cold_runs=args.v3_cold_runs,
            v3_warm_runs=args.v3_warm_runs,
            dependency_bindings_path=args.dependency_bindings.resolve() if args.dependency_bindings else None,
            rebound_case_output=args.rebound_case_output.resolve() if args.rebound_case_output else None,
        ), args.report)
    if args.v2_runs < 3 or args.v3_cold_runs < 3 or args.v3_warm_runs < 5:
        return finish(result("FAIL", "benchmark_v3", findings=[{"code": "PERFORMANCE_REPEAT_COUNT_TOO_LOW"}]), args.report)
    preflight_findings = _contract_findings(case)
    if args.report:
        report_parent = args.report.expanduser().resolve().parent
        if not report_parent.is_dir():
            preflight_findings.append({"code": "PERFORMANCE_REPORT_PARENT_INVALID", "path": str(report_parent)})
        elif args.report.exists() and not args.report.is_file():
            preflight_findings.append({"code": "PERFORMANCE_REPORT_PATH_INVALID", "path": str(args.report)})
    temporary = tempfile.TemporaryDirectory() if args.work_root is None else None
    root = args.work_root.resolve() if args.work_root else Path(temporary.name)
    if args.work_root and root.exists() and not root.is_dir():
        preflight_findings.append({"code": "PERFORMANCE_WORK_ROOT_INVALID", "path": str(root)})
    elif args.work_root and root.exists() and any(root.iterdir()):
        preflight_findings.append({"code": "PERFORMANCE_WORK_ROOT_NOT_EMPTY", "path": str(root)})
    if preflight_findings:
        return finish(
            result(
                "FAIL",
                "benchmark_v3",
                findings=preflight_findings,
            ),
            args.report,
        )
    root.mkdir(parents=True, exist_ok=True)
    cwd = Path(case.get("cwd") or args.case.parent).resolve()
    report_relative = str(case.get("report_relative") or "reports/pipeline_report.json")
    expected_provenance = _provenance_from_case(case)
    samples: dict[str, list[dict[str, Any]]] = {"v2": [], "v3_cold": [], "v3_warm": []}
    try:
        for group, runs, command_key in (
            ("v2", args.v2_runs, "v2_command"),
            ("v3_cold", args.v3_cold_runs, "v3_command"),
        ):
            for index in range(1, runs + 1):
                run_dir = root / group / f"run-{index}"
                cache_dir = root / group / f"cache-{index}"
                run_dir.mkdir(parents=True, exist_ok=True)
                command = _expanded(case[command_key], run_dir=str(run_dir), cache_dir=str(cache_dir))
                samples[group].append(_run_sample(command, run_dir / report_relative, cwd, expected_provenance))

        warm_cache = root / "v3_warm" / "cache"
        prime_dir = root / "v3_warm" / "prime"
        prime_dir.mkdir(parents=True, exist_ok=True)
        prime = _expanded(case["v3_command"], run_dir=str(prime_dir), cache_dir=str(warm_cache))
        prime_sample = _run_sample(prime, prime_dir / report_relative, cwd, expected_provenance)
        for index in range(1, args.v3_warm_runs + 1):
            run_dir = root / "v3_warm" / f"run-{index}"
            run_dir.mkdir(parents=True, exist_ok=True)
            command = _expanded(case["v3_command"], run_dir=str(run_dir), cache_dir=str(warm_cache))
            samples["v3_warm"].append(_run_sample(command, run_dir / report_relative, cwd, expected_provenance))

        payload = evaluate_benchmark(samples["v2"], samples["v3_cold"], samples["v3_warm"], expected_provenance)
        if (
            prime_sample.get("returncode") != 0
            or prime_sample.get("quality_signature", {}).get("status") != "PASS"
            or prime_sample.get("quality_signature", {}).get("deliverable") is not True
        ):
            payload["status"] = "FAIL"
            payload.setdefault("findings", []).append(
                {
                    "code": "PERFORMANCE_WARM_PRIME_FAILED",
                    "returncode": prime_sample.get("returncode"),
                    "quality_signature": prime_sample.get("quality_signature"),
                }
            )
        payload["case_id"] = case.get("case_id")
        payload["samples"] = samples
        payload["warm_prime_sample"] = prime_sample
        payload["benchmark_runtime"] = {
            "python": sys.executable,
            "python_version": list(sys.version_info[:3]),
            "machine": platform.node(),
            "platform": platform.platform(),
        }
        if args.dependency_bindings:
            payload["dependency_bindings"] = dependency_bindings_reference(args.dependency_bindings.resolve())
        if args.report:
            write_json(args.report, payload)
        return finish(payload)
    finally:
        if temporary is not None:
            temporary.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
