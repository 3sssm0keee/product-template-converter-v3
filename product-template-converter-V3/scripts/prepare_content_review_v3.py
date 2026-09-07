from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from decision_bundle_v3 import _source_items, source_document_sha256, validate_content_decision
from pipeline_common import finish, read_json, result, sha256_file, write_json
from review_preview_assets_v3 import materialize_preview_images
from review_preview_v3 import _block, _location_label, _source_material, build_content_document_preview
from review_v3 import ReviewError, build_review_queue, validate_review_receipt, write_review_html
from run_pipeline import _content_review_binding, _content_review_items, _decision_requires_content_review
from template_catalog_v3 import resolve_pack_reference


ROOT = Path(__file__).resolve().parents[1]
COMPILER = ROOT / "scripts" / "fixed_template_compiler_v3.py"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


# D13：隔离删除后 481 项测试通过，但涉及路径遍历防护，安全审查要求保留。
# 实际图片处理仍使用 review_preview_assets_v3；此候选等待额外安全确认。




def _safe_package_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix.lower() not in IMAGE_EXTENSIONS
    ):
        raise ReviewError(f"unsafe image package path: {value}")
    return normalized


def prepare_content_review(
    *,
    source_document_path: Path,
    decision_path: Path,
    template_pack_reference: str,
    output_dir: Path,
    receipt_path: Path | None = None,
    include_unresolved: bool = False,
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        return result(
            "BLOCKED",
            "prepare_content_review_v3",
            findings=[{
                "code": "CONTENT_REVIEW_OUTPUT_NOT_EMPTY",
                "path": str(output_dir),
                "message": "Use a new empty directory so stale review evidence cannot be mixed into the queue.",
            }],
            deliverable=False,
            conversion_executed=False,
        )
    try:
        source_document = read_json(source_document_path)
        decision = read_json(decision_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return result(
            "FAIL",
            "prepare_content_review_v3",
            findings=[{"code": "CONTENT_REVIEW_INPUT_INVALID", "message": str(exc)}],
            deliverable=False,
            conversion_executed=False,
        )

    resolver = resolve_pack_reference(ROOT, template_pack_reference)
    if resolver.get("status") != "PASS":
        return result(
            str(resolver.get("status") or "HUMAN_REVIEW"),
            "prepare_content_review_v3",
            findings=list(resolver.get("findings") or []),
            deliverable=False,
            conversion_executed=False,
        )
    template_pack = resolver["template_pack"]
    expected_source_ids = [str(item.get("source_id")) for item in _source_items(source_document)]
    decision_stage = validate_content_decision(
        decision,
        task_bundles=None,
        expected_source_ids=expected_source_ids,
    )
    pending_review = bool(include_unresolved and decision.get("unresolved_items"))
    if decision_stage.get("status") == "FAIL" or (
        decision_stage.get("status") == "HUMAN_REVIEW"
        and not _decision_requires_content_review(decision_stage)
        and not pending_review
    ):
        return result(
            str(decision_stage.get("status") or "FAIL"),
            "prepare_content_review_v3",
            findings=list(decision_stage.get("findings") or []),
            decision_validation=decision_stage,
            deliverable=False,
            conversion_executed=False,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    validation_path = output_dir / "content_decision_validation_v3.json"
    preview_path = output_dir / "document-preview-v3.json"
    queue_path = output_dir / "content_review_queue_v3.json"
    html_path = output_dir / "content_review.html"
    stable_validation = {key: value for key, value in decision_stage.items() if key != "generated_at"}
    write_json(validation_path, stable_validation)
    try:
        preview = build_content_document_preview(source_document, decision, template_pack)
        review_items = _content_review_items(source_document, decision)
        if pending_review:
            # 消融 B-N1：省略本段后真实 PPTX 预览仅余 8 块而非 50 块，故保留此入口。
            # 沿用现有预览/回执合同收集修订；不把未决源项伪装成已映射内容。
            by_id = {str(item.get("source_id")): item for item in _source_items(source_document)}
            blocks = []
            pending_ids = []
            for unresolved in decision["unresolved_items"]:
                source_id = unresolved["source_id"]
                if source_id not in by_id:
                    raise ReviewError(f"unresolved source item not found: {source_id}")
                item = by_id[source_id]
                kind, text, rows, image = _source_material(item)
                blocks.append(_block(
                    kind=kind, label=f"{_location_label(item)} · {source_id}",
                    preview_text=text, source_rule=_location_label(item),
                    transformation="待确认保留/删除、内容修订与目标位置；未写入成品。",
                    growth="位置确认后再按模板排版", table_rows=rows, image=image,
                ))
                pending_ids.append(source_id)
            preview.update({
                "title": "待决源内容 · 人工编辑与图片核验",
                "subtitle": "可编辑文字、选择并批注图片，再导出审核回执。此页收集内容意见，不是转换成品；批准不会跳过未决映射。",
                "summary": f"目标模板 {template_pack['id']}；共 {len(blocks)} 个源项等待内容取舍与位置确认。",
                "string_rules": [],
                "pages": [{"page_role": "body", "title": "待确认的原文与图片", "badge": "尚未映射",
                           "summary": "请直接修改文字；图片选择和批注位于下方人工复核事项中。",
                           "sections": [{"title": "源内容核验", "purpose": "确认内容而非内部槽位代码",
                                         "layout": "源项顺序，不代表成品分页", "blocks": blocks}]}],
            })
            review_items.insert(0, {
                "review_item_id": "CONTENT-UNRESOLVED", "category": "CONTENT",
                "title": "确认待决内容、图片和目标位置", "risk_level": "HIGH",
                "conclusion": "请编辑上方原文，逐图选择/双击批注，并在意见中说明保留、删除或目标章节。",
                "source_ids": pending_ids,
                "evidence_ids": ["SOURCE-DOCUMENT", "CONTENT-DECISION", "DECISION-VALIDATION", "DOCUMENT-PREVIEW"],
                "details": {"notes": "本回执只记录本轮意见。未决内容仍须落实为决策并重新校验，不能直接解锁交付。"},
            })
        materialize_preview_images(source_document, preview, output_dir)
        write_json(preview_path, preview)
        semantic_source_sha = source_document_sha256(source_document)
        queue = build_review_queue(
            "content_decision",
            _content_review_binding(
                source_document,
                decision_path,
                validation_path,
                template_pack,
                COMPILER,
            ),
            review_items,
            evidence_refs=[
                {
                    "evidence_id": "SOURCE-DOCUMENT",
                    "label": "SourceDocumentV3（业务语义内容）",
                    "kind": "source_document_semantic",
                    "path": str(source_document_path),
                    "sha256": semantic_source_sha,
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
                    "path": str(validation_path),
                    "sha256": sha256_file(validation_path),
                },
                {
                    "evidence_id": "DOCUMENT-PREVIEW",
                    "label": "面向业务用户的拟生成 DOCX 内容预览",
                    "kind": "document_preview",
                    "path": str(preview_path),
                    "sha256": sha256_file(preview_path),
                },
            ],
            document_preview=preview,
        )
        write_json(queue_path, queue)
        write_review_html(queue, html_path)
        receipt = read_json(receipt_path) if receipt_path and receipt_path.is_file() else None
        review_validation = validate_review_receipt(queue, receipt)
    except (ReviewError, OSError, ValueError, json.JSONDecodeError) as exc:
        return result(
            "FAIL",
            "prepare_content_review_v3",
            findings=[{"code": "CONTENT_REVIEW_BUILD_FAILED", "message": str(exc)}],
            deliverable=False,
            conversion_executed=False,
        )

    findings = list(review_validation.get("findings") or [])
    if pending_review:
        findings.extend(decision_stage.get("findings") or [])
    return result(
        "PASS" if review_validation.get("status") == "PASS" and not pending_review else "HUMAN_REVIEW",
        "prepare_content_review_v3",
        findings=findings,
        review_id=queue["review_id"],
        template_pack={"id": template_pack["id"], "version": template_pack["version"]},
        source_document=str(source_document_path),
        source_document_semantic_sha256=semantic_source_sha,
        content_decision=str(decision_path),
        decision_validation=str(validation_path),
        document_preview=str(preview_path),
        review_queue=str(queue_path),
        review_html=str(html_path),
        review_validation=review_validation,
        deliverable=False,
        conversion_executed=False,
        authorization_bypassed=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="离线生成面向业务用户的 V3 拟生成 DOCX 内容复核页；不执行转换或交付")
    parser.add_argument("--source-document", type=Path, required=True)
    parser.add_argument("--content-decision", type=Path, required=True)
    parser.add_argument("--template-pack", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--review-receipt", type=Path)
    parser.add_argument("--include-unresolved", action="store_true", help="为待决源项生成可编辑核验页，仅收集意见，不解除决策门禁")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = prepare_content_review(
        source_document_path=args.source_document.resolve(),
        decision_path=args.content_decision.resolve(),
        template_pack_reference=args.template_pack,
        output_dir=args.output_dir.resolve(),
        receipt_path=args.review_receipt.resolve() if args.review_receipt else None,
        include_unresolved=args.include_unresolved,
    )
    return finish(report, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
