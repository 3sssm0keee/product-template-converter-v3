from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pipeline_common import finish, result, sha256_file, write_json
from schema_validation_v3 import load_schema, validate_instance


ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION = ROOT / "agents" / "prompts" / "content-mapper.system.md"
BUNDLE_SCHEMA = ROOT / "references" / "schemas" / "decision-task-bundle-v3.schema.json"
V2_CONTEXT_FILES = (
    "SKILL.md",
    "references/access-policy.md",
    "references/default-template-contract.md",
    "references/conversion-contract.md",
    "references/failure-patterns.md",
    "references/delivery-contract.md",
    "references/repair-policy.md",
    "references/content-map-schema.md",
    "references/content-plan-schema.md",
    "agents/prompts/content-mapper.system.md",
    "agents/prompts/identity-classifier.system.md",
)


def text_char_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8-sig"))


def v2_context_evidence(v2_root: Path) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    files: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    total = 0
    for relative in V2_CONTEXT_FILES:
        path = v2_root / relative
        if not path.is_file():
            findings.append({"code": "V2_CONTEXT_FILE_MISSING", "path": str(path)})
            continue
        characters = text_char_count(path)
        total += characters
        files.append({"path": relative, "characters": characters, "sha256": sha256_file(path)})
    return total, files, findings


def _inline_chars(item: dict[str, Any]) -> int:
    inline = {
        key: item[key]
        for key in ("summary", "inline_text", "table_preview")
        if key in item
    }
    return len(json.dumps(inline, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def inspect_bundle_groups(bundle_root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    schema = load_schema(BUNDLE_SCHEMA)
    findings: list[dict[str, Any]] = []
    bundles = sorted(bundle_root.rglob("decision_tasks/bundles/decision-task-*.json"))
    groups: dict[Path, list[Path]] = {}
    for path in bundles:
        groups.setdefault(path.parent, []).append(path)

    observed_instruction_hashes: set[str] = set()
    max_items = 0
    max_inline = 0
    reference_only_items = 0
    source_item_count = 0
    for group_path, group_bundles in sorted(groups.items(), key=lambda entry: str(entry[0])):
        group_source_ids: list[str] = []
        for path in group_bundles:
            try:
                bundle = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError, UnicodeError) as exc:
                findings.append({"code": "PROMPT_BUNDLE_INVALID_JSON", "path": str(path), "message": str(exc)})
                continue
            errors = validate_instance(bundle, schema)
            if errors:
                findings.append({"code": "PROMPT_BUNDLE_SCHEMA_INVALID", "path": str(path), "errors": errors[:5]})
                continue
            instruction = str(bundle["instruction"])
            observed_instruction_hashes.add(sha256_file(INSTRUCTION) if instruction.strip() == INSTRUCTION.read_text(encoding="utf-8-sig").strip() else "MISMATCH")
            if len(instruction) > 2000:
                findings.append({"code": "PROMPT_INSTRUCTION_TOO_LARGE", "path": str(path), "characters": len(instruction)})
            items = bundle["item_batch"]
            source_ids = [str(item["source_id"]) for item in items]
            group_source_ids.extend(source_ids)
            source_item_count += len(items)
            max_items = max(max_items, len(items))
            actual_inline = sum(_inline_chars(item) for item in items)
            max_inline = max(max_inline, actual_inline)
            if actual_inline != bundle["inline_evidence_chars"]:
                findings.append({"code": "PROMPT_INLINE_ACCOUNTING_MISMATCH", "path": str(path)})
            evidence_ids = [str(ref["evidence_id"]) for ref in bundle["evidence_refs"]]
            if len(evidence_ids) != len(set(evidence_ids)):
                findings.append({"code": "PROMPT_EVIDENCE_DUPLICATED", "path": str(path)})
            defined = set(evidence_ids)
            for item in items:
                if not item.get("inline_text") and not item.get("table_preview"):
                    reference_only_items += 1
                missing = sorted(set(item["evidence_ids"]) - defined)
                if missing:
                    findings.append({"code": "PROMPT_EVIDENCE_REFERENCE_MISSING", "path": str(path), "source_id": item["source_id"], "evidence_ids": missing})
        if len(group_source_ids) != len(set(group_source_ids)):
            findings.append({"code": "PROMPT_SOURCE_ID_REPEATED_ACROSS_BATCHES", "group": str(group_path)})

    if not bundles:
        findings.append({"code": "PROMPT_REAL_BUNDLE_EVIDENCE_MISSING", "root": str(bundle_root)})
    if "MISMATCH" in observed_instruction_hashes:
        findings.append({"code": "PROMPT_INSTRUCTION_DRIFT"})
    return {
        "bundle_group_count": len(groups),
        "bundle_count": len(bundles),
        "source_item_count": source_item_count,
        "max_items_per_bundle": max_items,
        "max_inline_evidence_characters": max_inline,
        "reference_only_item_count": reference_only_items,
        "instruction_sha256": sha256_file(INSTRUCTION),
    }, findings


def verify(v2_root: Path, bundle_root: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    instruction_characters = len(INSTRUCTION.read_text(encoding="utf-8-sig").strip())
    if instruction_characters > 2000:
        findings.append({"code": "PROMPT_INSTRUCTION_TOO_LARGE", "characters": instruction_characters})
    v2_characters, v2_files, v2_findings = v2_context_evidence(v2_root)
    findings.extend(v2_findings)
    reduction = 1.0 - (instruction_characters / v2_characters) if v2_characters else 0.0
    if reduction < 0.70:
        findings.append({"code": "PROMPT_RULE_TEXT_REDUCTION_TARGET_UNMET", "actual": round(reduction, 6), "minimum": 0.70})
    bundles, bundle_findings = inspect_bundle_groups(bundle_root)
    findings.extend(bundle_findings)
    return result(
        "PASS" if not findings else "FAIL",
        "verify_prompt_evidence_v3",
        findings=findings,
        fixed_instruction_characters=instruction_characters,
        maximum_instruction_characters=2000,
        v2_full_startup_context_characters=v2_characters,
        fixed_rule_text_reduction_ratio=round(reduction, 6),
        minimum_reduction_ratio=0.70,
        v2_context_files=v2_files,
        real_bundle_evidence=bundles,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="核验 V3 紧凑 Prompt 与真实 DecisionTaskBundle 证据")
    parser.add_argument("--v2-root", type=Path, default=ROOT.parent / "product-template-converter-V2-work")
    parser.add_argument("--bundle-root", type=Path, default=ROOT / "development-evidence")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload = verify(args.v2_root.resolve(), args.bundle_root.resolve())
    if args.report:
        write_json(args.report, payload)
    return finish(payload)


if __name__ == "__main__":
    raise SystemExit(main())
