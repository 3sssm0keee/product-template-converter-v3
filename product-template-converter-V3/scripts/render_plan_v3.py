from __future__ import annotations

from pathlib import Path
from typing import Any

from delivery_contract import DeliveryProfile, get_profile
from pipeline_common import sha256_file
from schema_validation_v3 import load_schema, validate_instance
from v3_common import V3_GENERATOR_VERSION, canonical_json_sha256


SCHEMA_VERSION = "render-plan-v3"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "references" / "schemas" / "render-plan-v3.schema.json"


def build_render_plan_v3(
    *,
    source_document_path: Path,
    content_plan_path: Path,
    template_pack: dict[str, Any],
    template_program_path: Path,
    delivery_profile: DeliveryProfile,
    intermediate_docx: Path,
    formal_output: Path,
) -> dict[str, Any]:
    target = delivery_profile.target.value
    contract = get_profile(target)
    contract_fields = (
        contract.target.value,
        contract.delivery_format,
        contract.suffix,
        contract.intended_use,
        contract.validation_profile,
        contract.formal_renderer,
        tuple(contract.required_renderers),
    )
    supplied_fields = (
        delivery_profile.target.value,
        delivery_profile.delivery_format,
        delivery_profile.suffix,
        delivery_profile.intended_use,
        delivery_profile.validation_profile,
        delivery_profile.formal_renderer,
        tuple(delivery_profile.required_renderers),
    )
    if contract_fields != supplied_fields:
        raise ValueError("delivery profile conflicts with delivery contract")
    target_config = template_pack.get("delivery_targets", {}).get(target)
    if not isinstance(target_config, dict):
        raise ValueError(f"template pack does not support delivery_target: {target}")
    if target_config.get("delivery_format") != contract.delivery_format:
        raise ValueError("template pack delivery format conflicts with delivery contract")
    if formal_output.suffix.lower() != contract.suffix:
        raise ValueError("formal output extension conflicts with delivery contract")

    pack_version = str(template_pack.get("version") or template_pack.get("pack_version") or "1")
    compiler = template_pack.get("compiler") if isinstance(template_pack.get("compiler"), dict) else {}
    validators = template_pack.get("validators") if isinstance(template_pack.get("validators"), dict) else {}
    pack_binding: dict[str, Any] = {
        "id": template_pack["id"],
        "version": pack_version,
        "template_path": template_pack["template_path"],
        "template_sha256": template_pack.get("template_sha256", ""),
    }
    pack_dir = template_pack.get("pack_dir")
    if isinstance(pack_dir, str) and pack_dir:
        pack_config = Path(pack_dir).resolve() / "template.json"
        if not pack_config.is_file():
            raise ValueError("resolved template pack is missing template.json")
        pack_binding.update({"path": str(Path(pack_dir).resolve()), "sha256": sha256_file(pack_config)})

    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generator": {"id": "render-plan-builder", "version": V3_GENERATOR_VERSION},
        "upstream": {
            "source_document": {
                "path": str(source_document_path.resolve()),
                "sha256": sha256_file(source_document_path.resolve()),
            },
            "content_plan": {
                "path": str(content_plan_path.resolve()),
                "sha256": sha256_file(content_plan_path.resolve()),
                "schema_version": "content-plan-v3",
            },
            "template_program": {
                "path": str(template_program_path.resolve()),
                "sha256": sha256_file(template_program_path.resolve()),
                "schema_version": "template-program-v3",
            },
        },
        "template_pack": pack_binding,
        "compiler": {
            "id": compiler.get("id", "fixed-template-compiler-v3"),
            "version": str(compiler.get("version", V3_GENERATOR_VERSION)),
        },
        "validator_dispatch": {
            "core": validators.get("core", [
                "validate_content_plan_v3",
                "verify_tables",
                "scan_source_identity",
            ]),
            "pack": validators.get("pack", [
                "verify_template_invariants",
                "verify_document_structure",
            ]),
        },
        "delivery_target": target,
        "delivery_format": contract.delivery_format,
        "renderer_profile": {
            "formal_renderer": contract.formal_renderer,
            "required_renderers": list(contract.required_renderers),
            "validation_profile": contract.validation_profile,
        },
        "outputs": {
            "intermediate_docx": str(intermediate_docx.resolve()),
            "formal_output": str(formal_output.resolve()),
        },
        "delivery": contract.report(formal_output, contract.formal_renderer),
    }
    plan["canonical_sha256"] = canonical_json_sha256(plan)
    errors = validate_instance(plan, load_schema(SCHEMA_PATH))
    if errors:
        raise ValueError(f"generated RenderPlanV3 is invalid: {errors[:3]}")
    return plan
