from __future__ import annotations

import argparse
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pipeline_common import finish, result, sha256_file, write_json
from review_v3 import validate_review_receipt
from schema_validation_v3 import load_schema, validate_instance
from template_ir_v3 import TemplateIRError, artifact_sha256, canonical_sha256, validate_docx_template, validate_template_program


TEMPLATE_DIFF_SCHEMA = Path(__file__).resolve().parents[1] / "references" / "schemas" / "template-diff-report-v3.schema.json"


class TemplateRegistrationError(ValueError):
    pass


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateRegistrationError(f"无法读取 JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TemplateRegistrationError(f"JSON 顶层必须是 object: {path}")
    return value


def _safe_draft_file(draft_dir: Path, relative: str, label: str) -> Path:
    path = (draft_dir / relative).resolve()
    try:
        path.relative_to(draft_dir.resolve())
    except ValueError as exc:
        raise TemplateRegistrationError(f"{label} 越出草案目录: {path}") from exc
    if not path.is_file():
        raise TemplateRegistrationError(f"{label} 不存在: {path}")
    return path


def _validate_artifact_hash(payload: dict[str, Any], field: str, label: str) -> None:
    expected = payload.get(field)
    if not isinstance(expected, str) or expected != artifact_sha256(payload, field):
        raise TemplateRegistrationError(f"{label} 内嵌哈希无效")


def _validate_queue_artifacts(draft_dir: Path, draft: dict[str, Any], queue: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = queue.get("binding", {})
    artifacts = draft.get("artifacts", {})
    findings: list[dict[str, Any]] = []
    artifact_bindings = (
        ("template", "template_sha256"),
        ("candidate_output", "candidate_output_sha256"),
        ("diff_report", "diff_report_sha256"),
    )
    for artifact_key, binding_key in artifact_bindings:
        relative = artifacts.get(artifact_key)
        if not isinstance(relative, str) or not relative:
            findings.append({"code": "TEMPLATE_REVIEW_EVIDENCE_INCOMPLETE", "artifact": artifact_key})
            continue
        try:
            path = _safe_draft_file(draft_dir, relative, artifact_key)
        except TemplateRegistrationError as exc:
            findings.append({"code": "TEMPLATE_REVIEW_EVIDENCE_INCOMPLETE", "artifact": artifact_key, "message": str(exc)})
            continue
        actual = sha256_file(path)
        if actual != bindings.get(binding_key):
            findings.append(
                {
                    "code": "REVIEW_RECEIPT_STALE",
                    "field": binding_key,
                    "approved": bindings.get(binding_key),
                    "current": actual,
                }
            )
    return findings


def register_template_pack(draft_dir: Path, receipt_path: Path, catalog_dir: Path) -> dict[str, Any]:
    draft_dir = draft_dir.resolve()
    receipt_path = receipt_path.resolve()
    catalog_dir = catalog_dir.resolve()
    try:
        draft = _read_object(draft_dir / "draft.json")
        receipt = _read_object(receipt_path)
        _validate_artifact_hash(draft, "draft_sha256", "onboarding draft")
        queue_relative = draft.get("artifacts", {}).get("review_queue") or draft.get("review_queue")
        if not isinstance(queue_relative, str) or not queue_relative:
            raise TemplateRegistrationError("onboarding draft 缺少 ReviewQueueV3")
        queue_path = _safe_draft_file(draft_dir, queue_relative, "review_queue")
        queue = _read_object(queue_path)
    except TemplateRegistrationError as exc:
        return result(
            "FAIL",
            "template_register_v3",
            findings=[{"code": "TEMPLATE_DRAFT_INVALID", "message": str(exc)}],
        )

    receipt_result = validate_review_receipt(queue, receipt)
    if receipt_result["status"] != "PASS":
        return {**receipt_result, "stage": "template_register_v3"}
    evidence_findings = _validate_queue_artifacts(draft_dir, draft, queue)
    if evidence_findings:
        return result("HUMAN_REVIEW", "template_register_v3", findings=evidence_findings)

    try:
        artifacts = draft["artifacts"]
        template_path = _safe_draft_file(draft_dir, artifacts["template"], "template")
        ir_path = _safe_draft_file(draft_dir, artifacts["template_ir"], "template_ir")
        slots_path = _safe_draft_file(draft_dir, artifacts["slot_contract"], "slot_contract")
        program_path = _safe_draft_file(draft_dir, artifacts["program"], "program")
        validators_path = _safe_draft_file(draft_dir, artifacts["validators"], "validators")
        candidate_path = _safe_draft_file(draft_dir, artifacts["candidate_output"], "candidate_output")
        diff_path = _safe_draft_file(draft_dir, artifacts["diff_report"], "diff_report")
        ir = _read_object(ir_path)
        slots = _read_object(slots_path)
        program = _read_object(program_path)
        validators = _read_object(validators_path)
        _validate_artifact_hash(ir, "ir_sha256", "Template IR")
        _validate_artifact_hash(slots, "slot_contract_sha256", "Slot Contract")
        validate_template_program(program)
    except (KeyError, TemplateRegistrationError, ValueError) as exc:
        return result(
            "FAIL",
            "template_register_v3",
            findings=[{"code": "TEMPLATE_DRAFT_INVALID", "message": str(exc)}],
        )

    if candidate_path.suffix.lower() != ".docx":
        return result(
            "HUMAN_REVIEW",
            "template_register_v3",
            findings=[{"code": "TEMPLATE_CANDIDATE_OUTPUT_NOT_COMPILED", "path": str(candidate_path)}],
        )
    try:
        validate_docx_template(candidate_path)
        diff_report = _read_object(diff_path)
    except (TemplateIRError, TemplateRegistrationError) as exc:
        return result(
            "HUMAN_REVIEW",
            "template_register_v3",
            findings=[{"code": "TEMPLATE_REVIEW_EVIDENCE_INVALID", "message": str(exc)}],
        )
    diff_errors = validate_instance(diff_report, load_schema(TEMPLATE_DIFF_SCHEMA))
    if diff_errors or diff_report.get("diff_report_sha256") != artifact_sha256(diff_report, "diff_report_sha256"):
        return result(
            "FAIL",
            "template_register_v3",
            findings=[{"code": "TEMPLATE_DIFF_SCHEMA_INVALID", "errors": diff_errors[:10]}],
        )
    if diff_report.get("status") != "PASS" or diff_report.get("findings"):
        return result(
            "HUMAN_REVIEW",
            "template_register_v3",
            findings=[{"code": "TEMPLATE_DIFF_NOT_PASS"}],
        )

    bindings = queue["binding"]
    actual_bindings = {
        "source_sha256": sha256_file(template_path),
        "template_sha256": sha256_file(template_path),
        "template_ir_sha256": ir["ir_sha256"],
        "template_dsl_sha256": canonical_sha256(
            {
                "program_sha256": program["program_sha256"],
                "slot_contract_sha256": slots["slot_contract_sha256"],
            }
        ),
        "compiler_sha256": canonical_sha256(program.get("compiler", {})),
        "candidate_output_sha256": sha256_file(candidate_path),
        "diff_report_sha256": sha256_file(diff_path),
    }
    stale = [
        {"code": "REVIEW_RECEIPT_STALE", "field": field, "approved": bindings.get(field), "current": actual}
        for field, actual in actual_bindings.items()
        if bindings.get(field) != actual
    ]
    if stale:
        return result("HUMAN_REVIEW", "template_register_v3", findings=stale)
    adapter = program.get("adapter")
    declarative_receipt_promotion = adapter == "declarative-docx-v3" and validators.get("review_state") == "REVIEW_REQUIRED"
    if adapter == "review-required" or (validators.get("review_state") != "APPROVED" and not declarative_receipt_promotion):
        return result(
            "HUMAN_REVIEW",
            "template_register_v3",
            findings=[{"code": "TEMPLATE_PROGRAM_NOT_COMPILED_AND_VALIDATED"}],
        )
    validation = draft.get("validation")
    if not isinstance(validation, dict) or validation.get("status") != "PASS" or validation.get("open_findings"):
        return result(
            "HUMAN_REVIEW",
            "template_register_v3",
            findings=[{"code": "TEMPLATE_VALIDATION_NOT_PASS"}],
        )

    proposed = draft.get("proposed_pack", {})
    pack_id = str(proposed.get("id", ""))
    version = str(proposed.get("version", ""))
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", pack_id) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        return result(
            "FAIL",
            "template_register_v3",
            findings=[{"code": "TEMPLATE_PACK_ID_OR_VERSION_INVALID", "id": pack_id, "version": version}],
        )
    target = catalog_dir / pack_id
    if target.exists():
        return result(
            "BLOCKED",
            "template_register_v3",
            findings=[{"code": "TEMPLATE_PACK_ALREADY_EXISTS", "path": str(target)}],
        )

    catalog_dir.mkdir(parents=True, exist_ok=True)
    copies = {
        "template.docx": template_path,
        "template-ir.json": ir_path,
        "slots.json": slots_path,
        "program.json": program_path,
        "validators.json": validators_path,
        "review-queue.json": queue_path,
        "review-receipt.json": receipt_path,
        "candidate-output.docx": candidate_path,
        "diff-report.json": diff_path,
    }
    try:
        with tempfile.TemporaryDirectory(prefix=f".{pack_id}-", dir=catalog_dir) as temporary_directory:
            staging = Path(temporary_directory) / pack_id
            staging.mkdir()
            for name, source in copies.items():
                shutil.copy2(source, staging / name)
            if declarative_receipt_promotion:
                promoted_validators = _read_object(staging / "validators.json")
                promoted_validators["review_state"] = "APPROVED"
                promoted_validators["approval_mechanism"] = "hash_bound_review_receipt"
                write_json(staging / "validators.json", promoted_validators)

            pack_config = {
                "schema_version": "template-pack-v3",
                "id": pack_id,
                "version": version,
                "display_name": draft.get("display_name", pack_id),
                "lifecycle": "RELEASED",
                "template": "template.docx",
                "template_sha256": sha256_file(staging / "template.docx"),
                "template_ir": "template-ir.json",
                "template_ir_file_sha256": sha256_file(staging / "template-ir.json"),
                "slot_contract": "slots.json",
                "slot_contract_file_sha256": sha256_file(staging / "slots.json"),
                "program": "program.json",
                "program_file_sha256": sha256_file(staging / "program.json"),
                "validators": "validators.json",
                "validators_file_sha256": sha256_file(staging / "validators.json"),
                "compiler": {**program["compiler"], "adapter": program["adapter"]},
                "delivery_targets": draft.get(
                    "delivery_targets",
                    {"desktop": {"delivery_format": "DOCX"}, "mobile": {"delivery_format": "PDF"}},
                ),
                "invariants": draft.get(
                    "invariants",
                    {"sections": len(ir.get("page_contract", {}).get("sections", []))},
                ),
                "routing": draft.get("routing", {"candidate_when_unspecified": False, "rules": []}),
                "approval": {
                    "status": "APPROVED",
                    "queue": "review-queue.json",
                    "queue_sha256": sha256_file(staging / "review-queue.json"),
                    "receipt": "review-receipt.json",
                    "receipt_sha256": sha256_file(staging / "review-receipt.json"),
                    "reviewer": receipt["reviewer"],
                    "reviewed_at": receipt["reviewed_at"],
                    "binding": receipt["binding"],
                },
                "validation": validation,
            }
            write_json(staging / "template.json", pack_config)
            staging.replace(target)
    except OSError as exc:
        return result(
            "BLOCKED",
            "template_register_v3",
            findings=[{"code": "TEMPLATE_PACK_REGISTRATION_IO_FAILED", "message": str(exc)}],
        )
    return result(
        "PASS",
        "template_register_v3",
        template_pack={"id": pack_id, "version": version, "reference": f"{pack_id}@{version}", "path": str(target)},
        receipt_sha256=pack_config["approval"]["receipt_sha256"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="验证绑定哈希的人工回执并注册 V3 模板包")
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload = register_template_pack(args.draft, args.receipt, args.catalog)
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
