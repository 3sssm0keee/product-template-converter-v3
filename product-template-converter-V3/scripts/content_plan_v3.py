from __future__ import annotations

from pathlib import Path
from typing import Any

from decision_bundle_v3 import source_document_sha256
from pipeline_common import read_json, sha256_file
from schema_validation_v3 import load_schema, validate_instance
from v3_common import V3_GENERATOR_VERSION, canonical_json_sha256


SCHEMA_VERSION = "content-plan-v3"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "references" / "schemas" / "content-plan-v3.schema.json"
REVIEW_RECEIPT_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "references" / "schemas" / "review-receipt-v3.schema.json"
REVIEW_BINDING_FIELDS = (
    "source_sha256",
    "template_sha256",
    "template_ir_sha256",
    "template_dsl_sha256",
    "compiler_sha256",
    "candidate_output_sha256",
    "diff_report_sha256",
)


def _source_items(source_document: dict[str, Any]) -> list[dict[str, Any]]:
    for candidate in (
        source_document.get("items"),
        source_document.get("content", {}).get("items") if isinstance(source_document.get("content"), dict) else None,
        source_document.get("payload", {}).get("items") if isinstance(source_document.get("payload"), dict) else None,
        source_document.get("inventory", {}).get("items") if isinstance(source_document.get("inventory"), dict) else None,
    ):
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _source_id(item: dict[str, Any]) -> str:
    return str(item.get("source_id") or item.get("id") or "")


def _normalized_ref(source_document: dict[str, Any]) -> dict[str, Any]:
    normalized = source_document.get("normalized_source") or source_document.get("normalized")
    if not isinstance(normalized, dict):
        raise ValueError("SourceDocumentV3 normalized source reference missing")
    if not normalized.get("path") or not normalized.get("sha256"):
        raise ValueError("SourceDocumentV3 normalized source reference incomplete")
    return {
        "path": str(normalized["path"]),
        "sha256": str(normalized["sha256"]).upper(),
        "format": normalized.get("format") or normalized.get("suffix", ""),
    }


def _public_review_binding(binding: dict[str, Any]) -> dict[str, str]:
    return {name: str(binding.get(name) or "").strip().upper() for name in REVIEW_BINDING_FIELDS}


def _item_sha_candidates(source_document: dict[str, Any], item: dict[str, Any]) -> set[str]:
    source = source_document.get("source") if isinstance(source_document.get("source"), dict) else {}
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    candidates = {
        item.get("sha256"),
        item.get("visual_sha256"),
        payload.get("sha256"),
        payload.get("visual_sha256"),
        source.get("sha256"),
    }
    return {str(value).strip().upper() for value in candidates if str(value or "").strip()}


def legacy_migrated_decision_matches_source_document(
    source_document: dict[str, Any],
    decision: dict[str, Any],
) -> bool:
    migration = decision.get("migration") if isinstance(decision.get("migration"), dict) else {}
    if migration.get("from_schema") != "content_map_v1":
        return False
    items = {_source_id(item): item for item in _source_items(source_document)}
    if not items:
        return False
    decision_ids = {
        str(value.get("source_id") or "")
        for value in decision.get("decisions", [])
        if isinstance(value, dict)
    }
    unresolved_ids = {
        str(value.get("source_id") or "")
        for value in decision.get("unresolved_items", [])
        if isinstance(value, dict)
    }
    if decision_ids | unresolved_ids != set(items):
        return False
    evidence_by_source: dict[str, list[dict[str, Any]]] = {}
    for evidence in decision.get("evidence_bindings", []):
        if isinstance(evidence, dict):
            evidence_by_source.setdefault(str(evidence.get("source_id") or ""), []).append(evidence)
    for source_id, item in items.items():
        evidence_values = evidence_by_source.get(source_id, [])
        if not evidence_values:
            return False
        location = str(item.get("location") or "")
        valid_hashes = _item_sha_candidates(source_document, item)
        if not any(
            str(evidence.get("sha256") or "").strip().upper() in valid_hashes
            and str(evidence.get("page_or_position") or "") == location
            for evidence in evidence_values
        ):
            return False
    return True


def validate_content_decision(
    source_document: dict[str, Any],
    decision: dict[str, Any],
    template_pack: dict[str, Any],
    *,
    source_document_artifact_sha256: str | None = None,
) -> None:
    if decision.get("schema_version") not in {"3.0", "content-decision-v3", "v3"}:
        raise ValueError("ContentDecisionV3 schema_version invalid")
    unresolved = decision.get("unresolved_items", [])
    if unresolved:
        raise ValueError("ContentDecisionV3 still contains unresolved_items")

    items = _source_items(source_document)
    source_ids = [_source_id(item) for item in items]
    if not source_ids or any(not value for value in source_ids):
        raise ValueError("SourceDocumentV3 contains missing source_id")
    if len(source_ids) != len(set(source_ids)):
        raise ValueError("SourceDocumentV3 source_id values must be unique")

    decisions = decision.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("ContentDecisionV3 decisions must be an array")
    decision_ids = [str(item.get("source_id", "")) for item in decisions if isinstance(item, dict)]
    if len(decision_ids) != len(decisions) or any(not value for value in decision_ids):
        raise ValueError("ContentDecisionV3 decision source_id missing")
    if len(decision_ids) != len(set(decision_ids)):
        raise ValueError("ContentDecisionV3 decision source_id values must be unique")
    missing = sorted(set(source_ids) - set(decision_ids))
    unknown = sorted(set(decision_ids) - set(source_ids))
    if missing or unknown:
        raise ValueError(f"ContentDecisionV3 coverage mismatch: missing={missing} unknown={unknown}")

    if source_document_artifact_sha256 is not None:
        expected_source_sha = source_document_artifact_sha256.upper()
        actual_source_sha = str(decision.get("source_document_sha256") or "").upper()
        if actual_source_sha != expected_source_sha and not legacy_migrated_decision_matches_source_document(
            source_document,
            decision,
        ):
            raise ValueError("ContentDecisionV3 source_document_sha256 conflicts with SourceDocumentV3 artifact")

    expected_pack = str(template_pack.get("id", ""))
    expected_version = str(template_pack.get("version") or template_pack.get("pack_version") or "1")
    selected = decision.get("template_pack")
    if isinstance(selected, dict):
        if selected.get("id") not in {None, "", expected_pack}:
            raise ValueError("ContentDecisionV3 template pack conflicts with resolved pack")
        if str(selected.get("version") or expected_version) != expected_version:
            raise ValueError("ContentDecisionV3 template pack version conflicts with resolved pack")


def build_content_plan_v3(
    source_document: dict[str, Any],
    decision: dict[str, Any],
    template_pack: dict[str, Any],
    *,
    source_document_path: Path,
    decision_path: Path,
    policy_sha256: str,
    review_receipt: dict[str, Any] | None = None,
    review_receipt_path: Path | None = None,
    expected_review_binding: dict[str, str] | None = None,
) -> dict[str, Any]:
    stable_source_sha = source_document_sha256(source_document)
    file_source_sha = sha256_file(source_document_path.resolve())
    decision_source_sha = str(decision.get("source_document_sha256") or "").upper()
    # 兼容 V3 开发期已人工批准、按 SourceDocument 文件字节绑定的旧 decision；
    # 新生成 decision 一律使用与输出目录无关的稳定语义哈希。
    source_artifact_sha = file_source_sha if decision_source_sha == file_source_sha else stable_source_sha
    if (
        decision_source_sha not in {file_source_sha, stable_source_sha}
        and legacy_migrated_decision_matches_source_document(source_document, decision)
    ):
        source_artifact_sha = decision_source_sha
    validate_content_decision(
        source_document,
        decision,
        template_pack,
        source_document_artifact_sha256=source_artifact_sha,
    )
    if review_receipt is None or review_receipt_path is None or not review_receipt_path.is_file():
        raise ValueError("ContentPlanV3 requires an approved ReviewReceiptV3 artifact")
    if read_json(review_receipt_path.resolve()) != review_receipt:
        raise ValueError("ContentPlanV3 ReviewReceiptV3 object differs from its bound artifact")
    receipt_errors = validate_instance(review_receipt, load_schema(REVIEW_RECEIPT_SCHEMA_PATH))
    if receipt_errors:
        raise ValueError(f"ContentPlanV3 ReviewReceiptV3 is invalid: {receipt_errors[:3]}")
    if review_receipt.get("overall_action") != "APPROVED":
        raise ValueError("ContentPlanV3 ReviewReceiptV3 is not approved")
    if not isinstance(expected_review_binding, dict):
        raise ValueError("ContentPlanV3 requires the current content review artifact binding")
    actual_binding = review_receipt.get("binding")
    if actual_binding != expected_review_binding:
        raise ValueError("ContentPlanV3 ReviewReceiptV3 binding does not match current content review artifacts")
    if actual_binding.get("candidate_output_sha256") != sha256_file(decision_path.resolve()):
        raise ValueError("ContentPlanV3 ReviewReceiptV3 candidate output is not the current ContentDecisionV3")
    current_source_sha = str(source_document.get("source", {}).get("sha256") or "").upper()
    if current_source_sha and actual_binding.get("source_sha256") != current_source_sha:
        raise ValueError("ContentPlanV3 ReviewReceiptV3 source binding is stale")
    current_template_sha = str(template_pack.get("template_sha256") or "").upper()
    if current_template_sha and actual_binding.get("template_sha256") != current_template_sha:
        raise ValueError("ContentPlanV3 ReviewReceiptV3 template binding is stale")
    source_items = {_source_id(item): item for item in _source_items(source_document)}
    materialized: list[dict[str, Any]] = []
    for order, item in enumerate(decision["decisions"], 1):
        source_id = str(item["source_id"])
        source_item = source_items[source_id]
        materialized.append({
            "source_id": source_id,
            "source": source_item,
            "decision": item,
            "execution_order": order,
        })

    pack_version = str(template_pack.get("version") or template_pack.get("pack_version") or "1")
    upstream: dict[str, Any] = {
        "source_document": {
            "path": str(source_document_path.resolve()),
            "sha256": source_artifact_sha,
            "schema_version": source_document.get("schema_version", "source-document-v3"),
        },
        "content_decision": {
            "path": str(decision_path.resolve()),
            "sha256": sha256_file(decision_path.resolve()),
            "schema_version": decision.get("schema_version"),
        },
        "policy_sha256": policy_sha256.upper(),
    }
    upstream["review_receipt"] = {
        "path": str(review_receipt_path.resolve()),
        "sha256": sha256_file(review_receipt_path.resolve()),
        "schema_version": review_receipt.get("schema_version"),
    }
    review_approval = {
        "review_id": review_receipt.get("review_id"),
        "reviewer": review_receipt.get("reviewer"),
        "reviewed_at": review_receipt.get("reviewed_at"),
        "overall_action": review_receipt.get("overall_action"),
        "scope": review_receipt.get("scope"),
        "binding": _public_review_binding(actual_binding),
    }

    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generator": {"id": "content-plan-builder", "version": V3_GENERATOR_VERSION},
        "upstream": upstream,
        "template_pack": {"id": template_pack["id"], "version": pack_version},
        "normalized_source": _normalized_ref(source_document),
        "product": decision.get("product", {}),
        "manufacturer_terms": (
            decision.get("identity_review", {}).get("manufacturer_terms", [])
            if isinstance(decision.get("identity_review"), dict)
            else []
        ),
        "identity_review": decision.get("identity_review"),
        "review_approval": review_approval,
        "items": materialized,
        "review_items": [
            item for item in materialized
            if item["decision"].get("action") == "human_review"
            or item["decision"].get("review_required") is True
        ],
        "evidence_bindings": decision.get("evidence_bindings", []),
    }
    plan["canonical_sha256"] = canonical_json_sha256(plan)
    errors = validate_instance(plan, load_schema(SCHEMA_PATH))
    if errors:
        raise ValueError(f"generated ContentPlanV3 is invalid: {errors[:3]}")
    return plan
