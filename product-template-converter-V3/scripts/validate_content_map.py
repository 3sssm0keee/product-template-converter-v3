from __future__ import annotations

import argparse
import hashlib
import io
import re
import zipfile
from pathlib import Path

from lxml import etree
from PIL import Image

from image_fingerprint import canonical_visual_sha256

from pipeline_common import SECTIONS, finish, normalize_text, read_json, result, write_json


ALLOWED_ACTIONS = {"preserve_exact", "preserve_image", "preserve_sanitized_image", "redact_identity", "reviewed_text", "remove_identity", "remove_template_background", "exclude_from_output", "human_review"}
CONTENT_ACTIONS = {"preserve_exact", "preserve_image", "preserve_sanitized_image", "redact_identity", "reviewed_text"}
IDENTITY_ACTIONS = {"remove_identity", "redact_identity", "preserve_sanitized_image"}

_MODEL_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9-]*\d)[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])")
_MODEL_CANDIDATE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])")
_NUMBER_UNIT_RE = re.compile(
    r"(?<![\w.])(?:\d+(?:\.\d+)?(?:\s*[-~至]\s*\d+(?:\.\d+)?)?)\s*"
    r"(?:mm|cm|km|m|μm|um|nm|kg|mg|μg|ug|g|kV|mV|V|mA|A|kW|mW|W|MHz|kHz|Hz|MPa|kPa|Pa|ms|s|℃|°C|%|dB|"
    r"分钟|小时|秒|年|页|个|套|次|倍|像素|英寸|道|级)(?![A-Za-z])",
    re.IGNORECASE,
)
_CERTIFICATION_RES = (
    re.compile(r"(?<![A-Za-z0-9])(?:ISO|IEC)\s*[-/]?\s*\d{3,6}(?![A-Za-z0-9])", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])(?:CE|FDA|CCC|CNAS|CMA)(?![A-Za-z0-9])", re.IGNORECASE),
    re.compile(r"认证|资质|证书|注册证|备案|检测机构|检测报告|合格证|许可证|专利|符合"),
)
_OCR_CONFUSABLES = str.maketrans({"I": "1", "L": "1", "O": "0", "Q": "0", "S": "5", "B": "8"})


def mapped_section_counts(mapping: dict) -> dict[str, int]:
    counts = {section: 0 for section in SECTIONS}
    for entry in mapping.get("mappings", []):
        if entry.get("action") not in CONTENT_ACTIONS:
            continue
        blocks = entry.get("reviewed_blocks") if entry.get("action") == "reviewed_text" else None
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict) or not normalize_text(str(block.get("text", ""))):
                    continue
                target = block.get("target_section")
                if target in counts:
                    counts[target] += 1
            continue
        target = entry.get("target_section")
        if target in counts:
            counts[target] += 1
    return counts


def empty_required_sections(mapping: dict) -> list[str]:
    counts = mapped_section_counts(mapping)
    return [section for section in SECTIONS[:-1] if counts[section] == 0]


def identity_review_findings(mapping: dict) -> list[dict]:
    review = mapping.get("identity_review")
    terms = [str(term).strip() for term in mapping.get("manufacturer_terms", []) if str(term).strip()]
    if terms or not isinstance(review, dict) or review.get("status") != "NO_SOURCE_IDENTITY_FOUND":
        return []
    identity_entries = [
        entry
        for entry in mapping.get("mappings", [])
        if entry.get("action") in IDENTITY_ACTIONS
    ]
    if not identity_entries:
        return []
    return [{
        "code": "IDENTITY_REVIEW_CONTRADICTS_MAPPINGS",
        "message": "NO_SOURCE_IDENTITY_FOUND cannot be used when mappings remove, redact, or sanitize source identity.",
        "source_ids": [entry.get("source_id", "") for entry in identity_entries],
        "actions": sorted({str(entry.get("action", "")) for entry in identity_entries}),
    }]


def _fact_key(kind: str, token: str) -> str:
    value = normalize_text(token).upper()
    if kind == "model":
        value = re.sub(r"\s+", "", value).translate(_OCR_CONFUSABLES)
    elif kind == "number_unit":
        value = re.sub(r"\s+", "", value).replace("℃", "°C")
        value = re.sub(r"(?<=\d)[,，．](?=\d)", ".", value)
    else:
        value = re.sub(r"\s+", "", value)
    return value


def _sensitive_facts(text: str) -> list[tuple[str, str, str]]:
    facts: list[tuple[str, str, str]] = []
    for match in _MODEL_TOKEN_RE.finditer(text or ""):
        token = match.group(0)
        if not token.upper().startswith(("ISO", "IEC")):
            facts.append(("model", token, _fact_key("model", token)))
    for match in _NUMBER_UNIT_RE.finditer(text or ""):
        token = match.group(0)
        facts.append(("number_unit", token, _fact_key("number_unit", token)))
    for pattern in _CERTIFICATION_RES:
        for match in pattern.finditer(text or ""):
            token = match.group(0)
            facts.append(("certification", token, _fact_key("certification", token)))
    return facts


def _source_text(item: dict) -> str:
    values = [str(item.get("text", "")), str(item.get("source_text", ""))]
    for row in item.get("rows", []) if isinstance(item.get("rows"), list) else []:
        if isinstance(row, list):
            values.extend(str(cell) for cell in row)
    return normalize_text(" ".join(value for value in values if value))


def _fact_present(kind: str, key: str, source_text: str) -> bool:
    if any(found_kind == kind and _fact_key(found_kind, token) == key for found_kind, token, _ in _sensitive_facts(source_text)):
        return True
    if kind == "model":
        return any(_fact_key("model", token) == key for token in _MODEL_CANDIDATE_RE.findall(source_text or ""))
    compact_source = re.sub(r"\s+", "", normalize_text(source_text).upper()).replace("℃", "°C")
    compact_source = re.sub(r"(?<=\d)[,，．](?=\d)", ".", compact_source)
    if kind in {"number_unit", "certification"}:
        return key in compact_source
    return False


def _evidence_findings(evidence: object, source_id: str, block: int | None, source_items: dict[str, dict]) -> list[dict]:
    location = {"source_id": source_id}
    if block is not None:
        location["block"] = block
    findings: list[dict] = []
    if not isinstance(evidence, dict):
        return [{"code": "REVIEWED_TEXT_SOURCE_EVIDENCE_MISSING", **location}]
    required_text = {
        "original_ocr_source_id": "REVIEWED_TEXT_ORIGINAL_OCR_SOURCE_MISSING",
        "page_or_position": "REVIEWED_TEXT_EVIDENCE_POSITION_MISSING",
        "review_reason": "REVIEWED_TEXT_REVIEW_REASON_MISSING",
    }
    for field, code in required_text.items():
        if not normalize_text(str(evidence.get(field, ""))):
            findings.append({"code": code, **location})
    if evidence.get("original_ocr_source_id") != source_id:
        findings.append({"code": "REVIEWED_TEXT_ORIGINAL_OCR_SOURCE_MISMATCH", **location, "original_ocr_source_id": evidence.get("original_ocr_source_id", "")})
    reviewer = evidence.get("reviewer")
    if not isinstance(reviewer, dict) or not normalize_text(str(reviewer.get("id", ""))) or not normalize_text(str(reviewer.get("role", ""))):
        findings.append({"code": "REVIEWED_TEXT_REVIEWER_MISSING", **location})
    evidence_ids = evidence.get("evidence_source_ids")
    if not isinstance(evidence_ids, list) or not evidence_ids or not all(isinstance(value, str) and normalize_text(value) for value in evidence_ids):
        findings.append({"code": "REVIEWED_TEXT_EVIDENCE_SOURCE_IDS_MISSING", **location})
    else:
        if source_id not in evidence_ids:
            findings.append({"code": "REVIEWED_TEXT_EVIDENCE_MUST_INCLUDE_OCR_SOURCE", **location})
        for evidence_id in evidence_ids:
            if evidence_id not in source_items:
                findings.append({"code": "REVIEWED_TEXT_EVIDENCE_SOURCE_ID_UNKNOWN", **location, "evidence_source_id": evidence_id})
    return findings


def _unsupported_sensitive_findings(text: str, entry: dict, item: dict, source_items: dict[str, dict], block: int | None) -> list[dict]:
    evidence = entry.get("source_evidence") if isinstance(entry.get("source_evidence"), dict) else {}
    evidence_ids = evidence.get("evidence_source_ids", []) if isinstance(evidence.get("evidence_source_ids"), list) else []
    source_text = _source_text(item)
    findings: list[dict] = []
    for kind, token, key in _sensitive_facts(text):
        if _fact_present(kind, key, source_text):
            continue
        supported = any(
            evidence_id != entry.get("source_id")
            and evidence_id in source_items
            and _fact_present(kind, key, _source_text(source_items[evidence_id]))
            for evidence_id in evidence_ids
        )
        if not supported:
            finding = {
                "code": "REVIEWED_TEXT_UNSUPPORTED_SENSITIVE_TOKEN",
                "source_id": entry.get("source_id"),
                "token": token,
                "token_type": kind,
                "evidence_source_ids": evidence_ids,
            }
            if block is not None:
                finding["block"] = block
            findings.append(finding)
    return findings


def redacted_text(text: str, entry: dict, manufacturer_terms: list[str]) -> tuple[str, list[dict]]:
    output = text
    findings = []
    allowed = {term for term in manufacturer_terms if term}
    redactions = [str(value) for value in entry.get("redactions", []) if str(value)]
    if not redactions:
        findings.append({"code": "REDACTIONS_EMPTY", "source_id": entry.get("source_id")})
    for value in redactions:
        if value not in allowed:
            findings.append({"code": "REDACTION_NOT_IN_MANUFACTURER_TERMS", "source_id": entry.get("source_id"), "value": value})
        if value not in output:
            findings.append({"code": "REDACTION_NOT_IN_SOURCE", "source_id": entry.get("source_id"), "value": value})
        replacement = str(entry.get("replacement", ""))
        if replacement:
            findings.append({"code": "REDACTION_REPLACEMENT_FORBIDDEN", "source_id": entry.get("source_id")})
        output = output.replace(value, "")
    return normalize_text(output), findings


def replacement_path(entry: dict, content_map_path: Path) -> Path | None:
    raw_path = entry.get("replacement_path")
    if not isinstance(raw_path, str) or not normalize_text(raw_path):
        return None
    path = Path(raw_path)
    return path if path.is_absolute() else content_map_path.parent / path


def sanitized_image_findings(entry: dict, item: dict, content_map_path: Path) -> list[dict]:
    """校验人工审定脱敏图片的替换文件，不把源图哈希当作替换文件证明。"""
    source_id = entry.get("source_id", "")
    findings = []
    if item.get("kind") != "image":
        findings.append({"code": "PRESERVE_SANITIZED_IMAGE_REQUIRES_IMAGE", "source_id": source_id})

    replacement = replacement_path(entry, content_map_path)
    if replacement is None:
        findings.append({"code": "SANITIZED_IMAGE_REPLACEMENT_PATH_MISSING", "source_id": source_id})
    elif not replacement.is_file():
        findings.append({"code": "SANITIZED_IMAGE_REPLACEMENT_PATH_NOT_FOUND", "source_id": source_id, "path": str(replacement)})

    expected_sha = entry.get("replacement_sha256")
    if not isinstance(expected_sha, str) or not normalize_text(expected_sha):
        findings.append({"code": "SANITIZED_IMAGE_REPLACEMENT_SHA256_MISSING", "source_id": source_id})
    elif replacement is not None and replacement.is_file():
        actual_sha = hashlib.sha256(replacement.read_bytes()).hexdigest().upper()
        if actual_sha != expected_sha.strip().upper():
            findings.append({
                "code": "SANITIZED_IMAGE_REPLACEMENT_SHA256_MISMATCH",
                "source_id": source_id,
                "expected": expected_sha.strip().upper(),
                "actual": actual_sha,
            })

    review = entry.get("sanitization_review")
    if not isinstance(review, str) or not normalize_text(review):
        findings.append({"code": "SANITIZED_IMAGE_REVIEW_MISSING", "source_id": source_id})
    return findings


def reviewed_text_findings(entry: dict, item: dict, source_items: dict[str, dict] | None = None) -> list[dict]:
    findings = []
    source_id = entry.get("source_id")
    source_items = source_items or {source_id: item}
    if item.get("kind") != "text" or item.get("extraction") != "ocr":
        findings.append({"code": "REVIEWED_TEXT_REQUIRES_OCR_TEXT", "source_id": source_id})
    blocks = entry.get("reviewed_blocks")
    if blocks is None:
        findings.extend(_evidence_findings(entry.get("source_evidence"), source_id, None, source_items))
        if not normalize_text(str(entry.get("reviewed_text", ""))):
            findings.append({"code": "REVIEWED_TEXT_EMPTY", "source_id": source_id})
        else:
            findings.extend(_unsupported_sensitive_findings(str(entry.get("reviewed_text", "")), entry, item, source_items, None))
        return findings
    if normalize_text(str(entry.get("reviewed_text", ""))):
        findings.append({"code": "REVIEWED_TEXT_AND_BLOCKS_CONFLICT", "source_id": source_id})
    if not isinstance(blocks, list) or not blocks:
        findings.append({"code": "REVIEWED_BLOCKS_EMPTY", "source_id": source_id})
        return findings
    for index, block in enumerate(blocks, 1):
        if not isinstance(block, dict):
            findings.append({"code": "REVIEWED_BLOCK_INVALID", "source_id": source_id, "block": index})
            continue
        if not normalize_text(str(block.get("text", ""))):
            findings.append({"code": "REVIEWED_BLOCK_TEXT_EMPTY", "source_id": source_id, "block": index})
        if block.get("target_section") not in SECTIONS:
            findings.append({"code": "REVIEWED_BLOCK_TARGET_INVALID", "source_id": source_id, "block": index, "target_section": block.get("target_section", "")})
        findings.extend(_evidence_findings(block.get("source_evidence"), source_id, index, source_items))
        findings.extend(_unsupported_sensitive_findings(str(block.get("text", "")), {**entry, "source_evidence": block.get("source_evidence")}, item, source_items, index))
    return findings


def skeleton(inventory: dict) -> dict:
    mappings = []
    for item in inventory.get("items", []):
        mappings.append({
            "source_id": item["id"],
            "action": "preserve_image" if item["kind"] == "image" else "preserve_exact",
            "target_section": "",
            "target_order": 0,
            "source_text": item.get("text", ""),
            "source_sha256": item.get("sha256", ""),
            "notes": "",
        })
    return {"schema_version": 1, "product": {"model": "", "full_name": ""}, "manufacturer_terms": [], "mappings": mappings}


def image_visual_sha256(data: bytes) -> str:
    return canonical_visual_sha256(data)


def output_evidence(path: Path) -> tuple[str, set[str], set[str]]:
    with zipfile.ZipFile(path) as package:
        root = etree.fromstring(package.read("word/document.xml"))
        text = normalize_text(" ".join(root.itertext()))
        media = [
            package.read(name)
            for name in package.namelist()
            if name.startswith("word/media/") and not name.endswith("/") and package.getinfo(name).file_size
        ]
    return text, {hashlib.sha256(data).hexdigest().upper() for data in media}, {value for data in media if (value := image_visual_sha256(data))}


def main() -> int:
    parser = argparse.ArgumentParser(description="检查每个源内容ID都有唯一去向")
    parser.add_argument("inventory", type=Path)
    parser.add_argument("content_map", type=Path, nargs="?")
    parser.add_argument("--create-skeleton", type=Path)
    parser.add_argument("--document", type=Path, help="可选：反向核验映射内容是否实际进入成品DOCX")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    inventory = read_json(args.inventory)
    if args.create_skeleton:
        write_json(args.create_skeleton, skeleton(inventory))
        return finish(result("HUMAN_REVIEW", "validate_content_map", findings=[{"code": "MAP_SKELETON_CREATED", "path": str(args.create_skeleton)}]), args.report)
    if not args.content_map or not args.content_map.is_file():
        return finish(result("BLOCKED", "validate_content_map", findings=[{"code": "CONTENT_MAP_MISSING"}]), args.report)
    mapping = read_json(args.content_map)
    manufacturer_terms = [str(term) for term in mapping.get("manufacturer_terms", []) if str(term)]
    source_items = {item["id"]: item for item in inventory.get("items", [])}
    seen: dict[str, list[dict]] = {}
    findings = identity_review_findings(mapping)
    review = []
    for entry in mapping.get("mappings", []):
        source_id = entry.get("source_id", "")
        seen.setdefault(source_id, []).append(entry)
        if source_id not in source_items:
            findings.append({"code": "UNKNOWN_SOURCE_ID", "source_id": source_id})
        if entry.get("action") not in ALLOWED_ACTIONS:
            findings.append({"code": "INVALID_ACTION", "source_id": source_id, "action": entry.get("action")})
        action = entry.get("action")
        target = entry.get("target_section", "")
        has_reviewed_blocks = action == "reviewed_text" and entry.get("reviewed_blocks") is not None
        if action in {"preserve_exact", "preserve_image", "preserve_sanitized_image", "redact_identity", "reviewed_text"} and not has_reviewed_blocks and target not in SECTIONS:
            findings.append({"code": "INVALID_TARGET_SECTION", "source_id": source_id, "target_section": target})
        if action == "preserve_sanitized_image":
            findings.extend(sanitized_image_findings(entry, source_items.get(source_id, {}), args.content_map))
        if action == "redact_identity":
            item = source_items.get(source_id, {})
            if item.get("kind") != "text":
                findings.append({"code": "REDACT_IDENTITY_REQUIRES_TEXT", "source_id": source_id})
            else:
                _, redact_findings = redacted_text(item.get("text", ""), entry, manufacturer_terms)
                findings.extend(redact_findings)
        if action == "reviewed_text":
            findings.extend(reviewed_text_findings(entry, source_items.get(source_id, {}), source_items))
        if action == "human_review":
            review.append(source_id)
    for source_id in source_items:
        entries = seen.get(source_id, [])
        if not entries:
            findings.append({"code": "UNMAPPED_SOURCE_ID", "source_id": source_id})
        elif len(entries) != 1:
            findings.append({"code": "DUPLICATE_MAPPING", "source_id": source_id, "count": len(entries)})
    product = mapping.get("product", {})
    if not product.get("model") or not product.get("full_name"):
        review.append("product_identity")
    empty_sections = empty_required_sections(mapping)
    review.extend(f"empty_section:{section}" for section in empty_sections)
    if args.document:
        if not args.document.is_file():
            findings.append({"code": "OUTPUT_DOCUMENT_MISSING"})
        else:
            try:
                output_text, output_hashes, output_visual_hashes = output_evidence(args.document)
                for entry in mapping.get("mappings", []):
                    item = source_items.get(entry.get("source_id", ""), {})
                    action = entry.get("action")
                    if action == "preserve_exact" and item.get("kind") == "text":
                        source_text = normalize_text(item.get("text", ""))
                        if source_text and source_text not in output_text:
                            findings.append({"code": "MAPPED_TEXT_MISSING_FROM_OUTPUT", "source_id": item.get("id")})
                    elif action == "redact_identity" and item.get("kind") == "text":
                        expected_text, _ = redacted_text(item.get("text", ""), entry, manufacturer_terms)
                        if expected_text and expected_text not in output_text:
                            findings.append({"code": "REDACTED_TEXT_MISSING_FROM_OUTPUT", "source_id": item.get("id")})
                    elif action == "reviewed_text" and item.get("kind") == "text":
                        blocks = entry.get("reviewed_blocks")
                        expected = [str(block.get("text", "")) for block in blocks] if isinstance(blocks, list) else [str(entry.get("reviewed_text", ""))]
                        for block_index, value in enumerate(expected, 1):
                            reviewed = normalize_text(value)
                            if reviewed and reviewed not in output_text:
                                finding = {"code": "REVIEWED_TEXT_MISSING_FROM_OUTPUT", "source_id": item.get("id")}
                                if isinstance(blocks, list):
                                    finding["block"] = block_index
                                findings.append(finding)
                    elif action == "preserve_exact" and item.get("kind") == "table":
                        for row_index, row in enumerate(item.get("rows", []), 1):
                            for cell_index, cell in enumerate(row, 1):
                                value = normalize_text(cell)
                                if value and value not in output_text:
                                    findings.append({"code": "MAPPED_TABLE_CELL_MISSING_FROM_OUTPUT", "source_id": item.get("id"), "row": row_index, "cell": cell_index})
                    elif action == "preserve_image" and item.get("sha256") not in output_hashes and item.get("visual_sha256") not in output_visual_hashes:
                        findings.append({"code": "MAPPED_IMAGE_MISSING_FROM_OUTPUT", "source_id": item.get("id"), "sha256": item.get("sha256")})
                    elif action == "preserve_sanitized_image":
                        replacement = replacement_path(entry, args.content_map)
                        expected_visual = ""
                        if replacement is not None and replacement.is_file():
                            expected_visual = image_visual_sha256(replacement.read_bytes())
                        expected_sha = str(entry.get("replacement_sha256", "")).strip().upper()
                        if expected_sha not in output_hashes and (not expected_visual or expected_visual not in output_visual_hashes):
                            findings.append({
                                "code": "MAPPED_SANITIZED_IMAGE_MISSING_FROM_OUTPUT",
                                "source_id": item.get("id"),
                                "replacement_sha256": expected_sha,
                                "replacement_visual_sha256": expected_visual,
                            })
            except Exception as exc:
                findings.append({"code": "OUTPUT_EVIDENCE_FAILED", "message": str(exc)})
    status = "FAIL" if findings else "HUMAN_REVIEW" if review else "PASS"
    return finish(result(status, "validate_content_map", findings=findings, mapped_count=len(seen), source_count=len(source_items), human_review=review, empty_required_sections=empty_sections), args.report)


if __name__ == "__main__":
    raise SystemExit(main())
