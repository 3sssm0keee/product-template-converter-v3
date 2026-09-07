from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path
from typing import Any

from pipeline_common import finish, result, sha256_file, utc_now, write_json
from review_preview_v3 import build_template_document_preview
from review_v3 import build_review_queue, write_review_html
from template_candidate_v3 import TemplateCandidateError, compile_template_candidate
from template_ir_v3 import (
    COMPILER_ID,
    COMPILER_VERSION,
    TemplateIRError,
    artifact_sha256,
    build_conservative_program,
    build_conservative_slot_contract,
    build_docx_template_ir,
    canonical_sha256,
)


DRAFT_SCHEMA_VERSION = "template-onboarding-draft-v3"


def propose_pack_id(template: Path, template_sha256: str = "") -> str:
    value = template.stem.lower().strip()
    value = re.sub(r"[^0-9a-z]+", "-", value).strip("-")
    if not value:
        suffix = template_sha256[:12].lower() if template_sha256 else "unresolved"
        value = f"docx-template-{suffix}"
    return f"{value}-v1"


def _draft_payload(
    pack_id: str,
    version: str,
    template_ir: dict[str, Any],
    slot_contract: dict[str, Any],
    program: dict[str, Any],
    review_queue: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": DRAFT_SCHEMA_VERSION,
        "artifact_type": "TemplateOnboardingDraftV3",
        "generated_at": utc_now(),
        "lifecycle": "REVIEW_REQUIRED",
        "proposed_pack": {"id": pack_id, "version": version},
        "artifacts": {
            "template": "template.docx",
            "template_ir": "template-ir.json",
            "slot_contract": "slots.json",
            "program": "program.json",
            "validators": "validators.json",
            "candidate_output": "candidate-output.docx",
            "candidate_ir": "candidate-template-ir.json",
            "diff_report": "diff-report.json",
            "document_preview": "document-preview-v3.json",
            "review_queue": "review-queue.json",
            "review_html": "review.html",
        },
        "review_queue": "review-queue.json",
        "review_id": review_queue["review_id"],
        "review_binding": review_queue["binding"],
    }
    payload["draft_sha256"] = artifact_sha256(payload, "draft_sha256")
    return payload


def create_onboarding_draft(template: Path, output_dir: Path, *, pack_id: str | None = None, version: str = "1.0.0") -> dict[str, Any]:
    template = template.resolve()
    output_dir = output_dir.resolve()
    if template.suffix.lower() != ".docx":
        return result(
            "BLOCKED",
            "template_onboarding_v3",
            findings=[{"code": "UNSUPPORTED_TEMPLATE_FORMAT", "path": str(template), "supported": [".docx"]}],
        )
    try:
        template_ir = build_docx_template_ir(template)
    except TemplateIRError as exc:
        return result(
            "BLOCKED",
            "template_onboarding_v3",
            findings=[{"code": "TEMPLATE_ANALYSIS_FAILED", "path": str(template), "message": str(exc)}],
        )
    blocked = [item for item in template_ir.get("unsupported_objects", []) if item.get("severity") == "BLOCKED"]
    if blocked:
        return result(
            "BLOCKED",
            "template_onboarding_v3",
            findings=[{"code": "UNSUPPORTED_TEMPLATE_OBJECT", **item} for item in blocked],
            template_ir=template_ir,
        )

    resolved_pack_id = pack_id or propose_pack_id(template, template_ir["source_template"]["sha256"])
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", resolved_pack_id):
        return result(
            "FAIL",
            "template_onboarding_v3",
            findings=[{"code": "TEMPLATE_PACK_ID_INVALID", "id": resolved_pack_id}],
        )
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        return result(
            "FAIL",
            "template_onboarding_v3",
            findings=[{"code": "TEMPLATE_PACK_VERSION_INVALID", "version": version}],
        )
    slot_contract = build_conservative_slot_contract(template_ir)
    program = build_conservative_program(template_ir, resolved_pack_id, version)
    validators = {
        "schema_version": "template-validator-dispatch-v3",
        "core": ["document_structure", "tables", "rendered_pages", "identity_scan"],
        "pack": [],
        "review_state": "REVIEW_REQUIRED",
        "approval_mechanism": "hash_bound_review_receipt",
    }

    if (output_dir / "draft.json").exists():
        return result(
            "BLOCKED",
            "template_onboarding_v3",
            findings=[{"code": "ONBOARDING_DRAFT_ALREADY_EXISTS", "path": str(output_dir)}],
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    template_copy = output_dir / "template.docx"
    if template_copy.exists() and sha256_file(template_copy) != template_ir["source_template"]["sha256"]:
        return result(
            "BLOCKED",
            "template_onboarding_v3",
            findings=[{"code": "DRAFT_TEMPLATE_COLLISION", "path": str(template_copy)}],
        )
    if not template_copy.exists():
        shutil.copy2(template, template_copy)
    write_json(output_dir / "template-ir.json", template_ir)
    write_json(output_dir / "slots.json", slot_contract)
    write_json(output_dir / "program.json", program)
    write_json(output_dir / "validators.json", validators)
    document_preview = build_template_document_preview(template_copy, slot_contract, program)
    write_json(output_dir / "document-preview-v3.json", document_preview)
    try:
        candidate_ir, diff_report = compile_template_candidate(
            template_copy,
            template_ir,
            program,
            output_dir / "candidate-output.docx",
        )
    except (TemplateCandidateError, TemplateIRError, ValueError, OSError) as exc:
        return result(
            "BLOCKED",
            "template_onboarding_v3",
            findings=[{"code": "TEMPLATE_CANDIDATE_COMPILE_FAILED", "message": str(exc)}],
            draft_dir=str(output_dir),
        )
    write_json(output_dir / "candidate-template-ir.json", candidate_ir)
    write_json(output_dir / "diff-report.json", diff_report)
    if diff_report["status"] != "PASS" or diff_report["findings"]:
        return result(
            "FAIL",
            "template_onboarding_v3",
            findings=diff_report["findings"],
            draft_dir=str(output_dir),
            diff_report=str(output_dir / "diff-report.json"),
        )
    compiler_sha = canonical_sha256({"id": COMPILER_ID, "version": COMPILER_VERSION})
    dsl_sha = canonical_sha256(
        {
            "program_sha256": program["program_sha256"],
            "slot_contract_sha256": slot_contract["slot_contract_sha256"],
        }
    )
    binding = {
        "source_sha256": template_ir["source_template"]["sha256"],
        "template_sha256": template_ir["source_template"]["sha256"],
        "template_ir_sha256": template_ir["ir_sha256"],
        "template_dsl_sha256": dsl_sha,
        "compiler_sha256": compiler_sha,
        "candidate_output_sha256": sha256_file(output_dir / "candidate-output.docx"),
        "diff_report_sha256": sha256_file(output_dir / "diff-report.json"),
    }
    evidence_refs = [
        {
            "evidence_id": "TEMPLATE-SOURCE",
            "label": "原始 DOCX 模板",
            "kind": "template",
            "path": "template.docx",
            "sha256": binding["template_sha256"],
        },
        {
            "evidence_id": "TEMPLATE-IR",
            "label": "Template IR",
            "kind": "template_ir",
            "path": "template-ir.json",
            "sha256": sha256_file(output_dir / "template-ir.json"),
        },
        {
            "evidence_id": "TEMPLATE-DSL",
            "label": "Template DSL",
            "kind": "template_dsl",
            "path": "program.json",
            "sha256": sha256_file(output_dir / "program.json"),
        },
        {
            "evidence_id": "CANDIDATE-OUTPUT",
            "label": "固定编译器重建候选 DOCX",
            "kind": "candidate_output",
            "path": "candidate-output.docx",
            "sha256": binding["candidate_output_sha256"],
        },
        {
            "evidence_id": "CANDIDATE-IR",
            "label": "重建候选 Template IR",
            "kind": "candidate_template_ir",
            "path": "candidate-template-ir.json",
            "sha256": sha256_file(output_dir / "candidate-template-ir.json"),
        },
        {
            "evidence_id": "DIFF-REPORT",
            "label": "结构与视觉代理差异报告",
            "kind": "diff_report",
            "path": "diff-report.json",
            "sha256": binding["diff_report_sha256"],
        },
        {
            "evidence_id": "DOCUMENT-PREVIEW",
            "label": "面向业务用户的拟生成 DOCX 结构预览",
            "kind": "document_preview",
            "path": "document-preview-v3.json",
            "sha256": sha256_file(output_dir / "document-preview-v3.json"),
        },
    ]
    review_queue = build_review_queue(
        "template_onboarding",
        binding,
        [
            {
                "review_item_id": "TEMPLATE-SEMANTIC-SLOTS",
                "category": "TEMPLATE",
                "title": "确认模板槽位和内容增长方向",
                "conclusion": "当前槽位由确定性分析生成，必须由人工确认业务语义。",
                "risk_level": "HIGH",
                "source_ids": [],
                "evidence_ids": ["TEMPLATE-SOURCE", "TEMPLATE-IR", "TEMPLATE-DSL", "DOCUMENT-PREVIEW"],
                "details": {
                    "recognized_conclusion": "待人工确认文本、图片、表格和重复块。",
                    "template_ir": "template-ir.json",
                    "dsl_diff": "program.json",
                    "preview_paths": [],
                    "diff_overlay_paths": [],
                    "notes": "保守 DSL 仍为 review-required，不可直接注册。",
                },
            },
            {
                "review_item_id": "TEMPLATE-COMPILED-PREVIEW",
                "category": "TEMPLATE",
                "title": "检查编译预览和差异叠图",
                "conclusion": "固定编译器已生成真实候选 DOCX；结构、页面合同、样式、媒体视觉指纹及 Drawing 必须逐项人工复核。",
                "risk_level": "CRITICAL",
                "source_ids": [],
                "evidence_ids": ["CANDIDATE-OUTPUT", "CANDIDATE-IR", "DIFF-REPORT"],
                "details": {
                    "recognized_conclusion": "确定性重建比较已通过；Office 双引擎页面预览仍属于正式注册前独立质量门禁。",
                    "template_ir": "template-ir.json",
                    "dsl_diff": "program.json",
                    "preview_paths": ["candidate-output.docx"],
                    "diff_overlay_paths": ["diff-report.json"],
                    "notes": "候选不再是占位文件，但保守 DSL 的业务槽位语义和双引擎页面证据仍不得由代码自动批准。",
                },
            },
        ],
        evidence_refs=evidence_refs,
        document_preview=document_preview,
    )
    write_json(output_dir / "review-queue.json", review_queue)
    write_review_html(review_queue, output_dir / "review.html")
    draft = _draft_payload(resolved_pack_id, version, template_ir, slot_contract, program, review_queue)
    write_json(output_dir / "draft.json", draft)
    return result(
        "HUMAN_REVIEW",
        "template_onboarding_v3",
        findings=[{"code": "TEMPLATE_REVIEW_REQUIRED", "review_item_count": len(review_queue["items"])}],
        draft_dir=str(output_dir),
        draft=str(output_dir / "draft.json"),
        draft_sha256=draft["draft_sha256"],
        review_queue=str(output_dir / "review-queue.json"),
        review_html=str(output_dir / "review.html"),
        review_id=review_queue["review_id"],
        template_sha256=template_ir["source_template"]["sha256"],
        proposed_pack=draft["proposed_pack"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="将未知 DOCX 模板分析为 V3 IR/DSL 审核草案")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--pack-id")
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload = create_onboarding_draft(args.template, args.output_dir, pack_id=args.pack_id, version=args.version)
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
