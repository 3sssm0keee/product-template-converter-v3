from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from cache_v3 import ContentAddressedCache, cache_key, default_cache_root
from content_plan_v3 import build_content_plan_v3, legacy_migrated_decision_matches_source_document
from decision_bundle_v3 import (
    DecisionBundleError,
    build_deterministic_content_decision,
    build_decision_task_bundles,
    canonical_sha256,
    migrate_content_map_v1,
    normalize_deterministic_candidates,
    source_document_sha256,
    validate_content_decision,
    write_decision_task_bundles,
)
from dependency_bindings_v3 import (
    dependency_bindings_reference,
    load_dependency_bindings,
    missing_dependency_findings,
    resolve_dependency,
)
from delivery_contract import final_path, get_profile, intermediate_path, select_primary_pdf, target_values
from format_probe import probe_format
from pipeline_common import read_json, result, sha256_file, write_json
from powershell_host_v3 import (
    POWERSHELL_HOST_ENV,
    WINRT_OCR_POWERSHELL_HOST_ENV,
    PowerShellHostError,
    probe_powershell_host,
    resolve_powershell_host,
)
from render_plan_v3 import build_render_plan_v3
from review_preview_assets_v3 import materialize_preview_images
from review_preview_v3 import build_content_document_preview
from review_v3 import HIDDEN_REVIEWER_BINDING_FIELD, ReviewError, build_review_queue, rebind_volatile_review_receipt, validate_review_receipt, write_review_html
from source_document_v3 import SourceDocumentError, build_source_document_v3
from telemetry_v3 import StageTelemetryRecorder
from template_catalog_v3 import resolve_template
from template_onboard import create_onboarding_draft
from validator_runner_v3 import ValidatorJob, run_validators_parallel


PASS_CACHE_STAGE = "final_artifact"
PASS_CACHE_EVIDENCE_MEMBER = "evidence/pass-cache-evidence-v3.json"
PASS_CACHE_REQUIRED_STAGES = {
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
}
CONTENT_REVIEW_RESOLVABLE_DECISION_CODES = {
    "DECISION_LOW_CONFIDENCE",
    "IDENTITY_REVIEW_REQUIRED",
}
ACTIVE_DEPENDENCY_BINDINGS_FACT: dict[str, Any] | None = None


def _hidden_reviewer_from_receipt(receipt: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(receipt, dict):
        return None
    binding = receipt.get("binding")
    if not isinstance(binding, dict) or not str(binding.get(HIDDEN_REVIEWER_BINDING_FIELD) or "").strip():
        return None
    reviewer = receipt.get("reviewer")
    if not isinstance(reviewer, dict):
        return None
    reviewer_id = str(reviewer.get("id") or "").strip()
    reviewer_role = str(reviewer.get("role") or "").strip()
    if not reviewer_id or not reviewer_role:
        return None
    return {"reviewer_id": reviewer_id, "reviewer_role": reviewer_role}


def _receipt_evidence_sha(receipt: dict[str, Any] | None, evidence_id: str) -> str | None:
    if not isinstance(receipt, dict):
        return None
    values = [
        str(value.get("sha256") or "").strip().upper()
        for value in receipt.get("evidence_bindings", [])
        if isinstance(value, dict) and value.get("evidence_id") == evidence_id
    ]
    values = [value for value in values if re.fullmatch(r"[0-9A-F]{64}", value)]
    return values[0] if len(values) == 1 else None


def _decision_requires_content_review(report: dict[str, Any]) -> bool:
    """Return true only for validation findings the human queue can resolve."""

    if report.get("status") != "HUMAN_REVIEW":
        return False
    findings = report.get("findings")
    if not isinstance(findings, list) or not findings:
        return False
    codes = {
        str(value.get("code") or "")
        for value in findings
        if isinstance(value, dict)
    }
    return bool(codes) and codes <= CONTENT_REVIEW_RESOLVABLE_DECISION_CODES


def resolve_powershell(explicit: str | os.PathLike[str] | None = None) -> str:
    return str(resolve_powershell_host(explicit).path)


def execute(command: list[str], report: Path, timeout: int = 600) -> dict:
    report.parent.mkdir(parents=True, exist_ok=True)
    try:
        report.unlink(missing_ok=True)
    except OSError as exc:
        payload = result("BLOCKED", "subprocess", findings=[{
            "code": "STALE_REPORT_REMOVE_FAILED",
            "message": str(exc),
            "report": str(report),
        }])
        write_json(report, payload)
        return payload

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        payload = result("BLOCKED", "subprocess", findings=[{
            "code": "SUBPROCESS_TIMEOUT",
            "timeout_seconds": timeout,
            "stdout": _tail(exc.stdout),
            "stderr": _tail(exc.stderr),
        }])
        write_json(report, payload)
        return payload
    except OSError as exc:
        payload = result("BLOCKED", "subprocess", findings=[{
            "code": "SUBPROCESS_START_FAILED",
            "message": str(exc),
        }])
        write_json(report, payload)
        return payload

    if not report.is_file():
        payload = result("FAIL", "subprocess", findings=[{
            "code": "REPORT_NOT_CREATED",
            "returncode": completed.returncode,
            "stdout": _tail(completed.stdout),
            "stderr": _tail(completed.stderr),
        }])
        write_json(report, payload)
        payload["returncode"] = completed.returncode
        return payload

    try:
        payload = read_json(report)
        if not isinstance(payload, dict) or payload.get("status") not in {"PASS", "FAIL", "HUMAN_REVIEW", "BLOCKED"}:
            raise ValueError("report must be an object with a supported status")
    except Exception as exc:
        payload = result("FAIL", "subprocess", findings=[{
            "code": "REPORT_INVALID",
            "message": str(exc),
            "returncode": completed.returncode,
            "stdout": _tail(completed.stdout),
            "stderr": _tail(completed.stderr),
        }])
        write_json(report, payload)

    expected_codes = {"PASS": {0}, "HUMAN_REVIEW": {3}, "FAIL": {2}, "BLOCKED": {2}}
    status = payload.get("status", "FAIL")
    if completed.returncode not in expected_codes.get(status, set()):
        payload = result("FAIL", "subprocess", findings=[{
            "code": "SUBPROCESS_STATUS_EXIT_MISMATCH",
            "reported_status": status,
            "returncode": completed.returncode,
            "stdout": _tail(completed.stdout),
            "stderr": _tail(completed.stderr),
        }])
        write_json(report, payload)
    payload["returncode"] = completed.returncode
    return payload


def _tail(value: object, limit: int = 2000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value)[-limit:]


def _write_report_if_possible(report: Path | None, payload: dict) -> None:
    if report is None or not report.parent.exists():
        return
    try:
        write_json(report, payload)
    except OSError:
        pass


def _access_payload(completed: subprocess.CompletedProcess[str] | None) -> dict:
    if completed is None:
        return {"status": "STATE_INVALID", "message": "授权检查未返回有效结果。"}
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError, TypeError):
        return {"status": "STATE_INVALID", "message": "授权检查未返回有效结果。"}
    return payload if isinstance(payload, dict) else {"status": "STATE_INVALID", "message": "授权检查未返回有效结果。"}


def _run_access_gate(python: str, scripts: Path, password_stdin: bool, state_file: Path | None = None) -> dict:
    command = [python, str(scripts / "access_gate.py"), "check"]
    if state_file is not None:
        command.extend(["--state-file", str(state_file)])

    def invoke(args: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                command + args,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    # A normal check never reads stdin. It also starts a first-run trial without a password.
    first = _access_payload(invoke([]))
    if first.get("status") != "EXPIRED":
        return first
    if not password_stdin:
        return first

    renewal_password = sys.stdin.readline().rstrip("\r\n")
    second = invoke(["--password-stdin"], renewal_password + "\n")
    return _access_payload(second)


def clean_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]', "-", value.strip())
    return re.sub(r"\s+", " ", value).rstrip(". ")


def stop_status(stages: list[dict]) -> str:
    statuses = [stage.get("status") for stage in stages]
    if "BLOCKED" in statuses:
        return "BLOCKED"
    if "FAIL" in statuses:
        return "FAIL"
    if "HUMAN_REVIEW" in statuses:
        return "HUMAN_REVIEW"
    if not statuses or any(status != "PASS" for status in statuses):
        return "FAIL"
    return "PASS"


def exit_code(status: str) -> int:
    return 0 if status == "PASS" else 3 if status == "HUMAN_REVIEW" else 2


def emit_json(payload: dict) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def final_delivery_summary(stages: list[dict], delivery_profile, deliverable_path: Path) -> dict:
    final_status = stop_status(stages)
    return {
        "status": final_status,
        "deliverable": final_status == "PASS",
        "delivery_target": delivery_profile.target.value,
        "delivery_format": delivery_profile.delivery_format,
        "deliverable_path": str(deliverable_path),
    }


def _finish_run(
    *,
    report_path: Path,
    telemetry_path: Path,
    telemetry: StageTelemetryRecorder,
    stages: list[dict[str, Any]],
    status: str | None = None,
    findings: list[dict[str, Any]] | None = None,
    deliverable: bool = False,
    **facts: Any,
) -> int:
    telemetry_payload = telemetry.report()
    write_json(telemetry_path, telemetry_payload)
    resolved_status = status or stop_status(stages)
    if "powershell_host" not in facts:
        try:
            facts["powershell_host"] = resolve_powershell_host().report()
        except PowerShellHostError:
            pass
    resolved_findings = findings if findings is not None else [
        finding
        for stage in stages
        for finding in stage.get("findings", [])
        if isinstance(finding, dict)
    ]
    if ACTIVE_DEPENDENCY_BINDINGS_FACT is not None and "dependency_bindings" not in facts:
        facts["dependency_bindings"] = ACTIVE_DEPENDENCY_BINDINGS_FACT
    payload = result(
        resolved_status,
        "run_pipeline",
        findings=resolved_findings,
        stages=stages,
        telemetry=str(telemetry_path),
        telemetry_sha256=sha256_file(telemetry_path),
        deliverable=bool(deliverable and resolved_status == "PASS"),
        **facts,
    )
    write_json(report_path, payload)
    emit_json(payload)
    return exit_code(resolved_status)


def _mark_human_review_resolved(stage: dict[str, Any], **resolution: Any) -> None:
    """Close a review stage while retaining its findings as resolved audit evidence."""
    if stage.get("status") != "HUMAN_REVIEW":
        stage.update(resolution)
        return
    open_findings = [
        finding
        for finding in stage.get("findings", [])
        if isinstance(finding, dict)
    ]
    prior_resolved = [
        finding
        for finding in stage.get("resolved_findings", [])
        if isinstance(finding, dict)
    ]
    stage["resolved_findings"] = prior_resolved + open_findings
    stage["findings"] = []
    stage["status"] = "PASS"
    stage.update(resolution)


def _validate_or_rebind_content_review(
    review_queue: dict[str, Any],
    receipt: dict[str, Any] | None,
    receipt_path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    review_stage = validate_review_receipt(review_queue, receipt)
    if review_stage.get("status") == "PASS" or receipt is None or receipt_path is None:
        return review_stage, receipt, None
    stale_codes = {
        str(finding.get("code") or "")
        for finding in review_stage.get("findings", [])
        if isinstance(finding, dict)
    }
    if "REVIEW_RECEIPT_STALE" not in stale_codes:
        return review_stage, receipt, None

    prior_queue_path = receipt_path.resolve().parent / "content_review_queue_v3.json"
    if not prior_queue_path.is_file():
        return review_stage, receipt, result(
            "HUMAN_REVIEW",
            "review_receipt_volatile_rebind_v3",
            findings=[{"code": "PRIOR_REVIEW_QUEUE_MISSING", "path": str(prior_queue_path)}],
        )
    try:
        prior_queue = read_json(prior_queue_path)
        prior_validation = validate_review_receipt(prior_queue, receipt)
        rebound, rebind_report = rebind_volatile_review_receipt(
            review_queue,
            receipt,
            prior_validation,
            prior_queue=prior_queue,
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return review_stage, receipt, result(
            "FAIL",
            "review_receipt_volatile_rebind_v3",
            findings=[{"code": "REVIEW_REBIND_FAILED", "message": str(exc)}],
        )
    if rebound is None or rebind_report.get("status") != "PASS":
        return review_stage, receipt, rebind_report
    rebound_stage = validate_review_receipt(review_queue, rebound)
    if rebound_stage.get("status") != "PASS":
        return review_stage, receipt, result(
            "HUMAN_REVIEW",
            "review_receipt_volatile_rebind_v3",
            findings=[{
                "code": "REVIEW_REBIND_RECEIPT_NOT_APPLICABLE",
                "validation": rebound_stage,
            }],
        )
    return rebound_stage, rebound, rebind_report


def _source_items(source_document: dict[str, Any]) -> list[dict[str, Any]]:
    content = source_document.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("items"), list):
        return []
    return [item for item in content["items"] if isinstance(item, dict)]


def _source_text(item: dict[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
    if payload.get("type") == "table" and isinstance(payload.get("rows"), list):
        return "\n".join(
            "\t".join(str(cell) for cell in row)
            for row in payload["rows"]
            if isinstance(row, list)
        )
    return str(payload.get("text") or payload.get("description") or "")


def _allowed_slots(template_pack: dict[str, Any]) -> list[str]:
    slots_path = Path(str(template_pack.get("slot_contract_path") or ""))
    slot_ids: list[str] = []
    if slots_path.is_file():
        slots = read_json(slots_path)
        slot_ids.extend(
            str(item["slot_id"])
            for item in slots.get("slots", [])
            if isinstance(item, dict) and item.get("slot_id")
        )
    invariants = template_pack.get("invariants") if isinstance(template_pack.get("invariants"), dict) else {}
    slot_ids.extend(str(value) for value in invariants.get("chapter_names", []) if str(value))
    return list(dict.fromkeys(slot_ids))


def _hydrate_template_pack(template_pack: dict[str, Any]) -> dict[str, Any]:
    pack = dict(template_pack)
    for source_key, target_key in (
        ("validators_path", "validators"),
        ("slot_contract_path", "slot_contract_payload"),
        ("template_ir_path", "template_ir_payload"),
        ("program_path", "program_payload"),
    ):
        path = Path(str(pack.get(source_key) or ""))
        if path.is_file():
            pack[target_key] = read_json(path)
    pack_dir = Path(str(pack.get("program_path") or "")).resolve().parent
    config_path = pack_dir / "template.json"
    if config_path.is_file():
        pack["pack_dir"] = str(pack_dir)
        pack["template_config_path"] = str(config_path)
        pack["template_config_sha256"] = sha256_file(config_path)
    return pack


def _content_review_items(
    source_document: dict[str, Any],
    decision: dict[str, Any],
) -> list[dict[str, Any]]:
    source_items = _source_items(source_document)
    by_id = {str(item.get("source_id") or ""): item for item in source_items}
    items: list[dict[str, Any]] = []
    review_reasons = source_document.get("review", {}).get("reasons", []) if isinstance(source_document.get("review"), dict) else []
    if review_reasons:
        items.append({
            "review_item_id": "CONTENT-SOURCE-FEATURES",
            "category": "CONTENT",
            "title": "确认源件中无法完全确定的结构特性",
            "conclusion": "SourceDocumentV3 标记了需要人工确认的格式专属对象。",
            "risk_level": "HIGH",
            "source_ids": [],
            "evidence_ids": ["SOURCE-DOCUMENT"],
            "details": {"recognized_conclusion": ", ".join(str(value) for value in review_reasons)},
        })

    sensitive_actions = {
        str(value.get("source_id"))
        for value in decision.get("decisions", [])
        if isinstance(value, dict)
        and value.get("action") in {"reviewed_text", "preserve_sanitized_image", "redact_identity", "remove_identity"}
    }
    if sensitive_actions:
        items.append({
            "review_item_id": "CONTENT-SENSITIVE-TRANSFORM",
            "category": "CONTENT",
            "title": "确认人工修订、图片脱敏和身份处理",
            "conclusion": "这些项目改变了源内容或涉及厂家身份，不能只依赖模型置信度。",
            "risk_level": "CRITICAL",
            "source_ids": sorted(sensitive_actions),
            "evidence_ids": ["SOURCE-DOCUMENT", "CONTENT-DECISION"],
            "details": {"recognized_conclusion": "需逐项核对变更、证据和目标槽位。"},
        })

    low_confidence_ids = sorted({
        str(value.get("source_id") or "")
        for value in decision.get("decisions", [])
        if isinstance(value, dict)
        and isinstance(value.get("confidence"), (int, float))
        and float(value["confidence"]) < 0.8
        and str(value.get("source_id") or "")
    })
    if low_confidence_ids:
        items.append({
            "review_item_id": "CONTENT-LOW-CONFIDENCE",
            "category": "CONTENT",
            "title": "核对低置信内容、图片及目标章节",
            "conclusion": "这些项目只能作为待确认建议，必须对照原文件后由人眼批准或修订。",
            "risk_level": "HIGH",
            "source_ids": low_confidence_ids,
            "evidence_ids": ["SOURCE-DOCUMENT", "CONTENT-DECISION", "DECISION-VALIDATION"],
            "details": {"recognized_conclusion": "模型置信度只用于排序，不构成事实或版式批准。"},
        })

    image_action_ids = sorted({
        str(value.get("source_id") or "")
        for value in decision.get("decisions", [])
        if isinstance(value, dict)
        and value.get("action") in {"preserve_image", "preserve_sanitized_image"}
        and str(value.get("source_id") or "") in by_id
        and (
            by_id[str(value.get("source_id") or "")].get("kind") == "image"
            or (
                isinstance(by_id[str(value.get("source_id") or "")].get("payload"), dict)
                and by_id[str(value.get("source_id") or "")]["payload"].get("type") == "asset"
            )
        )
    })
    if image_action_ids:
        items.append({
            "review_item_id": "CONTENT-IMAGE-ASSETS",
            "category": "CONTENT",
            "title": "核对进入成品的图片缩略图",
            "conclusion": "这些图片会进入拟生成 DOCX，必须逐张核对缩略图、来源和目标章节。",
            "risk_level": "HIGH",
            "source_ids": image_action_ids,
            "evidence_ids": ["SOURCE-DOCUMENT", "CONTENT-DECISION", "DOCUMENT-PREVIEW"],
            "details": {"recognized_conclusion": "请逐张确认图片是否应保留、是否需要脱敏，以及是否放入正确章节。"},
        })

    fact_ids: list[str] = []
    for source_id, item in by_id.items():
        text = _source_text(item)
        context = json.dumps(
            {"context": item.get("context", {}), "attributes": item.get("attributes", {})},
            ensure_ascii=False,
        )
        is_ocr = "ocr" in (source_id + " " + context).lower()
        is_sensitive_fact = bool(
            re.search(r"(?:[A-Za-z]*\d[\w.-]*|\d+(?:\.\d+)?\s*(?:mm|cm|m|kg|g|V|W|Hz|MPa|℃|%))", text, re.IGNORECASE)
            or re.search(r"认证|资质|证书|专利|检测|检验|许可", text)
        )
        if is_ocr or is_sensitive_fact:
            fact_ids.append(source_id)
    if fact_ids:
        items.append({
            "review_item_id": "CONTENT-OCR-FACTS",
            "category": "FACT",
            "title": "核对 OCR、型号、数字、单位与资质事实",
            "conclusion": "代码检测到必须由人眼核对的事实型内容。",
            "risk_level": "CRITICAL",
            "source_ids": sorted(set(fact_ids)),
            "evidence_ids": ["SOURCE-DOCUMENT", "CONTENT-DECISION"],
            "details": {"recognized_conclusion": "不得新增无证据事实；OCR 文本必须与原页对照。"},
        })

    identity = decision.get("identity_review") if isinstance(decision.get("identity_review"), dict) else {}
    items.append({
        "review_item_id": "CONTENT-IDENTITY-REVIEW",
        "category": "IDENTITY",
        "title": "确认厂家身份分类与零命中结论",
        "conclusion": "无论是否发现厂家身份，结论都必须由人眼复核，静态与双引擎扫描仍会在下游执行。",
        "risk_level": "CRITICAL",
        "source_ids": sorted(
            str(value.get("source_id"))
            for value in decision.get("decisions", [])
            if isinstance(value, dict) and value.get("action") in {"remove_identity", "redact_identity", "preserve_sanitized_image"}
        ),
        "evidence_ids": ["SOURCE-DOCUMENT", "CONTENT-DECISION", "DECISION-VALIDATION"],
        "details": {
            "recognized_conclusion": str(identity.get("status") or "REVIEW_REQUIRED"),
            "notes": str(identity.get("notes") or ""),
        },
    })
    return items


def _content_review_binding(
    source_document: dict[str, Any],
    decision_path: Path,
    decision_validation_path: Path,
    template_pack: dict[str, Any],
    compiler_path: Path,
) -> dict[str, str]:
    template_ir = template_pack.get("template_ir_payload") if isinstance(template_pack.get("template_ir_payload"), dict) else {}
    program = template_pack.get("program_payload") if isinstance(template_pack.get("program_payload"), dict) else {}
    # TemplateResolver 对外只返回轻量 pack，主流水线随后会 hydrate；独立的
    # HTML 复核入口则直接使用轻量 pack。两条入口必须绑定到制品内声明的
    # 逻辑哈希，不能一条使用 ir_sha256/program_sha256，另一条退化为 JSON
    # 文件字节哈希，否则同一内容会产生不同 review_id 并误判回执过期。
    if not template_ir:
        template_ir = read_json(Path(template_pack["template_ir_path"]))
    if not program:
        program = read_json(Path(template_pack["program_path"]))
    return {
        "source_sha256": str(source_document.get("source", {}).get("sha256") or "").upper(),
        "template_sha256": str(template_pack.get("template_sha256") or "").upper(),
        "template_ir_sha256": str(template_ir.get("ir_sha256") or "").upper(),
        "template_dsl_sha256": str(program.get("program_sha256") or "").upper(),
        "compiler_sha256": sha256_file(compiler_path),
        "candidate_output_sha256": sha256_file(decision_path),
        "diff_report_sha256": sha256_file(decision_validation_path),
    }


def _validator_identity_review(content_plan: dict[str, Any]) -> dict[str, Any] | None:
    review = content_plan.get("identity_review")
    if not isinstance(review, dict):
        return None
    adapted = dict(review)
    approval = content_plan.get("review_approval")
    if (
        not isinstance(approval, dict)
        or approval.get("overall_action") != "APPROVED"
        or approval.get("scope") != "ENTIRE_QUEUE"
        or not isinstance(approval.get("reviewer"), dict)
        or not isinstance(approval.get("binding"), dict)
    ):
        raise ValueError("空厂家身份词表缺少完整人工批准，不能作为零命中结论")
    if adapted.get("status") == "REVIEW_REQUIRED":
        # 空词表也必须沿用真实批准的源哈希与人员信息，不能凭空设成零命中。
        adapted["status"] = "VERIFIED" if adapted.get("manufacturer_terms") else "NO_SOURCE_IDENTITY_FOUND"
        adapted["reviewed_at"] = str(approval.get("reviewed_at") or "")
        adapted["scope"] = str(approval.get("scope") or "")
        adapted["reviewer"] = dict(approval["reviewer"])
    if adapted.get("status") != "NO_SOURCE_IDENTITY_FOUND":
        return adapted
    adapted.update({
        "source_sha256": str(approval["binding"].get("source_sha256") or "").upper(),
        "reviewed_at": str(approval.get("reviewed_at") or ""),
        "scope": str(approval.get("scope") or ""),
        "reviewer": dict(approval["reviewer"]),
    })
    if not all((
        adapted["source_sha256"],
        adapted["reviewed_at"],
        adapted["scope"],
        adapted["reviewer"].get("id"),
        adapted["reviewer"].get("role"),
    )):
        raise ValueError("空厂家身份词表的人工批准证据不完整")
    return adapted


def _decision_policy_sha256(skill_root: Path) -> str:
    return canonical_sha256({
        "prompt_sha256": sha256_file(skill_root / "agents" / "prompts" / "content-mapper.system.md"),
        "schema_sha256": sha256_file(skill_root / "references" / "schemas" / "content-decision-v3.schema.json"),
        "policy": "decision-policy-v3",
    })


def _office_binary_identity(preflight: dict[str, Any]) -> dict[str, Any]:
    """Create a read-only identity snapshot for the currently registered engines."""

    office = preflight.get("office") if isinstance(preflight.get("office"), dict) else {}
    commands: set[str] = set()
    servers = office.get("servers") if isinstance(office.get("servers"), dict) else {}
    commands.update(str(value) for value in servers.values() if str(value))
    for key in ("word_machine_candidates",):
        for candidate in office.get(key, []) if isinstance(office.get(key), list) else []:
            if isinstance(candidate, dict) and candidate.get("server"):
                commands.add(str(candidate["server"]))
    for engine in ("word", "wps"):
        engine_facts = office.get(engine) if isinstance(office.get(engine), dict) else {}
        for key in ("server", "effective_server"):
            if engine_facts.get(key):
                commands.add(str(engine_facts[key]))
        for candidate in engine_facts.get("candidates", []) if isinstance(engine_facts.get("candidates"), list) else []:
            if isinstance(candidate, dict) and candidate.get("server"):
                commands.add(str(candidate["server"]))

    binaries: list[dict[str, Any]] = []
    for command in sorted(commands, key=str.casefold):
        value = command.strip()
        if value.startswith('"') and '"' in value[1:]:
            raw_path = value[1:value.find('"', 1)]
        else:
            match = re.search(r"\.exe(?=\s|$)", value, flags=re.IGNORECASE)
            raw_path = value[:match.end()] if match else ""
        path = Path(raw_path)
        identity: dict[str, Any] = {"server": command, "executable": raw_path}
        if raw_path and path.is_file():
            stat_result = path.stat()
            identity.update({
                "bytes": stat_result.st_size,
                "mtime_ns": stat_result.st_mtime_ns,
                "binary_sha256": sha256_file(path),
            })
        binaries.append(identity)
    return {
        "office_registration_sha256": canonical_sha256(office),
        "binaries": binaries,
        "identity_sha256": canonical_sha256({"office": office, "binaries": binaries}),
    }


def _final_cache_semantic_artifact_shas(
    *,
    source_document_path: Path,
    decision_path: Path,
    content_plan_path: Path,
    render_plan_path: Path,
    review_receipt_path: Path,
    normalized: Path,
) -> tuple[str, str, str]:
    """Bind final-cache identity to verified semantic artifacts, not run directories."""

    def read_artifact(path: Path, label: str) -> dict[str, Any]:
        try:
            payload = read_json(path)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ValueError(f"final cache cannot read {label}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"final cache {label} must be a JSON object")
        return payload

    def reference(
        payload: dict[str, Any],
        artifact: str,
        label: str,
        expected_path: Path,
        actual_sha: str | None = None,
    ) -> dict[str, Any]:
        upstream = payload.get("upstream")
        binding = upstream.get(artifact) if isinstance(upstream, dict) else None
        raw_path = str(binding.get("path") or "") if isinstance(binding, dict) else ""
        expected_sha = str(binding.get("sha256") or "").upper() if isinstance(binding, dict) else ""
        try:
            resolved_path = Path(raw_path).resolve()
        except (OSError, ValueError) as exc:
            raise ValueError(f"{label} {artifact} path binding failed") from exc
        if not raw_path or resolved_path != expected_path.resolve():
            raise ValueError(f"{label} {artifact} path binding failed")
        if not re.fullmatch(r"[0-9A-F]{64}", expected_sha) or (actual_sha is not None and expected_sha != actual_sha):
            raise ValueError(f"{label} {artifact} hash binding failed")
        return binding

    source_document = read_artifact(source_document_path, "source_document")
    decision = read_artifact(decision_path, "content_decision")
    content_plan = read_artifact(content_plan_path, "content_plan")
    render_plan = read_artifact(render_plan_path, "render_plan")
    # 仅在原计划自校验通过后消除运行路径，保留编译器的哈希门禁。
    for label, plan in (("content_plan", content_plan), ("render_plan", render_plan)):
        unsigned = {key: value for key, value in plan.items() if key != "canonical_sha256"}
        if str(plan.get("canonical_sha256") or "").upper() != canonical_sha256(unsigned):
            raise ValueError(f"{label} canonical hash binding failed")
    source_document_file_sha = sha256_file(source_document_path)
    decision_file_sha = sha256_file(decision_path)
    receipt_file_sha = sha256_file(review_receipt_path)
    normalized_file_sha = sha256_file(normalized)

    source_document_binding = reference(content_plan, "source_document", "content_plan", source_document_path)
    reference(content_plan, "content_decision", "content_plan", decision_path, decision_file_sha)
    reference(content_plan, "review_receipt", "content_plan", review_receipt_path, receipt_file_sha)
    normalized_source = content_plan.get("normalized_source")
    expected_normalized_sha = str(normalized_source.get("sha256") or "").upper() if isinstance(normalized_source, dict) else ""
    normalized_path = str(normalized_source.get("path") or "") if isinstance(normalized_source, dict) else ""
    if not normalized_path or Path(normalized_path).resolve() != normalized.resolve():
        raise ValueError("content_plan normalized_source path binding failed")
    if not re.fullmatch(r"[0-9A-F]{64}", expected_normalized_sha) or expected_normalized_sha != normalized_file_sha:
        raise ValueError("content_plan normalized_source hash binding failed")

    stable_source_document_sha = source_document_sha256(source_document)
    content_plan_source_sha = str(source_document_binding["sha256"]).upper()
    if content_plan_source_sha == source_document_file_sha:
        content_plan_source_cache_sha = stable_source_document_sha
    elif content_plan_source_sha == stable_source_document_sha:
        content_plan_source_cache_sha = content_plan_source_sha
    elif (
        content_plan_source_sha == str(decision.get("source_document_sha256") or "").upper()
        and legacy_migrated_decision_matches_source_document(source_document, decision)
    ):
        content_plan_source_cache_sha = content_plan_source_sha
    else:
        raise ValueError("content_plan source_document hash binding failed")
    stable_content_plan = copy.deepcopy(content_plan)
    stable_content_plan.pop("canonical_sha256", None)
    stable_content_plan["normalized_source"].pop("path", None)
    for artifact in ("source_document", "content_decision", "review_receipt"):
        stable_content_plan["upstream"][artifact].pop("path", None)
    stable_content_plan["upstream"]["source_document"]["sha256"] = content_plan_source_cache_sha
    stable_content_plan_sha = canonical_sha256(stable_content_plan)

    reference(render_plan, "source_document", "render_plan", source_document_path, source_document_file_sha)
    reference(render_plan, "content_plan", "render_plan", content_plan_path, sha256_file(content_plan_path))
    stable_render_plan = copy.deepcopy(render_plan)
    stable_render_plan.pop("canonical_sha256", None)
    delivery = stable_render_plan.get("delivery")
    if isinstance(delivery, dict):
        delivery.pop("path", None)
    outputs = stable_render_plan.get("outputs")
    if isinstance(outputs, dict):
        outputs.pop("intermediate_docx", None)
        outputs.pop("formal_output", None)
    stable_render_plan["upstream"]["source_document"].pop("path", None)
    stable_render_plan["upstream"]["source_document"]["sha256"] = stable_source_document_sha
    stable_render_plan["upstream"]["content_plan"].pop("path", None)
    stable_render_plan["upstream"]["content_plan"]["sha256"] = stable_content_plan_sha
    return stable_source_document_sha, stable_content_plan_sha, canonical_sha256(stable_render_plan)


def _final_cache_contract(
    *,
    skill_root: Path,
    scripts: Path,
    source: Path,
    normalized: Path,
    source_document_path: Path,
    decision_path: Path,
    content_plan_path: Path,
    render_plan: dict[str, Any],
    render_plan_path: Path,
    review_receipt_path: Path,
    template_pack: dict[str, Any],
    delivery_profile: Any,
    preflight: dict[str, Any],
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    validation_scripts = [
        "validate_content_plan_v3.py",
        "validate_content_map.py",
        "image_fingerprint.py",
        "pipeline_common.py",
        "v3_common.py",
        "verify_template_invariants.py",
        "verify_document_structure.py",
        "verify_tables.py",
        "scan_source_identity.py",
        "export_word_wps.ps1",
        "analyze_rendered_pages.py",
    ]
    validation_config = {
        "validator_dispatch": render_plan.get("validator_dispatch"),
        "renderer_profile": render_plan.get("renderer_profile"),
        "scripts": {
            name: sha256_file(scripts / name)
            for name in validation_scripts
        },
    }
    engine_identity = _office_binary_identity(preflight)
    manifest_path = skill_root / "manifest.json"
    source_document_cache_sha, content_plan_cache_sha, render_plan_cache_sha = _final_cache_semantic_artifact_shas(
        source_document_path=source_document_path,
        decision_path=decision_path,
        content_plan_path=content_plan_path,
        render_plan_path=render_plan_path,
        review_receipt_path=review_receipt_path,
        normalized=normalized,
    )
    inputs = {
        "source_sha256": sha256_file(source),
        "normalized_sha256": sha256_file(normalized),
        "source_document_sha256": source_document_cache_sha,
        "template": {
            "id": template_pack["id"],
            "version": template_pack["version"],
            "sha256": template_pack["template_sha256"],
            "config_sha256": template_pack.get("template_config_sha256"),
        },
        "decision_sha256": sha256_file(decision_path),
        "content_plan_sha256": content_plan_cache_sha,
        "render_plan_sha256": render_plan_cache_sha,
        "review_receipt_sha256": sha256_file(review_receipt_path),
        "compiler_sha256": sha256_file(scripts / "fixed_template_compiler_v3.py"),
        "compiler": render_plan.get("compiler"),
        "delivery": {
            "target": delivery_profile.target.value,
            "format": delivery_profile.delivery_format,
            "formal_renderer": delivery_profile.formal_renderer,
            "required_renderers": list(delivery_profile.required_renderers),
            "validation_profile": delivery_profile.validation_profile,
        },
        "validation_config_sha256": canonical_sha256(validation_config),
        "engine_identity_sha256": engine_identity["identity_sha256"],
        "skill_manifest_sha256": sha256_file(manifest_path) if manifest_path.is_file() else "",
    }
    facts = {
        "status": "PASS",
        "deliverable": True,
        "template_pack_id": template_pack["id"],
        "template_pack_version": template_pack["version"],
        "review_receipt_sha256": inputs["review_receipt_sha256"],
        "compiler_sha256": inputs["compiler_sha256"],
        "validation_config_sha256": inputs["validation_config_sha256"],
        "engine_identity_sha256": inputs["engine_identity_sha256"],
        "skill_manifest_sha256": inputs["skill_manifest_sha256"],
        "delivery_target": delivery_profile.target.value,
    }
    return cache_key(PASS_CACHE_STAGE, **inputs), inputs, facts


def _cached_validation_stages(
    evidence: dict[str, Any],
    *,
    expected_facts: dict[str, Any],
    final_artifact: Path,
) -> list[dict[str, Any]]:
    if evidence.get("schema_version") != "pass-cache-evidence-v3":
        raise ValueError("PASS cache evidence schema changed")
    if evidence.get("status") != "PASS" or evidence.get("deliverable") is not True:
        raise ValueError("PASS cache evidence does not prove deliverability")
    facts = evidence.get("facts")
    if not isinstance(facts, dict) or any(facts.get(name) != value for name, value in expected_facts.items()):
        raise ValueError("PASS cache evidence facts are stale")
    expected_output_sha = str(evidence.get("output_sha256") or "").upper()
    if not final_artifact.is_file() or sha256_file(final_artifact) != expected_output_sha:
        raise ValueError("PASS cache final artifact hash binding failed")
    stages = evidence.get("validation_stages")
    if not isinstance(stages, list) or any(not isinstance(stage, dict) or stage.get("status") != "PASS" for stage in stages):
        raise ValueError("PASS cache contains a non-PASS validation stage")
    names = {str(stage.get("stage") or "") for stage in stages}
    missing = sorted(PASS_CACHE_REQUIRED_STAGES - names)
    if missing:
        raise ValueError(f"PASS cache is missing validation stages: {missing}")
    return [{**stage, "cache_reused": True} for stage in stages]


def _pass_bundle_files(
    *,
    delivery_path: Path,
    evidence_path: Path,
    output_docx: Path,
    source_document_path: Path,
    inventory_path: Path,
    decision_path: Path,
    review_receipt_path: Path,
    content_plan_path: Path,
    render_plan_path: Path,
    reports: Path,
    render_dir: Path,
    engines: dict[str, dict[str, Any]],
) -> tuple[dict[str, Path], str]:
    final_member = f"delivery/final{delivery_path.suffix.lower()}"
    files: dict[str, Path] = {
        final_member: delivery_path,
        PASS_CACHE_EVIDENCE_MEMBER: evidence_path,
        "evidence/artifacts/source-document-v3.json": source_document_path,
        "evidence/artifacts/source-inventory.json": inventory_path,
        "evidence/artifacts/content-decision-v3.json": decision_path,
        "evidence/artifacts/review-receipt-v3.json": review_receipt_path,
        "evidence/artifacts/content-plan-v3.json": content_plan_path,
        "evidence/artifacts/render-plan-v3.json": render_plan_path,
    }
    if delivery_path.resolve() != output_docx.resolve():
        files["evidence/artifacts/intermediate.docx"] = output_docx
    for report_file in sorted(reports.glob("*.json"), key=lambda path: path.name.casefold()):
        if report_file.resolve() != evidence_path.resolve() and report_file.name not in {"pipeline_report.json", "stage_telemetry_v3.json"}:
            files[f"evidence/reports/{report_file.name}"] = report_file
    for engine_name, engine in sorted(engines.items()):
        pdf_path = Path(str(engine.get("pdf") or ""))
        if pdf_path.is_file():
            files[f"evidence/rendered/{engine_name}.pdf"] = pdf_path
        engine_dir = render_dir / engine_name
        page_count = int(engine.get("page_count") or 0)
        for page_index in range(1, page_count + 1):
            page = engine_dir / f"page-{page_index:03d}.png"
            if page.is_file():
                files[f"evidence/rendered/{engine_name}/{page.name}"] = page
    return files, final_member


def main() -> int:
    parser = argparse.ArgumentParser(description="V3 多模板固定版式转换总控；沿用 V2 fail-closed 关卡与交付合同")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    password_group = parser.add_mutually_exclusive_group()
    password_group.add_argument("--password-stdin", action="store_true", help="仅在授权到期时从标准输入读取一行续期密码")
    decision_group = parser.add_mutually_exclusive_group()
    decision_group.add_argument("--decision-bundle", "--content-decision", dest="decision_bundle", type=Path, help="外部生成的 ContentDecisionV3 JSON")
    decision_group.add_argument("--content-map", type=Path, help="仅通过显式迁移器导入已批准的 V2 content_map v1")
    template_group = parser.add_mutually_exclusive_group()
    template_group.add_argument("--template-pack", help="已发布模板包，必须使用 id@version")
    template_group.add_argument("--template", type=Path, help="显式 DOCX 目标模板；未知或字节变化时进入 onboarding")
    parser.add_argument("--review-receipt", type=Path, help="与当前内容复核队列七项哈希绑定的 ReviewReceiptV3")
    parser.add_argument("--delivery-target", choices=target_values(), default="desktop", help="desktop 交付 DOCX；mobile 交付 WPS PDF")
    parser.add_argument("--cache-dir", type=Path, help="V3 内容寻址缓存目录")
    parser.add_argument("--no-cache", action="store_true", help="禁用 V3 缓存，用于冷路径基准")
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1), help="静态校验、PDF/OCR 的受控 worker 数")
    parser.add_argument("--state-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--python", type=Path, help="具备 python-docx/python-pptx/Pillow/pypdf 的完整运行时")
    parser.add_argument("--powershell-host", type=Path, help="同一次流水线统一使用的本机 PowerShell 可执行文件")
    parser.add_argument("--dependency-bindings", type=Path, help="DependencyBindingsV3 JSON；同一文件绑定 Python、Office PowerShell、WinRT OCR PowerShell 和本机依赖")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    scripts = Path(__file__).resolve().parent
    skill_root = scripts.parent
    global ACTIVE_DEPENDENCY_BINDINGS_FACT
    dependency_bindings: dict[str, Any] | None = None
    office_powershell = args.powershell_host
    winrt_ocr_powershell: Path | None = None
    if args.dependency_bindings:
        bindings_path = args.dependency_bindings.resolve()
        try:
            dependency_bindings = load_dependency_bindings(bindings_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            final = result(
                "BLOCKED",
                "dependency_bindings_v3",
                findings=[{
                    "code": "DEPENDENCY_MISSING",
                    "dependency": "DependencyBindingsV3",
                    "checked_locations": [str(bindings_path)],
                    "resolution_hint": f"Regenerate DependencyBindingsV3 before running the pipeline: {exc}",
                }],
                deliverable=False,
            )
            _write_report_if_possible(args.report, final)
            emit_json(final)
            return 2
        ACTIVE_DEPENDENCY_BINDINGS_FACT = {
            **dependency_bindings_reference(bindings_path),
            "status": dependency_bindings.get("status"),
            "office_powershell": dependency_bindings["dependencies"]["office_powershell"]["path"] or "",
            "winrt_ocr_powershell": dependency_bindings["dependencies"]["winrt_ocr_powershell"]["path"] or "",
        }
        missing = missing_dependency_findings(dependency_bindings)
        if missing:
            final = result(
                "BLOCKED",
                "dependency_bindings_v3",
                findings=missing,
                dependency_bindings=ACTIVE_DEPENDENCY_BINDINGS_FACT,
                deliverable=False,
            )
            _write_report_if_possible(args.report, final)
            emit_json(final)
            return 2
        python = str(resolve_dependency(dependency_bindings, "python"))
        office_powershell = resolve_dependency(dependency_bindings, "office_powershell")
        winrt_ocr_powershell = resolve_dependency(dependency_bindings, "winrt_ocr_powershell")
    else:
        python = str(args.python.resolve()) if args.python else sys.executable
    source = args.source.resolve()
    output_dir = args.output_dir.resolve()
    try:
        powershell_host = resolve_powershell_host(office_powershell)
    except PowerShellHostError as exc:
        final = result(
            "BLOCKED",
            "run_pipeline",
            findings=[{
                "code": "POWERSHELL_HOST_UNAVAILABLE",
                "source": exc.source,
                "candidate": exc.candidate,
                "message": str(exc),
            }],
            deliverable=False,
        )
        _write_report_if_possible(args.report, final)
        emit_json(final)
        return 2
    powershell_report, powershell_finding = probe_powershell_host(powershell_host)
    if powershell_finding is not None:
        final = result(
            "BLOCKED",
            "run_pipeline",
            findings=[powershell_finding],
            powershell_host=powershell_report,
            deliverable=False,
        )
        _write_report_if_possible(args.report, final)
        emit_json(final)
        return 2
    os.environ[POWERSHELL_HOST_ENV] = str(powershell_host.path)
    if winrt_ocr_powershell is not None:
        os.environ[WINRT_OCR_POWERSHELL_HOST_ENV] = str(winrt_ocr_powershell)
    access_payload = _run_access_gate(python, scripts, args.password_stdin, state_file=args.state_file.resolve() if args.state_file else None)
    if access_payload.get("status") != "AUTHORIZED":
        final = result(
            "BLOCKED",
            "run_pipeline",
            findings=[{
                "code": access_payload.get("status", "ACCESS_GATE_FAILED"),
                "message": access_payload.get("message", "未授权"),
            }],
            powershell_host=powershell_report,
            deliverable=False,
        )
        _write_report_if_possible(args.report, final)
        emit_json(final)
        return 2

    reports = output_dir / "reports"
    work = output_dir / "work"
    normalized_dir = work / "normalized"
    render_dir = work / "rendered"
    task_dir = work / "decision_tasks"
    output_dir.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    report_path = (args.report or reports / "pipeline_report.json").resolve()
    telemetry_path = reports / "stage_telemetry_v3.json"
    workers = max(1, min(int(args.workers), 16))
    telemetry = StageTelemetryRecorder(run_mode="cold" if args.no_cache else "cache-enabled", worker_count=workers)
    cache = ContentAddressedCache(args.cache_dir.resolve() if args.cache_dir else default_cache_root())
    stages: list[dict[str, Any]] = [{"stage": "access_gate", "status": "PASS"}]

    format_probe_path = work / "format_probe_result_v3.json"
    with telemetry.stage("format_probe") as timing:
        probe = probe_format(source)
        write_json(format_probe_path, probe)
        timing["finding_count"] = len(probe.get("findings", []))
        if source.is_file():
            timing["output_hashes"] = {"format_probe": sha256_file(format_probe_path)}
    stages.append(probe)
    if probe.get("status") != "PASS":
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            source=str(source),
            format_probe=str(format_probe_path),
        )
    detected_format = str(probe["detected_format"])

    preflight_report = reports / "preflight_dependencies.json"
    with telemetry.stage("preflight_dependencies", input_hashes={"source": probe["source"]["sha256"]}) as timing:
        preflight = execute(
            [
                python,
                str(scripts / "preflight_dependencies.py"),
                "--source",
                str(source),
                "--source-format",
                detected_format,
                "--report",
                str(preflight_report),
            ],
            preflight_report,
            timeout=180,
        )
        timing["finding_count"] = len(preflight.get("findings", []))
        if preflight_report.is_file():
            timing["output_hashes"] = {"preflight": sha256_file(preflight_report)}
    stages.append(preflight)
    if preflight.get("status") != "PASS":
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            status="BLOCKED",
            findings=preflight.get("findings", []),
            powershell_host=powershell_report,
            source=str(source),
            format_probe=str(format_probe_path),
            actions=preflight.get("actions", []),
            message=preflight.get("message", "运行依赖不完整；请先补全并重试。"),
        )

    normalize_report = reports / "normalize_source.json"
    target_suffix = str(probe["normalized_extension"])
    normalized = normalized_dir / f"{source.stem}.normalized{target_suffix}"
    normalize_inputs = {
        "source_sha256": probe["source"]["sha256"],
        "detected_format": detected_format,
        "adapter_sha256": sha256_file(scripts / "normalize_source.py"),
        "office": preflight.get("office", {}),
    }
    normalize_key = cache_key("normalize", **normalize_inputs)
    normalize_lookup = cache.lookup_file("normalize", normalize_key) if not args.no_cache and not probe["requires_normalization"] else None
    with telemetry.stage("normalize_source", input_hashes={"source": probe["source"]["sha256"]}) as timing:
        if normalize_lookup is not None and cache.materialize_file(normalize_lookup, normalized):
            telemetry.run_mode = "warm"
            timing["cache_status"] = "HIT"
            normalize = result(
                "PASS",
                "normalize_source",
                normalized=str(normalized),
                normalized_sha256=sha256_file(normalized),
                source=str(source),
                source_sha256=probe["source"]["sha256"],
                source_format=detected_format.upper(),
                normalized_format=target_suffix.lstrip(".").upper(),
                method="content-addressed-cache",
                format_changed=False,
            )
            write_json(normalize_report, normalize)
        else:
            timing["cache_status"] = normalize_lookup.status if normalize_lookup is not None else "MISS"
            command = [
                python,
                str(scripts / "normalize_source.py"),
                str(source),
                "--out-dir",
                str(normalized_dir),
                "--detected-format",
                detected_format.upper(),
                "--report",
                str(normalize_report),
            ]
            if probe["requires_normalization"]:
                command.extend(["--visual-evidence-dir", str(work / "legacy_visual_evidence")])
            normalize = execute(command, normalize_report, timeout=600)
            if normalize.get("status") == "PASS":
                normalized = Path(normalize["normalized"]).resolve()
                if not args.no_cache and not probe["requires_normalization"]:
                    cache.store_file("normalize", normalize_key, normalized, inputs=normalize_inputs)
        timing["finding_count"] = len(normalize.get("findings", []))
        if normalized.is_file():
            timing["output_hashes"] = {"normalized": sha256_file(normalized)}
    stages.append(normalize)
    if normalize.get("status") != "PASS":
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            source=str(source),
            format_probe=str(format_probe_path),
            normalized=str(normalized),
        )

    pending_review_stages: list[dict[str, Any]] = []
    if probe["requires_normalization"]:
        legacy_report = reports / "legacy_visual_compare_v3.json"
        with telemetry.stage("legacy_visual_compare_v3", input_hashes={"normalized": sha256_file(normalized)}) as timing:
            legacy_stage = execute(
                [
                    python,
                    str(scripts / "legacy_visual_compare_v3.py"),
                    "--original-pdf",
                    str(normalize.get("original_render_pdf", "")),
                    "--normalized-pdf",
                    str(normalize.get("normalized_render_pdf", "")),
                    "--output-dir",
                    str(work / "legacy_visual_compare"),
                    "--report",
                    str(legacy_report),
                ],
                legacy_report,
                timeout=900,
            )
            timing["finding_count"] = len(legacy_stage.get("findings", []))
            if legacy_report.is_file():
                timing["output_hashes"] = {"legacy_diff": sha256_file(legacy_report)}
        stages.append(legacy_stage)
        if legacy_stage.get("status") in {"FAIL", "BLOCKED"}:
            return _finish_run(
                report_path=report_path,
                telemetry_path=telemetry_path,
                telemetry=telemetry,
                stages=stages,
                source=str(source),
                normalized=str(normalized),
                legacy_visual_report=str(legacy_report),
            )
        if legacy_stage.get("status") == "HUMAN_REVIEW":
            pending_review_stages.append(legacy_stage)

    inventory_path = work / "source_inventory.json"
    inventory_report = reports / "inventory_source.json"
    inventory_inputs = {
        "normalized_sha256": sha256_file(normalized),
        "extractor_sha256": sha256_file(scripts / "inventory_source.py"),
        "ocr_backends": preflight.get("ocr", {}),
        "image_decoders": preflight.get("image_decoders", {}),
    }
    inventory_key = cache_key("inventory", **inventory_inputs)
    inventory_lookup = cache.lookup_json("inventory", inventory_key) if not args.no_cache else None
    with telemetry.stage("inventory_source", input_hashes={"normalized": inventory_inputs["normalized_sha256"]}) as timing:
        if inventory_lookup is not None and cache.materialize_file(inventory_lookup, inventory_path):
            telemetry.run_mode = "warm"
            timing["cache_status"] = "HIT"
            inventory_stage = result(
                "PASS",
                "inventory_source",
                inventory=str(inventory_path),
                inventory_sha256=sha256_file(inventory_path),
                cache_key=inventory_key,
            )
            write_json(inventory_report, inventory_stage)
        else:
            timing["cache_status"] = inventory_lookup.status if inventory_lookup is not None else "MISS"
            inventory_stage = execute(
                [
                    python,
                    str(scripts / "inventory_source.py"),
                    str(normalized),
                    "--output",
                    str(inventory_path),
                    "--report",
                    str(inventory_report),
                ],
                inventory_report,
                timeout=900,
            )
            if inventory_stage.get("status") == "PASS" and not args.no_cache:
                cache.store_file("inventory", inventory_key, inventory_path, inputs=inventory_inputs)
        timing["finding_count"] = len(inventory_stage.get("findings", []))
        if inventory_path.is_file():
            timing["output_hashes"] = {"inventory": sha256_file(inventory_path)}
    stages.append(inventory_stage)
    if inventory_stage.get("status") != "PASS":
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            source=str(source),
            normalized=str(normalized),
            source_inventory=str(inventory_path),
        )

    source_document_path = work / "source_document_v3.json"
    try:
        with telemetry.stage("source_document_v3", input_hashes={"inventory": sha256_file(inventory_path)}) as timing:
            source_document = build_source_document_v3(
                source,
                normalized,
                read_json(inventory_path),
                probe,
                normalize,
            )
            if pending_review_stages:
                source_document["review"]["required"] = True
                source_document["review"]["reasons"] = sorted(set(
                    source_document["review"].get("reasons", [])
                    + [
                        str(finding.get("code") or "LEGACY_VISUAL_REVIEW_REQUIRED")
                        for stage in pending_review_stages
                        for finding in stage.get("findings", [])
                        if isinstance(finding, dict)
                    ]
                ))
            write_json(source_document_path, source_document)
            timing["finding_count"] = len(source_document["review"]["reasons"])
            timing["output_hashes"] = {"source_document": sha256_file(source_document_path)}
    except (SourceDocumentError, OSError, ValueError, TypeError) as exc:
        finding = {
            "code": getattr(exc, "code", "SOURCE_DOCUMENT_BUILD_FAILED"),
            "message": getattr(exc, "message", str(exc)),
            **getattr(exc, "facts", {}),
        }
        source_stage = result("BLOCKED", "source_document_v3", findings=[finding])
        stages.append(source_stage)
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            source=str(source),
            normalized=str(normalized),
            source_inventory=str(inventory_path),
        )
    source_stage = result(
        "HUMAN_REVIEW" if source_document["review"]["required"] else "PASS",
        "source_document_v3",
        findings=[{"code": code} for code in source_document["review"]["reasons"]],
        source_document=str(source_document_path),
        item_count=len(_source_items(source_document)),
    )
    stages.append(source_stage)

    source_document_artifact_sha = source_document_sha256(source_document)
    source_document_file_sha = sha256_file(source_document_path)

    with telemetry.stage("template_resolver_v3", input_hashes={"source_document": source_document_artifact_sha}) as timing:
        template_stage = resolve_template(
            skill_root,
            pack_reference=args.template_pack,
            template=args.template.resolve() if args.template else None,
            routing_context={"source_family": probe["family"], "source_format": detected_format},
        )
        timing["finding_count"] = len(template_stage.get("findings", []))
    stages.append(template_stage)
    if template_stage.get("status") != "PASS":
        onboarding = None
        if args.template is not None and template_stage.get("status") == "HUMAN_REVIEW":
            onboarding = create_onboarding_draft(args.template.resolve(), work / "template_onboarding")
            stages.append(onboarding)
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            source=str(source),
            normalized=str(normalized),
            source_document=str(source_document_path),
            template_onboarding=onboarding,
        )
    template_pack = _hydrate_template_pack(template_stage["template_pack"])
    template = Path(template_pack["template_path"])
    allowed_slots = _allowed_slots(template_pack)

    declared_pack_validators = template_pack.get("validators", {}).get("pack", []) if isinstance(template_pack.get("validators"), dict) else []
    if "verify_fixed_template" in declared_pack_validators:
        fixed_template_report = reports / "verify_fixed_template.json"
        fixed_template_stage = execute(
            [
                python,
                str(scripts / "verify_fixed_template.py"),
                str(template),
                "--report",
                str(fixed_template_report),
            ],
            fixed_template_report,
            timeout=180,
        )
        stages.append(fixed_template_stage)
        if fixed_template_stage.get("status") != "PASS":
            return _finish_run(
                report_path=report_path,
                telemetry_path=telemetry_path,
                telemetry=telemetry,
                stages=stages,
                template_pack={"id": template_pack["id"], "version": template_pack["version"]},
            )

    configured_manufacturer_terms: list[str] = []
    try:
        if args.content_map and args.content_map.is_file():
            configured_manufacturer_terms = [
                str(value)
                for value in read_json(args.content_map.resolve()).get("manufacturer_terms", [])
                if str(value)
            ]
        elif args.decision_bundle and args.decision_bundle.is_file():
            preview_decision = read_json(args.decision_bundle.resolve())
            identity_preview = preview_decision.get("identity_review") if isinstance(preview_decision.get("identity_review"), dict) else {}
            configured_manufacturer_terms = [str(value) for value in identity_preview.get("manufacturer_terms", []) if str(value)]
    except (OSError, ValueError, json.JSONDecodeError):
        # 正式 decision/schema 阶段会给出确定性 FAIL；候选引擎不吞掉输入错误。
        configured_manufacturer_terms = []

    try:
        from deterministic_candidates_v3 import build_deterministic_candidates

        with telemetry.stage("deterministic_candidate_engine", input_hashes={"source_document": source_document_artifact_sha}) as timing:
            deterministic_candidates = build_deterministic_candidates(
                source_document,
                allowed_slots=allowed_slots,
                manufacturer_terms=configured_manufacturer_terms,
            )
            timing["finding_count"] = sum(
                1 for item in deterministic_candidates if item.get("resolution_status") in {"conflict", "low_confidence"}
            )
    except (ImportError, TypeError, ValueError) as exc:
        candidate_stage = result("BLOCKED", "deterministic_candidate_engine", findings=[{
            "code": "DETERMINISTIC_CANDIDATE_ENGINE_FAILED",
            "message": str(exc),
        }])
        stages.append(candidate_stage)
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            source_document=str(source_document_path),
            template_pack={"id": template_pack["id"], "version": template_pack["version"]},
        )
    candidates_path = work / "deterministic_candidates_v3.json"
    write_json(candidates_path, {"schema_version": "3.0", "deterministic_candidates": deterministic_candidates})
    stages.append(result("PASS", "deterministic_candidate_engine", candidate_count=len(deterministic_candidates)))

    policy_sha = _decision_policy_sha256(skill_root)
    try:
        with telemetry.stage("decision_task_bundle_v3", input_hashes={"source_document": source_document_artifact_sha}) as timing:
            bundles = build_decision_task_bundles(
                source_document,
                deterministic_candidates,
                template_pack_id=template_pack["id"],
                template_pack_version=template_pack["version"],
                allowed_slots=allowed_slots,
                evidence_dir=task_dir / "evidence",
                bundle_root=task_dir,
                policy_version="decision-policy-v3",
                policy_sha256=policy_sha,
                source_document_artifact_sha256=source_document_artifact_sha,
            )
            bundle_paths = write_decision_task_bundles(bundles, task_dir / "bundles")
            inline_chars = sum(int(bundle.get("inline_evidence_chars", 0)) for bundle in bundles)
            unresolved_items = sum(len(bundle.get("item_batch", [])) for bundle in bundles)
            telemetry.record_prompt(
                task_count=len(bundles),
                inline_characters=inline_chars,
                unresolved_items=unresolved_items,
            )
            timing["prompt_task_count"] = len(bundles)
            timing["inline_characters"] = inline_chars
            timing["unresolved_items"] = unresolved_items
            timing["output_hashes"] = {path.name: sha256_file(path) for path in bundle_paths}
    except (DecisionBundleError, OSError, ValueError) as exc:
        bundle_stage = result("FAIL", "decision_task_bundle_v3", findings=[{
            "code": "DECISION_TASK_BUNDLE_INVALID",
            "message": str(exc),
        }])
        stages.append(bundle_stage)
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            source_document=str(source_document_path),
            deterministic_candidates=str(candidates_path),
        )
    stages.append(result(
        "HUMAN_REVIEW" if bundles else "PASS",
        "decision_task_bundle_v3",
        findings=[{"code": "DECISION_BUNDLE_REQUIRED"}] if bundles else [],
        task_count=len(bundles),
        external_bundle_required=bool(bundles),
        resolution_mode="model_task_required" if bundles else "deterministic_only",
        task_bundles=[str(path) for path in bundle_paths],
    ))

    decision_path = work / "content_decision_v3.json"
    decision_inputs = {
        "source_document_sha256": source_document_artifact_sha,
        "template_pack": {"id": template_pack["id"], "version": template_pack["version"]},
        "policy_sha256": policy_sha,
        "schema_sha256": sha256_file(skill_root / "references" / "schemas" / "content-decision-v3.schema.json"),
    }
    decision_key = cache_key("decision", **decision_inputs)
    # A zero-task deterministic run must materialize the current complete
    # deterministic decision rather than reusing a stale decision cache entry
    # produced before the candidate filtering/merge boundary.
    decision_lookup = cache.lookup_json("decision", decision_key) if not args.no_cache and bundles and not args.decision_bundle and not args.content_map else None
    try:
        with telemetry.stage("content_decision_v3", input_hashes={"source_document": decision_inputs["source_document_sha256"]}) as timing:
            if args.decision_bundle:
                decision = read_json(args.decision_bundle.resolve())
                write_json(decision_path, decision)
                timing["cache_status"] = "MISS"
            elif args.content_map:
                decision = migrate_content_map_v1(
                    read_json(args.content_map.resolve()),
                    source_document,
                    content_map_path=args.content_map.resolve(),
                    template_pack_id=template_pack["id"],
                    template_pack_version=template_pack["version"],
                    policy_version="decision-policy-v3",
                    policy_sha256=policy_sha,
                    source_document_artifact_sha256=source_document_artifact_sha,
                )
                write_json(decision_path, decision)
                timing["cache_status"] = "MISS"
            elif decision_lookup is not None and cache.materialize_file(decision_lookup, decision_path):
                telemetry.run_mode = "warm"
                timing["cache_status"] = "HIT"
                decision = read_json(decision_path)
            elif not bundles:
                # An all-resolved deterministic candidate set is a complete
                # decision input, not a request to fabricate an empty model
                # bundle.  The normal ReviewReceiptV3 gate still prevents
                # delivery without explicit human approval.
                decision = build_deterministic_content_decision(
                    source_document,
                    deterministic_candidates,
                    template_pack_id=template_pack["id"],
                    template_pack_version=template_pack["version"],
                    policy_version="decision-policy-v3",
                    policy_sha256=policy_sha,
                    source_document_artifact_sha256=source_document_artifact_sha,
                )
                write_json(decision_path, decision)
                timing["cache_status"] = "DETERMINISTIC_ONLY"
            else:
                timing["cache_status"] = decision_lookup.status if decision_lookup is not None else "MISS"
                decision = None
            timing["output_hashes"] = {"content_decision": sha256_file(decision_path)} if decision_path.is_file() else {}
    except (DecisionBundleError, OSError, ValueError, json.JSONDecodeError) as exc:
        decision_stage = result("FAIL", "content_decision_v3", findings=[{
            "code": "DECISION_SCHEMA_INVALID",
            "message": str(exc),
        }])
        stages.append(decision_stage)
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            source_document=str(source_document_path),
            task_bundles=[str(path) for path in bundle_paths],
        )

    if decision is None:
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            status="HUMAN_REVIEW",
            findings=[{"code": "DECISION_BUNDLE_REQUIRED", "task_count": len(bundles)}],
            source=str(source),
            normalized=str(normalized),
            source_inventory=str(inventory_path),
            source_document=str(source_document_path),
            deterministic_candidates=str(candidates_path),
            task_bundles=[str(path) for path in bundle_paths],
            template_pack={"id": template_pack["id"], "version": template_pack["version"]},
        )

    try:
        from deterministic_candidates_v3 import apply_deterministic_resolutions

        decision_source_sha = str(decision.get("source_document_sha256") or "").upper()
        compatible_source_sha = (
            source_document_file_sha
            if decision_source_sha == source_document_file_sha
            else source_document_artifact_sha
        )
        if (
            decision_source_sha not in {source_document_file_sha, source_document_artifact_sha}
            and legacy_migrated_decision_matches_source_document(source_document, decision)
        ):
            compatible_source_sha = decision_source_sha
        decision = apply_deterministic_resolutions(
            decision,
            normalize_deterministic_candidates(deterministic_candidates),
            source_document,
            source_document_artifact_sha256=compatible_source_sha,
        )
        write_json(decision_path, decision)
    except (ImportError, DecisionBundleError, ValueError, TypeError) as exc:
        stages.append(result("FAIL", "deterministic_decision_merge", findings=[{
            "code": "DETERMINISTIC_DECISION_MERGE_FAILED",
            "message": str(exc),
        }]))
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            content_decision=str(decision_path),
        )

    decision_validation_path = reports / "content_decision_v3.json"
    decision_stage = validate_content_decision(
        decision,
        task_bundles=None if decision.get("migration") or args.review_receipt else bundles,
        expected_source_ids=[str(item.get("source_id")) for item in _source_items(source_document)],
    )
    # The stage timestamp belongs in telemetry/pipeline history, not in the
    # approval-bound validation artifact.  Keeping it here made an unchanged
    # decision produce a different review hash on every rerun.
    decision_validation_artifact = {
        key: value for key, value in decision_stage.items() if key != "generated_at"
    }
    write_json(decision_validation_path, decision_validation_artifact)
    stages.append(decision_stage)
    decision_requires_content_review = _decision_requires_content_review(decision_stage)
    if decision_stage.get("status") != "PASS" and not decision_requires_content_review:
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            source_document=str(source_document_path),
            content_decision=str(decision_path),
            decision_validation=str(decision_validation_path),
            task_bundles=[str(path) for path in bundle_paths],
        )
    if not args.no_cache:
        cache.store_file("decision", decision_key, decision_path, inputs=decision_inputs)

    review_queue_path = work / "content_review_queue_v3.json"
    review_html_path = work / "content_review.html"
    document_preview_path = work / "document-preview-v3.json"
    review_timing = telemetry.begin_stage("content_review_v3", input_hashes={
        "source_document": source_document_artifact_sha,
        "content_decision": sha256_file(decision_path),
        "decision_validation": sha256_file(decision_validation_path),
    })
    try:
        document_preview = build_content_document_preview(source_document, decision, template_pack)
        materialize_preview_images(source_document, document_preview, work)
        write_json(document_preview_path, document_preview)
        receipt = read_json(args.review_receipt.resolve()) if args.review_receipt and args.review_receipt.is_file() else None
        review_binding = _content_review_binding(
            source_document,
            decision_path,
            decision_validation_path,
            template_pack,
            scripts / "fixed_template_compiler_v3.py",
        )
        source_evidence_sha = source_document_artifact_sha
        if (
            isinstance(receipt, dict)
            and receipt.get("binding") == {
                **review_binding,
                **({HIDDEN_REVIEWER_BINDING_FIELD: receipt["binding"][HIDDEN_REVIEWER_BINDING_FIELD]}
                   if isinstance(receipt.get("binding"), dict)
                   and receipt["binding"].get(HIDDEN_REVIEWER_BINDING_FIELD)
                   else {}),
            }
            and legacy_migrated_decision_matches_source_document(source_document, decision)
        ):
            source_evidence_sha = _receipt_evidence_sha(receipt, "SOURCE-DOCUMENT") or source_evidence_sha
        review_queue = build_review_queue(
            "content_decision",
            review_binding,
            _content_review_items(source_document, decision),
            evidence_refs=[
                {
                    "evidence_id": "SOURCE-DOCUMENT",
                    "label": "SourceDocumentV3（业务语义内容）",
                    "kind": "source_document_semantic",
                    "path": str(source_document_path),
                    # SourceDocumentV3 包含运行目录中的 normalized.path。人工
                    # 审批必须绑定内容、格式、适配器与上游合同，而不能因同一
                    # 源件换了输出目录就失效。原始文件和归一化文件的字节 SHA
                    # 仍保存在 SourceDocumentV3 内，并由其语义哈希共同绑定。
                    "sha256": source_evidence_sha,
                },
                {
                    "evidence_id": "CONTENT-DECISION",
                    "label": "ContentDecisionV3",
                    "kind": "content_decision",
                    "path": str(decision_path),
                    "sha256": sha256_file(decision_path),
                },
                {
                    "evidence_id": "DECISION-VALIDATION",
                    "label": "决策确定性校验报告",
                    "kind": "validation_report",
                    "path": str(decision_validation_path),
                    "sha256": sha256_file(decision_validation_path),
                },
                {
                    "evidence_id": "DOCUMENT-PREVIEW",
                    "label": "面向业务用户的拟生成 DOCX 内容预览",
                    "kind": "document_preview",
                    "path": str(document_preview_path),
                    "sha256": sha256_file(document_preview_path),
                },
            ],
            document_preview=document_preview,
            hidden_reviewer=_hidden_reviewer_from_receipt(receipt),
        )
        write_json(review_queue_path, review_queue)
        write_review_html(review_queue, review_html_path)
        review_stage, receipt, rebind_report = _validate_or_rebind_content_review(
            review_queue,
            receipt,
            args.review_receipt.resolve() if args.review_receipt else None,
        )
        review_stage["stage"] = "content_review_v3"
        review_stage["review_queue"] = str(review_queue_path)
        review_stage["review_html"] = str(review_html_path)
        if rebind_report is not None:
            review_stage["review_rebind"] = rebind_report
    except (ReviewError, OSError, ValueError, json.JSONDecodeError) as exc:
        review_stage = result("FAIL", "content_review_v3", findings=[{
            "code": "REVIEW_QUEUE_OR_RECEIPT_INVALID",
            "message": str(exc),
        }])
    telemetry.finish_stage(
        review_timing,
        stage_result=review_stage,
        output_hashes={
            **({"review_queue": sha256_file(review_queue_path)} if review_queue_path.is_file() else {}),
            **({"review_html": sha256_file(review_html_path)} if review_html_path.is_file() else {}),
        },
    )
    stages.append(review_stage)
    if review_stage.get("status") != "PASS":
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            source=str(source),
            normalized=str(normalized),
            source_inventory=str(inventory_path),
            source_document=str(source_document_path),
            content_decision=str(decision_path),
            review_queue=str(review_queue_path),
            review_html=str(review_html_path),
            template_pack={"id": template_pack["id"], "version": template_pack["version"]},
        )
    if decision_requires_content_review:
        _mark_human_review_resolved(
            decision_stage,
            review_resolved_by=review_queue["review_id"],
        )
    review_receipt_path = work / "content_review_receipt_v3.json"
    write_json(review_receipt_path, receipt)
    _mark_human_review_resolved(
        source_stage,
        review_resolved_by=review_queue["review_id"],
    )
    for pending in pending_review_stages:
        _mark_human_review_resolved(
            pending,
            review_resolved_by=review_queue["review_id"],
        )
    for stage in stages:
        if stage.get("stage") == "decision_task_bundle_v3" and stage.get("status") == "HUMAN_REVIEW":
            _mark_human_review_resolved(
                stage,
                decision=str(decision_path),
                review_resolved_by=review_queue["review_id"],
            )

    content_plan_path = work / "content_plan_v3.json"
    content_plan_timing = telemetry.begin_stage("content_plan_v3", input_hashes={
        "source_document": source_document_artifact_sha,
        "content_decision": sha256_file(decision_path),
        "review_receipt": sha256_file(review_receipt_path),
    })
    try:
        content_plan = build_content_plan_v3(
            source_document,
            decision,
            template_pack,
            source_document_path=source_document_path,
            decision_path=decision_path,
            policy_sha256=policy_sha,
            review_receipt=receipt,
            review_receipt_path=review_receipt_path,
            expected_review_binding=review_queue["binding"],
        )
        write_json(content_plan_path, content_plan)
        content_plan_stage = result("PASS", "content_plan_v3", content_plan=str(content_plan_path), sha256=sha256_file(content_plan_path))
    except (OSError, ValueError, KeyError) as exc:
        content_plan_stage = result("FAIL", "content_plan_v3", findings=[{
            "code": "CONTENT_PLAN_BUILD_FAILED",
            "message": str(exc),
        }])
    telemetry.finish_stage(
        content_plan_timing,
        stage_result=content_plan_stage,
        output_hashes={"content_plan": sha256_file(content_plan_path)} if content_plan_path.is_file() else {},
    )
    stages.append(content_plan_stage)
    if content_plan_stage.get("status") != "PASS":
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            source_document=str(source_document_path),
            content_decision=str(decision_path),
        )

    product = decision["product"]
    delivery_profile = get_profile(args.delivery_target)
    output_docx = intermediate_path(work, product["model"], product["full_name"], "初始生成", ".docx")
    formal_output = final_path(output_dir, product["model"], product["full_name"], delivery_profile)
    render_plan_path = work / "render_plan_v3.json"
    render_plan_timing = telemetry.begin_stage("render_plan_v3", input_hashes={
        "source_document": source_document_artifact_sha,
        "content_plan": sha256_file(content_plan_path),
        "template": str(template_pack["template_sha256"]).upper(),
    })
    try:
        render_plan = build_render_plan_v3(
            source_document_path=source_document_path,
            content_plan_path=content_plan_path,
            template_pack=template_pack,
            template_program_path=Path(template_pack["program_path"]),
            delivery_profile=delivery_profile,
            intermediate_docx=output_docx,
            formal_output=formal_output,
        )
        write_json(render_plan_path, render_plan)
        render_plan_stage = result("PASS", "render_plan_v3", render_plan=str(render_plan_path), sha256=sha256_file(render_plan_path))
    except (OSError, ValueError, KeyError) as exc:
        render_plan_stage = result("FAIL", "render_plan_v3", findings=[{
            "code": "RENDER_PLAN_BUILD_FAILED",
            "message": str(exc),
        }])
    telemetry.finish_stage(
        render_plan_timing,
        stage_result=render_plan_stage,
        output_hashes={"render_plan": sha256_file(render_plan_path)} if render_plan_path.is_file() else {},
    )
    stages.append(render_plan_stage)
    if render_plan_stage.get("status") != "PASS":
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            content_plan=str(content_plan_path),
        )

    final_cache_enabled = not args.no_cache
    try:
        final_cache_key, final_cache_inputs, final_cache_facts = _final_cache_contract(
            skill_root=skill_root,
            scripts=scripts,
            source=source,
            normalized=normalized,
            source_document_path=source_document_path,
            decision_path=decision_path,
            content_plan_path=content_plan_path,
            render_plan=render_plan,
            render_plan_path=render_plan_path,
            review_receipt_path=review_receipt_path,
            template_pack=template_pack,
            delivery_profile=delivery_profile,
            preflight=preflight,
        )
    except (OSError, ValueError) as exc:
        final_cache_enabled = False
        final_cache_key, final_cache_inputs, final_cache_facts = "", {}, {}
        stages.append(result(
            "PASS",
            "final_artifact_cache",
            cache_status="CORRUPT",
            findings=[{"code": "FINAL_CACHE_CONTRACT_INVALID", "message": str(exc)}],
        ))
    if final_cache_enabled:
        with telemetry.stage("final_artifact_cache_lookup", input_hashes={
            "cache_key": final_cache_key,
            "content_plan": sha256_file(content_plan_path),
            "render_plan": sha256_file(render_plan_path),
        }) as timing:
            final_lookup = cache.lookup_pass_bundle(
                PASS_CACHE_STAGE,
                final_cache_key,
                expected_facts=final_cache_facts,
            )
            timing["cache_status"] = final_lookup.status
            if final_lookup.status == "HIT":
                materialized = cache.materialize_pass_bundle(
                    final_lookup,
                    work / "pass_cache_reuse" / final_cache_key[:16],
                )
                if materialized.status == "HIT" and materialized.files and materialized.final_artifact:
                    try:
                        evidence_path = materialized.files[PASS_CACHE_EVIDENCE_MEMBER]
                        reused_stages = _cached_validation_stages(
                            read_json(evidence_path),
                            expected_facts=final_cache_facts,
                            final_artifact=materialized.final_artifact,
                        )
                    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
                        timing["cache_status"] = "CORRUPT"
                        timing["cache_reason"] = str(exc)
                    else:
                        telemetry.run_mode = "warm"
                        delivery_timing = telemetry.begin_stage("final_delivery", input_hashes={
                            "cached_artifact": sha256_file(materialized.final_artifact),
                            "cache_key": final_cache_key,
                        })
                        formal_output.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(materialized.final_artifact, formal_output)
                        timing["output_hashes"] = {"deliverable": sha256_file(formal_output)}
                        stages.extend(reused_stages)
                        stages.append(result(
                            "PASS",
                            "final_artifact_cache",
                            cache_status="HIT",
                            cache_key=final_cache_key,
                            evidence=str(evidence_path),
                        ))
                        delivery_renderer = delivery_profile.formal_renderer
                        delivery_stage = {
                            "stage": "final_delivery",
                            "status": "PASS",
                            "output": str(formal_output),
                            "output_sha256": sha256_file(formal_output),
                            "renderer": delivery_renderer,
                            "validation_profile": delivery_profile.validation_profile,
                            "cache_reused": True,
                        }
                        stages.append(delivery_stage)
                        telemetry.finish_stage(
                            delivery_timing,
                            stage_result=delivery_stage,
                            output_hashes={"deliverable": sha256_file(formal_output)},
                            cache_status="HIT",
                        )
                        delivery_summary = final_delivery_summary(stages, delivery_profile, formal_output)
                        return _finish_run(
                            report_path=report_path,
                            telemetry_path=telemetry_path,
                            telemetry=telemetry,
                            stages=stages,
                            status=delivery_summary["status"],
                            deliverable=delivery_summary["deliverable"],
                            source=str(source),
                            source_sha256=sha256_file(source),
                            source_format=detected_format,
                            normalized=str(normalized),
                            source_inventory=str(inventory_path),
                            source_document=str(source_document_path),
                            content_decision=str(decision_path),
                            review_queue=str(review_queue_path),
                            content_plan=str(content_plan_path),
                            render_plan=str(render_plan_path),
                            template_pack={"id": template_pack["id"], "version": template_pack["version"]},
                            output=str(formal_output),
                            output_sha256=sha256_file(formal_output),
                            rendered_dir=str(materialized.destination / "evidence" / "rendered"),
                            delivery_target=delivery_summary["delivery_target"],
                            delivery_format=delivery_summary["delivery_format"],
                            deliverable_path=delivery_summary["deliverable_path"],
                            delivery_renderer=delivery_renderer,
                            validation_profile=delivery_profile.validation_profile,
                            delivery=delivery_profile.report(formal_output, delivery_renderer),
                            final_artifact_cache={"status": "HIT", "key": final_cache_key},
                        )
                else:
                    timing["cache_status"] = materialized.status
                    timing["cache_reason"] = materialized.reason

    build_report = reports / "fixed_template_compiler_v3.json"
    with telemetry.stage("fixed_template_compiler_v3", input_hashes={
        "content_plan": sha256_file(content_plan_path),
        "render_plan": sha256_file(render_plan_path),
    }) as timing:
        build_stage = execute(
            [
                python,
                str(scripts / "fixed_template_compiler_v3.py"),
                "--content-plan",
                str(content_plan_path),
                "--render-plan",
                str(render_plan_path),
                "--output",
                str(output_docx),
                "--report",
                str(build_report),
            ],
            build_report,
            timeout=900,
        )
        timing["finding_count"] = len(build_stage.get("findings", []))
        if output_docx.is_file():
            timing["output_hashes"] = {"intermediate_docx": sha256_file(output_docx)}
    stages.append(build_stage)
    if build_stage.get("status") != "PASS":
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            source=str(source),
            normalized=str(normalized),
            source_inventory=str(inventory_path),
            source_document=str(source_document_path),
            content_decision=str(decision_path),
            content_plan=str(content_plan_path),
            render_plan=str(render_plan_path),
            template_pack={"id": template_pack["id"], "version": template_pack["version"]},
        )

    try:
        from fixed_template_compiler_v3 import adapt_content_plan_for_zxty

        _, adapter_inventory, adapter_map = adapt_content_plan_for_zxty(content_plan, content_plan_path)
        adapter_map["identity_review"] = _validator_identity_review(content_plan)
        adapter_inventory_path = work / "validator_inventory_adapter.json"
        adapter_map_path = work / "validator_content_map_adapter.json"
        write_json(adapter_inventory_path, adapter_inventory)
        write_json(adapter_map_path, adapter_map)
    except Exception as exc:
        stages.append(result("FAIL", "validator_adapter_v3", findings=[{
            "code": "VALIDATOR_ADAPTER_FAILED",
            "message": str(exc),
        }]))
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            intermediate_docx=str(output_docx),
        )

    static_jobs = [
        ValidatorJob(
            "validate_content_plan_v3",
            [
                python,
                str(scripts / "validate_content_plan_v3.py"),
                str(content_plan_path),
                "--render-plan",
                str(render_plan_path),
                "--document",
                str(output_docx),
                "--report",
                str(reports / "validate_content_plan_v3.json"),
            ],
            reports / "validate_content_plan_v3.json",
        ),
        ValidatorJob(
            "verify_template_invariants",
            [
                python,
                str(scripts / "verify_template_invariants.py"),
                "--template",
                str(template),
                "--document",
                str(output_docx),
                "--report",
                str(reports / "verify_template_invariants.json"),
            ],
            reports / "verify_template_invariants.json",
        ),
        ValidatorJob(
            "verify_document_structure",
            [
                python,
                str(scripts / "verify_document_structure.py"),
                str(output_docx),
                "--model",
                product["model"],
                "--full-name",
                product["full_name"],
                "--content-map",
                str(adapter_map_path),
                "--allow-intermediate-name",
                "--report",
                str(reports / "verify_document_structure.json"),
            ],
            reports / "verify_document_structure.json",
        ),
        ValidatorJob(
            "verify_tables",
            [
                python,
                str(scripts / "verify_tables.py"),
                str(output_docx),
                "--report",
                str(reports / "verify_tables.json"),
            ],
            reports / "verify_tables.json",
        ),
        ValidatorJob(
            "scan_source_identity",
            [
                python,
                str(scripts / "scan_source_identity.py"),
                str(output_docx),
                "--terms",
                str(adapter_map_path),
                "--template",
                str(template),
                "--report",
                str(reports / "scan_source_identity.json"),
            ],
            reports / "scan_source_identity.json",
        ),
    ]
    # B 线：在原校验器上传递已批准模板上下文，不把首模板规则套给第二模板。
    if template_pack.get("compiler", {}).get("adapter") == "declarative-docx-v3":
        for job in static_jobs:
            if job.name in {"verify_template_invariants", "verify_document_structure", "verify_tables"}:
                job.command.extend(["--template-pack", f"{template_pack['id']}@{template_pack['version']}"])
            if job.name == "verify_document_structure":
                job.command.extend(["--content-plan", str(content_plan_path)])
    with telemetry.stage("static_validators", input_hashes={"document": sha256_file(output_docx)}, worker_count=workers) as timing:
        static_results = run_validators_parallel(static_jobs, execute, max_workers=workers)
        timing["finding_count"] = sum(len(stage.get("findings", [])) for stage in static_results)
    stages.extend(static_results)
    if any(stage.get("status") != "PASS" for stage in static_results):
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            source=str(source),
            normalized=str(normalized),
            source_inventory=str(inventory_path),
            source_document=str(source_document_path),
            content_decision=str(decision_path),
            content_plan=str(content_plan_path),
            render_plan=str(render_plan_path),
            template_pack={"id": template_pack["id"], "version": template_pack["version"]},
            intermediate_artifacts=[{
                "kind": "DOCX",
                "role": "base",
                "path": str(output_docx),
                "sha256": sha256_file(output_docx),
            }],
        )

    export_report = reports / "export_word_wps.json"
    power_shell = str(powershell_host.path)
    with telemetry.stage("export_word_wps", input_hashes={"document": sha256_file(output_docx)}) as timing:
        export_stage = execute(
            [
                power_shell,
                "-NoProfile",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(scripts / "export_word_wps.ps1"),
                "-InputPath",
                str(output_docx),
                "-OutputDir",
                str(work / "pdf"),
                "-Report",
                str(export_report),
            ],
            export_report,
            timeout=900,
        )
        timing["finding_count"] = len(export_stage.get("findings", []))
    stages.append(export_stage)
    if export_stage.get("status") != "PASS":
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            intermediate_docx=str(output_docx),
            template_pack={"id": template_pack["id"], "version": template_pack["version"]},
        )

    engines = {
        str(entry.get("engine")): entry
        for entry in export_stage.get("engines", [])
        if isinstance(entry, dict) and entry.get("engine")
    }
    if not all(engine in engines and Path(str(engines[engine].get("pdf", ""))).is_file() for engine in ("word", "wps")):
        stages.append(result("FAIL", "export_word_wps_identity", findings=[{"code": "ENGINE_PDF_MISSING"}]))
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            intermediate_docx=str(output_docx),
        )

    analyze_report = reports / "analyze_rendered_pages.json"
    with telemetry.stage("analyze_rendered_pages", input_hashes={
        "word_pdf": sha256_file(Path(engines["word"]["pdf"])),
        "wps_pdf": sha256_file(Path(engines["wps"]["pdf"])),
    }, worker_count=min(2, workers)) as timing:
        analyze_stage = execute(
            [
                python,
                str(scripts / "analyze_rendered_pages.py"),
                "--word-pdf",
                str(engines["word"]["pdf"]),
                "--wps-pdf",
                str(engines["wps"]["pdf"]),
                "--output-dir",
                str(render_dir),
                "--report",
                str(analyze_report),
            ],
            analyze_report,
            timeout=900,
        )
        timing["finding_count"] = len(analyze_stage.get("findings", []))
    stages.append(analyze_stage)
    if analyze_stage.get("status") != "PASS":
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            intermediate_docx=str(output_docx),
            rendered_dir=str(render_dir),
        )

    rendered_identity_jobs: list[ValidatorJob] = []
    for engine_name in delivery_profile.required_renderers:
        identity_report = reports / f"scan_source_identity_rendered_{engine_name}.json"
        rendered_identity_jobs.append(ValidatorJob(
            f"scan_source_identity_rendered_{engine_name}",
            [
                python,
                str(scripts / "scan_source_identity.py"),
                str(output_docx),
                "--terms",
                str(adapter_map_path),
                "--template",
                str(template),
                "--ocr-dir",
                str(render_dir / engine_name),
                "--expected-page-count",
                str(analyze_stage.get(engine_name, {}).get("page_count", 0)),
                "--report",
                str(identity_report),
            ],
            identity_report,
            timeout=900,
        ))
    with telemetry.stage("rendered_identity_scans", input_hashes={"document": sha256_file(output_docx)}, worker_count=workers) as timing:
        rendered_identity = run_validators_parallel(rendered_identity_jobs, execute, max_workers=workers)
        timing["finding_count"] = sum(len(stage.get("findings", [])) for stage in rendered_identity)
    stages.extend(rendered_identity)
    if any(stage.get("status") != "PASS" for stage in rendered_identity):
        return _finish_run(
            report_path=report_path,
            telemetry_path=telemetry_path,
            telemetry=telemetry,
            stages=stages,
            intermediate_docx=str(output_docx),
            rendered_dir=str(render_dir),
        )

    delivery_path = formal_output
    delivery_renderer = delivery_profile.formal_renderer
    delivery_timing = telemetry.begin_stage("final_delivery", input_hashes={
        "intermediate_docx": sha256_file(output_docx),
        "render_plan": sha256_file(render_plan_path),
    })
    if args.delivery_target == "desktop":
        delivery_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output_docx, delivery_path)
    else:
        mobile_pdf = select_primary_pdf(engines, primary_engine="wps")
        if mobile_pdf is None:
            delivery_stage = result("FAIL", "final_delivery", findings=[{"code": "WPS_PDF_MISSING"}])
            stages.append(delivery_stage)
            telemetry.finish_stage(delivery_timing, stage_result=delivery_stage)
            return _finish_run(
                report_path=report_path,
                telemetry_path=telemetry_path,
                telemetry=telemetry,
                stages=stages,
                intermediate_docx=str(output_docx),
            )
        delivery_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mobile_pdf, delivery_path)
        delivery_renderer = "wps_pdf_export"
    delivery_stage = {
        "stage": "final_delivery",
        "status": "PASS",
        "output": str(delivery_path),
        "output_sha256": sha256_file(delivery_path),
        "renderer": delivery_renderer,
        "validation_profile": delivery_profile.validation_profile,
    }
    stages.append(delivery_stage)
    telemetry.finish_stage(
        delivery_timing,
        stage_result=delivery_stage,
        output_hashes={"deliverable": sha256_file(delivery_path)},
    )
    if final_cache_enabled:
        stored_facts = {
            **final_cache_facts,
            "output_sha256": sha256_file(delivery_path),
            "export_engine_identity_sha256": canonical_sha256({
                name: {
                    key: value
                    for key, value in engine.items()
                    if key not in {"pdf", "output", "generated_at"}
                }
                for name, engine in sorted(engines.items())
            }),
        }
        cache_evidence_path = reports / "pass_cache_evidence_v3.json"
        validation_stages = [
            stage
            for stage in stages
            if stage.get("stage") in PASS_CACHE_REQUIRED_STAGES
        ]
        cache_evidence = {
            "schema_version": "pass-cache-evidence-v3",
            "status": "PASS",
            "deliverable": True,
            "facts": stored_facts,
            "output_sha256": sha256_file(delivery_path),
            "validation_stages": validation_stages,
        }
        write_json(cache_evidence_path, cache_evidence)
        try:
            bundle_engines = {
                name: {
                    **engine,
                    "page_count": analyze_stage.get(name, {}).get("page_count", 0),
                }
                for name, engine in engines.items()
            }
            bundle_files, final_member = _pass_bundle_files(
                delivery_path=delivery_path,
                evidence_path=cache_evidence_path,
                output_docx=output_docx,
                source_document_path=source_document_path,
                inventory_path=inventory_path,
                decision_path=decision_path,
                review_receipt_path=review_receipt_path,
                content_plan_path=content_plan_path,
                render_plan_path=render_plan_path,
                reports=reports,
                render_dir=render_dir,
                engines=bundle_engines,
            )
            cache_metadata = cache.store_pass_bundle(
                PASS_CACHE_STAGE,
                final_cache_key,
                bundle_files,
                final_artifact_member=final_member,
                inputs=final_cache_inputs,
                facts=stored_facts,
            )
        except (OSError, ValueError, KeyError) as exc:
            stages.append(result(
                "PASS",
                "final_artifact_cache_store",
                cache_status="SKIPPED",
                reason=str(exc),
            ))
        else:
            stages.append(result(
                "PASS",
                "final_artifact_cache_store",
                cache_status="STORED",
                cache_key=final_cache_key,
                bundle_sha256=cache_metadata["bundle_sha256"],
            ))
    delivery_summary = final_delivery_summary(stages, delivery_profile, delivery_path)
    return _finish_run(
        report_path=report_path,
        telemetry_path=telemetry_path,
        telemetry=telemetry,
        stages=stages,
        status=delivery_summary["status"],
        deliverable=delivery_summary["deliverable"],
        source=str(source),
        source_sha256=sha256_file(source),
        source_format=detected_format,
        normalized=str(normalized),
        source_inventory=str(inventory_path),
        source_document=str(source_document_path),
        content_decision=str(decision_path),
        review_queue=str(review_queue_path),
        content_plan=str(content_plan_path),
        render_plan=str(render_plan_path),
        template_pack={"id": template_pack["id"], "version": template_pack["version"]},
        intermediate_artifacts=[{
            "kind": "DOCX",
            "role": "base",
            "path": str(output_docx),
            "sha256": sha256_file(output_docx),
        }],
        output=str(delivery_path),
        output_sha256=sha256_file(delivery_path),
        rendered_dir=str(render_dir),
        delivery_target=delivery_summary["delivery_target"],
        delivery_format=delivery_summary["delivery_format"],
        deliverable_path=delivery_summary["deliverable_path"],
        delivery_renderer=delivery_renderer,
        validation_profile=delivery_profile.validation_profile,
        delivery=delivery_profile.report(delivery_path, delivery_renderer),
        powershell_host=powershell_report,
    )


if __name__ == "__main__":
    raise SystemExit(main())
