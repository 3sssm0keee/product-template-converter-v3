from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

import build_fixed_docx
from declarative_docx_adapter_v3 import ADAPTER_ID as DECLARATIVE_ADAPTER_ID, compile_declarative_docx
from pipeline_common import finish, result, sha256_file
from schema_validation_v3 import load_schema, validate_instance
from template_catalog_v3 import TemplateCatalogError, load_template_pack_v3
from template_ir_v3 import COMPILER_ID, COMPILER_VERSION, TemplateProgramError, validate_template_program
from v3_common import canonical_json_sha256


CONTENT_PLAN_SCHEMA_VERSIONS = {"content-plan-v3", "v3"}
RENDER_PLAN_SCHEMA_VERSIONS = {"render-plan-v3", "v3"}
ZXTY_ADAPTER_ID = "zxty-fixed-builder-v2-adapter"
SUPPORTED_ADAPTERS = {ZXTY_ADAPTER_ID, DECLARATIVE_ADAPTER_ID}
ZXTY_LEGACY_SECTIONS = {
    "section.introduction": "一、产品简介",
    "section.functions": "二、功能介绍",
    "section.advantages": "三、产品优势",
    "section.scenarios": "四、应用场景",
    "section.parameters": "五、技术参数",
    "section.qualifications": "六、产品资质",
}
ZXTY_PRODUCT_METADATA_SLOTS = {"product.model", "product.full_name"}
SUPPORTED_ACTIONS = {
    "preserve_exact",
    "preserve_image",
    "preserve_sanitized_image",
    "redact_identity",
    "reviewed_text",
    "remove_identity",
    "remove_template_background",
    "exclude_from_output",
    "human_review",
}
NON_RENDER_ACTIONS = {"remove_identity", "remove_template_background", "exclude_from_output"}
SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "references" / "schemas"


class CompilerInputError(ValueError):
    pass


class CompilerBlockedError(RuntimeError):
    pass


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompilerInputError(f"无法读取 {label}: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CompilerInputError(f"{label} 顶层必须是 JSON object")
    return payload


def _validate_execution_plan(payload: dict[str, Any], schema_name: str, label: str) -> None:
    errors = validate_instance(payload, load_schema(SCHEMA_ROOT / schema_name))
    if errors:
        raise CompilerInputError(f"{label} JSON Schema 校验失败: {errors[:3]}")
    declared = str(payload.get("canonical_sha256") or "").upper()
    actual = canonical_json_sha256({key: value for key, value in payload.items() if key != "canonical_sha256"})
    if declared != actual:
        raise CompilerInputError(f"{label} canonical_sha256 失效")


def _resolve_relative(raw: str, owner: Path) -> Path:
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (owner.parent / path).resolve()


def _verify_binding(binding: Any, actual_path: Path, owner: Path, label: str) -> None:
    if not isinstance(binding, dict):
        raise CompilerInputError(f"RenderPlanV3 缺少 {label} 哈希绑定")
    raw_path = binding.get("path")
    expected_sha = binding.get("sha256")
    if not isinstance(raw_path, str) or not isinstance(expected_sha, str):
        raise CompilerInputError(f"RenderPlanV3 {label} 必须同时包含 path 和 sha256")
    bound_path = _resolve_relative(raw_path, owner)
    if bound_path != actual_path.resolve():
        raise CompilerInputError(f"RenderPlanV3 {label} 路径绑定与 CLI 输入不一致")
    actual_sha = sha256_file(actual_path)
    if actual_sha != expected_sha.upper():
        raise CompilerInputError(f"RenderPlanV3 {label} 哈希绑定失效")


def _resolve_pack(render_plan: dict[str, Any], render_plan_path: Path) -> dict[str, Any]:
    binding = render_plan.get("template_pack")
    if not isinstance(binding, dict):
        raise CompilerInputError("RenderPlanV3 缺少 template_pack")
    raw_path = binding.get("path") or binding.get("pack_dir")
    expected_sha = binding.get("sha256")
    if isinstance(raw_path, str) and isinstance(expected_sha, str):
        pack_path = _resolve_relative(raw_path, render_plan_path)
        config_path = pack_path / "template.json" if pack_path.is_dir() else pack_path
        if not config_path.is_file() or sha256_file(config_path) != expected_sha.upper():
            raise CompilerInputError("RenderPlanV3 template_pack 路径或哈希绑定失效")
        pack_dir = config_path.parent
    else:
        # RenderPlanV3 的公共合同仅带 id/version/template hash；pack 仍只能从当前 V3 受控目录解析。
        pack_id = binding.get("id")
        if not isinstance(pack_id, str) or not pack_id:
            raise CompilerInputError("RenderPlanV3 template_pack 缺少 id")
        skill_root = Path(__file__).resolve().parents[1]
        pack_dir = skill_root / "template_packs" / pack_id
        config_path = pack_dir / "template.json"
        if not config_path.is_file():
            raise CompilerInputError("RenderPlanV3 template_pack 无法在受控目录解析")
    if pack_dir.parent.name != "template_packs":
        raise CompilerInputError("template_pack 必须来自受控 template_packs 目录")
    skill_root = pack_dir.parent.parent
    try:
        pack = load_template_pack_v3(skill_root, pack_dir)
    except TemplateCatalogError as exc:
        raise CompilerInputError(str(exc)) from exc
    if binding.get("id") != pack["id"] or binding.get("version") != pack["version"]:
        raise CompilerInputError("RenderPlanV3 template_pack id/version 与目录制品不一致")
    declared_template = binding.get("template_path")
    if isinstance(declared_template, str) and Path(declared_template).resolve() != Path(pack["template_path"]).resolve():
        raise CompilerInputError("RenderPlanV3 template_path 与已发布 pack 不一致")
    declared_template_sha = str(binding.get("template_sha256") or "").upper()
    if declared_template_sha != pack["template_sha256"]:
        raise CompilerInputError("RenderPlanV3 template_sha256 与已发布 pack 不一致")
    if pack.get("lifecycle") != "RELEASED" or pack.get("approval", {}).get("status") != "APPROVED":
        raise CompilerBlockedError("模板包未处于 RELEASED/APPROVED 状态")
    if pack.get("validation", {}).get("status") != "PASS" or pack.get("validation", {}).get("open_findings"):
        raise CompilerBlockedError("模板包尚未通过确定性验证")
    return pack


def _resolve_program(render_plan: dict[str, Any], render_plan_path: Path, pack: dict[str, Any]) -> dict[str, Any]:
    upstream = render_plan.get("upstream") if isinstance(render_plan.get("upstream"), dict) else {}
    binding = render_plan.get("template_program") or upstream.get("template_program")
    if not isinstance(binding, dict) or not isinstance(binding.get("path"), str) or not isinstance(binding.get("sha256"), str):
        raise CompilerInputError("RenderPlanV3 template_program 必须包含 path 和 sha256")
    program_path = _resolve_relative(binding["path"], render_plan_path)
    if program_path != Path(pack["program_path"]).resolve():
        raise CompilerInputError("RenderPlanV3 不得使用模板包之外的 Template Program")
    if sha256_file(program_path) != binding["sha256"].upper():
        raise CompilerInputError("RenderPlanV3 template_program 文件哈希失效")
    program = _read_object(program_path, "Template Program")
    try:
        validate_template_program(program)
    except TemplateProgramError as exc:
        raise CompilerInputError(str(exc)) from exc
    if binding.get("artifact_sha256") is not None and program.get("program_sha256") != binding.get("artifact_sha256"):
        raise CompilerInputError("RenderPlanV3 template_program 规范化制品哈希失效")
    if program.get("template_sha256") != pack["template_sha256"]:
        raise CompilerInputError("Template Program 的模板哈希绑定失效")
    if program.get("adapter") not in SUPPORTED_ADAPTERS:
        raise CompilerBlockedError(f"当前固定编译器不支持 adapter: {program.get('adapter')}")
    return program


def _validate_compiler_identity(render_plan: dict[str, Any], program: dict[str, Any]) -> None:
    compiler = render_plan.get("compiler")
    expected = {"id": COMPILER_ID, "version": COMPILER_VERSION}
    if compiler != expected or program.get("compiler") != expected:
        raise CompilerBlockedError("编译器身份或版本与已批准 Template Program 不一致")


def _normalized_source(content_plan: dict[str, Any], content_plan_path: Path) -> Path:
    source = content_plan.get("normalized_source")
    if not isinstance(source, dict) or not isinstance(source.get("path"), str) or not isinstance(source.get("sha256"), str):
        raise CompilerInputError("ContentPlanV3 缺少 normalized_source path/sha256")
    path = _resolve_relative(source["path"], content_plan_path)
    if not path.is_file() or sha256_file(path) != source["sha256"].upper():
        raise CompilerInputError("ContentPlanV3 normalized_source 路径或哈希绑定失效")
    return path


def _legacy_inventory_item(source_id: str, source: Any) -> dict[str, Any]:
    """Flatten one typed SourceDocumentV3 item for the locked V2 builder.

    The compatibility object exists only inside the fixed compiler.  The V3
    renderer contract remains ContentPlanV3 + RenderPlanV3; raw inventory never
    crosses the public execution-bus boundary.
    """

    if not isinstance(source, dict):
        raise CompilerInputError(f"ContentPlanV3 source item 必须是 object: {source_id}")
    kind = str(source.get("kind") or "")
    location = str(source.get("location") or "")
    if not kind or not location:
        raise CompilerInputError(f"ContentPlanV3 source item 缺少 kind/location: {source_id}")
    payload = source.get("payload") if isinstance(source.get("payload"), dict) else {}
    context = source.get("context") if isinstance(source.get("context"), dict) else {}
    attributes = source.get("attributes") if isinstance(source.get("attributes"), dict) else {}

    legacy: dict[str, Any] = {**attributes, **context, "id": source_id, "kind": kind, "location": location}
    payload_type = payload.get("type")
    if payload_type == "text":
        legacy["text"] = str(payload.get("text") or "")
    elif payload_type == "table":
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise CompilerInputError(f"ContentPlanV3 table payload 缺少 rows: {source_id}")
        legacy["rows"] = rows
    elif payload_type == "asset":
        for field in ("name", "sha256", "visual_sha256", "bytes"):
            if field in payload and payload[field] is not None:
                legacy[field] = payload[field]
    elif payload_type == "description":
        legacy["text"] = str(payload.get("text") or payload.get("description") or "")
        for field in ("name", "title", "description"):
            if field in payload:
                legacy[field] = payload[field]
    elif payload_type == "relationship":
        legacy.update({key: value for key, value in payload.items() if key != "type"})
    elif payload_type == "generic" and isinstance(payload.get("value"), dict):
        legacy.update(payload["value"])
    else:
        # Keep the adapter compatible with early V3 fixtures while requiring
        # production SourceDocumentV3 to use a typed payload.
        for field in ("text", "rows", "name", "sha256", "visual_sha256", "bytes"):
            if field in source:
                legacy[field] = source[field]
    return legacy


def _legacy_mapping(source_id: str, decision: dict[str, Any]) -> dict[str, Any]:
    action = decision.get("action")
    if action == "human_review":
        raise CompilerBlockedError(f"ContentPlanV3 仍包含人工复核 action: {source_id}")
    if action not in SUPPORTED_ACTIONS:
        raise CompilerInputError(f"ContentPlanV3 action 不可由当前适配器编译: {source_id}: {action}")

    target = decision.get("target_slot") or decision.get("target_section")
    metadata_only = target in ZXTY_PRODUCT_METADATA_SLOTS or target == "closing.fixed_background"
    legacy: dict[str, Any] = {
        "source_id": source_id,
        "action": "discard" if action in NON_RENDER_ACTIONS or metadata_only else action,
    }
    if action not in NON_RENDER_ACTIONS and not metadata_only:
        if not isinstance(target, str) or not target:
            raise CompilerInputError(f"ContentPlanV3 可渲染 action 缺少 target_slot: {source_id}")
        legacy["target_section"] = (
            "一、产品简介" if target == "cover.primary_image" else ZXTY_LEGACY_SECTIONS.get(target, target)
        )
        legacy["target_order"] = int(decision.get("target_order") or 0)
        if target == "cover.primary_image":
            legacy["use_on_cover"] = True
            legacy["use_on_cover_only"] = True
    for field in ("use_on_cover", "use_on_cover_only", "reviewed_text", "redactions"):
        if field in decision:
            legacy[field] = decision[field]

    blocks = decision.get("reviewed_blocks")
    if blocks is not None:
        if not isinstance(blocks, list) or not blocks:
            raise CompilerInputError(f"ContentPlanV3 reviewed_blocks 非法: {source_id}")
        legacy["reviewed_blocks"] = [
            {
                "text": block.get("text"),
                "target_section": ZXTY_LEGACY_SECTIONS.get(
                    block.get("target_slot") or block.get("target_section") or target,
                    block.get("target_slot") or block.get("target_section") or target,
                ),
                "target_order": int(block.get("target_order") or 0),
            }
            for block in blocks
            if isinstance(block, dict)
        ]
        if len(legacy["reviewed_blocks"]) != len(blocks):
            raise CompilerInputError(f"ContentPlanV3 reviewed_blocks 含非 object: {source_id}")

    replacement = decision.get("replacement_ref")
    if replacement is not None:
        if not isinstance(replacement, dict):
            raise CompilerInputError(f"ContentPlanV3 replacement_ref 非法: {source_id}")
        legacy["replacement_path"] = replacement.get("path")
        legacy["replacement_sha256"] = replacement.get("sha256")
        legacy["sanitization_review"] = replacement.get("review_note")
    return legacy


def adapt_content_plan_for_zxty(content_plan: dict[str, Any], content_plan_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    if content_plan.get("schema_version") not in CONTENT_PLAN_SCHEMA_VERSIONS:
        raise CompilerInputError("ContentPlan 不是 V3 schema")
    product = content_plan.get("product")
    if not isinstance(product, dict) or not product.get("model") or not product.get("full_name"):
        raise CompilerInputError("ContentPlanV3 缺少已审核的 product.model/full_name")
    unresolved = content_plan.get("unresolved_items", [])
    review_items = content_plan.get("review_items", [])
    if unresolved or review_items:
        raise CompilerBlockedError("ContentPlanV3 仍包含未决或未处理复核项")
    items = content_plan.get("items")
    if not isinstance(items, list) or not items:
        raise CompilerInputError("ContentPlanV3 items 必须是非空数组")

    source_ids: set[str] = set()
    inventory_items: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise CompilerInputError(f"ContentPlanV3 items[{index}] 必须是 object")
        source_id = item.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            raise CompilerInputError(f"ContentPlanV3 items[{index}] 缺少 source_id")
        if source_id in source_ids:
            raise CompilerInputError(f"ContentPlanV3 source_id 重复: {source_id}")
        source_ids.add(source_id)
        decision = item.get("decision")
        if not isinstance(decision, dict):
            raise CompilerInputError(f"ContentPlanV3 item 缺少 decision: {source_id}")
        inventory_items.append(_legacy_inventory_item(source_id, item.get("source")))
        mappings.append(_legacy_mapping(source_id, decision))

    source_path = _normalized_source(content_plan, content_plan_path)
    inventory = {
        "schema_version": "v3-compiler-adapter",
        "source": str(source_path),
        "items": inventory_items,
    }
    mapping = {
        "schema_version": "v3-compiler-adapter",
        "product": product,
        "manufacturer_terms": content_plan.get("manufacturer_terms", []),
        "identity_review": content_plan.get("identity_review"),
        "mappings": mappings,
    }
    return source_path, inventory, mapping


def compile_fixed_template(
    content_plan_path: Path,
    render_plan_path: Path,
    output: Path,
) -> dict[str, Any]:
    content_plan_path = content_plan_path.resolve()
    render_plan_path = render_plan_path.resolve()
    output = output.resolve()
    try:
        content_plan = _read_object(content_plan_path, "ContentPlanV3")
        render_plan = _read_object(render_plan_path, "RenderPlanV3")
        _validate_execution_plan(content_plan, "content-plan-v3.schema.json", "ContentPlanV3")
        _validate_execution_plan(render_plan, "render-plan-v3.schema.json", "RenderPlanV3")
        if render_plan.get("schema_version") not in RENDER_PLAN_SCHEMA_VERSIONS:
            raise CompilerInputError("RenderPlan 不是 V3 schema")
        upstream = render_plan.get("upstream") if isinstance(render_plan.get("upstream"), dict) else {}
        _verify_binding(render_plan.get("content_plan") or upstream.get("content_plan"), content_plan_path, render_plan_path, "content_plan")
        outputs = render_plan.get("outputs") if isinstance(render_plan.get("outputs"), dict) else {}
        declared_output = render_plan.get("intermediate_docx") or outputs.get("intermediate_docx")
        if isinstance(declared_output, str) and _resolve_relative(declared_output, render_plan_path) != output:
            raise CompilerInputError("CLI --output 与 RenderPlanV3 intermediate_docx 不一致")
        if output.suffix.lower() != ".docx":
            raise CompilerInputError("固定模板编译器只输出中间 DOCX")
        pack = _resolve_pack(render_plan, render_plan_path)
        program = _resolve_program(render_plan, render_plan_path, pack)
        _validate_compiler_identity(render_plan, program)
        adapter_id = program["adapter"]
        if adapter_id == ZXTY_ADAPTER_ID:
            if pack["id"] != "zxty-fixed-v1":
                raise CompilerBlockedError("zxty 适配器仅支持 zxty-fixed-v1")
            if pack["template_sha256"] != build_fixed_docx.TEMPLATE_SHA256:
                raise CompilerBlockedError("zxty 适配器与旧 builder 锁定的模板哈希不一致")
            source_path, inventory, mapping = adapt_content_plan_for_zxty(content_plan, content_plan_path)
        else:
            if content_plan.get("review_items") or content_plan.get("unresolved_items"):
                raise CompilerBlockedError("ContentPlanV3 仍包含未决或未处理复核项")
            source_path = inventory = mapping = None
    except CompilerBlockedError as exc:
        return result(
            "HUMAN_REVIEW" if "未决" in str(exc) or "复核" in str(exc) else "BLOCKED",
            "fixed_template_compiler_v3",
            findings=[{"code": "COMPILER_BLOCKED", "message": str(exc)}],
        )
    except (CompilerInputError, KeyError, ValueError) as exc:
        return result(
            "FAIL",
            "fixed_template_compiler_v3",
            findings=[{"code": "COMPILER_INPUT_INVALID", "message": str(exc)}],
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".docx", dir=output.parent) as stream:
        temporary = Path(stream.name)
    try:
        if program["adapter"] == ZXTY_ADAPTER_ID:
            findings = build_fixed_docx.create_document(
                Path(pack["template_path"]),
                source_path,
                inventory,
                mapping,
                temporary,
                content_plan_path,
            )
        else:
            findings = compile_declarative_docx(
                Path(pack["template_path"]),
                content_plan,
                content_plan_path,
                pack["slot_contract_payload"],
                program,
                temporary,
            )
        temporary.replace(output)
    except Exception as exc:
        if temporary.exists():
            temporary.unlink()
        return result(
            "FAIL",
            "fixed_template_compiler_v3",
            findings=[{"code": "COMPILE_FAILED", "message": str(exc)}],
        )
    return result(
        "HUMAN_REVIEW" if findings else "PASS",
        "fixed_template_compiler_v3",
        findings=findings,
        output=str(output),
        output_sha256=sha256_file(output),
        content_plan_sha256=sha256_file(content_plan_path),
        render_plan_sha256=sha256_file(render_plan_path),
        template_pack={"id": pack["id"], "version": pack["version"]},
        template_program_sha256=program["program_sha256"],
        compiler={"id": COMPILER_ID, "version": COMPILER_VERSION},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="仅消费 ContentPlanV3 + RenderPlanV3 的固定模板编译器")
    parser.add_argument("--content-plan", type=Path, required=True)
    parser.add_argument("--render-plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload = compile_fixed_template(args.content_plan, args.render_plan, args.output)
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
