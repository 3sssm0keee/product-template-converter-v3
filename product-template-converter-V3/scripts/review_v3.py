from __future__ import annotations

import argparse
from html import escape as html_escape
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from pipeline_common import finish, read_json, result, sha256_file, utc_now, write_json
from schema_validation_v3 import load_schema, validate_instance

from decision_bundle_v3 import canonical_sha256, source_document_sha256
from review_annotation_port_v3 import build_review_annotation_port


SCHEMA_VERSION = "3.0"
REVIEW_UI_VERSION = "3.1.3"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = REPOSITORY_ROOT / "references" / "schemas"
REVIEW_QUEUE_SCHEMA_PATH = SCHEMA_ROOT / "review-queue-v3.schema.json"
REVIEW_RECEIPT_SCHEMA_PATH = SCHEMA_ROOT / "review-receipt-v3.schema.json"
REVIEW_BINDING_FIELDS = (
    "source_sha256",
    "template_sha256",
    "template_ir_sha256",
    "template_dsl_sha256",
    "compiler_sha256",
    "candidate_output_sha256",
    "diff_report_sha256",
)
HIDDEN_REVIEWER_BINDING_FIELD = "hidden_reviewer_sha256"
ITEM_ACTIONS = {"APPROVE", "REJECT", "REQUEST_REVISION", "FACT_PENDING"}
VOLATILE_REBIND_BINDING_FIELDS = {"diff_report_sha256"}
VOLATILE_REBIND_EVIDENCE_IDS = {"SOURCE-DOCUMENT", "DECISION-VALIDATION"}


class ReviewError(ValueError):
    pass


def _is_safe_local_reference(value: Any) -> bool:
    raw = str(value or "").strip()
    if not raw or any(ord(char) < 32 for char in raw):
        return False
    if raw.startswith(("//", "\\\\")):
        return False
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        return True
    scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", raw)
    if scheme:
        return scheme.group(1).casefold() == "file" and raw.casefold().startswith("file:///")
    return True


def _schema_errors(payload: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    return validate_instance(payload, load_schema(path))


def _normalize_binding(binding: dict[str, Any]) -> dict[str, str]:
    normalized = {name: str(binding.get(name) or "").strip().upper() for name in REVIEW_BINDING_FIELDS}
    hidden_reviewer_sha256 = str(binding.get(HIDDEN_REVIEWER_BINDING_FIELD) or "").strip().upper()
    if hidden_reviewer_sha256:
        normalized[HIDDEN_REVIEWER_BINDING_FIELD] = hidden_reviewer_sha256
    return normalized


def _normalize_hidden_reviewer(value: dict[str, Any] | None) -> dict[str, str] | None:
    if value is None:
        return None
    reviewer_id = str(value.get("reviewer_id") or "").strip()
    reviewer_role = str(value.get("reviewer_role") or "").strip()
    if not reviewer_id or not reviewer_role:
        raise ReviewError("hidden reviewer requires non-empty reviewer_id and reviewer_role")
    if any(ord(char) < 32 for char in reviewer_id + reviewer_role):
        raise ReviewError("hidden reviewer contains control characters")
    return {"reviewer_id": reviewer_id, "reviewer_role": reviewer_role}


def _normalize_details(value: Any) -> dict[str, Any]:
    details = value if isinstance(value, dict) else {}
    return {
        "recognized_conclusion": str(details.get("recognized_conclusion") or ""),
        "template_ir": str(details.get("template_ir") or ""),
        "dsl_diff": str(details.get("dsl_diff") or ""),
        "preview_paths": [str(path) for path in details.get("preview_paths", []) if _is_safe_local_reference(path)]
        if isinstance(details.get("preview_paths"), list)
        else [],
        "diff_overlay_paths": [str(path) for path in details.get("diff_overlay_paths", []) if _is_safe_local_reference(path)]
        if isinstance(details.get("diff_overlay_paths"), list)
        else [],
        "notes": str(details.get("notes") or ""),
    }


def _review_identity_material(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the stable, human-reviewable identity of a review queue.

    ``created_at`` and local evidence paths describe when and where a queue was
    materialized.  They do not change the evidence bytes, the document preview,
    or the decision being approved, so they must not invalidate a receipt when
    the same task is rerun in another output directory.
    """

    material = {
        key: deepcopy(value)
        for key, value in payload.items()
        if key not in {"review_id", "created_at"}
    }
    evidence_refs = material.get("evidence_refs")
    if isinstance(evidence_refs, list):
        material["evidence_refs"] = [
            {key: value for key, value in evidence.items() if key != "path"}
            if isinstance(evidence, dict)
            else evidence
            for evidence in evidence_refs
        ]
    return material


def _approval_content_identity_material(payload: dict[str, Any]) -> dict[str, Any]:
    material = _review_identity_material(payload)
    contract = material.get("review_contract")
    if isinstance(contract, dict):
        material["review_contract"] = {
            key: value for key, value in contract.items() if key != "ui_version"
        }
    items = material.get("items")
    if isinstance(items, list):
        normalized_items = []
        for item in items:
            normalized_item = deepcopy(item)
            details = normalized_item.get("details") if isinstance(normalized_item, dict) else None
            if isinstance(details, dict):
                details.pop("preview_paths", None)
                details.pop("diff_overlay_paths", None)
            normalized_items.append(normalized_item)
        material["items"] = normalized_items
    return material


def build_review_queue(
    review_type: str,
    binding: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    evidence_refs: list[dict[str, Any]] | None = None,
    document_preview: dict[str, Any] | None = None,
    created_at: str | None = None,
    hidden_reviewer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_items: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        review_item_id = str(item.get("review_item_id") or f"ITEM-{index:04d}")
        normalized_items.append({
            "review_item_id": review_item_id,
            "category": str(item.get("category") or "CONTENT").upper(),
            "title": str(item.get("title") or review_item_id),
            "conclusion": str(item.get("conclusion") or "待人工复核"),
            "risk_level": str(item.get("risk_level") or "MEDIUM").upper(),
            "source_ids": sorted({str(value) for value in item.get("source_ids", []) if str(value)})
            if isinstance(item.get("source_ids"), list)
            else [],
            "evidence_ids": sorted({str(value) for value in item.get("evidence_ids", []) if str(value)})
            if isinstance(item.get("evidence_ids"), list)
            else [],
            "details": _normalize_details(item.get("details")),
        })
    ids = [item["review_item_id"] for item in normalized_items]
    if len(ids) != len(set(ids)):
        raise ReviewError("review_item_id must be unique")
    normalized_evidence: list[dict[str, Any]] = []
    for value in evidence_refs or []:
        if not isinstance(value, dict) or not _is_safe_local_reference(value.get("path")):
            raise ReviewError("evidence path must be a safe local reference")
        normalized_evidence.append({
            "evidence_id": str(value.get("evidence_id") or ""),
            "label": str(value.get("label") or value.get("evidence_id") or "evidence"),
            "kind": str(value.get("kind") or "evidence"),
            "path": str(value.get("path") or ""),
            "sha256": str(value.get("sha256") or "").strip().upper(),
        })
    evidence_ids = [value["evidence_id"] for value in normalized_evidence]
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ReviewError("evidence_id must be unique")
    known_evidence = set(evidence_ids)
    unknown_evidence = sorted({evidence_id for item in normalized_items for evidence_id in item["evidence_ids"] if evidence_id not in known_evidence})
    if unknown_evidence:
        raise ReviewError(f"review items reference unknown evidence: {unknown_evidence}")

    normalized_hidden_reviewer = _normalize_hidden_reviewer(hidden_reviewer)
    normalized_binding = _normalize_binding(binding)
    if normalized_hidden_reviewer is not None:
        normalized_binding[HIDDEN_REVIEWER_BINDING_FIELD] = canonical_sha256(normalized_hidden_reviewer)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "review_id": "",
        "review_type": review_type,
        "status": "REVIEW_REQUIRED",
        "created_at": created_at or utc_now(),
        "review_contract": {
            "ui_version": REVIEW_UI_VERSION,
            "receipt_schema_sha256": sha256_file(REVIEW_RECEIPT_SCHEMA_PATH),
        },
        "binding": normalized_binding,
        "items": normalized_items,
        "evidence_refs": normalized_evidence,
    }
    if document_preview is not None:
        payload["document_preview"] = deepcopy(document_preview)
    if normalized_hidden_reviewer is not None:
        payload["hidden_reviewer"] = normalized_hidden_reviewer
    payload["review_id"] = f"REVIEW-{canonical_sha256(_review_identity_material(payload))}"
    errors = _schema_errors(payload, REVIEW_QUEUE_SCHEMA_PATH)
    if errors:
        raise ReviewError(f"ReviewQueueV3 is invalid: {errors[:3]}")
    return payload


def bind_hidden_reviewer(
    queue: dict[str, Any],
    hidden_reviewer: dict[str, Any],
) -> dict[str, Any]:
    """Create a new queue whose review identity is bound to one local reviewer.

    This intentionally creates a new review ID.  It never rewrites an existing
    receipt, so a prior approval cannot be silently carried into the stronger
    identity-binding contract.
    """
    errors = _schema_errors(queue, REVIEW_QUEUE_SCHEMA_PATH)
    if errors:
        raise ReviewError(f"cannot bind invalid ReviewQueueV3: {errors[:3]}")
    normalized_hidden_reviewer = _normalize_hidden_reviewer(hidden_reviewer)
    if normalized_hidden_reviewer is None:
        raise ReviewError("hidden reviewer is required")
    existing_hidden_reviewer = _normalize_hidden_reviewer(queue.get("hidden_reviewer"))
    if existing_hidden_reviewer is not None and existing_hidden_reviewer != normalized_hidden_reviewer:
        raise ReviewError("cannot replace an existing hidden reviewer binding")

    payload = deepcopy(queue)
    payload["hidden_reviewer"] = normalized_hidden_reviewer
    payload["binding"][HIDDEN_REVIEWER_BINDING_FIELD] = canonical_sha256(normalized_hidden_reviewer)
    payload["review_contract"] = {
        "ui_version": REVIEW_UI_VERSION,
        "receipt_schema_sha256": sha256_file(REVIEW_RECEIPT_SCHEMA_PATH),
    }
    payload["review_id"] = f"REVIEW-{canonical_sha256(_review_identity_material(payload))}"
    errors = _schema_errors(payload, REVIEW_QUEUE_SCHEMA_PATH)
    if errors:
        raise ReviewError(f"bound ReviewQueueV3 is invalid: {errors[:3]}")
    return payload


def _overall_action(decisions: list[dict[str, Any]]) -> str:
    actions = {value["action"] for value in decisions}
    if actions == {"APPROVE"}:
        return "APPROVED"
    if "REJECT" in actions:
        return "REJECTED"
    if "REQUEST_REVISION" in actions:
        return "REVISION_REQUIRED"
    return "FACT_PENDING"


def make_review_receipt(
    queue: dict[str, Any],
    *,
    reviewer_id: str,
    reviewer_role: str,
    item_actions: dict[str, str],
    comments: dict[str, str] | None = None,
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    comments = comments or {}
    decisions = [
        {
            "review_item_id": item["review_item_id"],
            "action": item_actions[item["review_item_id"]],
            "comment": str(comments.get(item["review_item_id"], "")),
            "evidence_ids": list(item["evidence_ids"]),
        }
        for item in queue["items"]
        if item["review_item_id"] in item_actions
    ]
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "review_id": queue["review_id"],
        "reviewer": {"id": reviewer_id, "role": reviewer_role},
        "reviewed_at": reviewed_at or utc_now(),
        "overall_action": _overall_action(decisions) if decisions else "FACT_PENDING",
        "scope": "ENTIRE_QUEUE" if len(decisions) == len(queue["items"]) else "SELECTED_ITEMS",
        "binding": deepcopy(queue["binding"]),
        "evidence_bindings": [
            {"evidence_id": value["evidence_id"], "sha256": value["sha256"]}
            for value in queue["evidence_refs"]
        ],
        "decisions": decisions,
    }
    return receipt


def validate_review_receipt(queue: dict[str, Any], receipt: dict[str, Any] | None) -> dict[str, Any]:
    queue_errors = _schema_errors(queue, REVIEW_QUEUE_SCHEMA_PATH)
    if queue_errors:
        return result("FAIL", "review_v3", findings=[{"code": "REVIEW_QUEUE_SCHEMA_INVALID", **value} for value in queue_errors])
    if receipt is None:
        return result("HUMAN_REVIEW", "review_v3", findings=[{"code": "REVIEW_RECEIPT_REQUIRED"}])
    receipt_errors = _schema_errors(receipt, REVIEW_RECEIPT_SCHEMA_PATH)
    if receipt_errors:
        return result("FAIL", "review_v3", findings=[{"code": "REVIEW_RECEIPT_SCHEMA_INVALID", **value} for value in receipt_errors])

    try:
        hidden_reviewer = _normalize_hidden_reviewer(queue.get("hidden_reviewer"))
    except ReviewError as exc:
        return result("FAIL", "review_v3", findings=[{"code": "HIDDEN_REVIEWER_INVALID", "message": str(exc)}])
    if hidden_reviewer is not None:
        expected_hidden_reviewer_sha = canonical_sha256(hidden_reviewer)
        if queue["binding"].get(HIDDEN_REVIEWER_BINDING_FIELD) != expected_hidden_reviewer_sha:
            return result(
                "FAIL",
                "review_v3",
                findings=[{"code": "HIDDEN_REVIEWER_BINDING_INVALID"}],
            )

    expected_evidence_bindings = [
        {"evidence_id": value["evidence_id"], "sha256": value["sha256"]}
        for value in queue["evidence_refs"]
    ]
    receipt_evidence_bindings = receipt.get("evidence_bindings", [])
    evidence_binding_ids = [value.get("evidence_id") for value in receipt_evidence_bindings if isinstance(value, dict)]
    evidence_bindings_stale = (
        len(evidence_binding_ids) != len(set(evidence_binding_ids))
        or receipt_evidence_bindings != expected_evidence_bindings
    )
    if receipt["review_id"] != queue["review_id"] or receipt["binding"] != queue["binding"] or evidence_bindings_stale:
        changed_fields = [name for name in queue["binding"] if receipt["binding"].get(name) != queue["binding"].get(name)]
        if evidence_bindings_stale:
            changed_fields.append("evidence_bindings")
        return result(
            "HUMAN_REVIEW",
            "review_v3",
            findings=[{
                "code": "REVIEW_RECEIPT_STALE",
                "message": "源件、模板、IR、DSL、编译器、候选输出或差异报告已经变化。",
                "changed_fields": changed_fields,
            }],
        )

    if hidden_reviewer is not None and receipt["reviewer"] != {
        "id": hidden_reviewer["reviewer_id"],
        "role": hidden_reviewer["reviewer_role"],
    }:
        return result(
            "HUMAN_REVIEW",
            "review_v3",
            findings=[{
                "code": "REVIEW_RECEIPT_STALE",
                "message": "绑定的复核身份已经变化。",
                "changed_fields": ["hidden_reviewer"],
            }],
        )

    queue_ids = {item["review_item_id"] for item in queue["items"]}
    queue_evidence_by_item = {item["review_item_id"]: item["evidence_ids"] for item in queue["items"]}
    receipt_ids = [item["review_item_id"] for item in receipt["decisions"]]
    invalid_actions = [item for item in receipt["decisions"] if item["action"] not in ITEM_ACTIONS]
    invalid_evidence = [
        item["review_item_id"]
        for item in receipt["decisions"]
        if item["review_item_id"] in queue_evidence_by_item
        and item.get("evidence_ids") != queue_evidence_by_item[item["review_item_id"]]
    ]
    if len(receipt_ids) != len(set(receipt_ids)) or set(receipt_ids) - queue_ids or invalid_actions:
        return result("FAIL", "review_v3", findings=[{"code": "REVIEW_RECEIPT_ITEM_INVALID"}])
    if invalid_evidence:
        return result(
            "HUMAN_REVIEW",
            "review_v3",
            findings=[{
                "code": "REVIEW_RECEIPT_STALE",
                "message": "复核项所绑定的证据已经变化。",
                "changed_fields": ["decision.evidence_ids"],
                "review_item_ids": invalid_evidence,
            }],
        )
    missing = sorted(queue_ids - set(receipt_ids))
    if missing:
        return result("HUMAN_REVIEW", "review_v3", findings=[{"code": "REVIEW_RECEIPT_INCOMPLETE", "review_item_ids": missing}])
    derived_overall = _overall_action(receipt["decisions"])
    if receipt["overall_action"] != derived_overall:
        return result("FAIL", "review_v3", findings=[{"code": "REVIEW_RECEIPT_ACTION_CONFLICT", "expected": derived_overall}])
    non_approved = [item for item in receipt["decisions"] if item["action"] != "APPROVE"]
    if non_approved:
        return result(
            "HUMAN_REVIEW",
            "review_v3",
            findings=[{
                "code": "REVIEW_NOT_APPROVED",
                "review_item_id": item["review_item_id"],
                "action": item["action"],
            } for item in non_approved],
            overall_action=receipt["overall_action"],
        )
    return result(
        "PASS",
        "review_v3",
        review_id=queue["review_id"],
        reviewer=receipt["reviewer"],
        reviewed_at=receipt["reviewed_at"],
        binding=receipt["binding"],
        evidence_bindings=receipt["evidence_bindings"],
    )


def rebind_volatile_review_receipt(
    queue: dict[str, Any],
    receipt: dict[str, Any],
    prior_validation: dict[str, Any],
    prior_queue: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Rebind a previously validated receipt after the V3 runtime-hash defect.

    This is deliberately narrower than ordinary receipt validation.  It only
    carries an already-PASS, fully approved receipt across changes to the raw
    SourceDocument/decision-validation files when every human-visible and
    business-semantic artifact remains identical.
    """

    queue_errors = _schema_errors(queue, REVIEW_QUEUE_SCHEMA_PATH)
    prior_queue_errors = (
        _schema_errors(prior_queue, REVIEW_QUEUE_SCHEMA_PATH)
        if isinstance(prior_queue, dict)
        else []
    )
    receipt_errors = _schema_errors(receipt, REVIEW_RECEIPT_SCHEMA_PATH)
    if queue_errors or receipt_errors or prior_queue_errors:
        return None, result(
            "FAIL",
            "review_receipt_volatile_rebind_v3",
            findings=[{
                "code": "REVIEW_REBIND_SCHEMA_INVALID",
                "queue_errors": queue_errors,
                "prior_queue_errors": prior_queue_errors,
                "receipt_errors": receipt_errors,
            }],
        )

    prior_matches_receipt = (
        prior_validation.get("status") == "PASS"
        and prior_validation.get("stage") in {"review_v3", "content_review_v3"}
        and prior_validation.get("review_id") == receipt.get("review_id")
        and prior_validation.get("reviewer") == receipt.get("reviewer")
        and prior_validation.get("reviewed_at") == receipt.get("reviewed_at")
        and prior_validation.get("binding") == receipt.get("binding")
        and prior_validation.get("evidence_bindings") == receipt.get("evidence_bindings")
        and not prior_validation.get("findings")
    )
    if not prior_matches_receipt:
        return None, result(
            "FAIL",
            "review_receipt_volatile_rebind_v3",
            findings=[{
                "code": "PRIOR_REVIEW_VALIDATION_MISMATCH",
                "message": "旧回执没有与之完全一致的既有 PASS 校验报告。",
            }],
        )

    if receipt.get("overall_action") != "APPROVED" or receipt.get("scope") != "ENTIRE_QUEUE":
        return None, result(
            "HUMAN_REVIEW",
            "review_receipt_volatile_rebind_v3",
            findings=[{"code": "PRIOR_REVIEW_NOT_FULLY_APPROVED"}],
        )

    changed_binding_fields = {
        name
        for name in REVIEW_BINDING_FIELDS
        if receipt["binding"].get(name) != queue["binding"].get(name)
    }
    old_evidence = {
        value["evidence_id"]: value["sha256"] for value in receipt["evidence_bindings"]
    }
    new_evidence = {
        value["evidence_id"]: value["sha256"] for value in queue["evidence_refs"]
    }
    changed_evidence_ids = {
        evidence_id
        for evidence_id in set(old_evidence) | set(new_evidence)
        if old_evidence.get(evidence_id) != new_evidence.get(evidence_id)
    }
    review_identity_only_change = (
        not changed_binding_fields
        and not changed_evidence_ids
        and isinstance(prior_queue, dict)
        and prior_queue.get("review_id") == receipt.get("review_id")
        and _approval_content_identity_material(prior_queue) == _approval_content_identity_material(queue)
    )
    binding_change_allowed = (
        changed_binding_fields <= VOLATILE_REBIND_BINDING_FIELDS
        and (
            bool(changed_binding_fields)
            or changed_evidence_ids == {"SOURCE-DOCUMENT"}
            or review_identity_only_change
        )
    )
    if (
        not binding_change_allowed
        or (not changed_evidence_ids and not review_identity_only_change)
        or not changed_evidence_ids <= VOLATILE_REBIND_EVIDENCE_IDS
        or set(old_evidence) != set(new_evidence)
        or {"CONTENT-DECISION", "DOCUMENT-PREVIEW"} - set(old_evidence)
        or old_evidence["CONTENT-DECISION"] != receipt["binding"]["candidate_output_sha256"]
        or new_evidence["CONTENT-DECISION"] != queue["binding"]["candidate_output_sha256"]
    ):
        return None, result(
            "HUMAN_REVIEW",
            "review_receipt_volatile_rebind_v3",
            findings=[{
                "code": "REVIEW_REBIND_SEMANTIC_CHANGE_DETECTED",
                "changed_binding_fields": sorted(changed_binding_fields),
                "changed_evidence_ids": sorted(changed_evidence_ids),
            }],
        )

    evidence_by_id = {value["evidence_id"]: value for value in queue["evidence_refs"]}
    try:
        source_document = read_json(Path(evidence_by_id["SOURCE-DOCUMENT"]["path"]))
        decision_validation = read_json(Path(evidence_by_id["DECISION-VALIDATION"]["path"]))
        content_decision = (
            read_json(Path(evidence_by_id["CONTENT-DECISION"]["path"]))
            if not changed_binding_fields
            else None
        )
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
        return None, result(
            "FAIL",
            "review_receipt_volatile_rebind_v3",
            findings=[{"code": "REVIEW_REBIND_EVIDENCE_UNREADABLE", "message": str(exc)}],
        )
    current_evidence_valid = (
        source_document.get("artifact_type") == "SourceDocumentV3"
        and str(source_document.get("source", {}).get("sha256") or "").upper()
        == queue["binding"]["source_sha256"]
        and decision_validation.get("status") == "PASS"
        and decision_validation.get("stage") == "content_decision_v3"
        and not decision_validation.get("findings")
        and (
            bool(changed_binding_fields)
            or (
                isinstance(content_decision, dict)
                and str(content_decision.get("source_document_sha256") or "").upper()
                == source_document_sha256(source_document)
                == new_evidence["SOURCE-DOCUMENT"]
            )
        )
    )
    if not current_evidence_valid:
        return None, result(
            "FAIL",
            "review_receipt_volatile_rebind_v3",
            findings=[{"code": "REVIEW_REBIND_CURRENT_EVIDENCE_INVALID"}],
        )

    rebound = deepcopy(receipt)
    rebound["review_id"] = queue["review_id"]
    rebound["binding"] = deepcopy(queue["binding"])
    rebound["evidence_bindings"] = [
        {"evidence_id": value["evidence_id"], "sha256": value["sha256"]}
        for value in queue["evidence_refs"]
    ]
    rebound_validation = validate_review_receipt(queue, rebound)
    if rebound_validation.get("status") != "PASS":
        return None, result(
            "HUMAN_REVIEW",
            "review_receipt_volatile_rebind_v3",
            findings=[{
                "code": "REVIEW_REBIND_RECEIPT_NOT_APPLICABLE",
                "validation": rebound_validation,
            }],
        )
    return rebound, result(
        "PASS",
        "review_receipt_volatile_rebind_v3",
        findings=[{
            "code": "VOLATILE_REVIEW_BINDING_REBOUND",
            "message": "既有人工批准仅重绑定到确定性运行态证据；业务内容与可见预览未变化。",
        }],
        prior_review_id=receipt["review_id"],
        rebound_review_id=rebound["review_id"],
        changed_binding_fields=sorted(changed_binding_fields),
        changed_evidence_ids=sorted(changed_evidence_ids),
        review_identity_only_change=review_identity_only_change,
        reviewer=receipt["reviewer"],
        reviewed_at=receipt["reviewed_at"],
    )


HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>文档转换人工复核</title>
  <style>
    :root { color-scheme: light; font-family: "Microsoft YaHei", Arial, sans-serif; color: #182433; background: #e9eef4; }
    * { box-sizing: border-box; }
    body { margin: 0; overflow-wrap: anywhere; }
    header { padding: 22px 30px; color: white; background: linear-gradient(110deg, #173453, #216466); }
    header h1 { margin: 0 0 7px; font-size: 24px; }
    main { max-width: 1240px; margin: 24px auto; padding: 0 20px 54px; }
    .notice, .reviewer, .preview-shell, article { background: white; border: 1px solid #cbd6df; border-radius: 12px; padding: 18px; margin-bottom: 18px; box-shadow: 0 5px 16px rgba(34, 54, 75, .05); }
    .notice { border-left: 5px solid #e69a27; line-height: 1.75; }
    .eyebrow { color: #1a7067; font-size: 13px; font-weight: 700; letter-spacing: .08em; }
    .preview-header h2 { margin: 7px 0; font-size: 25px; }
    .preview-header p { margin: 6px 0; color: #536475; line-height: 1.75; }
    .summary-card { margin: 16px 0; padding: 14px 16px; border-radius: 8px; background: #edf7f5; border: 1px solid #b8ddd7; line-height: 1.7; }
    .doc-pages { display: grid; grid-template-columns: minmax(0, 1fr); gap: 24px; margin-top: 18px; }
    .doc-page { width: min(100%, 880px); min-height: 520px; margin: auto; padding: 46px 52px; background: #fff; border: 1px solid #ccd4dc; box-shadow: 0 12px 28px rgba(30, 46, 61, .12); }
    .doc-page.cover, .doc-page.back_cover { display: flex; min-height: 620px; flex-direction: column; justify-content: center; }
    .page-heading { display: flex; gap: 12px; align-items: center; justify-content: space-between; border-bottom: 2px solid #3f7d86; padding-bottom: 12px; }
    .page-heading h3 { margin: 0; font-size: 22px; }
    .badge { padding: 5px 10px; border-radius: 999px; background: #e8f2f4; color: #285d66; font-size: 12px; font-weight: 700; }
    .page-summary { color: #5f6e7a; line-height: 1.7; }
    .doc-section { margin-top: 28px; }
    .doc-section h4 { margin: 0 0 7px; color: #265f72; font-size: 18px; }
    .section-purpose { color: #71808c; font-size: 13px; }
    .content-block { margin-top: 14px; padding: 14px 16px; border-left: 4px solid #94b8be; background: #f7f9fb; }
    .content-block.title { text-align: center; border-left: 0; border-top: 4px solid #477e89; background: #f3f7f8; }
    .content-block.subtitle { border-left-color: #d39a3e; }
    .content-block.image { min-height: 150px; text-align: center; border: 2px dashed #a8b8c4; background: #fafcfd; }
    .content-block.image .block-text { margin-bottom: 12px; }
    .content-block.image figure { width: 100%; max-width: 720px; margin: 0 auto 10px; }
    .content-block.image img { max-height: 420px; object-fit: contain; }
    .content-block.warning { border-left-color: #c34e45; background: #fff3f1; }
    .block-label { display: block; margin-bottom: 7px; color: #546877; font-size: 12px; font-weight: 700; }
    .block-text { margin: 0; white-space: pre-wrap; line-height: 1.8; }
    .block-notes { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; }
    .chip { padding: 4px 8px; border-radius: 5px; background: #e8edf1; color: #53616c; font-size: 11px; }
    .preview-table { width: 100%; margin-top: 10px; border-collapse: collapse; font-size: 13px; }
    .preview-table th, .preview-table td { padding: 9px; border: 1px solid #b9c5cf; text-align: left; vertical-align: top; }
    .preview-table tr:first-child { background: #e9f1f3; font-weight: 700; }
    .rules { display: grid; grid-template-columns: repeat(auto-fit, minmax(270px, 1fr)); gap: 12px; margin: 20px 0; }
    .rule { padding: 14px; border: 1px solid #d6dfe6; border-radius: 9px; background: #fafbfc; }
    .rule h4 { margin: 0 0 9px; }
    .before { color: #8b4340; text-decoration: line-through; }
    .after { color: #176b5a; font-weight: 700; }
    .excluded { margin: 16px 0; padding: 14px 18px; border-radius: 8px; background: #fff4e8; border: 1px solid #e7c597; }
    .excluded h3 { margin-top: 0; }
    .review-title { margin: 34px 0 14px; }
    .review-title h2 { margin-bottom: 6px; }
    label { display: inline-block; margin: 6px 14px 6px 0; }
    input[type=text] { min-width: 220px; padding: 8px; border: 1px solid #aebcca; border-radius: 5px; }
    article h2 { margin: 0 0 8px; font-size: 19px; }
    .meta { color: #60707e; font-size: 12px; }
    .conclusion { padding: 11px 13px; border-radius: 7px; background: #f2f6f8; line-height: 1.7; }
    .actions { padding: 14px 0; border-top: 1px solid #e2e8ed; }
    textarea { width: 100%; min-height: 72px; box-sizing: border-box; padding: 9px; border: 1px solid #b8c4cd; border-radius: 6px; }
    button { cursor: pointer; border: 0; border-radius: 7px; padding: 11px 19px; color: white; background: #146c5b; font-weight: 700; }
    #message { margin-left: 12px; color: #a33; }
    a.evidence { display: inline-block; margin: 5px 12px 5px 0; color: #17608c; }
    details.technical { margin: 12px 0; padding: 10px 12px; border: 1px solid #d6dee5; border-radius: 7px; background: #fafbfc; }
    details.technical summary { cursor: pointer; color: #435767; font-weight: 700; }
    .technical-row { overflow-wrap: anywhere; margin: 8px 0; color: #566775; font-size: 12px; }
    .gallery { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; margin: 12px 0; }
    .item-thumbnails { grid-template-columns: repeat(auto-fit, minmax(150px, 220px)); }
    figure { margin: 0; padding: 8px; border: 1px solid #dde4eb; border-radius: 7px; background: #fafbfc; }
    figure img { display: block; width: 100%; max-height: 620px; object-fit: contain; background: white; }
    .editable-text-block { min-height: 34px; padding: 5px 6px; border-radius: 5px; outline: 1px dashed transparent; }
    .editable-text-block:focus { background: white; outline-color: #2d8b7b; box-shadow: 0 0 0 3px rgba(45, 139, 123, .12); }
    .editable-text-block::after { content: ""; }
    .item-thumbnail { cursor: pointer; position: relative; user-select: none; }
    .item-thumbnail:focus { outline: 3px solid rgba(20, 108, 91, .35); outline-offset: 2px; }
    .item-thumbnail.is-selected { border-color: #0d7868; background: #eef8f5; box-shadow: 0 0 0 2px rgba(13, 120, 104, .28); }
    .item-thumbnail img { height: 132px; max-height: 132px; }
    .thumbnail-state { display: inline-block; margin-top: 8px; padding: 4px 8px; border-radius: 5px; background: #e8edf1; color: #4f5f6d; font-size: 12px; font-weight: 700; }
    .item-thumbnail.is-selected .thumbnail-state { background: #0d7868; color: white; }
    .thumbnail-note { margin-top: 7px; padding: 7px; border-radius: 5px; background: white; color: #374957; font-size: 12px; white-space: pre-wrap; }
    figcaption { margin-top: 6px; overflow-wrap: anywhere; color: #526271; font-size: 12px; }
    @media (max-width: 700px) { main { padding: 0 10px 36px; } .doc-page { padding: 28px 20px; } .page-heading { align-items: flex-start; flex-direction: column; } }
    @media print { body { background: white; } header, .notice, .reviewer, #items, #export, #message, .review-title { display: none; } .preview-shell { border: 0; box-shadow: none; } .doc-page { break-after: page; box-shadow: none; } }
  </style>
</head>
<body>
  <header><h1>文档转换人工复核</h1><div id="review-meta"></div></header>
  <main>
    <section class="notice"><strong>先看内容，再做决定。</strong> 下方展示本轮待核验的文字和图片；是否已映射、是否为成品，请以预览说明为准。内部代码、哈希和编译信息放在折叠的技术证据区。此页完全离线，只导出绑定哈希的 JSON 审批回执；编辑和图片选择会作为意见保存，不会自行修改正式模板或解除转换门禁。</section>
    <section id="document-preview" class="preview-shell" hidden></section>
    <section class="review-title"><h2>人工复核结论</h2><div>请结合上方内容预览和页面证据，对每个事项作出明确选择。</div></section>
    __REVIEWER_FORM__
    <section id="items"></section>
    <button id="export" type="button">导出审批回执</button><span id="message"></span>
  </main>
  <script id="review-queue" type="application/json">__QUEUE_JSON__</script>
  <script id="review-annotation-port" type="application/json">__ANNOTATION_PORT_JSON__</script>
  <script>
    'use strict';
    const queue = JSON.parse(document.getElementById('review-queue').textContent);
    const actionLabels = {APPROVE: '批准', REJECT: '驳回', REQUEST_REVISION: '要求修订', FACT_PENDING: '标记事实待确认'};
    const text = (tag, value, className) => {
      const node = document.createElement(tag); node.textContent = value == null ? '' : String(value);
      if (className) node.className = className; return node;
    };
    const isLocalPath = value => {
      if (typeof value !== 'string' || value.length === 0 || /[\u0000-\u001f]/.test(value)) return false;
      if (/^(?:\/\/|\\\\)/.test(value)) return false;
      if (/^[A-Za-z]:[\\/]/.test(value)) return true;
      const scheme = value.match(/^([A-Za-z][A-Za-z0-9+.-]*):/);
      return !scheme || (scheme[1].toLowerCase() === 'file' && /^file:\/\/\//i.test(value));
    };
    const isImagePath = value => /\.(?:png|jpe?g|gif|webp)$/i.test(value);
    const sourceIdsForBlock = block => Array.from(new Set([
      block && block.source_id,
      block && block.image && block.image.source_id,
      ...(block && Array.isArray(block.source_ids) ? block.source_ids : []),
    ].filter(Boolean).map(String)));
    const appendImage = (root, path, label) => {
      if (!isLocalPath(path) || !isImagePath(path)) return;
      const figure = document.createElement('figure');
      const img = document.createElement('img'); img.src = path; img.alt = label; img.loading = 'lazy';
      figure.append(img, text('figcaption', `${label} · ${path}`)); root.append(figure);
      return figure;
    };
    const appendTable = (root, rows) => {
      if (!Array.isArray(rows) || rows.length === 0) return;
      const table = document.createElement('table'); table.className = 'preview-table';
      rows.forEach((row, rowIndex) => {
        const tr = document.createElement('tr');
        (row || []).forEach(cell => tr.append(text(rowIndex === 0 ? 'th' : 'td', cell)));
        table.append(tr);
      });
      root.append(table); return table;
    };
    const appendBlock = (root, block, pageIndex, sectionIndex, blockIndex) => {
      const anchor = `${pageIndex}:${sectionIndex}:${blockIndex}`;
      const card = document.createElement('div'); card.className = `content-block ${block.kind}`;
      card.setAttribute('data-annotation-target-id', `preview-block:${anchor}`);
      card.setAttribute('data-annotation-target-kind', 'preview_block');
      card.append(text('span', block.label, 'block-label'));
      const body = text('p', block.preview_text, 'block-text editable-text-block');
      if (block.kind === 'text') {
        body.setAttribute('data-annotation-target-id', `preview-text:${anchor}`);
        body.setAttribute('data-annotation-target-kind', 'preview_text');
      }
      body.setAttribute('contenteditable', 'true');
      body.spellcheck = true;
      body.setAttribute('data-original-text', String(block.preview_text || ''));
      body.setAttribute('data-block-label', String(block.label || ''));
      body.setAttribute('data-source-ids', sourceIdsForBlock(block).join('|'));
      body.title = '可直接编辑文本，导出审批回执时会写入 comment。';
      card.append(body);
      if (block.image && block.image.path) {
        appendImage(card, block.image.path, block.image.name || block.label || '文档图片预览');
        const latestFigure = card.querySelector('figure:last-of-type');
        if (latestFigure) {
          latestFigure.classList.add('document-preview-image');
          latestFigure.setAttribute('data-annotation-target-id', `preview-image:${anchor}`);
          latestFigure.setAttribute('data-annotation-target-kind', 'preview_image');
        }
      }
      const table = appendTable(card, block.table_rows);
      if (table) {
        table.setAttribute('data-annotation-target-id', `preview-table:${anchor}`);
        table.setAttribute('data-annotation-target-kind', 'preview_table');
      }
      const notes = document.createElement('div'); notes.className = 'block-notes';
      [['内容来源', block.source_rule], ['进入文档前处理', block.transformation], ['排版增长', block.growth]].forEach(([label, value]) => {
        if (value) notes.append(text('span', `${label}：${value}`, 'chip'));
      });
      card.append(notes); root.append(card);
    };
    const renderDocumentPreview = preview => {
      if (!preview) return;
      const root = document.getElementById('document-preview'); root.hidden = false;
      const head = document.createElement('div'); head.className = 'preview-header';
      head.append(text('div', '拟生成 DOCX 内容预览', 'eyebrow'), text('h2', preview.title), text('p', preview.subtitle));
      root.append(head, text('div', preview.summary, 'summary-card'));
      if (preview.string_rules && preview.string_rules.length) {
        root.append(text('h3', '字符串与内容会怎样处理'));
        const rules = document.createElement('div'); rules.className = 'rules';
        preview.string_rules.forEach(rule => {
          const card = document.createElement('div'); card.className = 'rule';
          card.append(text('h4', rule.label), text('div', `处理前：${rule.before}`, 'before'), text('div', `处理后：${rule.after}`, 'after'), text('p', rule.reason));
          rules.append(card);
        });
        root.append(rules);
      }
      const pages = document.createElement('div'); pages.className = 'doc-pages';
      (preview.pages || []).forEach((page, pageIndex) => {
        const sheet = document.createElement('section'); sheet.className = `doc-page ${page.page_role}`;
        const heading = document.createElement('div'); heading.className = 'page-heading';
        heading.append(text('h3', page.title), text('span', page.badge, 'badge')); sheet.append(heading, text('p', page.summary, 'page-summary'));
        (page.sections || []).forEach((section, sectionIndex) => {
          const sectionRoot = document.createElement('section'); sectionRoot.className = 'doc-section';
          sectionRoot.append(text('h4', section.title), text('div', `${section.purpose} · ${section.layout}`, 'section-purpose'));
          (section.blocks || []).forEach((block, blockIndex) => appendBlock(sectionRoot, block, pageIndex + 1, sectionIndex + 1, blockIndex + 1)); sheet.append(sectionRoot);
        });
        pages.append(sheet);
      });
      root.append(pages);
      if (preview.excluded_items && preview.excluded_items.length) {
        const excluded = document.createElement('section'); excluded.className = 'excluded';
        excluded.append(text('h3', '不会进入成品的内容'));
        const list = document.createElement('ul'); preview.excluded_items.forEach(value => list.append(text('li', value))); excluded.append(list); root.append(excluded);
      }
    };
    const previewImagesBySource = new Map();
    const indexPreviewImages = preview => {
      if (!preview) return;
      (preview.pages || []).forEach(page => (page.sections || []).forEach(section => (section.blocks || []).forEach(block => {
        if (!block || !block.image || !block.image.path) return;
        const ids = new Set([block.image.source_id, block.source_id, ...(block.source_ids || [])].filter(Boolean).map(String));
        ids.forEach(id => {
          if (!previewImagesBySource.has(id)) previewImagesBySource.set(id, []);
          previewImagesBySource.get(id).push(block);
        });
      })));
    };
    const imageKeyForFigure = figure => [
      figure.getAttribute('data-review-item-id') || '',
      figure.getAttribute('data-source-id') || '',
      figure.getAttribute('data-image-path') || '',
    ].join('::');
    const toggleImageSelection = (figure, forceSelected) => {
      const selected = forceSelected === undefined ? !figure.classList.contains('is-selected') : Boolean(forceSelected);
      figure.classList.toggle('is-selected', selected);
      figure.setAttribute('aria-pressed', selected ? 'true' : 'false');
      const state = figure.querySelector('.thumbnail-state');
      if (state) state.textContent = selected ? '已选中' : '未选中';
      return selected;
    };
    const editImageNote = figure => {
      const current = figure.getAttribute('data-note') || '';
      const label = figure.getAttribute('data-image-label') || figure.getAttribute('data-source-id') || '图片';
      const kind = figure.getAttribute('data-selection-kind') === 'page' ? '页面注释' : '图片注释';
      const note = window.prompt(`${kind}：${label}`, current);
      if (note === null) return;
      figure.setAttribute('data-note', note.trim());
      const noteBox = figure.querySelector('.thumbnail-note');
      if (noteBox) {
        noteBox.textContent = note.trim();
        noteBox.hidden = !note.trim();
      }
    };
    const enhanceItemThumbnail = (figure, item, sourceId, block, path, label, selectionKind = 'source') => {
      figure.classList.add('item-thumbnail');
      figure.tabIndex = 0;
      figure.setAttribute('role', 'button');
      figure.setAttribute('aria-pressed', 'false');
      figure.setAttribute('data-review-item-id', item.review_item_id);
      figure.setAttribute('data-source-id', String(sourceId));
      figure.setAttribute('data-selection-kind', selectionKind);
      figure.setAttribute('data-image-path', path);
      figure.setAttribute('data-image-label', label);
      figure.title = selectionKind === 'page' ? '单击选择待批注页面，双击添加页面注释；不代表保留正文图片。' : '单击选中/取消，双击添加图片注释。';
      figure.append(text('span', '未选中', 'thumbnail-state'));
      const note = text('div', '', 'thumbnail-note'); note.hidden = true; figure.append(note);
      figure.addEventListener('click', event => {
        if (event.detail > 1) return;
        toggleImageSelection(figure);
      });
      figure.addEventListener('dblclick', event => {
        event.preventDefault();
        toggleImageSelection(figure, true);
        editImageNote(figure);
      });
      figure.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        toggleImageSelection(figure);
      });
    };
    const appendItemThumbnails = (root, item) => {
      const gallery = document.createElement('div'); gallery.className = 'gallery item-thumbnails';
      const seen = new Set();
      (item.source_ids || []).forEach(sourceId => {
        (previewImagesBySource.get(String(sourceId)) || []).forEach(block => {
          const path = block.image.path;
          const imageKey = `${sourceId}::${path}`;
          if (seen.has(imageKey)) return;
          seen.add(imageKey);
          const label = `缩略图：${block.image.name || block.label || sourceId}`;
          const figure = appendImage(gallery, path, label);
          if (figure) enhanceItemThumbnail(figure, item, sourceId, block, path, label);
        });
      });
      if (gallery.childElementCount) root.append(gallery);
    };
    const collectImageComments = (article, selectionKind = 'source') => {
      const rows = [];
      article.querySelectorAll('.item-thumbnail.is-selected').forEach(figure => {
        // 页面截图是核验对象，不得混入业务图片保留清单。
        if (figure.getAttribute('data-selection-kind') !== selectionKind) return;
        const sourceId = figure.getAttribute(selectionKind === 'page' ? 'data-image-path' : 'data-source-id') || '';
        const label = figure.getAttribute('data-image-label') || sourceId || '图片';
        const note = figure.getAttribute('data-note') || '已选中';
        rows.push(`- ${sourceId} / ${label}：${note}`);
      });
      const heading = selectionKind === 'page' ? '页面注释' : '图片注释';
      return rows.length ? `[${heading}]\n${rows.join('\n')}` : '';
    };
    const collectTextEdits = article => {
      const itemSourceIds = new Set((article.getAttribute('data-source-ids') || '').split('|').filter(Boolean));
      const rows = [];
      document.querySelectorAll('.editable-text-block').forEach(block => {
        const blockSourceIds = (block.getAttribute('data-source-ids') || '').split('|').filter(Boolean);
        if (itemSourceIds.size && blockSourceIds.length && !blockSourceIds.some(id => itemSourceIds.has(id))) return;
        const before = block.getAttribute('data-original-text') || '';
        const after = block.textContent || '';
        if (before === after) return;
        const label = block.getAttribute('data-block-label') || '文本';
        const sourcePart = blockSourceIds.length ? ` / ${blockSourceIds.join('、')}` : '';
        rows.push(`- ${label}${sourcePart}\n  原文本：${before}\n  批准文本：${after}`);
      });
      return rows.length ? `[文本修改]\n${rows.join('\n')}` : '';
    };
    const buildDecisionComment = article => [
      article.querySelector('.comment').value.trim(),
      collectImageComments(article),
      collectImageComments(article, 'page'),
      collectTextEdits(article),
    ].filter(Boolean).join('\n\n');
    document.getElementById('review-meta').textContent = `${queue.review_type} · ${queue.review_id}`;
    indexPreviewImages(queue.document_preview);
    renderDocumentPreview(queue.document_preview);
    const evidence = Object.fromEntries(queue.evidence_refs.map(value => [value.evidence_id, value]));
    const itemsRoot = document.getElementById('items');
    queue.items.forEach(item => {
      const article = document.createElement('article'); article.dataset.reviewItemId = item.review_item_id;
      article.setAttribute('data-annotation-target-id', `review-item:${item.review_item_id}`);
      article.setAttribute('data-annotation-target-kind', 'review_item');
      article.setAttribute('data-source-ids', (item.source_ids || []).map(String).join('|'));
      article.append(text('h2', item.title), text('div', `${item.category} · 风险 ${item.risk_level}`, 'meta'), text('p', item.conclusion, 'conclusion'));
      if (item.details.recognized_conclusion) article.append(text('p', `机器识别结论：${item.details.recognized_conclusion}`));
      if (item.details.notes) article.append(text('p', `请注意：${item.details.notes}`));
      const choices = document.createElement('div'); choices.className = 'actions';
      Object.entries(actionLabels).forEach(([action, label]) => {
        const option = document.createElement('label'); const input = document.createElement('input'); input.type = 'radio';
        input.name = `action-${item.review_item_id}`; input.value = action; option.append(input, document.createTextNode(label)); choices.append(option);
      });
      article.append(choices);
      appendItemThumbnails(article, item);
      const gallery = document.createElement('div'); gallery.className = 'gallery item-thumbnails rendered-evidence';
      const seenRenderedPaths = new Set();
      const appendRenderedImage = (path, label) => {
        if (seenRenderedPaths.has(path)) return;
        const figure = appendImage(gallery, path, label);
        if (!figure) return;
        seenRenderedPaths.add(path);
        enhanceItemThumbnail(figure, item, '', null, path, label, 'page');
      };
      item.evidence_ids.forEach(id => { if (evidence[id]) appendRenderedImage(evidence[id].path, evidence[id].label); });
      (item.details.preview_paths || []).forEach(path => appendRenderedImage(path, '渲染预览'));
      (item.details.diff_overlay_paths || []).forEach(path => appendRenderedImage(path, '差异叠图'));
      if (gallery.childElementCount) article.append(text('p', '页面证据：单击选中/取消，双击批注；这不是正文图片保留清单。原尺寸图片可从下方技术证据打开。'), gallery);
      const technical = document.createElement('details'); technical.className = 'technical';
      technical.append(text('summary', '查看技术证据（开发 / 审计人员）'));
      [['复核项 ID', item.review_item_id], ['来源 ID', (item.source_ids || []).join('、')], ['Template IR', item.details.template_ir], ['DSL / 变更', item.details.dsl_diff]].forEach(([label, value]) => {
        if (value) technical.append(text('div', `${label}：${value}`, 'technical-row'));
      });
      const links = document.createElement('div');
      item.evidence_ids.forEach(id => {
        if (!evidence[id] || !isLocalPath(evidence[id].path)) return;
        const link = document.createElement('a'); link.className = 'evidence'; link.href = evidence[id].path;
        link.textContent = `打开证据：${evidence[id].label}`; link.target = '_blank'; link.rel = 'noopener noreferrer'; links.append(link);
      });
      technical.append(links); article.append(technical);
      const comment = document.createElement('textarea'); comment.placeholder = '复核意见（可选）'; comment.className = 'comment'; article.append(comment); itemsRoot.append(article);
    });
    document.getElementById('export').addEventListener('click', () => {
      const reviewerId = document.getElementById('reviewer-id').value.trim(); const reviewerRole = document.getElementById('reviewer-role').value.trim(); const decisions = [];
      for (const article of itemsRoot.querySelectorAll('article')) {
        const selected = article.querySelector('input[type=radio]:checked');
        if (!selected) { document.getElementById('message').textContent = '请完成全部复核项。'; return; }
        const queueItem = queue.items.find(value => value.review_item_id === article.dataset.reviewItemId);
        decisions.push({review_item_id: article.dataset.reviewItemId, action: selected.value, comment: buildDecisionComment(article), evidence_ids: queueItem.evidence_ids});
      }
      if (!reviewerId || !reviewerRole) { document.getElementById('message').textContent = '请填写复核人 ID 和角色。'; return; }
      const actions = new Set(decisions.map(value => value.action));
      const overall = actions.size === 1 && actions.has('APPROVE') ? 'APPROVED' : actions.has('REJECT') ? 'REJECTED' : actions.has('REQUEST_REVISION') ? 'REVISION_REQUIRED' : 'FACT_PENDING';
      const evidenceBindings = queue.evidence_refs.map(value => ({evidence_id: value.evidence_id, sha256: value.sha256}));
      const receipt = {schema_version: '3.0', review_id: queue.review_id, reviewer: {id: reviewerId, role: reviewerRole}, reviewed_at: new Date().toISOString(), overall_action: overall, scope: 'ENTIRE_QUEUE', binding: queue.binding, evidence_bindings: evidenceBindings, decisions};
      const blob = new Blob([JSON.stringify(receipt, null, 2)], {type: 'application/json'}); const anchor = document.createElement('a'); anchor.href = URL.createObjectURL(blob);
      anchor.download = `${queue.review_id}-receipt.json`; anchor.click(); URL.revokeObjectURL(anchor.href); document.getElementById('message').textContent = '回执已导出；仍需由 V3 校验器验证。';
    });
  </script>
</body>
</html>
"""


def _reviewer_form_html(hidden_reviewer: dict[str, Any] | None) -> str:
    if hidden_reviewer is None:
        return (
            '<section class="reviewer">\n'
            '  <label>复核人 ID <input id="reviewer-id" type="text" autocomplete="off"></label>\n'
            '  <label>角色 <input id="reviewer-role" type="text" autocomplete="off"></label>\n'
            '</section>'
        )
    normalized_hidden_reviewer = _normalize_hidden_reviewer(hidden_reviewer)
    if normalized_hidden_reviewer is None:
        raise ReviewError("hidden reviewer is required")
    reviewer_id = normalized_hidden_reviewer["reviewer_id"]
    reviewer_role = normalized_hidden_reviewer["reviewer_role"]
    return (
        '<section class="reviewer reviewer-bound">\n'
        '  <span>已绑定本地复核身份；身份信息只写入审批回执，不进入业务预览或交付文件。</span>\n'
        f'  <input id="reviewer-id" type="hidden" value="{html_escape(reviewer_id, quote=True)}">\n'
        f'  <input id="reviewer-role" type="hidden" value="{html_escape(reviewer_role, quote=True)}">\n'
        '</section>'
    )


def render_review_html(
    queue: dict[str, Any],
    hidden_reviewer: dict[str, Any] | None = None,
) -> str:
    errors = _schema_errors(queue, REVIEW_QUEUE_SCHEMA_PATH)
    if errors:
        raise ReviewError(f"cannot render invalid ReviewQueueV3: {errors[:3]}")
    queue_hidden_reviewer = _normalize_hidden_reviewer(queue.get("hidden_reviewer"))
    requested_hidden_reviewer = _normalize_hidden_reviewer(hidden_reviewer)
    if queue_hidden_reviewer is not None and requested_hidden_reviewer is not None and queue_hidden_reviewer != requested_hidden_reviewer:
        raise ReviewError("hidden reviewer does not match the queue binding")
    queue_json = json.dumps(queue, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c")
    return (
        HTML_TEMPLATE
        .replace("__REVIEWER_FORM__", _reviewer_form_html(queue_hidden_reviewer or requested_hidden_reviewer))
        .replace("__QUEUE_JSON__", queue_json)
        .replace("__ANNOTATION_PORT_JSON__", json.dumps(build_review_annotation_port(queue), ensure_ascii=False, sort_keys=True).replace("<", "\\u003c"))
    )


def write_review_html(
    queue: dict[str, Any],
    output: Path,
    hidden_reviewer: dict[str, Any] | None = None,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_review_html(queue, hidden_reviewer=hidden_reviewer), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成离线复核页或校验 V3 审批回执")
    subparsers = parser.add_subparsers(dest="command", required=True)
    html_parser = subparsers.add_parser("html")
    html_parser.add_argument("--queue", type=Path, required=True)
    html_parser.add_argument("--output", type=Path, required=True)
    html_parser.add_argument("--hidden-reviewer-id")
    html_parser.add_argument("--hidden-reviewer-role")
    html_parser.add_argument("--report", type=Path)
    bind_parser = subparsers.add_parser("bind-hidden-reviewer")
    bind_parser.add_argument("--queue", type=Path, required=True)
    bind_parser.add_argument("--output", type=Path, required=True)
    bind_parser.add_argument("--hidden-reviewer-id", required=True)
    bind_parser.add_argument("--hidden-reviewer-role", required=True)
    bind_parser.add_argument("--report", type=Path)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--queue", type=Path, required=True)
    validate_parser.add_argument("--receipt", type=Path, required=True)
    validate_parser.add_argument("--report", type=Path)
    rebind_parser = subparsers.add_parser("rebind-volatile")
    rebind_parser.add_argument("--queue", type=Path, required=True)
    rebind_parser.add_argument("--receipt", type=Path, required=True)
    rebind_parser.add_argument("--prior-validation", type=Path, required=True)
    rebind_parser.add_argument("--prior-queue", type=Path)
    rebind_parser.add_argument("--output", type=Path, required=True)
    rebind_parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        queue = read_json(args.queue)
        if args.command == "bind-hidden-reviewer":
            bound_queue = bind_hidden_reviewer(
                queue,
                {"reviewer_id": args.hidden_reviewer_id, "reviewer_role": args.hidden_reviewer_role},
            )
            write_json(args.output, bound_queue)
            return finish(
                result(
                    "HUMAN_REVIEW",
                    "review_v3",
                    findings=[{"code": "REVIEW_RECEIPT_REQUIRED"}],
                    output=str(args.output.resolve()),
                    output_sha256=sha256_file(args.output),
                    review_id=bound_queue["review_id"],
                ),
                args.report,
            )
        if args.command == "html":
            hidden_values = (args.hidden_reviewer_id, args.hidden_reviewer_role)
            if any(value is not None for value in hidden_values) and not all(hidden_values):
                raise ReviewError("--hidden-reviewer-id and --hidden-reviewer-role must be supplied together")
            hidden_reviewer = (
                {"reviewer_id": args.hidden_reviewer_id, "reviewer_role": args.hidden_reviewer_role}
                if all(hidden_values)
                else None
            )
            write_review_html(queue, args.output, hidden_reviewer=hidden_reviewer)
            return finish(result("HUMAN_REVIEW", "review_v3", findings=[{"code": "REVIEW_RECEIPT_REQUIRED"}], html=str(args.output)), args.report)
        receipt = read_json(args.receipt) if args.receipt.is_file() else None
        if args.command == "rebind-volatile":
            if receipt is None:
                raise ReviewError("receipt does not exist")
            prior_validation = read_json(args.prior_validation)
            prior_queue = read_json(args.prior_queue) if args.prior_queue else None
            rebound, report = rebind_volatile_review_receipt(
                queue,
                receipt,
                prior_validation,
                prior_queue=prior_queue,
            )
            if rebound is not None:
                write_json(args.output, rebound)
                report["output"] = str(args.output.resolve())
                report["output_sha256"] = sha256_file(args.output)
                report["prior_receipt_sha256"] = sha256_file(args.receipt)
                report["prior_validation_sha256"] = sha256_file(args.prior_validation)
                if args.prior_queue:
                    report["prior_queue_sha256"] = sha256_file(args.prior_queue)
            return finish(report, args.report)
        return finish(validate_review_receipt(queue, receipt), args.report)
    except (ReviewError, OSError, ValueError, json.JSONDecodeError) as exc:
        return finish(result("FAIL", "review_v3", findings=[{"code": "REVIEW_SCHEMA_INVALID", "message": str(exc)}]), getattr(args, "report", None))


if __name__ == "__main__":
    raise SystemExit(main())
