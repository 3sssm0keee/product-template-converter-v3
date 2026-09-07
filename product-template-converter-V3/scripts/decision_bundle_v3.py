from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from pipeline_common import finish, read_json, result, sha256_file, write_json
from schema_validation_v3 import load_schema, validate_instance


SCHEMA_VERSION = "3.0"
MAX_ITEMS_PER_BUNDLE = 40
MAX_INLINE_EVIDENCE_CHARS = 16_000
MAX_INLINE_TEXT_CHARS = 4_000

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = REPOSITORY_ROOT / "references" / "schemas"
TASK_BUNDLE_SCHEMA_PATH = SCHEMA_ROOT / "decision-task-bundle-v3.schema.json"
CONTENT_DECISION_SCHEMA_PATH = SCHEMA_ROOT / "content-decision-v3.schema.json"
INSTRUCTION_PATH = REPOSITORY_ROOT / "agents" / "prompts" / "content-mapper.system.md"
OUTPUT_SCHEMA_REF = "references/schemas/content-decision-v3.schema.json"

ALLOWED_ACTIONS = (
    "preserve_exact",
    "preserve_image",
    "preserve_sanitized_image",
    "redact_identity",
    "reviewed_text",
    "remove_identity",
    "remove_template_background",
    "exclude_from_output",  # 明确人工取舍；不加入确定性自动删除白名单。
    "human_review",
)
TARGET_ACTIONS = {
    "preserve_exact",
    "preserve_image",
    "preserve_sanitized_image",
    "redact_identity",
    "reviewed_text",
}
DETERMINISTIC_REMOVE_ACTIONS = {"remove_identity", "remove_template_background"}
RESOLVED_CANDIDATE_STATES = {"resolved", "deterministic_resolved"}
SHA256_RE = re.compile(r"^[A-F0-9]{64}$")

# These presentation parts are template infrastructure rather than article
# content. Keep this whitelist aligned with SourceDocumentV3: all listed item
# kinds are removable in a master/layout/notes-master part, while visible slide
# and notes content remains restricted to shape descriptions below.
PRESENTATION_TEMPLATE_REGIONS = {"slide_master", "slide_layout", "notes_master"}
PRESENTATION_TEMPLATE_ITEM_KINDS = {
    "text",
    "shape_description",
    "image",
    "media",
    "table",
}


class DecisionBundleError(ValueError):
    pass


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def _normalized_sha(value: Any, *, fallback: Any) -> str:
    candidate = str(value or "").strip().upper()
    return candidate if SHA256_RE.fullmatch(candidate) else canonical_sha256(fallback)


def _policy_sha(value: Any, *, fallback: Any) -> str:
    candidate = str(value or "").strip().upper()
    if not candidate:
        return canonical_sha256(fallback)
    if not SHA256_RE.fullmatch(candidate):
        raise DecisionBundleError("policy_sha256 must be a 64-character hexadecimal SHA-256")
    return candidate


def source_document_sha256(source_document: dict[str, Any]) -> str:
    for candidate in (
        source_document.get("artifact_sha256"),
        source_document.get("source_document_sha256"),
        source_document.get("sha256"),
    ):
        value = str(candidate or "").strip().upper()
        if SHA256_RE.fullmatch(value):
            return value
    source = source_document.get("source") if isinstance(source_document.get("source"), dict) else {}
    normalized = source_document.get("normalized") if isinstance(source_document.get("normalized"), dict) else {}
    upstream = source_document.get("upstream") if isinstance(source_document.get("upstream"), dict) else {}
    upstream_contract = {
        str(name): {
            "artifact_type": str(value.get("artifact_type") or ""),
            "schema_version": str(value.get("schema_version") or ""),
        }
        if isinstance(value, dict)
        else None
        for name, value in sorted(upstream.items())
    }
    # 绝对路径、运行时间和上游报告文件哈希属于运行位置证据，不能让同一源件
    # 在不同输出目录产生不同的语义身份。实际证据文件仍由 ReviewReceiptV3
    # 按字节哈希绑定；这里仅绑定会影响内容、适配器行为或人工决策的稳定材料。
    content = source_document.get("content")
    stable_content = dict(content) if isinstance(content, dict) else content
    if isinstance(stable_content, dict):
        # Office/WPS rewrites document properties such as modified time and
        # custom IDs on every normalization pass. Those metadata strings are
        # runtime evidence, not addressable source items reviewed by decisions.
        stable_content.pop("metadata", None)
    stable_material = {
        "schema_version": source_document.get("schema_version"),
        "artifact_type": source_document.get("artifact_type"),
        "policy_version": source_document.get("policy_version"),
        "generator": source_document.get("generator"),
        "upstream_contract": upstream_contract,
        "source": {
            "sha256": source.get("sha256"),
            "size_bytes": source.get("size_bytes"),
            "format": source.get("format"),
            "family": source.get("family"),
        },
        "normalized": {
            "format": normalized.get("format"),
        },
        "content": stable_content,
        "review": source_document.get("review"),
    }
    return canonical_sha256(stable_material)


def _source_items(source_document: dict[str, Any]) -> list[dict[str, Any]]:
    containers: list[Any] = [
        source_document.get("items"),
        source_document.get("inventory", {}).get("items") if isinstance(source_document.get("inventory"), dict) else None,
        source_document.get("content", {}).get("items") if isinstance(source_document.get("content"), dict) else None,
    ]
    for container in containers:
        if isinstance(container, list):
            items = [value for value in container if isinstance(value, dict)]
            seen: set[str] = set()
            for item in items:
                source_id = str(item.get("source_id") or item.get("id") or "").strip()
                if not source_id:
                    raise DecisionBundleError("every source item must have source_id or id")
                if source_id in seen:
                    raise DecisionBundleError(f"duplicate source_id in SourceDocumentV3: {source_id}")
                seen.add(source_id)
            return items
    raise DecisionBundleError("SourceDocumentV3 contains no typed item list")


def _item_id(item: dict[str, Any]) -> str:
    return str(item.get("source_id") or item.get("id") or "").strip()


def _item_payload(item: dict[str, Any]) -> dict[str, Any]:
    return item.get("payload") if isinstance(item.get("payload"), dict) else {}


def _item_kind(item: dict[str, Any]) -> str:
    payload = _item_payload(item)
    return str(item.get("kind") or payload.get("type") or item.get("item_type") or "other")


def _item_text(item: dict[str, Any]) -> str:
    direct = item.get("text") or item.get("ocr_text")
    if direct is not None:
        return str(direct)
    payload = _item_payload(item)
    if payload.get("type") == "text":
        return str(payload.get("text") or "")
    if payload.get("type") == "description":
        values = [payload.get("text"), payload.get("name"), payload.get("title"), payload.get("description")]
        return "\n".join(str(value) for value in values if str(value or "").strip())
    return ""


def _item_rows(item: dict[str, Any]) -> Any:
    if isinstance(item.get("rows"), list):
        return item["rows"]
    payload = _item_payload(item)
    return payload.get("rows") if payload.get("type") == "table" else None


def _item_risk_flags(item: dict[str, Any]) -> list[str]:
    values = item.get("risk_flags")
    if not isinstance(values, list):
        attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        values = attributes.get("risk_flags")
    return sorted({str(value) for value in values if str(value)}) if isinstance(values, list) else []


def _location(item: dict[str, Any]) -> str:
    for field in ("location", "page_or_position", "position"):
        value = item.get(field)
        if isinstance(value, str):
            return value
    if item.get("page") is not None:
        return f"page:{item['page']}"
    if item.get("slide") is not None:
        return f"slide:{item['slide']}"
    return ""


def _relative_path(path: Path, base_dir: Path) -> str:
    try:
        relative = path.resolve().relative_to(base_dir.resolve())
    except ValueError:
        relative = Path(os.path.relpath(path.resolve(), base_dir.resolve()))
    return relative.as_posix()


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return safe[:80] or "source-item"


def _write_source_snapshot(item: dict[str, Any], source_id: str, evidence_dir: Path, bundle_root: Path) -> dict[str, Any]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    snapshot_hash = canonical_sha256(item)
    path = evidence_dir / f"{_safe_filename(source_id)}-{snapshot_hash[:12]}.json"
    if not path.is_file():
        write_json(path, item)
    actual_sha = sha256_file(path)
    return {
        "evidence_id": f"EVID-{actual_sha}",
        "source_id": source_id,
        "kind": "source_item_snapshot",
        "path": _relative_path(path, bundle_root),
        "sha256": actual_sha,
        "page_or_position": _location(item),
    }


def _existing_evidence_refs(item: dict[str, Any], source_id: str, bundle_root: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    raw_refs = item.get("evidence_refs")
    if not isinstance(raw_refs, list):
        raw_refs = []
    else:
        raw_refs = list(raw_refs)
    direct_path = item.get("path") or item.get("media_path")
    if direct_path:
        raw_refs.append({
            "kind": "source_file",
            "path": direct_path,
            "sha256": item.get("sha256"),
            "page_or_position": _location(item),
        })
    for index, raw in enumerate(raw_refs, 1):
        if not isinstance(raw, dict) or not raw.get("path"):
            continue
        path = Path(str(raw["path"]))
        resolved = path if path.is_absolute() else bundle_root / path
        digest = str(raw.get("sha256") or "").strip().upper()
        if not SHA256_RE.fullmatch(digest) and resolved.is_file():
            digest = sha256_file(resolved)
        if not SHA256_RE.fullmatch(digest):
            continue
        relative = _relative_path(resolved, bundle_root)
        evidence_id = str(raw.get("evidence_id") or f"EVID-{digest}-{index}")
        output.append({
            "evidence_id": evidence_id,
            "source_id": source_id,
            "kind": str(raw.get("kind") or "source_evidence"),
            "path": relative,
            "sha256": digest,
            "page_or_position": str(raw.get("page_or_position") or _location(item)),
        })
    return output


def _compact_item(item: dict[str, Any], evidence_dir: Path, bundle_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    source_id = _item_id(item)
    snapshot = _write_source_snapshot(item, source_id, evidence_dir, bundle_root)
    evidence = [snapshot, *_existing_evidence_refs(item, source_id, bundle_root)]
    deduplicated = {value["evidence_id"]: value for value in evidence}
    evidence = list(deduplicated.values())

    text = _item_text(item)
    summary = str(item.get("summary") or "").strip()
    if not summary:
        summary = re.sub(r"\s+", " ", text).strip()[:500] or f"{_item_kind(item)} source item"
    compact: dict[str, Any] = {
        "source_id": source_id,
        "kind": _item_kind(item),
        "location": _location(item),
        "summary": summary[:1000],
        "evidence_ids": [value["evidence_id"] for value in evidence],
        "risk_flags": _item_risk_flags(item),
    }
    if text and len(text) <= MAX_INLINE_TEXT_CHARS:
        compact["inline_text"] = text
    rows = _item_rows(item)
    if isinstance(rows, list):
        preview = [[str(cell)[:500] for cell in row[:20]] for row in rows[:20] if isinstance(row, list)]
        if len(json.dumps(preview, ensure_ascii=False)) <= MAX_INLINE_TEXT_CHARS:
            compact["table_preview"] = preview
    return compact, evidence


def _inline_chars(item: dict[str, Any]) -> int:
    inline = {
        key: item[key]
        for key in ("summary", "inline_text", "table_preview")
        if key in item
    }
    return len(json.dumps(inline, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _candidate_status(value: Any) -> str:
    status = str(value or "unresolved").strip().lower()
    if status in RESOLVED_CANDIDATE_STATES:
        return "resolved"
    if status == "conflict":
        return "conflict"
    if status in {"low_confidence", "low-confidence"}:
        return "low_confidence"
    return "unresolved"


_CANDIDATE_ACTION_ALIASES = ("resolved_action", "proposed_action", "action")


def _candidate_action(candidate: dict[str, Any]) -> str:
    """Return the action carried by a deterministic candidate.

    ``resolved_action`` is the explicit output name used by the deterministic
    stage contract; ``proposed_action``/``action`` remain accepted for the
    earlier candidate shape.  Keeping this compatibility at the bundle
    boundary means the source and candidate producers do not need to be
    changed merely to prevent a resolved item from reaching the model.
    """

    values = {
        key: str(candidate.get(key) or "").strip()
        for key in _CANDIDATE_ACTION_ALIASES
        if str(candidate.get(key) or "").strip()
    }
    distinct = set(values.values())
    if len(distinct) > 1:
        source_id = str(candidate.get("source_id") or "").strip()
        details = ", ".join(f"{key}={value}" for key, value in values.items())
        raise DecisionBundleError(f"candidate action aliases conflict: {source_id}: {details}")
    return next(iter(distinct), "")


def _validate_candidate_consistency(candidate: dict[str, Any]) -> tuple[str, str]:
    """Validate the status/action contract before a candidate is classified.

    A ``resolved_action`` is an approval-bearing field, so it must never be
    attached to a candidate whose status is unresolved, conflicted, or low
    confidence.  Conversely, a resolved status is only meaningful when all
    action aliases agree on a supported action.
    """

    status = _candidate_status(candidate.get("resolution_status") or candidate.get("status"))
    action = _candidate_action(candidate)
    source_id = str(candidate.get("source_id") or "").strip()
    resolved_action = str(candidate.get("resolved_action") or "").strip()
    if status == "resolved":
        if action not in ALLOWED_ACTIONS:
            raise DecisionBundleError(
                f"resolved candidate requires a supported action: {source_id}: {action or '<missing>'}"
            )
    elif resolved_action:
        raise DecisionBundleError(
            f"non-resolved candidate must not set resolved_action: {source_id}"
        )
    return status, action


def _candidate_is_resolved(candidate: dict[str, Any]) -> bool:
    status, action = _validate_candidate_consistency(candidate)
    return status == "resolved" and action in ALLOWED_ACTIONS


def normalize_deterministic_candidates(
    deterministic_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize candidate aliases for the legacy deterministic merge helper.

    The returned list is copied and never mutates the candidate artifact.  The
    merge helper in ``deterministic_candidates_v3.py`` predates the explicit
    ``resolved_action`` name and only reads ``proposed_action``.
    """

    output: list[dict[str, Any]] = []
    seen_source_ids: set[str] = set()
    for raw in deterministic_candidates:
        if not isinstance(raw, dict):
            continue
        candidate = dict(raw)
        source_id = str(candidate.get("source_id") or "").strip()
        if source_id in seen_source_ids:
            raise DecisionBundleError(f"source_id may have only one deterministic candidate: {source_id}")
        seen_source_ids.add(source_id)
        _validate_candidate_consistency(candidate)
        resolved_action = str(candidate.get("resolved_action") or "").strip()
        if not candidate.get("proposed_action") and candidate.get("resolved_action"):
            candidate["proposed_action"] = candidate["resolved_action"]
        if not candidate.get("proposed_slot"):
            for key in ("resolved_slot", "target_slot"):
                if candidate.get(key):
                    candidate["proposed_slot"] = candidate[key]
                    break
        output.append(candidate)
    return output


def _candidate_for_task(candidate: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    source_id = str(candidate.get("source_id") or "").strip()
    status = _candidate_status(candidate.get("resolution_status") or candidate.get("status"))
    reason_codes = candidate.get("reason_codes")
    if not isinstance(reason_codes, list) or not reason_codes:
        reason_codes = [status.upper()]
    payload: dict[str, Any] = {
        "candidate_id": str(candidate.get("candidate_id") or f"CAND-{canonical_sha256(candidate)}"),
        "source_id": source_id,
        "resolution_status": status,
        "reason_codes": sorted({str(value) for value in reason_codes if str(value)}),
        "evidence_ids": list(item["evidence_ids"]),
    }
    proposed_action = _candidate_action(candidate)
    if proposed_action in ALLOWED_ACTIONS:
        payload["proposed_action"] = proposed_action
    if candidate.get("proposed_slot") or candidate.get("target_slot"):
        payload["proposed_slot"] = str(candidate.get("proposed_slot") or candidate.get("target_slot"))
    confidence = candidate.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        payload["confidence"] = max(0.0, min(1.0, float(confidence)))
    return payload


def _validate_bundle(bundle: dict[str, Any]) -> None:
    errors = validate_instance(bundle, load_schema(TASK_BUNDLE_SCHEMA_PATH))
    if errors:
        raise DecisionBundleError(f"generated DecisionTaskBundleV3 is invalid: {errors[:3]}")
    source_ids = [item["source_id"] for item in bundle["item_batch"]]
    if len(source_ids) != len(set(source_ids)):
        raise DecisionBundleError("a source_id may appear only once in item_batch")
    actual_inline = sum(_inline_chars(item) for item in bundle["item_batch"])
    if actual_inline != bundle["inline_evidence_chars"] or actual_inline > MAX_INLINE_EVIDENCE_CHARS:
        raise DecisionBundleError("inline evidence accounting is inconsistent")
    expected_task_id = f"TASK-{canonical_sha256({key: value for key, value in bundle.items() if key != 'task_id'})}"
    if bundle["task_id"] != expected_task_id:
        raise DecisionBundleError("task_id does not match canonical task content")
    if bundle["output_schema_ref"] != OUTPUT_SCHEMA_REF or bundle["output_schema_sha256"] != sha256_file(CONTENT_DECISION_SCHEMA_PATH):
        raise DecisionBundleError("ContentDecisionV3 output schema binding is stale")
    evidence = {value["evidence_id"]: value for value in bundle["evidence_refs"]}
    if len(evidence) != len(bundle["evidence_refs"]):
        raise DecisionBundleError("evidence_id values must be unique")
    for item in bundle["item_batch"]:
        if any(
            evidence_id not in evidence or evidence[evidence_id]["source_id"] != item["source_id"]
            for evidence_id in item["evidence_ids"]
        ):
            raise DecisionBundleError(f"task item evidence binding is invalid: {item['source_id']}")
    candidate_ids = [value["candidate_id"] for value in bundle["deterministic_candidates"]]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise DecisionBundleError("candidate_id values must be unique")
    source_id_set = set(source_ids)
    for candidate in bundle["deterministic_candidates"]:
        if candidate["source_id"] not in source_id_set:
            raise DecisionBundleError(f"candidate source_id is outside item_batch: {candidate['source_id']}")
        if any(evidence_id not in evidence for evidence_id in candidate["evidence_ids"]):
            raise DecisionBundleError(f"candidate evidence binding is invalid: {candidate['candidate_id']}")


def build_decision_task_bundles(
    source_document: dict[str, Any],
    deterministic_candidates: list[dict[str, Any]],
    *,
    template_pack_id: str,
    template_pack_version: str,
    allowed_slots: list[str],
    evidence_dir: Path,
    bundle_root: Path,
    policy_version: str,
    policy_sha256: str,
    allowed_actions: list[str] | tuple[str, ...] = ALLOWED_ACTIONS,
    instruction_path: Path = INSTRUCTION_PATH,
    max_items: int = MAX_ITEMS_PER_BUNDLE,
    max_inline_chars: int = MAX_INLINE_EVIDENCE_CHARS,
    source_document_artifact_sha256: str | None = None,
) -> list[dict[str, Any]]:
    """Build deterministic, provider-neutral batches containing unresolved items only."""

    if max_items < 1 or max_items > MAX_ITEMS_PER_BUNDLE:
        raise DecisionBundleError("max_items must be between 1 and 40")
    if max_inline_chars < 1 or max_inline_chars > MAX_INLINE_EVIDENCE_CHARS:
        raise DecisionBundleError("max_inline_chars must be between 1 and 16000")
    instruction = instruction_path.read_text(encoding="utf-8").strip()
    if not instruction or len(instruction) > 2000:
        raise DecisionBundleError("system instruction must contain 1 to 2000 characters")
    if not allowed_slots or len(allowed_slots) != len(set(allowed_slots)):
        raise DecisionBundleError("allowed_slots must be a non-empty unique list")
    if not allowed_actions or any(value not in ALLOWED_ACTIONS for value in allowed_actions):
        raise DecisionBundleError("allowed_actions contains an unsupported value")

    items = _source_items(source_document)
    by_id = {_item_id(item): item for item in items}
    normalized_candidates = normalize_deterministic_candidates(deterministic_candidates)
    candidates_by_id: dict[str, list[dict[str, Any]]] = {}
    for candidate in normalized_candidates:
        if not isinstance(candidate, dict):
            continue
        source_id = str(candidate.get("source_id") or "").strip()
        if source_id not in by_id:
            raise DecisionBundleError(f"candidate references unknown source_id: {source_id}")
        candidates_by_id.setdefault(source_id, []).append(candidate)

    unresolved_ids = [
        source_id
        for source_id in by_id
        if not candidates_by_id.get(source_id)
        or any(not _candidate_is_resolved(value) for value in candidates_by_id[source_id])
    ]
    for source_id, source_candidates in candidates_by_id.items():
        resolved = [value for value in source_candidates if _candidate_is_resolved(value)]
        unresolved = [value for value in source_candidates if not _candidate_is_resolved(value)]
        if len(resolved) > 1:
            raise DecisionBundleError(f"source_id has multiple resolved candidates: {source_id}")
        if resolved and unresolved:
            raise DecisionBundleError(f"source_id has both resolved and unresolved candidates: {source_id}")
        if resolved:
            action = _candidate_action(resolved[0])
            if action not in ALLOWED_ACTIONS:
                raise DecisionBundleError(f"resolved candidate has unsupported action: {source_id}: {action}")
    if not unresolved_ids:
        return []

    compact_items: list[dict[str, Any]] = []
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for source_id in unresolved_ids:
        compact, evidence = _compact_item(by_id[source_id], evidence_dir, bundle_root)
        if _inline_chars(compact) > max_inline_chars:
            compact.pop("inline_text", None)
            compact.pop("table_preview", None)
        if _inline_chars(compact) > max_inline_chars:
            compact["summary"] = compact["summary"][: min(500, max_inline_chars // 2)]
        compact_items.append(compact)
        evidence_by_id.update({value["evidence_id"]: value for value in evidence})

    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for item in compact_items:
        item_chars = _inline_chars(item)
        if current and (len(current) >= max_items or current_chars + item_chars > max_inline_chars):
            batches.append(current)
            current = []
            current_chars = 0
        if item_chars > max_inline_chars:
            raise DecisionBundleError(f"source item cannot fit the inline evidence budget: {item['source_id']}")
        current.append(item)
        current_chars += item_chars
    if current:
        batches.append(current)

    output_schema_sha = sha256_file(CONTENT_DECISION_SCHEMA_PATH)
    if source_document_artifact_sha256:
        document_sha = _policy_sha(source_document_artifact_sha256, fallback=source_document)
    else:
        document_sha = source_document_sha256(source_document)
    normalized_policy_sha = _policy_sha(policy_sha256, fallback={"version": policy_version})
    output: list[dict[str, Any]] = []
    for batch_index, item_batch in enumerate(batches, 1):
        batch_id_order = [item["source_id"] for item in item_batch]
        batch_ids = set(batch_id_order)
        evidence_ids = {evidence_id for item in item_batch for evidence_id in item["evidence_ids"]}
        candidates = [
            _candidate_for_task(candidate, next(item for item in item_batch if item["source_id"] == source_id))
            for source_id in batch_id_order
            for candidate in candidates_by_id.get(source_id, [])
            if _candidate_status(candidate.get("resolution_status") or candidate.get("status")) != "resolved"
        ]
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "task_id": "",
            "task_type": "content_and_identity_decision",
            "batch_index": batch_index,
            "batch_count": len(batches),
            "instruction": instruction,
            "policy": {"version": str(policy_version), "sha256": normalized_policy_sha},
            "source_document_sha256": document_sha,
            "template_pack": {"id": str(template_pack_id), "version": str(template_pack_version)},
            "item_batch": item_batch,
            "deterministic_candidates": candidates,
            "allowed_actions": list(allowed_actions),
            "allowed_slots": list(allowed_slots),
            "evidence_refs": [evidence_by_id[value] for value in sorted(evidence_ids)],
            "output_schema_ref": OUTPUT_SCHEMA_REF,
            "output_schema_sha256": output_schema_sha,
            "inline_evidence_chars": sum(_inline_chars(item) for item in item_batch),
        }
        payload["task_id"] = f"TASK-{canonical_sha256({key: value for key, value in payload.items() if key != 'task_id'})}"
        _validate_bundle(payload)
        output.append(payload)

    all_task_ids = [item["source_id"] for bundle in output for item in bundle["item_batch"]]
    if all_task_ids != unresolved_ids or len(all_task_ids) != len(set(all_task_ids)):
        raise DecisionBundleError("batching changed unresolved source order or duplicated a source_id")
    return output


def _resolved_decision(
    candidate: dict[str, Any],
    *,
    source_id: str,
    evidence_id: str,
) -> dict[str, Any]:
    action = _candidate_action(candidate)
    if action not in ALLOWED_ACTIONS:
        raise DecisionBundleError(f"resolved candidate has unsupported action: {source_id}: {action}")
    decision: dict[str, Any] = {
        "source_id": source_id,
        "action": action,
        "confidence": 1.0,
        "evidence_ids": [evidence_id],
        "rationale": "由白名单确定性规则和可验证证据生成；未调用模型。",
    }
    if action in TARGET_ACTIONS:
        target_slot = str(
            candidate.get("resolved_slot")
            or candidate.get("proposed_slot")
            or candidate.get("target_slot")
            or ""
        ).strip()
        if not target_slot:
            raise DecisionBundleError(f"resolved candidate missing target slot: {source_id}")
        decision["target_slot"] = target_slot
        raw_order = candidate.get("resolved_order", candidate.get("target_order", 0))
        try:
            decision["target_order"] = max(0, int(raw_order or 0))
        except (TypeError, ValueError) as exc:
            raise DecisionBundleError(f"resolved candidate has invalid target order: {source_id}") from exc
    if action == "redact_identity":
        redactions = candidate.get("redactions")
        if not isinstance(redactions, list) or not redactions or not all(str(value).strip() for value in redactions):
            raise DecisionBundleError(f"resolved candidate missing redactions: {source_id}")
        decision["redactions"] = list(dict.fromkeys(str(value) for value in redactions))
    if action == "reviewed_text":
        reviewed_text = str(candidate.get("reviewed_text") or candidate.get("resolved_text") or "").strip()
        if reviewed_text:
            decision["reviewed_text"] = reviewed_text
        else:
            blocks = candidate.get("reviewed_blocks")
            if not isinstance(blocks, list) or not blocks:
                raise DecisionBundleError(f"resolved candidate missing reviewed text: {source_id}")
            converted = []
            for block in blocks:
                if not isinstance(block, dict) or not str(block.get("text") or "").strip():
                    raise DecisionBundleError(f"resolved candidate has invalid reviewed block: {source_id}")
                block_slot = str(block.get("target_slot") or decision["target_slot"]).strip()
                converted.append({
                    "text": str(block["text"]),
                    "target_slot": block_slot,
                    "target_order": max(0, int(block.get("target_order") or decision["target_order"])),
                    "evidence_ids": [evidence_id],
                })
            decision["reviewed_blocks"] = converted
    if action == "preserve_sanitized_image":
        replacement = candidate.get("replacement_ref")
        if not isinstance(replacement, dict):
            replacement_path = str(candidate.get("replacement_path") or "").strip()
            replacement_sha = str(candidate.get("replacement_sha256") or "").strip().upper()
            if replacement_path and SHA256_RE.fullmatch(replacement_sha):
                replacement = {"path": replacement_path, "sha256": replacement_sha, "review_note": "确定性候选提供的替换证据。"}
        if not isinstance(replacement, dict) or not replacement.get("path") or not SHA256_RE.fullmatch(str(replacement.get("sha256") or "").upper()):
            raise DecisionBundleError(f"resolved candidate missing replacement evidence: {source_id}")
        decision["replacement_ref"] = {
            "path": str(replacement["path"]),
            "sha256": str(replacement["sha256"]).upper(),
            "review_note": str(replacement.get("review_note") or "确定性候选提供的替换证据。"),
        }
    return decision


def _presentation_region(item: dict[str, Any]) -> str | None:
    """Resolve the typed presentation region from SourceDocument evidence."""

    context = item.get("context") if isinstance(item.get("context"), dict) else {}
    explicit = str(context.get("region") or item.get("region") or "").strip().casefold()
    if explicit:
        return explicit
    hints = [
        str(item.get("location") or ""),
        str(context.get("part") or ""),
        str(context.get("source_part") or ""),
    ]
    for raw_hint in hints:
        hint = raw_hint.replace("\\", "/").casefold()
        if "notesmasters/" in hint or hint.startswith("notes_master:"):
            return "notes_master"
        if "notesslides/" in hint or hint.startswith("notes:"):
            return "notes"
        if "slidemasters/" in hint or hint.startswith("slide_master:"):
            return "slide_master"
        if "slidelayouts/" in hint or hint.startswith("slide_layout:"):
            return "slide_layout"
        if hint.startswith("slide:") or re.search(r"(?:^|/)slides/slide\d+\.xml(?:/|$)", hint):
            return "slide"
    return None


def _shape_description_has_authored_metadata(item: dict[str, Any]) -> bool:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    return bool(
        str(payload.get("title") or "").strip()
        or str(payload.get("description") or "").strip()
    )


def _is_presentation_template_item(item: dict[str, Any], content_kind: str) -> bool:
    if content_kind.casefold() != "presentation":
        return False
    return (
        _presentation_region(item) in PRESENTATION_TEMPLATE_REGIONS
        and str(item.get("kind") or "").casefold() in PRESENTATION_TEMPLATE_ITEM_KINDS
    )


def _is_presentation_shape_description_placeholder(item: dict[str, Any], content_kind: str) -> bool:
    """Allow only structural shape descriptions on visible slides and notes."""

    if content_kind.casefold() != "presentation":
        return False
    if str(item.get("kind") or "").casefold() != "shape_description":
        return False
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    if str(payload.get("type") or "").casefold() != "description":
        return False
    region = _presentation_region(item)
    if region not in {"slide", "notes"}:
        return False
    if _shape_description_has_authored_metadata(item):
        return False
    location = str(item.get("location") or "").replace("\\", "/").casefold()
    context = item.get("context") if isinstance(item.get("context"), dict) else {}
    part = str(context.get("part") or context.get("source_part") or "").replace("\\", "/").casefold()
    hints = (location, part)
    if region == "slide":
        return any(
            hint.startswith("slide:")
            or bool(re.search(r"(?:^|[/ :])ppt/slides/slide\d+\.xml(?:/|$)", hint))
            for hint in hints
        )
    return any(
        hint.startswith("notes:")
        or bool(re.search(r"(?:^|[/ :])ppt/notesslides/notesslide\d+\.xml(?:/|$)", hint))
        for hint in hints
    )


def _validate_resolved_candidate_provenance(
    candidate: dict[str, Any],
    item: dict[str, Any],
    *,
    source_id: str,
    source_document_sha: str,
    source_document: dict[str, Any],
) -> None:
    """Require a resolved candidate to be traceable to this source artifact."""

    trace = candidate.get("resolution_trace")
    if not isinstance(trace, dict):
        raise DecisionBundleError(f"resolved candidate missing resolution trace: {source_id}")
    trace_source_id = str(trace.get("source_id") or "").strip()
    trace_document_sha = str(trace.get("source_document_sha256") or "").strip().upper()
    if trace_source_id != source_id or trace_document_sha != source_document_sha:
        raise DecisionBundleError(f"resolved candidate resolution trace is not bound to SourceDocument: {source_id}")

    action = _candidate_action(candidate)
    if action not in DETERMINISTIC_REMOVE_ACTIONS:
        raise DecisionBundleError(f"resolved candidate has unsupported deterministic action: {source_id}: {action}")
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    relationship_bound = (
        str(item.get("kind") or "").casefold() == "relationship"
        and str(payload.get("type") or "").casefold() == "relationship"
    )
    if action == "remove_identity" and not relationship_bound:
        raise DecisionBundleError(f"remove_identity candidate is not bound to a relationship source item: {source_id}")
    attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    approved_action = str(attributes.get("safe_remove_action") or "").strip()
    explicit_approval = approved_action == action and attributes.get("deterministic_remove_approved") is True
    target_text = str(payload.get("target") or "").casefold()
    configured_identity_trace = (
        action == "remove_identity"
        and relationship_bound
        and str(trace.get("reason_code") or "").strip() == "CONFIGURED_IDENTITY_RELATIONSHIP"
        and isinstance(candidate.get("signals"), list)
        and any(
            isinstance(signal, dict)
            and str(signal.get("type") or "").strip() == "configured_identity_term"
            and str(signal.get("field") or "").strip() == "payload.target"
            and str(signal.get("value") or "").strip().casefold() in target_text
            for signal in candidate["signals"]
        )
    )
    if not explicit_approval and not configured_identity_trace:
        raise DecisionBundleError(
            f"resolved candidate action lacks matching SourceDocument removal approval: {source_id}: {action}"
        )
    content = source_document.get("content") if isinstance(source_document.get("content"), dict) else {}
    if action == "remove_template_background":
        content_kind = str(content.get("kind") or "")
        if not (
            _is_presentation_template_item(item, content_kind)
            or _is_presentation_shape_description_placeholder(item, content_kind)
        ):
            raise DecisionBundleError(
                f"remove_template_background candidate is not bound to an allowed presentation source item: {source_id}"
            )


def build_deterministic_content_decision(
    source_document: dict[str, Any],
    deterministic_candidates: list[dict[str, Any]],
    *,
    template_pack_id: str,
    template_pack_version: str,
    policy_version: str,
    policy_sha256: str,
    source_document_artifact_sha256: str,
) -> dict[str, Any]:
    """Materialize a complete ContentDecisionV3 when no model task is needed.

    This path is deliberately limited to an all-resolved candidate set.  It
    creates no approval/receipt and therefore the normal review gate still
    prevents a successful delivery without explicit human approval.
    """

    items = _source_items(source_document)
    item_by_id = {_item_id(item): item for item in items}
    candidates = normalize_deterministic_candidates(deterministic_candidates)
    document_sha = _policy_sha(source_document_artifact_sha256, fallback=source_document)
    by_id: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        source_id = str(candidate.get("source_id") or "").strip()
        if source_id not in item_by_id:
            raise DecisionBundleError(f"candidate references unknown source_id: {source_id}")
        by_id.setdefault(source_id, []).append(candidate)
    if set(by_id) != set(item_by_id):
        missing = sorted(set(item_by_id) - set(by_id))
        raise DecisionBundleError(f"deterministic-only decision missing candidates: {missing}")

    evidence_bindings = [_evidence_for_migration(source_document, item, source_id) for source_id, item in item_by_id.items()]
    evidence_by_source = {value["source_id"]: value["evidence_id"] for value in evidence_bindings}
    decisions: list[dict[str, Any]] = []
    for source_id in item_by_id:
        source_candidates = by_id[source_id]
        if len(source_candidates) != 1 or not _candidate_is_resolved(source_candidates[0]):
            raise DecisionBundleError(f"deterministic-only decision has unresolved candidate: {source_id}")
        _validate_resolved_candidate_provenance(
            source_candidates[0],
            item_by_id[source_id],
            source_id=source_id,
            source_document_sha=document_sha,
            source_document=source_document,
        )
        decisions.append(_resolved_decision(
            source_candidates[0],
            source_id=source_id,
            evidence_id=evidence_by_source[source_id],
        ))
    normalized_policy_sha = _policy_sha(policy_sha256, fallback={"version": policy_version})
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": "",
        "task_ids": [f"DETERMINISTIC-TASK-{canonical_sha256({'source_document': document_sha, 'candidates': candidates})}"],
        "source_document_sha256": document_sha,
        "template_pack": {"id": str(template_pack_id), "version": str(template_pack_version)},
        "policy": {"version": str(policy_version), "sha256": normalized_policy_sha},
        "product": {"model": "", "full_name": ""},
        "decisions": decisions,
        "unresolved_items": [],
        "identity_review": {
            "status": "REVIEW_REQUIRED",
            "manufacturer_terms": [],
            "evidence_ids": [value["evidence_id"] for value in evidence_bindings if value["source_id"] in {
                source_id for source_id, values in by_id.items()
                if _candidate_action(values[0]) in {"remove_identity", "redact_identity", "preserve_sanitized_image"}
            }],
            "notes": "确定性候选已生成内容决策；产品身份与最终交付仍需 ReviewReceiptV3。",
        },
        "evidence_bindings": evidence_bindings,
    }
    payload["decision_id"] = f"DECISION-{canonical_sha256({key: value for key, value in payload.items() if key != 'decision_id'})}"
    errors = validate_instance(payload, load_schema(CONTENT_DECISION_SCHEMA_PATH))
    if errors:
        raise DecisionBundleError(f"deterministic-only ContentDecisionV3 is invalid: {errors[:3]}")
    return payload


def write_decision_task_bundles(bundles: list[dict[str, Any]], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for bundle in bundles:
        path = output_dir / f"decision-task-{bundle['batch_index']:03d}-of-{bundle['batch_count']:03d}.json"
        write_json(path, bundle)
        paths.append(path)
    return paths


def validate_content_decision(
    decision: dict[str, Any] | None,
    *,
    task_bundles: list[dict[str, Any]] | None = None,
    expected_source_ids: list[str] | None = None,
    confidence_threshold: float = 0.8,
) -> dict[str, Any]:
    if decision is None:
        return result(
            "HUMAN_REVIEW",
            "content_decision_v3",
            findings=[{"code": "DECISION_BUNDLE_REQUIRED", "message": "外部 ContentDecisionV3 结果尚未提供。"}],
        )
    schema_errors = validate_instance(decision, load_schema(CONTENT_DECISION_SCHEMA_PATH))
    if schema_errors:
        return result(
            "FAIL",
            "content_decision_v3",
            findings=[{"code": "DECISION_SCHEMA_INVALID", **value} for value in schema_errors],
        )

    fail_findings: list[dict[str, Any]] = []
    review_findings: list[dict[str, Any]] = []
    tasks = task_bundles or []
    if tasks:
        try:
            for bundle in tasks:
                _validate_bundle(bundle)
        except DecisionBundleError as exc:
            return result(
                "FAIL",
                "content_decision_v3",
                findings=[{"code": "DECISION_TASK_SCHEMA_INVALID", "message": str(exc)}],
            )
        expected_tasks = {bundle["task_id"] for bundle in tasks}
        if set(decision["task_ids"]) != expected_tasks:
            fail_findings.append({"code": "DECISION_TASK_BINDING_MISMATCH"})
        document_hashes = {bundle["source_document_sha256"] for bundle in tasks}
        packs = {(bundle["template_pack"]["id"], bundle["template_pack"]["version"]) for bundle in tasks}
        policies = {(bundle["policy"]["version"], bundle["policy"]["sha256"]) for bundle in tasks}
        if document_hashes != {decision["source_document_sha256"]}:
            fail_findings.append({"code": "DECISION_SOURCE_HASH_MISMATCH"})
        if packs != {(decision["template_pack"]["id"], decision["template_pack"]["version"])}:
            fail_findings.append({"code": "DECISION_TEMPLATE_PACK_MISMATCH"})
        if policies != {(decision["policy"]["version"], decision["policy"]["sha256"])}:
            fail_findings.append({"code": "DECISION_POLICY_MISMATCH"})
        if expected_source_ids is None:
            expected_source_ids = [item["source_id"] for bundle in tasks for item in bundle["item_batch"]]

    decisions = decision["decisions"]
    unresolved = decision["unresolved_items"]
    decision_ids = [value["source_id"] for value in decisions]
    unresolved_ids = [value["source_id"] for value in unresolved]
    if len(decision_ids) != len(set(decision_ids)) or len(unresolved_ids) != len(set(unresolved_ids)):
        fail_findings.append({"code": "DECISION_SOURCE_ID_DUPLICATE"})
    overlap = sorted(set(decision_ids) & set(unresolved_ids))
    if overlap:
        fail_findings.append({"code": "DECISION_SOURCE_ID_CONFLICT", "source_ids": overlap})

    if expected_source_ids is not None:
        expected = set(expected_source_ids)
        actual = set(decision_ids) | set(unresolved_ids)
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        if missing:
            review_findings.append({"code": "DECISION_SOURCE_UNRESOLVED", "source_ids": missing})
        if unknown:
            fail_findings.append({"code": "DECISION_SOURCE_ID_UNKNOWN", "source_ids": unknown})

    evidence = {value["evidence_id"]: value for value in decision["evidence_bindings"]}
    if len(evidence) != len(decision["evidence_bindings"]):
        fail_findings.append({"code": "DECISION_EVIDENCE_ID_DUPLICATE"})
    for value in decisions:
        source_id = value["source_id"]
        missing_evidence = [evidence_id for evidence_id in value["evidence_ids"] if evidence_id not in evidence]
        mismatched = [
            evidence_id
            for evidence_id in value["evidence_ids"]
            if evidence_id in evidence and evidence[evidence_id]["source_id"] != source_id
        ]
        if missing_evidence or mismatched:
            review_findings.append({
                "code": "DECISION_EVIDENCE_MISSING",
                "source_id": source_id,
                "missing_evidence_ids": missing_evidence,
                "mismatched_evidence_ids": mismatched,
            })
        if float(value["confidence"]) < confidence_threshold:
            review_findings.append({"code": "DECISION_LOW_CONFIDENCE", "source_id": source_id, "confidence": value["confidence"]})
        if value["action"] == "human_review":
            review_findings.append({"code": "DECISION_ACTION_REQUIRES_HUMAN_REVIEW", "source_id": source_id})
    for value in unresolved:
        code = "DECISION_CONFLICT" if value["reason_code"] == "CONFLICT" else "DECISION_UNRESOLVED_ITEM"
        review_findings.append({"code": code, "source_id": value["source_id"], "reason_code": value["reason_code"]})
        missing_evidence = [evidence_id for evidence_id in value["evidence_ids"] if evidence_id not in evidence]
        if missing_evidence:
            review_findings.append({"code": "DECISION_EVIDENCE_MISSING", "source_id": value["source_id"], "missing_evidence_ids": missing_evidence})

    if not decision["product"]["model"] or not decision["product"]["full_name"]:
        review_findings.append({"code": "PRODUCT_IDENTITY_REVIEW_REQUIRED"})
    identity = decision["identity_review"]
    if identity["status"] == "REVIEW_REQUIRED":
        review_findings.append({"code": "IDENTITY_REVIEW_REQUIRED"})
    identity_actions = {"remove_identity", "redact_identity", "preserve_sanitized_image"}
    if identity["status"] == "NO_SOURCE_IDENTITY_FOUND" and any(value["action"] in identity_actions for value in decisions):
        review_findings.append({"code": "IDENTITY_REVIEW_CONFLICT"})
    missing_identity_evidence = [value for value in identity["evidence_ids"] if value not in evidence]
    if missing_identity_evidence:
        review_findings.append({"code": "IDENTITY_EVIDENCE_MISSING", "evidence_ids": missing_identity_evidence})

    findings = [*fail_findings, *review_findings]
    status = "FAIL" if fail_findings else "HUMAN_REVIEW" if review_findings else "PASS"
    return result(
        status,
        "content_decision_v3",
        findings=findings,
        decision_count=len(decisions),
        unresolved_count=len(unresolved),
        evidence_count=len(evidence),
    )


def _evidence_for_migration(source_document: dict[str, Any], item: dict[str, Any], source_id: str) -> dict[str, Any]:
    normalized = source_document.get("normalized") if isinstance(source_document.get("normalized"), dict) else {}
    source = source_document.get("source") if isinstance(source_document.get("source"), dict) else {}
    path = str(item.get("path") or item.get("media_path") or normalized.get("path") or source.get("path") or f"source-document.json#{source_id}")
    payload = _item_payload(item)
    digest = _normalized_sha(item.get("sha256") or payload.get("sha256") or normalized.get("sha256") or source.get("sha256"), fallback=item)
    evidence_id = f"EVID-{canonical_sha256({'source_id': source_id, 'path': path, 'sha256': digest})}"
    return {
        "evidence_id": evidence_id,
        "source_id": source_id,
        "kind": _item_kind(item),
        "path": path,
        "sha256": digest,
        "page_or_position": _location(item),
    }


def migrate_content_map_v1(
    content_map: dict[str, Any],
    source_document: dict[str, Any],
    *,
    content_map_path: Path | None = None,
    template_pack_id: str,
    template_pack_version: str,
    policy_version: str = "decision-policy-v3",
    policy_sha256: str = "",
    source_document_artifact_sha256: str | None = None,
) -> dict[str, Any]:
    """Convert an explicitly supplied V2 content_map v1 into ContentDecisionV3.

    Invalid or incomplete legacy mappings become unresolved items.  The adapter
    never turns missing V2 evidence into a silently approved V3 decision.
    """

    items = _source_items(source_document)
    item_by_id = {_item_id(item): item for item in items}
    mappings_by_id: dict[str, list[dict[str, Any]]] = {}
    for mapping in content_map.get("mappings", []):
        if isinstance(mapping, dict):
            mappings_by_id.setdefault(str(mapping.get("source_id") or ""), []).append(mapping)
    unknown_mapping_ids = sorted(set(mappings_by_id) - set(item_by_id))
    if unknown_mapping_ids:
        raise DecisionBundleError(f"V2 content_map references unknown source_id values: {unknown_mapping_ids}")

    evidence_bindings = [_evidence_for_migration(source_document, item, source_id) for source_id, item in item_by_id.items()]
    evidence_by_source = {value["source_id"]: value["evidence_id"] for value in evidence_bindings}
    decisions: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for source_id, item in item_by_id.items():
        mappings = mappings_by_id.get(source_id, [])
        evidence_id = evidence_by_source[source_id]
        if not mappings:
            unresolved.append({"source_id": source_id, "reason_code": "UNCLASSIFIED", "evidence_ids": [evidence_id], "message": "V2 content_map 未覆盖该 source_id。"})
            continue
        if len(mappings) != 1:
            unresolved.append({"source_id": source_id, "reason_code": "CONFLICT", "evidence_ids": [evidence_id], "message": "V2 content_map 中 source_id 重复。"})
            continue
        mapping = mappings[0]
        action = mapping.get("action")
        if action == "human_review":
            unresolved.append({"source_id": source_id, "reason_code": "HUMAN_REVIEW", "evidence_ids": [evidence_id], "message": str(mapping.get("notes") or "V2 映射要求人工复核。")})
            continue
        if action not in ALLOWED_ACTIONS:
            unresolved.append({"source_id": source_id, "reason_code": "UNCLASSIFIED", "evidence_ids": [evidence_id], "message": f"V2 action 不受 V3 支持：{action}"})
            continue
        target_slot = str(mapping.get("target_section") or "").strip()
        reviewed_blocks = mapping.get("reviewed_blocks")
        if action == "reviewed_text" and not target_slot and isinstance(reviewed_blocks, list):
            block_targets = [
                str(block.get("target_section") or "").strip()
                for block in reviewed_blocks
                if isinstance(block, dict) and str(block.get("text") or "").strip()
            ]
            # V2 permits one reviewed OCR source item to be split across several
            # business sections. ContentDecisionV3 still requires a top-level
            # target_slot, so use the first reviewed block as the deterministic
            # fallback while preserving every block's own target below.
            if block_targets and all(block_targets):
                target_slot = block_targets[0]
        if action in TARGET_ACTIONS and not target_slot:
            unresolved.append({"source_id": source_id, "reason_code": "UNCLASSIFIED", "evidence_ids": [evidence_id], "message": "需要目标槽位。"})
            continue
        if action == "redact_identity" and not mapping.get("redactions"):
            unresolved.append({"source_id": source_id, "reason_code": "MISSING_EVIDENCE", "evidence_ids": [evidence_id], "message": "身份脱敏缺少 redactions。"})
            continue
        if action == "reviewed_text" and not (str(mapping.get("reviewed_text") or "").strip() or mapping.get("reviewed_blocks")):
            unresolved.append({"source_id": source_id, "reason_code": "MISSING_EVIDENCE", "evidence_ids": [evidence_id], "message": "审核文本缺少结果。"})
            continue
        replacement_path = str(mapping.get("replacement_path") or "").strip()
        replacement_sha = str(mapping.get("replacement_sha256") or "").strip().upper()
        if action == "preserve_sanitized_image" and (not replacement_path or not SHA256_RE.fullmatch(replacement_sha)):
            unresolved.append({"source_id": source_id, "reason_code": "MISSING_EVIDENCE", "evidence_ids": [evidence_id], "message": "脱敏图片缺少替换路径或 SHA-256。"})
            continue

        decision: dict[str, Any] = {
            "source_id": source_id,
            "action": action,
            "confidence": 1.0,
            "evidence_ids": [evidence_id],
            "rationale": "由显式提供且已经审批的 V2 content_map v1 迁移。",
        }
        if action in TARGET_ACTIONS:
            decision["target_slot"] = target_slot
            decision["target_order"] = max(0, int(mapping.get("target_order") or 0))
        if action == "redact_identity":
            decision["redactions"] = [str(value) for value in mapping.get("redactions", []) if str(value)]
        if action == "preserve_sanitized_image":
            resolved_replacement = Path(replacement_path)
            if not resolved_replacement.is_absolute() and content_map_path is not None:
                resolved_replacement = (content_map_path.resolve().parent / resolved_replacement).resolve()
            decision["replacement_ref"] = {
                "path": str(resolved_replacement),
                "sha256": replacement_sha,
                "review_note": str(mapping.get("sanitization_review") or "V2 已审批脱敏图片迁移"),
            }
        if action == "reviewed_text":
            blocks = reviewed_blocks
            if isinstance(blocks, list):
                converted_blocks = []
                for block in blocks:
                    if not isinstance(block, dict) or not str(block.get("text") or "").strip():
                        continue
                    converted_blocks.append({
                        "text": str(block["text"]),
                        "target_slot": str(block.get("target_section") or target_slot),
                        "target_order": max(0, int(block.get("target_order") or mapping.get("target_order") or 0)),
                        "evidence_ids": [evidence_id],
                    })
                if converted_blocks:
                    decision["reviewed_blocks"] = converted_blocks
                else:
                    unresolved.append({"source_id": source_id, "reason_code": "MISSING_EVIDENCE", "evidence_ids": [evidence_id], "message": "reviewed_blocks 为空或非法。"})
                    continue
            else:
                decision["reviewed_text"] = str(mapping.get("reviewed_text"))
        decisions.append(decision)

    identity_v2 = content_map.get("identity_review") if isinstance(content_map.get("identity_review"), dict) else {}
    identity_status = str(identity_v2.get("status") or "REVIEW_REQUIRED")
    if identity_status not in {"VERIFIED", "NO_SOURCE_IDENTITY_FOUND", "REVIEW_REQUIRED"}:
        identity_status = "REVIEW_REQUIRED"
    identity_evidence_ids = [
        evidence_by_source[source_id]
        for source_id, mappings in mappings_by_id.items()
        if source_id in evidence_by_source
        and any(mapping.get("action") in {"remove_identity", "redact_identity", "preserve_sanitized_image"} for mapping in mappings)
    ]
    if source_document_artifact_sha256:
        document_sha = _policy_sha(source_document_artifact_sha256, fallback=source_document)
    else:
        document_sha = source_document_sha256(source_document)
    normalized_policy_sha = _policy_sha(policy_sha256, fallback={"version": policy_version})
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": "",
        "task_ids": [f"MIGRATION-TASK-{canonical_sha256(content_map)}"],
        "source_document_sha256": document_sha,
        "template_pack": {"id": str(template_pack_id), "version": str(template_pack_version)},
        "policy": {"version": str(policy_version), "sha256": normalized_policy_sha},
        "product": {
            "model": str(content_map.get("product", {}).get("model") or ""),
            "full_name": str(content_map.get("product", {}).get("full_name") or ""),
        },
        "decisions": decisions,
        "unresolved_items": unresolved,
        "identity_review": {
            "status": identity_status,
            "manufacturer_terms": sorted({str(value) for value in content_map.get("manufacturer_terms", []) if str(value)}),
            "evidence_ids": sorted(set(identity_evidence_ids)),
            "notes": str(identity_v2.get("notes") or "V2 content_map v1 migration"),
        },
        "evidence_bindings": evidence_bindings,
        "migration": {
            "from_schema": "content_map_v1",
            "adapter_version": "v3.0.0",
            "source_sha256": canonical_sha256(content_map),
        },
    }
    payload["decision_id"] = f"MIGRATION-{canonical_sha256({key: value for key, value in payload.items() if key != 'decision_id'})}"
    errors = validate_instance(payload, load_schema(CONTENT_DECISION_SCHEMA_PATH))
    if errors:
        raise DecisionBundleError(f"V2 migration generated an invalid ContentDecisionV3: {errors[:3]}")
    return payload


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="生成或验证 V3 中立决策任务包")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="只为未决 source item 生成紧凑任务包")
    generate.add_argument("--source-document", type=Path, required=True)
    generate.add_argument("--candidates", type=Path)
    generate.add_argument("--template-pack-id", required=True)
    generate.add_argument("--template-pack-version", required=True)
    generate.add_argument("--allowed-slots", required=True, help="逗号分隔的已批准槽位")
    generate.add_argument("--policy-version", default="decision-policy-v3")
    generate.add_argument("--policy-sha256", default="")
    generate.add_argument("--output-dir", type=Path, required=True)
    generate.add_argument("--report", type=Path)

    validate = subparsers.add_parser("validate", help="校验外部 ContentDecisionV3")
    validate.add_argument("--decision", type=Path, required=True)
    validate.add_argument("--task-bundle", type=Path, action="append", default=[])
    validate.add_argument("--report", type=Path)

    migrate = subparsers.add_parser("migrate-v2", help="显式迁移已审批 content_map v1")
    migrate.add_argument("--content-map", type=Path, required=True)
    migrate.add_argument("--source-document", type=Path, required=True)
    migrate.add_argument("--template-pack-id", required=True)
    migrate.add_argument("--template-pack-version", required=True)
    migrate.add_argument("--output", type=Path, required=True)
    migrate.add_argument("--report", type=Path)

    args = parser.parse_args()
    try:
        if args.command == "generate":
            source_document = read_json(args.source_document)
            candidates = read_json(args.candidates) if args.candidates else []
            if isinstance(candidates, dict):
                candidates = candidates.get("deterministic_candidates", [])
            bundles = build_decision_task_bundles(
                source_document,
                candidates,
                template_pack_id=args.template_pack_id,
                template_pack_version=args.template_pack_version,
                allowed_slots=_split_csv(args.allowed_slots),
                evidence_dir=args.output_dir / "evidence",
                bundle_root=args.output_dir,
                policy_version=args.policy_version,
                policy_sha256=args.policy_sha256,
            )
            paths = write_decision_task_bundles(bundles, args.output_dir)
            status = "HUMAN_REVIEW" if bundles else "PASS"
            findings = [{"code": "DECISION_BUNDLE_REQUIRED", "bundle_count": len(bundles)}] if bundles else []
            return finish(result(status, "decision_bundle_v3", findings=findings, bundles=[str(path) for path in paths]), args.report)
        if args.command == "validate":
            decision = read_json(args.decision) if args.decision.is_file() else None
            bundles = [read_json(path) for path in args.task_bundle]
            return finish(validate_content_decision(decision, task_bundles=bundles), args.report)
        content_map = read_json(args.content_map)
        source_document = read_json(args.source_document)
        decision = migrate_content_map_v1(
            content_map,
            source_document,
            content_map_path=args.content_map,
            template_pack_id=args.template_pack_id,
            template_pack_version=args.template_pack_version,
        )
        write_json(args.output, decision)
        report = validate_content_decision(decision, expected_source_ids=[_item_id(item) for item in _source_items(source_document)])
        return finish(report, args.report)
    except (DecisionBundleError, OSError, ValueError, json.JSONDecodeError) as exc:
        report_path = getattr(args, "report", None)
        return finish(result("FAIL", "decision_bundle_v3", findings=[{"code": "DECISION_SCHEMA_INVALID", "message": str(exc)}]), report_path)


if __name__ == "__main__":
    raise SystemExit(main())
