"""Assemble externally produced decision fragments into ContentDecisionV3.

This is intentionally a narrow trust boundary: fragments may only decide items
from their task bundle and only schema-defined fields are copied downstream.
"""
from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from decision_bundle_v3 import (
    DecisionBundleError,
    _source_items,
    _validate_bundle,
    source_document_sha256,
    validate_content_decision,
)
from deterministic_candidates_v3 import apply_deterministic_resolutions
from pipeline_common import read_json, write_json

DECISION_FIELDS = {"source_id", "action", "confidence", "evidence_ids", "rationale",
                   "target_slot", "target_order", "reviewed_text", "reviewed_blocks",
                   "redactions", "replacement_ref"}
UNRESOLVED_FIELDS = {"source_id", "reason_code", "evidence_ids", "message"}
EVIDENCE_FIELDS = {"evidence_id", "source_id", "kind", "path", "sha256", "page_or_position"}


class FragmentAssemblyError(ValueError):
    pass


def _only(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FragmentAssemblyError(f"{label} 必须是对象")
    return {key: deepcopy(raw) for key, raw in value.items() if key in fields}


def _source_ids(bundle: dict[str, Any]) -> set[str]:
    batch = bundle.get("item_batch")
    if not isinstance(batch, list) or not batch:
        raise FragmentAssemblyError("task bundle.item_batch 必须是非空数组")
    if any(not isinstance(item, dict) for item in batch):
        raise FragmentAssemblyError("task bundle.item_batch 的每个成员都必须是对象")
    ids = [str(item.get("source_id") or "") for item in batch]
    if not all(ids) or len(ids) != len(set(ids)):
        raise FragmentAssemblyError("task bundle source_id 必须非空且唯一")
    return set(ids)


def assemble_decision_fragments(
    fragments: Iterable[dict[str, Any]],
    task_bundles: list[dict[str, Any]],
    source_document: dict[str, Any],
    deterministic_candidates: list[dict[str, Any]] | None = None,
    *,
    product_model: str = "",
    product_full_name: str = "",
    identity_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bundles = list(task_bundles)
    if not bundles:
        raise FragmentAssemblyError("至少需要一个 DecisionTaskBundle")
    document_sha = source_document_sha256(source_document)
    task_ids = {str(bundle.get("task_id") or "") for bundle in bundles}
    if not all(task_ids):
        raise FragmentAssemblyError("bundle 缺少 task_id")
    bundle_ids: set[str] = set()
    ids_by_task: dict[str, set[str]] = {}
    for bundle in bundles:
        try:
            _validate_bundle(bundle)
        except DecisionBundleError as exc:
            raise FragmentAssemblyError(f"DecisionTaskBundleV3 无效: {exc}") from exc
        if str(bundle.get("source_document_sha256") or "").upper() != document_sha:
            raise FragmentAssemblyError("DecisionTaskBundle 与 SourceDocumentV3 哈希不一致")
        ids = _source_ids(bundle)
        task_id = str(bundle["task_id"])
        if task_id in ids_by_task:
            raise FragmentAssemblyError(f"task_id 重复: {task_id}")
        ids_by_task[task_id] = ids
        bundle_ids |= ids

    decisions: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for fragment in fragments:
        if not isinstance(fragment, dict):
            raise FragmentAssemblyError("fragment 必须是对象")
        task_id = str(fragment.get("task_id") or "")
        if task_id not in task_ids:
            raise FragmentAssemblyError(f"fragment task_id 不属于 bundles: {task_id}")
        fragment_bundle_ids = ids_by_task[task_id]
        if str(fragment.get("source_document_sha256") or "").upper() != document_sha:
            raise FragmentAssemblyError("fragment source_document_sha256 与 bundle 不一致")
        for raw in fragment.get("decisions", []) or []:
            item = _only(raw, DECISION_FIELDS, "decision")
            source_id = str(item.get("source_id") or "")
            if source_id not in fragment_bundle_ids:
                raise FragmentAssemblyError(f"fragment source_id 不属于该 task bundle: {source_id}")
            if source_id in seen:
                raise FragmentAssemblyError(f"source_id 被重复决策: {source_id}")
            seen.add(source_id); decisions.append(item)
        for raw in fragment.get("unresolved_items", []) or []:
            item = _only(raw, UNRESOLVED_FIELDS, "unresolved_item")
            source_id = str(item.get("source_id") or "")
            if source_id not in fragment_bundle_ids:
                raise FragmentAssemblyError(f"fragment source_id 不属于该 task bundle: {source_id}")
            if source_id in seen:
                raise FragmentAssemblyError(f"source_id 被重复决策: {source_id}")
            seen.add(source_id); unresolved.append(item)
        for raw in fragment.get("evidence_bindings", []) or []:
            item = _only(raw, EVIDENCE_FIELDS, "evidence_binding")
            if item.get("source_id") not in fragment_bundle_ids:
                raise FragmentAssemblyError(f"evidence source_id 不属于该 task bundle: {item.get('source_id')}")
            evidence.append(item)
    if seen != bundle_ids:
        raise FragmentAssemblyError(f"fragment 未恰好覆盖所有 source_id: {sorted(bundle_ids - seen)}")
    if len({str(x.get('evidence_id') or '') for x in evidence}) != len(evidence):
        raise FragmentAssemblyError("evidence_id 重复")

    first = bundles[0]
    template = deepcopy(first["template_pack"])
    policy = deepcopy(first["policy"])
    if any(bundle.get("template_pack") != template or bundle.get("policy") != policy for bundle in bundles):
        raise FragmentAssemblyError("bundles 的 template_pack/policy 不一致")
    identity = {"status": "REVIEW_REQUIRED", "manufacturer_terms": [], "evidence_ids": [], "notes": ""}
    if identity_review:
        identity.update(_only(identity_review, {"status", "manufacturer_terms", "evidence_ids", "notes"}, "identity_review"))
    result = {"schema_version": "3.0", "decision_id": "", "task_ids": sorted(task_ids),
              "source_document_sha256": document_sha, "template_pack": template, "policy": policy,
              "product": {"model": str(product_model), "full_name": str(product_full_name)},
              "decisions": decisions, "unresolved_items": unresolved, "identity_review": identity,
              "evidence_bindings": evidence}
    result = apply_deterministic_resolutions(result, deterministic_candidates or [], source_document,
                                             source_document_artifact_sha256=document_sha)
    # Deterministic merge can add decisions; retain the strict schema contract.
    result["task_ids"] = sorted(task_ids)
    result["decision_id"] = "DECISION-" + _canonical_sha({k: v for k, v in result.items() if k != "decision_id"})
    return result


def _canonical_sha(value: Any) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser(description="组装 ContentDecisionV3 外部决策 fragments")
    parser.add_argument("--fragment", type=Path, action="append", required=True)
    parser.add_argument("--task-bundle", type=Path, action="append", required=True)
    parser.add_argument("--source-document", type=Path, required=True)
    parser.add_argument("--candidates", type=Path)
    parser.add_argument("--product-model", default="")
    parser.add_argument("--product-full-name", default="")
    parser.add_argument("--identity-review", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        fragments = [read_json(path) for path in args.fragment]
        bundles = [read_json(path) for path in args.task_bundle]
        source = read_json(args.source_document)
        candidates = read_json(args.candidates) if args.candidates else []
        if isinstance(candidates, dict): candidates = candidates.get("deterministic_candidates", [])
        identity = read_json(args.identity_review) if args.identity_review else None
        decision = assemble_decision_fragments(fragments, bundles, source, candidates,
                                               product_model=args.product_model,
                                               product_full_name=args.product_full_name,
                                               identity_review=identity)
        report = validate_content_decision(
            decision,
            task_bundles=bundles,
            expected_source_ids=[str(item.get("source_id") or "") for item in _source_items(source)],
        )
        write_json(args.output, decision)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report.get("status") == "FAIL" else 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
