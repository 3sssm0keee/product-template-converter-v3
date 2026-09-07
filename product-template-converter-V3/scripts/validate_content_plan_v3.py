from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from pipeline_common import finish, read_json, result, sha256_file
from validate_content_map import image_visual_sha256, normalize_text, output_evidence
from v3_common import canonical_json_sha256


PRESERVING_ACTIONS = {
    "preserve_exact",
    "preserve_image",
    "preserve_sanitized_image",
    "redact_identity",
    "reviewed_text",
}
NON_OUTPUT_ACTIONS = {
    "exclude_from_output",
    "remove_identity",
    "remove_template_background",
    "ignore_non_content",
    "human_review",
}
PRODUCT_METADATA_SLOTS = {"product.model", "product.full_name"}
SECTION_SLOTS = {
    "section.introduction",
    "section.functions",
    "section.advantages",
    "section.scenarios",
    "section.parameters",
    "section.qualifications",
}
INDEX_COLUMN_HEADERS = {"序号", "编号", "序列", "no", "no.", "index"}


def _payload(source: dict[str, Any]) -> dict[str, Any]:
    payload = source.get("payload")
    return payload if isinstance(payload, dict) else source


def _expected_text(source: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    action = decision.get("action")
    if action == "reviewed_text":
        blocks = decision.get("reviewed_blocks")
        if isinstance(blocks, list):
            return [normalize_text(str(block.get("text", ""))) for block in blocks if isinstance(block, dict)]
        return [normalize_text(str(decision.get("reviewed_text", "")))]
    text = str(_payload(source).get("text", ""))
    if action == "redact_identity":
        for value in decision.get("redactions", []) if isinstance(decision.get("redactions"), list) else []:
            text = text.replace(str(value), "")
    return [normalize_text(text)]


def _section_title_token(text: str) -> str:
    return normalize_text(str(text or "")).replace("▌", "").replace("▎", "").strip()


def _text_consumed_by_template(source: dict[str, Any], decision: dict[str, Any]) -> bool:
    target = str(decision.get("target_slot") or decision.get("target_section") or "")
    if target in PRODUCT_METADATA_SLOTS:
        return True
    if target not in SECTION_SLOTS:
        return False
    if source.get("kind") != "text":
        return False
    context = source.get("context") if isinstance(source.get("context"), dict) else {}
    style = str(context.get("style") or source.get("style") or "").casefold()
    if "heading" not in style:
        return False
    text = _section_title_token(_payload(source).get("text", ""))
    return bool(text) and bool(re.match(r"^[一二三四五六七八九十]+[、.．]", text))


def _table_cell_consumed_by_template(rows: list[Any], row_index: int, column_index: int) -> bool:
    if column_index != 1 or not rows or not isinstance(rows[0], list):
        return False
    header = normalize_text(str(rows[0][0] if rows[0] else "")).casefold()
    if header not in INDEX_COLUMN_HEADERS:
        return False
    if row_index == 1:
        return True
    row = rows[row_index - 1] if row_index - 1 < len(rows) and isinstance(rows[row_index - 1], list) else []
    value = normalize_text(str(row[0] if row else ""))
    return bool(re.fullmatch(r"\d+", value))


def validate_content_plan_v3(
    content_plan: dict[str, Any],
    *,
    content_plan_path: Path | None = None,
    render_plan: dict[str, Any] | None = None,
    document: Path | None = None,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    review: list[str] = []
    if content_plan.get("schema_version") != "content-plan-v3":
        findings.append({"code": "CONTENT_PLAN_SCHEMA_VERSION_INVALID", "actual": content_plan.get("schema_version")})
    canonical = str(content_plan.get("canonical_sha256", "")).upper()
    unsigned = {key: value for key, value in content_plan.items() if key != "canonical_sha256"}
    expected_canonical = canonical_json_sha256(unsigned)
    if not canonical:
        findings.append({"code": "CONTENT_PLAN_CANONICAL_SHA256_MISSING"})
    elif canonical != expected_canonical:
        findings.append({"code": "CONTENT_PLAN_CANONICAL_SHA256_MISMATCH", "expected": expected_canonical, "actual": canonical})

    normalized = content_plan.get("normalized_source") if isinstance(content_plan.get("normalized_source"), dict) else {}
    normalized_path = Path(str(normalized.get("path", ""))) if normalized.get("path") else None
    if normalized_path is None or not normalized_path.is_file():
        findings.append({"code": "CONTENT_PLAN_NORMALIZED_SOURCE_MISSING", "path": str(normalized_path or "")})
    elif sha256_file(normalized_path) != str(normalized.get("sha256", "")).upper():
        findings.append({"code": "CONTENT_PLAN_NORMALIZED_SOURCE_SHA256_MISMATCH", "path": str(normalized_path)})

    items = content_plan.get("items")
    if not isinstance(items, list) or not items:
        findings.append({"code": "CONTENT_PLAN_ITEMS_MISSING"})
        items = []
    source_ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            findings.append({"code": "CONTENT_PLAN_ITEM_INVALID"})
            continue
        source_id = str(item.get("source_id", ""))
        source_ids.append(source_id)
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        decision = item.get("decision") if isinstance(item.get("decision"), dict) else {}
        if not source_id or source_id != str(source.get("source_id") or source.get("id") or ""):
            findings.append({"code": "CONTENT_PLAN_SOURCE_ID_MISMATCH", "source_id": source_id})
        if source_id != str(decision.get("source_id", "")):
            findings.append({"code": "CONTENT_PLAN_DECISION_SOURCE_ID_MISMATCH", "source_id": source_id})
        action = str(decision.get("action", ""))
        if action not in PRESERVING_ACTIONS | NON_OUTPUT_ACTIONS:
            findings.append({"code": "CONTENT_PLAN_ACTION_INVALID", "source_id": source_id, "action": action})
        if action == "human_review" or decision.get("review_required") is True:
            review.append(source_id)
        evidence = decision.get("evidence_ids") or decision.get("evidence_refs")
        if action in PRESERVING_ACTIONS and (not isinstance(evidence, list) or not evidence):
            findings.append({"code": "CONTENT_PLAN_EVIDENCE_MISSING", "source_id": source_id})
    if len(source_ids) != len(set(source_ids)):
        findings.append({"code": "CONTENT_PLAN_SOURCE_ID_DUPLICATE"})

    if render_plan is not None:
        if render_plan.get("schema_version") != "render-plan-v3":
            findings.append({"code": "RENDER_PLAN_SCHEMA_VERSION_INVALID"})
        content_ref = render_plan.get("upstream", {}).get("content_plan", {}) if isinstance(render_plan.get("upstream"), dict) else {}
        if content_plan_path is not None:
            if str(content_ref.get("sha256", "")).upper() != sha256_file(content_plan_path):
                findings.append({"code": "RENDER_PLAN_CONTENT_PLAN_SHA256_MISMATCH"})
        pack = content_plan.get("template_pack") if isinstance(content_plan.get("template_pack"), dict) else {}
        render_pack = render_plan.get("template_pack") if isinstance(render_plan.get("template_pack"), dict) else {}
        if (pack.get("id"), str(pack.get("version"))) != (render_pack.get("id"), str(render_pack.get("version"))):
            findings.append({"code": "CONTENT_RENDER_TEMPLATE_PACK_MISMATCH"})

    if document is not None:
        if not document.is_file():
            findings.append({"code": "OUTPUT_DOCUMENT_MISSING", "path": str(document)})
        else:
            try:
                output_text, output_hashes, output_visual_hashes = output_evidence(document)
                product_bindings = {}
                program_ref = (render_plan or {}).get("upstream", {}).get("template_program", {})
                if program_ref:
                    program_path = Path(program_ref["path"])
                    if sha256_file(program_path) != program_ref.get("sha256"):
                        raise ValueError("Template Program 字节哈希失效")
                    program = read_json(program_path)
                    if program.get("template_pack") != content_plan.get("template_pack"):
                        # ContentPlan 带路径等附加字段时只比较注册身份。
                        identity = content_plan.get("template_pack", {})
                        if program.get("template_pack") != {"id": identity.get("id"), "version": identity.get("version")}:
                            raise ValueError("Template Program 模板身份不一致")
                    for op in program.get("operations", []):
                        source = str(op.get("source", ""))
                        if op.get("op") == "bind_slot" and source.startswith("content_plan.product."):
                            key = source.removeprefix("content_plan.product.")
                            value = str(content_plan.get("product", {}).get(key) or "")
                            if value:
                                product_bindings[op["slot_id"]] = normalize_text(value)
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    source_id = str(item.get("source_id", ""))
                    source = item.get("source") if isinstance(item.get("source"), dict) else {}
                    decision = item.get("decision") if isinstance(item.get("decision"), dict) else {}
                    action = decision.get("action")
                    kind = source.get("kind")
                    if action in {"preserve_exact", "redact_identity", "reviewed_text"} and kind == "text":
                        bound_product = product_bindings.get(decision.get("target_slot"))
                        if bound_product:
                            if bound_product not in output_text:
                                findings.append({"code": "CONTENT_PLAN_PRODUCT_MISSING_FROM_OUTPUT", "source_id": source_id, "text": bound_product})
                            continue
                        if _text_consumed_by_template(source, decision):
                            continue
                        for expected in _expected_text(source, decision):
                            if expected and expected not in output_text:
                                findings.append({"code": "CONTENT_PLAN_TEXT_MISSING_FROM_OUTPUT", "source_id": source_id, "text": expected})
                    elif action == "preserve_exact" and kind == "table":
                        rows = _payload(source).get("rows", [])
                        for row_index, row in enumerate(rows, 1):
                            for column_index, cell in enumerate(row if isinstance(row, list) else [], 1):
                                if _table_cell_consumed_by_template(rows, row_index, column_index):
                                    continue
                                value = normalize_text(str(cell))
                                if value and value not in output_text:
                                    findings.append({"code": "CONTENT_PLAN_TABLE_CELL_MISSING_FROM_OUTPUT", "source_id": source_id, "row": row_index, "column": column_index})
                    elif action in {"preserve_image", "preserve_sanitized_image"} and kind == "image":
                        source_payload = _payload(source)
                        expected_sha = str(
                            (
                                decision.get("replacement_ref", {}).get("sha256")
                                if action == "preserve_sanitized_image" and isinstance(decision.get("replacement_ref"), dict)
                                else source_payload.get("sha256", "")
                            )
                        ).upper()
                        expected_visual = str(source_payload.get("visual_sha256", "")).upper()
                        if expected_sha not in output_hashes and (not expected_visual or expected_visual not in output_visual_hashes):
                            findings.append({"code": "CONTENT_PLAN_IMAGE_MISSING_FROM_OUTPUT", "source_id": source_id, "sha256": expected_sha})
            except Exception as exc:
                findings.append({"code": "CONTENT_PLAN_OUTPUT_EVIDENCE_FAILED", "message": str(exc)})

    status = "FAIL" if findings else "HUMAN_REVIEW" if review else "PASS"
    return result(
        status,
        "validate_content_plan_v3",
        findings=findings,
        source_count=len(items),
        mapped_count=len(source_ids),
        human_review=review,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 ContentPlanV3 与 RenderPlanV3 的完整覆盖和真实输出")
    parser.add_argument("content_plan", type=Path)
    parser.add_argument("--render-plan", type=Path)
    parser.add_argument("--document", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        content_plan = read_json(args.content_plan)
        render_plan = read_json(args.render_plan) if args.render_plan else None
        payload = validate_content_plan_v3(
            content_plan,
            content_plan_path=args.content_plan.resolve(),
            render_plan=render_plan,
            document=args.document.resolve() if args.document else None,
        )
    except Exception as exc:
        payload = result("FAIL", "validate_content_plan_v3", findings=[{"code": "CONTENT_PLAN_VALIDATION_FAILED", "message": str(exc)}])
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
