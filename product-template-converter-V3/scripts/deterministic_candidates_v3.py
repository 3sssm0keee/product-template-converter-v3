from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from decision_bundle_v3 import source_document_sha256
from pipeline_common import finish, read_json, result, write_json


SCHEMA_VERSION = "3.0.0"
GENERATOR_VERSION = "3.0.0"
SAFE_REMOVE_ACTIONS = {"remove_identity", "remove_template_background"}

# This is deliberately kept in lockstep with SourceDocumentV3's presentation
# metadata contract.  The deterministic layer is a second trust boundary: it
# must not infer removal merely from a semantic role or from an item's text.
PRESENTATION_TEMPLATE_REGIONS = {
    "slide_master",
    "slide_layout",
    "notes_master",
}
PRESENTATION_TEMPLATE_ITEM_KINDS = {
    "text",
    "shape_description",
    "image",
    "media",
    "table",
}

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+(?![\w-])"
)
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\[\]{}\"'，。；、]+")
DOMAIN_RE = re.compile(
    r"(?i)(?<![@\w-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|cn|net|org|gov|edu|io|co|biz|info|tech|ai|xyz|top|shop|site)(?![\w-])"
)
MOBILE_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")
LABELED_PHONE_RE = re.compile(
    r"(?i)(?:电话|手机|热线|传真|tel(?:ephone)?|phone|fax)\s*[:：]?\s*"
    r"((?:\+?86[-\s]?)?(?:0\d{2,3}[-\s]?)?\d{7,11}(?:[-转]\d{1,6})?)"
)
ORGANIZATION_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9（）()·&]{2,40}?"
    r"(?:股份有限公司|有限责任公司|有限公司|集团有限公司|集团|研究院|研究所)"
)
QR_MARKERS = ("二维码", "qr code", "qrcode", "qr-code", "qr_code")


class DeterministicCandidateError(ValueError):
    pass


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest().upper()


def _source_items(source_document: dict[str, Any]) -> list[dict[str, Any]]:
    if source_document.get("artifact_type") not in {None, "SourceDocumentV3"}:
        raise DeterministicCandidateError("输入不是 SourceDocumentV3")
    content = source_document.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("items"), list):
        raise DeterministicCandidateError("SourceDocumentV3.content.items 必须是数组")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(content["items"]):
        if not isinstance(item, dict):
            raise DeterministicCandidateError(f"content.items[{index}] 不是对象")
        source_id = str(item.get("source_id") or item.get("id") or "").strip()
        if not source_id:
            raise DeterministicCandidateError(f"content.items[{index}] 缺少 source_id")
        if source_id in seen:
            raise DeterministicCandidateError(f"SourceDocumentV3 存在重复 source_id: {source_id}")
        seen.add(source_id)
        items.append(item)
    return items


def _identity_terms(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        for key in ("manufacturer_terms", "identity_terms", "terms"):
            if key in value:
                value = value[key]
                break
    if not isinstance(value, list):
        raise DeterministicCandidateError("identity_terms 必须是字符串数组")
    terms: list[str] = []
    for raw in value:
        term = str(raw).strip()
        if term and term.casefold() not in {existing.casefold() for existing in terms}:
            terms.append(term)
    return terms


def _identity_domains(terms: Iterable[str]) -> set[str]:
    domains: set[str] = set()
    for term in terms:
        candidate = term.strip().casefold()
        if "@" in candidate:
            candidate = candidate.rsplit("@", 1)[-1]
        if "://" in candidate:
            candidate = urlparse(candidate).hostname or ""
        candidate = candidate.strip("./ ")
        if re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", candidate):
            domains.add(candidate)
    return domains


def _normalize_slots(value: Any) -> tuple[list[dict[str, str]], dict[str, int]]:
    if value is None:
        return [], {}
    if isinstance(value, dict):
        value = value.get("slots")
    if not isinstance(value, list):
        raise DeterministicCandidateError("template_slots 必须是数组或包含 slots 数组的对象")
    slots: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if isinstance(raw, str):
            slot_id = raw.strip()
            slot_kind = "unspecified"
        elif isinstance(raw, dict):
            slot_id = str(raw.get("slot_id") or raw.get("id") or raw.get("name") or "").strip()
            slot_kind = str(
                raw.get("content_type") or raw.get("slot_type") or raw.get("kind") or "unspecified"
            ).strip().lower()
        else:
            raise DeterministicCandidateError(f"template_slots[{index}] 类型无效")
        if not slot_id or slot_id in seen:
            raise DeterministicCandidateError("模板 slot ID 必须非空且唯一")
        seen.add(slot_id)
        slots.append({"slot_id": slot_id, "slot_kind": slot_kind})
    return slots, dict(sorted(Counter(slot["slot_kind"] for slot in slots).items()))


def _source_document_sha256(source_document: dict[str, Any]) -> str:
    return source_document_sha256(source_document)


def _excerpt(text: str, start: int, end: int, radius: int = 36) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return text[left:right]


def _signal(
    signal_type: str,
    field: str,
    value: str,
    text: str,
    start: int,
    end: int,
) -> dict[str, Any]:
    core = {
        "type": signal_type,
        "field": field,
        "value": value,
        "start": start,
        "end": end,
        "excerpt": _excerpt(text, start, end),
    }
    return {"signal_id": f"SIG-{canonical_sha256(core)}", **core}


def _regex_signals(
    signal_type: str,
    pattern: re.Pattern[str],
    text: str,
    field: str,
    *,
    group: int = 0,
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for match in pattern.finditer(text):
        start, end = match.span(group)
        value = match.group(group).rstrip(".,;:!?，。；：！？、)")
        end = start + len(value)
        if value:
            signals.append(_signal(signal_type, field, value, text, start, end))
    return signals


def _text_signals(
    text: str,
    field: str,
    terms: list[str],
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    signals.extend(_regex_signals("email", EMAIL_RE, text, field))
    signals.extend(_regex_signals("url", URL_RE, text, field))
    signals.extend(_regex_signals("domain", DOMAIN_RE, text, field))
    signals.extend(_regex_signals("phone", MOBILE_RE, text, field))
    signals.extend(_regex_signals("phone", LABELED_PHONE_RE, text, field, group=1))
    signals.extend(_regex_signals("organization_name", ORGANIZATION_RE, text, field))
    folded = text.casefold()
    for term in terms:
        folded_term = term.casefold()
        start = 0
        while folded_term and (index := folded.find(folded_term, start)) >= 0:
            end = index + len(term)
            signals.append(_signal("configured_identity_term", field, text[index:end], text, index, end))
            start = end
    return signals


def _walk_strings(value: Any, prefix: str) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from _walk_strings(value[key], f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_strings(child, f"{prefix}[{index}]")


def _item_text_fields(item: dict[str, Any]) -> list[tuple[str, str]]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    payload_type = str(payload.get("type") or "")
    fields: list[tuple[str, str]] = []
    if payload_type == "text":
        fields.append(("payload.text", str(payload.get("text") or "")))
    elif payload_type == "description":
        for key in ("text", "name", "title", "description"):
            if payload.get(key):
                fields.append((f"payload.{key}", str(payload[key])))
    elif payload_type == "table":
        for row_index, row in enumerate(payload.get("rows") or []):
            if not isinstance(row, list):
                continue
            for column_index, cell in enumerate(row):
                fields.append((f"payload.rows[{row_index}][{column_index}]", str(cell)))
    elif payload_type == "relationship":
        fields.append(("payload.target", str(payload.get("target") or "")))
    elif payload_type == "asset":
        fields.append(("payload.name", str(payload.get("name") or "")))
    elif payload_type == "generic":
        fields.extend(_walk_strings(payload.get("value"), "payload.value"))
    if item.get("location"):
        fields.append(("location", str(item["location"])))
    attributes = item.get("attributes")
    if isinstance(attributes, dict):
        fields.extend(_walk_strings(attributes, "attributes"))
    return [(field, text) for field, text in fields if text]


def _deduplicate_signals(signals: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, int, int]] = set()
    for signal in signals:
        key = (
            str(signal["type"]),
            str(signal["field"]),
            str(signal["value"]).casefold(),
            int(signal["start"]),
            int(signal["end"]),
        )
        if key not in seen:
            seen.add(key)
            output.append(signal)
    return sorted(
        output,
        key=lambda value: (
            value["field"],
            value["start"],
            value["end"],
            value["type"],
            value["value"],
        ),
    )


def _asset_digest(item: dict[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    if payload.get("type") != "asset":
        return ""
    return str(payload.get("visual_sha256") or payload.get("sha256") or "").upper()


def _asset_occurrences(items: list[dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for item in items:
        digest = _asset_digest(item)
        if digest:
            groups[digest].append(str(item.get("source_id") or item.get("id") or ""))
    return groups


def _target_hostname(item: dict[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    if payload.get("type") != "relationship":
        return ""
    target = str(payload.get("target") or "").strip()
    parsed = urlparse(target if "://" in target else f"//{target}")
    if parsed.scheme == "mailto":
        return parsed.path.rsplit("@", 1)[-1].casefold()
    return str(parsed.hostname or "").casefold()


def _explicit_safe_remove(item: dict[str, Any]) -> str | None:
    attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    # Only the current, explicit approval contract is accepted.  In particular,
    # do not treat the historical deterministic_remove_action alias as approval:
    # callers must provide both safe_remove_action and the boolean approval flag.
    action = str(attributes.get("safe_remove_action") or "").strip()
    approved = attributes.get("deterministic_remove_approved") is True
    return action if approved and action in SAFE_REMOVE_ACTIONS else None


def _presentation_region(item: dict[str, Any]) -> str | None:
    """Resolve the typed presentation region from SourceDocument evidence."""

    context = item.get("context") if isinstance(item.get("context"), dict) else {}
    explicit = str(context.get("region") or "").strip().casefold()
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


def _is_presentation_template_item(
    item: dict[str, Any],
    content_kind: str,
) -> bool:
    """Return whether an approved item belongs to the removable template area."""

    if content_kind.casefold() != "presentation":
        return False
    if _presentation_region(item) not in PRESENTATION_TEMPLATE_REGIONS:
        return False
    return str(item.get("kind") or "").casefold() in PRESENTATION_TEMPLATE_ITEM_KINDS


def _is_presentation_shape_description_placeholder(
    item: dict[str, Any],
    content_kind: str,
) -> bool:
    """Return whether an item is a structurally identified slide shape.

    Shape descriptions are emitted by the presentation inventory from OOXML
    ``cNvPr``/``docPr`` metadata.  Their text is not sufficient evidence for
    deleting anything, so this predicate deliberately uses only the typed
    SourceDocument kind, presentation content kind, and structural slide
    location (or an explicit structural-placeholder marker).
    """

    if content_kind.casefold() != "presentation":
        return False
    if str(item.get("kind") or "").casefold() != "shape_description":
        return False
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    if str(payload.get("type") or "").casefold() != "description":
        return False

    region = _presentation_region(item)
    if region not in PRESENTATION_TEMPLATE_REGIONS | {"slide", "notes"}:
        return False
    location = str(item.get("location") or "").replace("\\", "/").casefold()
    context = item.get("context") if isinstance(item.get("context"), dict) else {}
    part = str(context.get("part") or context.get("source_part") or "").replace("\\", "/").casefold()
    hints = (location, part)
    if region in PRESENTATION_TEMPLATE_REGIONS:
        # Template shape descriptions are structural by region; unlike live
        # slide/notes shapes, their authored-looking names are still part of
        # the template infrastructure contract.
        marker = {
            "slide_master": "ppt/slidemasters/slidemaster",
            "slide_layout": "ppt/slidelayouts/slidelayout",
            "notes_master": "ppt/notesmasters/notesmaster",
        }[region]
        return any(marker in hint for hint in hints)
    # A title/descr on a visible slide or notes shape is authored metadata,
    # regardless of any stale approval attributes supplied by an upstream
    # producer.  ``name``/``text`` remain implementation evidence.
    if _shape_description_has_authored_metadata(item):
        return False
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


def _safe_resolution(
    item: dict[str, Any],
    signals: list[dict[str, Any]],
    identity_domains: set[str],
    *,
    content_kind: str,
) -> tuple[str, str] | None:
    explicit = _explicit_safe_remove(item)
    if explicit:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        item_kind = str(item.get("kind") or "").casefold()
        # Explicit removal is fail-closed by the presentation region/kind
        # whitelist.  Template infrastructure may contain text, shape
        # metadata, assets, or tables; live slides/notes accept only an
        # un-authored shape description.  It must never spill into ordinary
        # notes content.
        if explicit == "remove_template_background":
            if not (
                _is_presentation_template_item(item, content_kind)
                or _is_presentation_shape_description_placeholder(item, content_kind)
            ):
                return None
        if explicit == "remove_identity" and (
            item_kind != "relationship" or payload.get("type") != "relationship"
        ):
            return None
        return explicit, "EXPLICIT_DETERMINISTIC_REMOVE_APPROVAL"

    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    if payload.get("type") == "relationship" and identity_domains:
        hostname = _target_hostname(item)
        if hostname and any(hostname == domain or hostname.endswith(f".{domain}") for domain in identity_domains):
            return "remove_identity", "CONFIGURED_IDENTITY_RELATIONSHIP"

    # Do not infer template-background deletion from an image's semantic role.
    # A removal is permitted only when the SourceDocument carries the explicit
    # safe_remove_action plus deterministic_remove_approved contract above.
    return None


SIGNAL_REASON_CODES = {
    "email": "EMAIL_CANDIDATE",
    "url": "URL_CANDIDATE",
    "domain": "DOMAIN_CANDIDATE",
    "phone": "PHONE_CANDIDATE",
    "organization_name": "ORGANIZATION_NAME_CANDIDATE",
    "configured_identity_term": "CONFIGURED_IDENTITY_TERM_MATCH",
    "qr_candidate": "QR_CANDIDATE",
    "image": "IMAGE_ITEM",
    "repeated_asset": "REPEATED_ASSET_CANDIDATE",
    "obvious_table": "OBVIOUS_TABLE_ITEM",
    "obvious_text": "OBVIOUS_TEXT_ITEM",
    "ocr_text": "OCR_TEXT_ITEM",
    "metadata": "METADATA_ITEM",
    "external_relationship": "EXTERNAL_RELATIONSHIP_ITEM",
    "structural_placeholder": "STRUCTURAL_PLACEHOLDER_ITEM",
}


def _structural_signals(
    item: dict[str, Any],
    repeated_groups: dict[str, list[str]],
    *,
    content_kind: str,
) -> list[dict[str, Any]]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    payload_type = str(payload.get("type") or "")
    location = str(item.get("location") or "")
    source_id = str(item.get("source_id") or item.get("id") or "")
    signals: list[dict[str, Any]] = []

    def add(signal_type: str, field: str, value: str) -> None:
        signals.append(_signal(signal_type, field, value, value, 0, len(value)))

    if payload_type == "table":
        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        column_count = max((len(row) for row in rows if isinstance(row, list)), default=0)
        add("obvious_table", "payload.type", f"rows={len(rows)};columns={column_count}")
    elif payload_type in {"text", "description"}:
        add("obvious_text", "payload.type", payload_type)
        context = item.get("context") if isinstance(item.get("context"), dict) else {}
        if context.get("extraction") == "ocr":
            add("ocr_text", "context.extraction", "ocr")
    elif payload_type == "asset":
        add("image", "payload.type", str(payload.get("media_kind") or "asset"))
        digest = _asset_digest(item)
        occurrences = repeated_groups.get(digest, [])
        if len(occurrences) >= 2:
            add("repeated_asset", "payload.visual_sha256", f"count={len(occurrences)}")
    elif payload_type == "relationship":
        add("external_relationship", "payload.type", "relationship")
    if str(item.get("kind") or "").casefold() == "metadata":
        add("metadata", "kind", "metadata")

    qr_text = " ".join(text for _field, text in _item_text_fields(item)).casefold()
    attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
    qr_explicit = attributes.get("qr_detected") is True or bool(attributes.get("qr_payload"))
    if qr_explicit or any(marker in qr_text for marker in QR_MARKERS):
        add("qr_candidate", "item", f"source_id={source_id};location={location}")
    if _is_presentation_shape_description_placeholder(item, content_kind):
        add("structural_placeholder", "item", f"source_id={source_id};location={location}")
    return signals


def _candidate_for_item(
    item: dict[str, Any],
    *,
    content_kind: str,
    terms: list[str],
    identity_domains: set[str],
    repeated_groups: dict[str, list[str]],
    source_document_sha256: str,
) -> dict[str, Any]:
    source_id = str(item.get("source_id") or item.get("id") or "")
    signals: list[dict[str, Any]] = []
    for field, text in _item_text_fields(item):
        signals.extend(_text_signals(text, field, terms))
    signals.extend(_structural_signals(item, repeated_groups, content_kind=content_kind))
    signals = _deduplicate_signals(signals)
    reason_codes = sorted({SIGNAL_REASON_CODES[signal["type"]] for signal in signals})
    resolution = _safe_resolution(item, signals, identity_domains, content_kind=content_kind)
    core: dict[str, Any] = {
        "source_id": source_id,
        "resolution_status": "deterministic_resolved" if resolution else "unresolved",
        "reason_codes": reason_codes or ["NO_DETERMINISTIC_DISPOSITION"],
        "signals": signals,
        "evidence_trace": {
            "source_document_sha256": source_document_sha256,
            "source_id": source_id,
            "location": str(item.get("location") or ""),
            "payload_type": str(
                (item.get("payload") or {}).get("type")
                if isinstance(item.get("payload"), dict)
                else item.get("kind") or ""
            ),
        },
    }
    if resolution:
        core["proposed_action"] = resolution[0]
        # Keep the resolved action explicit for downstream deterministic merge
        # and make its provenance part of the candidate artifact itself.
        core["resolved_action"] = resolution[0]
        core["resolution_trace"] = {
            "source": "deterministic_rule",
            "reason_code": resolution[1],
            "source_document_sha256": source_document_sha256,
            "source_id": source_id,
        }
        core["reason_codes"] = sorted(set(core["reason_codes"] + [resolution[1]]))
        core["confidence"] = 1.0
    core["candidate_id"] = f"CAND-{canonical_sha256(core)}"
    return core


def _metadata_signals(content: dict[str, Any], terms: list[str]) -> list[dict[str, Any]]:
    metadata = content.get("metadata")
    if not isinstance(metadata, dict):
        return []
    signals: list[dict[str, Any]] = []
    for field, text in _walk_strings(metadata, "content.metadata"):
        signals.extend(_text_signals(text, field, terms))
    return _deduplicate_signals(signals)


def analyze_deterministic_candidates(
    source_document: dict[str, Any],
    *,
    template_slots: Any = None,
    identity_terms: Any = None,
) -> dict[str, Any]:
    if not isinstance(source_document, dict):
        raise DeterministicCandidateError("SourceDocumentV3 必须是 JSON 对象")
    items = _source_items(source_document)
    content = source_document.get("content") if isinstance(source_document.get("content"), dict) else {}
    content_kind = str(content.get("kind") or "")
    terms = _identity_terms(identity_terms)
    domains = _identity_domains(terms)
    slots, slot_summary = _normalize_slots(template_slots)
    repeated_groups = _asset_occurrences(items)
    document_sha = _source_document_sha256(source_document)
    candidates = [
        _candidate_for_item(
            item,
            content_kind=content_kind,
            terms=terms,
            identity_domains=domains,
            repeated_groups=repeated_groups,
            source_document_sha256=document_sha,
        )
        for item in items
    ]
    candidate_ids = [candidate["candidate_id"] for candidate in candidates]
    source_ids = [candidate["source_id"] for candidate in candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise DeterministicCandidateError("确定性 candidate_id 不唯一")
    if len(source_ids) != len(set(source_ids)) or len(source_ids) != len(items):
        raise DeterministicCandidateError("每个 source_id 必须且只能有一个确定性候选")
    metadata_signals = _metadata_signals(source_document.get("content") or {}, terms)
    resolved = sum(candidate["resolution_status"] == "deterministic_resolved" for candidate in candidates)
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "DeterministicCandidateAnalysisV3",
        "generator": {
            "name": "deterministic_candidates_v3",
            "version": GENERATOR_VERSION,
        },
        "source_document_sha256": document_sha,
        "deterministic_candidates": candidates,
        "document_signals": {
            "metadata_candidates": metadata_signals,
            "template_slots": {
                "count": len(slots),
                "kinds": slot_summary,
                "business_slot_inference_performed": False,
            },
        },
        "counts": {
            "source_items": len(items),
            "candidates": len(candidates),
            "resolved": resolved,
            "unresolved": len(candidates) - resolved,
            "metadata_signals": len(metadata_signals),
        },
    }


def build_deterministic_candidates(
    source_document: dict[str, Any],
    *,
    allowed_slots: Any = None,
    manufacturer_terms: Any = None,
    template_slots: Any = None,
    identity_terms: Any = None,
) -> list[dict[str, Any]]:
    """返回可直接传给 build_decision_task_bundles 的候选数组。"""

    return analyze_deterministic_candidates(
        source_document,
        template_slots=template_slots if template_slots is not None else allowed_slots,
        identity_terms=identity_terms if identity_terms is not None else manufacturer_terms,
    )["deterministic_candidates"]


def apply_deterministic_resolutions(
    decision: dict[str, Any],
    candidates: list[dict[str, Any]],
    source_document: dict[str, Any],
    *,
    source_document_artifact_sha256: str,
) -> dict[str, Any]:
    """把极少数可证明安全的代码决策合并进 ContentDecisionV3。

    该函数只接受 remove_identity/remove_template_background，不改写外部决策；
    任一冲突、缺少证据或哈希漂移都会 fail-closed。
    """

    from decision_bundle_v3 import CONTENT_DECISION_SCHEMA_PATH
    from schema_validation_v3 import load_schema, validate_instance

    if not isinstance(decision, dict):
        raise DeterministicCandidateError("ContentDecisionV3 必须是对象")
    expected_sha = str(source_document_artifact_sha256 or "").upper()
    if not re.fullmatch(r"[0-9A-F]{64}", expected_sha):
        raise DeterministicCandidateError("source_document_artifact_sha256 无效")
    if str(decision.get("source_document_sha256") or "").upper() != expected_sha:
        raise DeterministicCandidateError("ContentDecisionV3 与 SourceDocumentV3 哈希不一致")

    items = {
        str(item.get("source_id") or item.get("id") or ""): item
        for item in _source_items(source_document)
    }
    merged = deepcopy(decision)
    decisions = merged.get("decisions")
    unresolved = merged.get("unresolved_items")
    evidence = merged.get("evidence_bindings")
    if not isinstance(decisions, list) or not isinstance(unresolved, list) or not isinstance(evidence, list):
        raise DeterministicCandidateError("ContentDecisionV3 decisions/unresolved_items/evidence_bindings 无效")
    changed = False
    decision_ids = {str(value.get("source_id") or "") for value in decisions if isinstance(value, dict)}
    unresolved_ids = {str(value.get("source_id") or "") for value in unresolved if isinstance(value, dict)}
    evidence_ids = {str(value.get("evidence_id") or "") for value in evidence if isinstance(value, dict)}

    for candidate in candidates:
        if str(candidate.get("resolution_status") or "").lower() not in {"resolved", "deterministic_resolved"}:
            continue
        source_id = str(candidate.get("source_id") or "")
        action = str(
            candidate.get("resolved_action")
            or candidate.get("proposed_action")
            or candidate.get("action")
            or ""
        )
        if source_id not in items or action not in SAFE_REMOVE_ACTIONS:
            raise DeterministicCandidateError(f"resolved candidate 不可安全合并: {source_id}: {action}")
        candidate_actions = {
            str(candidate.get(key) or "").strip()
            for key in ("resolved_action", "proposed_action", "action")
            if str(candidate.get(key) or "").strip()
        }
        if candidate_actions != {action}:
            raise DeterministicCandidateError(
                f"resolved candidate action 字段冲突: {source_id}: {sorted(candidate_actions)}"
            )
        if action == "remove_template_background":
            item = items[source_id]
            content = source_document.get("content") if isinstance(source_document.get("content"), dict) else {}
            content_kind = str(content.get("kind") or "")
            # Never manufacture or trust a background-removal approval at
            # merge time.  The candidate must still point at the narrowly
            # supported typed item and carry both explicit source attributes.
            expected_resolution = _safe_resolution(
                items[source_id],
                [],
                set(),
                content_kind=content_kind,
            )
            if expected_resolution is None or expected_resolution[0] != action:
                raise DeterministicCandidateError(
                    f"remove_template_background 不符合 presentation 模板白名单: {source_id}"
                )
        if source_id in unresolved_ids:
            raise DeterministicCandidateError(f"resolved candidate 与外部 unresolved 冲突: {source_id}")
        if source_id in decision_ids:
            # 外部结果优先，函数不得覆盖它；后续 schema/证据校验仍会检查其有效性。
            continue
        item = items[source_id]
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        normalized = source_document.get("normalized") if isinstance(source_document.get("normalized"), dict) else {}
        source_ref = source_document.get("source") if isinstance(source_document.get("source"), dict) else {}
        evidence_sha = str(payload.get("sha256") or normalized.get("sha256") or source_ref.get("sha256") or "").upper()
        if not re.fullmatch(r"[0-9A-F]{64}", evidence_sha):
            raise DeterministicCandidateError(f"resolved candidate 缺少可验证证据 SHA: {source_id}")
        evidence_path = str(payload.get("path") or normalized.get("path") or source_ref.get("path") or "")
        if not evidence_path:
            raise DeterministicCandidateError(f"resolved candidate 缺少证据路径: {source_id}")
        evidence_id = f"EVID-{canonical_sha256({'source_id': source_id, 'path': evidence_path, 'sha256': evidence_sha})}"
        if evidence_id not in evidence_ids:
            evidence.append({
                "evidence_id": evidence_id,
                "source_id": source_id,
                "kind": str(item.get("kind") or payload.get("type") or "item"),
                "path": evidence_path,
                "sha256": evidence_sha,
                "page_or_position": str(item.get("location") or ""),
            })
            evidence_ids.add(evidence_id)
            changed = True
        decisions.append({
            "source_id": source_id,
            "action": action,
            "confidence": 1.0,
            "evidence_ids": [evidence_id],
            "rationale": "由白名单确定性规则和可验证证据生成；未调用模型。",
        })
        decision_ids.add(source_id)
        changed = True

    if not changed:
        return merged
    merged["decisions"] = sorted(
        decisions,
        key=lambda value: str(value.get("source_id") or "") if isinstance(value, dict) else "",
    )
    merged["evidence_bindings"] = sorted(
        evidence,
        key=lambda value: str(value.get("evidence_id") or "") if isinstance(value, dict) else "",
    )
    prefix = "MIGRATION" if merged.get("migration") else "DECISION"
    merged["decision_id"] = f"{prefix}-{canonical_sha256({key: value for key, value in merged.items() if key != 'decision_id'})}"
    errors = validate_instance(merged, load_schema(CONTENT_DECISION_SCHEMA_PATH))
    if errors:
        raise DeterministicCandidateError(f"确定性合并生成了非法 ContentDecisionV3: {errors[:3]}")
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 V3 确定性内容与身份候选")
    parser.add_argument("--source-document", type=Path, required=True)
    parser.add_argument("--template-slots", type=Path)
    parser.add_argument("--identity-terms", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="输出可直接传入 decision bundle 的 JSON 数组")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        source_document = read_json(args.source_document)
        template_slots = read_json(args.template_slots) if args.template_slots else None
        identity_terms = read_json(args.identity_terms) if args.identity_terms else None
        analysis = analyze_deterministic_candidates(
            source_document,
            template_slots=template_slots,
            identity_terms=identity_terms,
        )
        write_json(args.output, analysis["deterministic_candidates"])
        payload = result(
            "PASS",
            "deterministic_candidates_v3",
            findings=[],
            schema_version=SCHEMA_VERSION,
            output=str(args.output.resolve()),
            output_sha256=canonical_sha256(analysis["deterministic_candidates"]),
            analysis_sha256=canonical_sha256(analysis),
            counts=analysis["counts"],
            document_signals=analysis["document_signals"],
        )
    except (OSError, UnicodeError, json.JSONDecodeError, DeterministicCandidateError, TypeError, ValueError) as exc:
        payload = result(
            "BLOCKED",
            "deterministic_candidates_v3",
            findings=[{
                "code": "DETERMINISTIC_CANDIDATE_BUILD_FAILED",
                "message": str(exc),
            }],
            schema_version=SCHEMA_VERSION,
        )
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
