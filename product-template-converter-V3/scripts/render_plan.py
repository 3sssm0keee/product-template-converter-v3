from __future__ import annotations

from pathlib import Path
from typing import Any

from delivery_contract import DeliveryProfile, get_profile


# A-ABL-03：旧契约退役候选；仍被 test_render_plan.py 的 4 项测试使用。
# 删除旧测试后的通过不等于原验收保持；当前保留，等待 D 审核覆盖。
def build_render_plan(source_document_path: Path, content_plan_path: Path, template_pack: dict[str, Any], delivery_profile: DeliveryProfile, intermediate_docx: Path, formal_output: Path) -> dict[str, Any]:
    try:
        delivery_target = delivery_profile.target.value
        contract_profile = get_profile(delivery_target)
    except (AttributeError, ValueError) as error:
        raise ValueError("unknown delivery_target") from error

    if delivery_profile != contract_profile:
        raise ValueError("delivery profile conflicts with delivery contract")

    supported_target = template_pack.get("delivery_targets", {}).get(delivery_target)
    if not isinstance(supported_target, dict):
        raise ValueError(f"template pack does not support delivery_target: {delivery_target}")
    if supported_target.get("delivery_format") != contract_profile.delivery_format:
        raise ValueError("template pack delivery format conflicts with delivery contract")
    if formal_output.suffix.lower() != contract_profile.suffix:
        raise ValueError("formal output extension conflicts with delivery contract")

    return {
        "schema_version": "v2",
        "source_document": str(source_document_path),
        "content_plan": str(content_plan_path),
        "template_pack": {"id": template_pack["id"], "template_path": template_pack["template_path"], "invariants": template_pack["invariants"], "delivery_targets": template_pack["delivery_targets"]},
        "delivery_target": delivery_target,
        "delivery_format": contract_profile.delivery_format,
        "renderer_profile": {
            "formal_renderer": contract_profile.formal_renderer,
            "required_renderers": list(contract_profile.required_renderers),
            "validation_profile": contract_profile.validation_profile,
        },
        "delivery": contract_profile.report(formal_output, contract_profile.formal_renderer),
        "intermediate_docx": str(intermediate_docx),
        "formal_output": str(formal_output),
    }
