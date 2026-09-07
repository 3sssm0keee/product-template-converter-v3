from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pipeline_common import result, sha256_file
from template_ir_v3 import TemplateIRError, artifact_sha256, build_docx_template_ir, canonical_sha256, validate_template_program


PACK_SCHEMA_VERSION = "template-pack-v3"
RELEASED_LIFECYCLE = "RELEASED"


class TemplateCatalogError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateCatalogError(f"无法读取模板目录制品: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TemplateCatalogError(f"模板目录制品必须是 JSON object: {path}")
    return payload


def _resolve_artifact(pack_dir: Path, skill_root: Path, raw_path: str, label: str) -> Path:
    path = (pack_dir / raw_path).resolve()
    root = skill_root.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise TemplateCatalogError(f"{label} 越出模板目录根边界: {path}") from exc
    if not path.is_file():
        raise TemplateCatalogError(f"{label} 不存在: {path}")
    return path


def _validate_hash(path: Path, expected: str | None, label: str) -> str:
    actual = sha256_file(path)
    if expected and actual != str(expected).upper():
        raise TemplateCatalogError(f"{label} 哈希不匹配: expected={str(expected).upper()} actual={actual}")
    return actual


def load_template_pack_v3(skill_root: Path, pack_dir: Path) -> dict[str, Any]:
    skill_root = skill_root.resolve()
    pack_dir = pack_dir.resolve()
    config_path = pack_dir / "template.json"
    config = _read_json(config_path)
    if config.get("schema_version") != PACK_SCHEMA_VERSION:
        raise TemplateCatalogError(f"模板包不是 {PACK_SCHEMA_VERSION}: {config_path}")
    for field in ("id", "version", "template", "template_sha256", "template_ir", "slot_contract", "program", "validators"):
        if not isinstance(config.get(field), str) or not str(config[field]).strip():
            raise TemplateCatalogError(f"模板包缺少字段: {field}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", str(config["id"])):
        raise TemplateCatalogError("模板包 id 不符合受控命名规则")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(config["version"])):
        raise TemplateCatalogError("模板包 version 必须是三段数字版本")

    resolved: dict[str, str] = {}
    hashes: dict[str, str] = {}
    declarations = {
        "template": (config["template"], config.get("template_sha256")),
        "template_ir": (config["template_ir"], config.get("template_ir_file_sha256")),
        "slot_contract": (config["slot_contract"], config.get("slot_contract_file_sha256")),
        "program": (config["program"], config.get("program_file_sha256")),
        "validators": (config["validators"], config.get("validators_file_sha256")),
    }
    for label, (relative, expected) in declarations.items():
        path = _resolve_artifact(pack_dir, skill_root, str(relative), label)
        resolved[f"{label}_path"] = str(path)
        hashes[f"{label}_file_sha256"] = _validate_hash(path, str(expected) if expected else None, label)

    template_ir = _read_json(Path(resolved["template_ir_path"]))
    program = _read_json(Path(resolved["program_path"]))
    slots = _read_json(Path(resolved["slot_contract_path"]))
    validators = _read_json(Path(resolved["validators_path"]))
    validate_template_program(program)
    if template_ir.get("ir_sha256") != artifact_sha256(template_ir, "ir_sha256"):
        raise TemplateCatalogError("Template IR 内嵌哈希无效")
    if slots.get("slot_contract_sha256") != artifact_sha256(slots, "slot_contract_sha256"):
        raise TemplateCatalogError("Slot Contract 内嵌哈希无效")

    template_sha = hashes["template_file_sha256"]
    if template_sha != str(config["template_sha256"]).upper():
        raise TemplateCatalogError("模板字节哈希与 template.json 不一致")
    if template_ir.get("source_template", {}).get("sha256") != template_sha:
        raise TemplateCatalogError("模板 IR 未绑定当前模板哈希")
    if program.get("template_sha256") != template_sha:
        raise TemplateCatalogError("Template Program 未绑定当前模板哈希")
    if program.get("template_ir_sha256") != template_ir.get("ir_sha256"):
        raise TemplateCatalogError("Template Program 未绑定当前 Template IR")
    if program.get("template_pack") != {"id": config["id"], "version": config["version"]}:
        raise TemplateCatalogError("Template Program 的 pack id/version 与 template.json 不一致")
    if slots.get("template_sha256") != template_sha:
        raise TemplateCatalogError("Slot Contract 未绑定当前模板哈希")
    if slots.get("template_ir_sha256") != template_ir.get("ir_sha256"):
        raise TemplateCatalogError("Slot Contract 未绑定当前 Template IR")
    if not isinstance(validators.get("core"), list) or not isinstance(validators.get("pack"), list):
        raise TemplateCatalogError("validators.json 必须分别声明 core 和 pack validator")

    approval = config.get("approval")
    if not isinstance(approval, dict):
        raise TemplateCatalogError("模板包缺少 approval 声明")
    compiler = config.get("compiler")
    expected_compiler = {**program["compiler"], "adapter": program["adapter"]}
    if compiler != expected_compiler:
        raise TemplateCatalogError("template.json compiler 与 Template Program 不一致")
    migrated_bindings = approval.get("bindings")
    if isinstance(migrated_bindings, dict):
        required = {
            "template_sha256": template_sha,
            "template_ir_sha256": template_ir.get("ir_sha256"),
            "slot_contract_sha256": slots.get("slot_contract_sha256"),
            "program_sha256": program.get("program_sha256"),
            "compiler_id": program["compiler"]["id"],
            "compiler_version": program["compiler"]["version"],
        }
        if any(migrated_bindings.get(key) != value for key, value in required.items()):
            raise TemplateCatalogError("迁移审批绑定与当前模板包制品不一致")
    review_binding = approval.get("binding")
    if isinstance(review_binding, dict):
        dsl_sha = canonical_sha256(
            {
                "program_sha256": program.get("program_sha256"),
                "slot_contract_sha256": slots.get("slot_contract_sha256"),
            }
        )
        required = {
            "template_sha256": template_sha,
            "template_ir_sha256": template_ir.get("ir_sha256"),
            "template_dsl_sha256": dsl_sha,
            "compiler_sha256": canonical_sha256(program["compiler"]),
        }
        if any(review_binding.get(key) != value for key, value in required.items()):
            raise TemplateCatalogError("通用 ReviewReceiptV3 绑定与当前模板包制品不一致")

    return {
        **config,
        **resolved,
        "artifact_hashes": hashes,
        "pack_dir": str(pack_dir),
        "template_ir_payload": template_ir,
        "slot_contract_payload": slots,
        "program_payload": program,
        "validators_payload": validators,
        "reference": f"{config['id']}@{config['version']}",
    }


def discover_template_packs(skill_root: Path) -> list[dict[str, Any]]:
    catalog_root = skill_root.resolve() / "template_packs"
    if not catalog_root.is_dir():
        return []
    packs = []
    for config_path in sorted(catalog_root.glob("*/template.json")):
        packs.append(load_template_pack_v3(skill_root, config_path.parent))
    references = [pack["reference"] for pack in packs]
    if len(references) != len(set(references)):
        raise TemplateCatalogError("模板目录存在重复 id@version")
    return packs


def released_template_packs(skill_root: Path) -> list[dict[str, Any]]:
    return [
        pack
        for pack in discover_template_packs(skill_root)
        if pack.get("lifecycle") == RELEASED_LIFECYCLE
        and pack.get("approval", {}).get("status") == "APPROVED"
        and pack.get("validation", {}).get("status") == "PASS"
        and not pack.get("validation", {}).get("open_findings")
    ]


def _public_pack(pack: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": pack["id"],
        "version": pack["version"],
        "reference": pack["reference"],
        "template_path": pack["template_path"],
        "template_sha256": pack["template_sha256"],
        "template_ir_path": pack["template_ir_path"],
        "slot_contract_path": pack["slot_contract_path"],
        "program_path": pack["program_path"],
        "validators_path": pack["validators_path"],
        "compiler": pack.get("compiler", {}),
        "delivery_targets": pack["delivery_targets"],
        "invariants": pack["invariants"],
    }


def resolve_pack_reference(skill_root: Path, reference: str) -> dict[str, Any]:
    if "@" not in reference:
        return result(
            "HUMAN_REVIEW",
            "template_resolver_v3",
            findings=[{"code": "TEMPLATE_PACK_VERSION_REQUIRED", "reference": reference}],
        )
    pack_id, version = reference.rsplit("@", 1)
    matches = [pack for pack in released_template_packs(skill_root) if pack["id"] == pack_id and pack["version"] == version]
    if len(matches) != 1:
        return result(
            "HUMAN_REVIEW",
            "template_resolver_v3",
            findings=[{"code": "TEMPLATE_PACK_NOT_FOUND", "reference": reference}],
        )
    return result("PASS", "template_resolver_v3", template_pack=_public_pack(matches[0]), selection="explicit_pack")


def _candidate_similarity(template_ir: dict[str, Any], pack: dict[str, Any]) -> dict[str, Any] | None:
    candidate_ir = pack["template_ir_payload"]
    requested = template_ir.get("fingerprint", {})
    known = candidate_ir.get("fingerprint", {})
    structural_match = requested.get("normalized_structure_sha256") == known.get("normalized_structure_sha256")
    visual_match = requested.get("visual_proxy_sha256") == known.get("visual_proxy_sha256")
    if not structural_match and not visual_match:
        return None
    return {
        "id": pack["id"],
        "version": pack["version"],
        "reference": pack["reference"],
        "structural_match": structural_match,
        "visual_proxy_match": visual_match,
        "score": 1.0 if structural_match and visual_match else 0.75,
    }


def resolve_explicit_template(skill_root: Path, template: Path) -> dict[str, Any]:
    if template.suffix.lower() != ".docx":
        return result(
            "BLOCKED",
            "template_resolver_v3",
            findings=[{"code": "UNSUPPORTED_TEMPLATE_FORMAT", "path": str(template), "supported": [".docx"]}],
        )
    try:
        template_ir = build_docx_template_ir(template)
        packs = released_template_packs(skill_root)
    except (TemplateIRError, TemplateCatalogError) as exc:
        return result(
            "BLOCKED",
            "template_resolver_v3",
            findings=[{"code": "TEMPLATE_ANALYSIS_FAILED", "message": str(exc), "path": str(template)}],
        )
    requested_sha = template_ir["source_template"]["sha256"]
    exact = [pack for pack in packs if pack["template_sha256"] == requested_sha]
    if len(exact) == 1:
        return result("PASS", "template_resolver_v3", template_pack=_public_pack(exact[0]), selection="exact_template_sha256")
    candidates = [candidate for pack in packs if (candidate := _candidate_similarity(template_ir, pack)) is not None]
    candidates.sort(key=lambda value: (-value["score"], value["reference"]))
    return result(
        "HUMAN_REVIEW",
        "template_resolver_v3",
        findings=[{"code": "TEMPLATE_ONBOARDING_REQUIRED", "template_sha256": requested_sha}],
        template_ir=template_ir,
        candidates=candidates,
    )


def _route_matches(pack: dict[str, Any], routing_context: dict[str, Any]) -> bool:
    routing = pack.get("routing", {})
    rules = routing.get("rules", [])
    if not rules:
        return bool(routing.get("candidate_when_unspecified", False))
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("field"), str):
            return False
        actual = routing_context.get(rule["field"])
        operator = rule.get("operator", "equals")
        if operator == "equals" and actual != rule.get("value"):
            return False
        if operator == "in" and actual not in rule.get("values", []):
            return False
        if operator not in {"equals", "in"}:
            return False
    return True


def resolve_unspecified_template(skill_root: Path, routing_context: dict[str, Any] | None = None) -> dict[str, Any]:
    routing_context = routing_context or {}
    try:
        candidates = [pack for pack in released_template_packs(skill_root) if _route_matches(pack, routing_context)]
    except TemplateCatalogError as exc:
        return result(
            "BLOCKED",
            "template_resolver_v3",
            findings=[{"code": "TEMPLATE_CATALOG_INVALID", "message": str(exc)}],
        )
    if len(candidates) == 1:
        return result("PASS", "template_resolver_v3", template_pack=_public_pack(candidates[0]), selection="deterministic_route")
    return result(
        "HUMAN_REVIEW",
        "template_resolver_v3",
        findings=[
            {
                "code": "TEMPLATE_SELECTION_REQUIRED",
                "candidate_count": len(candidates),
                "candidates": [pack["reference"] for pack in candidates],
            }
        ],
    )


def resolve_template(
    skill_root: Path,
    *,
    pack_reference: str | None = None,
    template: Path | None = None,
    routing_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if pack_reference and template is not None:
        return result(
            "FAIL",
            "template_resolver_v3",
            findings=[{"code": "TEMPLATE_SELECTOR_CONFLICT", "message": "template pack 与 template 不能同时指定"}],
        )
    if pack_reference:
        return resolve_pack_reference(skill_root, pack_reference)
    if template is not None:
        return resolve_explicit_template(skill_root, template)
    return resolve_unspecified_template(skill_root, routing_context)
