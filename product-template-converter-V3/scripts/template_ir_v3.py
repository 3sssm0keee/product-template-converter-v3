from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import re
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from lxml import etree
from PIL import Image

from pipeline_common import sha256_file, utc_now


IR_SCHEMA_VERSION = "docx-template-ir-v3"
PROGRAM_SCHEMA_VERSION = "template-program-v3"
SLOT_SCHEMA_VERSION = "slot-contract-v3"
ANALYZER_ID = "docx-template-analyzer"
ANALYZER_VERSION = "3.0.0"
COMPILER_ID = "fixed-template-compiler-v3"
COMPILER_VERSION = "3.0.0"

MAX_PACKAGE_ENTRIES = 10_000
MAX_PACKAGE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS = {"w": W_NS, "r": R_NS, "wp": WP_NS, "a": A_NS}

REQUIRED_DOCX_MEMBERS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "word/document.xml",
}

ALLOWED_DSL_OPERATIONS = frozenset(
    {
        "retain_part",
        "bind_slot",
        "bind_asset_role",
        "apply_style_ref",
        "repeat_block",
        "insert_text",
        "insert_image",
        "insert_table",
        "bind_header_footer",
        "section_break",
        "page_break",
        "validate_invariant",
    }
)

_OPERATION_KEYS: dict[str, frozenset[str]] = {
    "retain_part": frozenset({"op", "part", "required"}),
    "bind_slot": frozenset({"op", "slot_id", "source", "required"}),
    "bind_asset_role": frozenset({"op", "role", "part", "required"}),
    "apply_style_ref": frozenset({"op", "slot_id", "style_id"}),
    "repeat_block": frozenset({"op", "slot_id", "source", "min_items", "max_items"}),
    "insert_text": frozenset({"op", "slot_id", "source", "style_id"}),
    "insert_image": frozenset({"op", "slot_id", "source", "fit"}),
    "insert_table": frozenset({"op", "slot_id", "source", "geometry_id"}),
    "bind_header_footer": frozenset({"op", "section", "kind", "part"}),
    "section_break": frozenset({"op", "type"}),
    "page_break": frozenset({"op"}),
    "validate_invariant": frozenset({"op", "invariant", "expected"}),
}

_FORBIDDEN_PROGRAM_KEYS = frozenset(
    {
        "python",
        "python_code",
        "shell",
        "command",
        "macro",
        "vba",
        "ooxml",
        "raw_ooxml",
        "script",
        "eval",
        "exec",
    }
)


class TemplateIRError(ValueError):
    """表示模板包不是可安全分析的 DOCX。"""


class TemplateProgramError(ValueError):
    """表示声明式 Template Program 越过白名单边界。"""


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest().upper()


def artifact_sha256(payload: dict[str, Any], field: str) -> str:
    material = deepcopy(payload)
    material.pop(field, None)
    # 生成时间是运行元数据，不得让同一输入的 IR/DSL 因时钟变成新制品。
    material.pop("generated_at", None)
    return canonical_sha256(material)


def _safe_parser() -> etree.XMLParser:
    return etree.XMLParser(resolve_entities=False, no_network=True, recover=False, huge_tree=False)


def _xml_root(data: bytes, member: str) -> etree._Element:
    try:
        return etree.fromstring(data, parser=_safe_parser())
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise TemplateIRError(f"DOCX XML 无法解析: {member}: {exc}") from exc


def _package_names(package: zipfile.ZipFile) -> set[str]:
    infos = package.infolist()
    if len(infos) > MAX_PACKAGE_ENTRIES:
        raise TemplateIRError("DOCX 容器条目数超过安全上限")
    total = sum(info.file_size for info in infos)
    if total > MAX_PACKAGE_UNCOMPRESSED_BYTES:
        raise TemplateIRError("DOCX 容器解压后大小超过安全上限")
    if any(info.flag_bits & 0x1 for info in infos):
        raise TemplateIRError("DOCX 容器已加密或受密码保护")
    names = {info.filename for info in infos}
    missing = sorted(REQUIRED_DOCX_MEMBERS - names)
    if missing:
        raise TemplateIRError(f"DOCX 缺少必需主部件: {', '.join(missing)}")
    return names


def _assert_docx_content_type(package: zipfile.ZipFile) -> None:
    root = _xml_root(package.read("[Content_Types].xml"), "[Content_Types].xml")
    document_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    overrides = {
        node.get("PartName"): node.get("ContentType")
        for node in root.xpath("./ct:Override", namespaces={"ct": CT_NS})
    }
    if overrides.get("/word/document.xml") != document_type:
        raise TemplateIRError("OOXML 容器不是 DOCX 主文档类型")


def validate_docx_template(path: Path) -> dict[str, Any]:
    path = path.resolve()
    if path.suffix.lower() != ".docx":
        raise TemplateIRError("V3 首版只支持 DOCX 目标模板")
    if not path.is_file():
        raise TemplateIRError(f"模板文件不存在: {path}")
    try:
        with zipfile.ZipFile(path) as package:
            names = _package_names(package)
            _assert_docx_content_type(package)
    except zipfile.BadZipFile as exc:
        raise TemplateIRError("DOCX 容器损坏或不是 ZIP/OOXML") from exc
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "member_count": len(names),
        "container": "ooxml-docx",
    }


def _canonical_xml(data: bytes, member: str) -> bytes:
    root = _xml_root(data, member)
    volatile_local_names = {"rsid", "rsidR", "rsidRDefault", "rsidP", "rsidRPr", "paraId", "textId"}
    for node in list(root.xpath(".//*")):
        if etree.QName(node).localname == "rsids" and node.getparent() is not None:
            node.getparent().remove(node)
            continue
        for attribute in list(node.attrib):
            if etree.QName(attribute).localname in volatile_local_names:
                del node.attrib[attribute]
    return etree.tostring(root, method="c14n", with_comments=False)


def _structure_fingerprint(package: zipfile.ZipFile, names: set[str]) -> tuple[str, list[str]]:
    selected = [
        name
        for name in names
        if name in {"[Content_Types].xml", "_rels/.rels"}
        or name.startswith("word/")
        and (
            name.endswith(".xml")
            or name.endswith(".xml.rels")
        )
    ]
    digest = hashlib.sha256()
    for member in sorted(selected):
        digest.update(member.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_canonical_xml(package.read(member), member))
        digest.update(b"\0")
    return digest.hexdigest().upper(), sorted(selected)


def _attr_int(node: etree._Element | None, local_name: str) -> int | None:
    if node is None:
        return None
    raw = node.get(f"{{{W_NS}}}{local_name}")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _section_contracts(document: etree._Element) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    for index, section in enumerate(document.xpath(".//w:sectPr", namespaces=NS), 1):
        size = section.find("w:pgSz", namespaces=NS)
        margins = section.find("w:pgMar", namespaces=NS)
        section_type = section.find("w:type", namespaces=NS)
        headers = [
            {"type": node.get(f"{{{W_NS}}}type", "default"), "relationship_id": node.get(f"{{{R_NS}}}id", "")}
            for node in section.findall("w:headerReference", namespaces=NS)
        ]
        footers = [
            {"type": node.get(f"{{{W_NS}}}type", "default"), "relationship_id": node.get(f"{{{R_NS}}}id", "")}
            for node in section.findall("w:footerReference", namespaces=NS)
        ]
        contracts.append(
            {
                "index": index,
                "type": section_type.get(f"{{{W_NS}}}val", "continuous") if section_type is not None else "continuous",
                "page_size_twips": {
                    "width": _attr_int(size, "w"),
                    "height": _attr_int(size, "h"),
                    "orientation": size.get(f"{{{W_NS}}}orient", "portrait") if size is not None else "portrait",
                },
                "margins_twips": {
                    key: _attr_int(margins, key)
                    for key in ("top", "right", "bottom", "left", "header", "footer", "gutter")
                },
                "title_page": section.find("w:titlePg", namespaces=NS) is not None,
                "headers": headers,
                "footers": footers,
            }
        )
    return contracts


def _document_relationships(package: zipfile.ZipFile, names: set[str]) -> dict[str, dict[str, str]]:
    member = "word/_rels/document.xml.rels"
    if member not in names:
        return {}
    root = _xml_root(package.read(member), member)
    return {
        str(node.get("Id")): {
            "type": str(node.get("Type", "")),
            "target": str(node.get("Target", "")),
            "target_mode": str(node.get("TargetMode", "Internal")),
        }
        for node in root.xpath("./pr:Relationship", namespaces={"pr": PKG_REL_NS})
    }


def _styles(package: zipfile.ZipFile, names: set[str]) -> list[dict[str, Any]]:
    member = "word/styles.xml"
    if member not in names:
        return []
    root = _xml_root(package.read(member), member)
    records = []
    for style in root.xpath("./w:style", namespaces=NS):
        name = style.find("w:name", namespaces=NS)
        based_on = style.find("w:basedOn", namespaces=NS)
        records.append(
            {
                "style_id": style.get(f"{{{W_NS}}}styleId", ""),
                "type": style.get(f"{{{W_NS}}}type", ""),
                "name": name.get(f"{{{W_NS}}}val", "") if name is not None else "",
                "based_on": based_on.get(f"{{{W_NS}}}val", "") if based_on is not None else "",
            }
        )
    return sorted(records, key=lambda value: (value["type"], value["style_id"]))


def _numbering(package: zipfile.ZipFile, names: set[str]) -> dict[str, list[str]]:
    member = "word/numbering.xml"
    if member not in names:
        return {"abstract_num_ids": [], "num_ids": []}
    root = _xml_root(package.read(member), member)
    return {
        "abstract_num_ids": sorted(
            filter(None, (node.get(f"{{{W_NS}}}abstractNumId") for node in root.xpath("./w:abstractNum", namespaces=NS)))
        ),
        "num_ids": sorted(filter(None, (node.get(f"{{{W_NS}}}numId") for node in root.xpath("./w:num", namespaces=NS)))),
    }


def _theme_summary(package: zipfile.ZipFile, names: set[str]) -> dict[str, Any]:
    member = "word/theme/theme1.xml"
    if member not in names:
        return {"part": None, "major_fonts": [], "minor_fonts": [], "colors": []}
    root = _xml_root(package.read(member), member)
    major_fonts = sorted(set(root.xpath(".//a:majorFont//@typeface", namespaces=NS)))
    minor_fonts = sorted(set(root.xpath(".//a:minorFont//@typeface", namespaces=NS)))
    colors = sorted(
        set(root.xpath(".//a:clrScheme/*/a:srgbClr/@val | .//a:clrScheme/*/a:sysClr/@lastClr", namespaces=NS))
    )
    return {"part": member, "major_fonts": major_fonts, "minor_fonts": minor_fonts, "colors": colors}


def _decoded_visual_sha256(data: bytes) -> str | None:
    """对解码后像素取指纹，消除 JPEG/PNG 容器元数据差异，且避免逐像素 Python 遍历。"""
    try:
        with Image.open(io.BytesIO(data)) as image:
            normalized = image.convert("RGBA")
            digest = hashlib.sha256()
            digest.update(f"{normalized.width}x{normalized.height}|RGBA".encode("ascii"))
            digest.update(normalized.tobytes())
            return digest.hexdigest().upper()
    except Exception:
        return None


def _media(package: zipfile.ZipFile, names: set[str], relationships: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    referenced: dict[str, list[str]] = {}
    for relationship_id, relationship in relationships.items():
        target = relationship["target"].replace("\\", "/")
        if target.startswith("media/"):
            referenced.setdefault(f"word/{target}", []).append(relationship_id)
    records = []
    for member in sorted(name for name in names if name.startswith("word/media/") and not name.endswith("/")):
        data = package.read(member)
        visual_sha = _decoded_visual_sha256(data)
        records.append(
            {
                "part": member,
                "sha256": hashlib.sha256(data).hexdigest().upper(),
                "visual_sha256": visual_sha,
                "content_type": mimetypes.guess_type(member)[0] or "application/octet-stream",
                "relationship_ids": sorted(referenced.get(member, [])),
                "semantic_role": "unassigned",
                "recognition_source": "deterministic",
            }
        )
    return records


def _regions(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not sections:
        return []
    if len(sections) == 1:
        return [{"role": "body", "section_indexes": [1], "recognition_source": "deterministic"}]
    body_indexes = list(range(2, len(sections)))
    records = [{"role": "cover", "section_indexes": [1], "recognition_source": "inferred"}]
    if body_indexes:
        records.append({"role": "body", "section_indexes": body_indexes, "recognition_source": "inferred"})
    records.append({"role": "closing", "section_indexes": [len(sections)], "recognition_source": "inferred"})
    return records


def _detected_slots(document: etree._Element) -> list[dict[str, Any]]:
    placeholders = re.compile(r"(?:X{2,}|[ＸＸ]{2,}|产品全称|产品型号|型号|待填|请输入)", re.IGNORECASE)
    records = []
    for index, paragraph in enumerate(document.xpath(".//w:p", namespaces=NS), 1):
        text = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS)).strip()
        if text and placeholders.search(text):
            records.append(
                {
                    "slot_id": f"detected-text-{index}",
                    "kind": "text",
                    "location": f"word/document.xml#paragraph:{index}",
                    "placeholder_text": text,
                    "recognition_source": "inferred",
                    "review_required": True,
                }
            )
    return records


def _unsupported_objects(names: set[str], document: etree._Element) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    prefixes = {
        "word/vbaProject.bin": "VBA_MACRO",
        "word/embeddings/": "EMBEDDED_OBJECT",
        "word/activeX/": "ACTIVEX_CONTROL",
    }
    for prefix, code in prefixes.items():
        matches = sorted(name for name in names if name == prefix or name.startswith(prefix))
        for member in matches:
            records.append({"code": code, "location": member, "severity": "BLOCKED"})
    for index, _ in enumerate(document.xpath(".//w:altChunk", namespaces=NS), 1):
        records.append({"code": "ALTCHUNK", "location": f"word/document.xml#altChunk:{index}", "severity": "BLOCKED"})
    for index, _ in enumerate(document.xpath(".//w:object | .//w:control", namespaces=NS), 1):
        records.append({"code": "INLINE_OBJECT_OR_CONTROL", "location": f"word/document.xml#object:{index}", "severity": "BLOCKED"})
    return records


def _drawing_summary(document: etree._Element) -> dict[str, int]:
    return {
        "drawing_count": len(document.xpath(".//w:drawing", namespaces=NS)),
        "floating_anchor_count": len(document.xpath(".//wp:anchor", namespaces=NS)),
        "inline_drawing_count": len(document.xpath(".//wp:inline", namespaces=NS)),
    }


def _visual_proxy_hash(sections: list[dict[str, Any]], media: list[dict[str, Any]], drawing: dict[str, int]) -> str:
    return canonical_sha256(
        {
            "method": "page-geometry-media-proxy-v1",
            "sections": sections,
            "media": [{"part": item["part"], "visual_sha256": item["visual_sha256"]} for item in media],
            "drawing": drawing,
        }
    )


def build_docx_template_ir(path: Path) -> dict[str, Any]:
    package_facts = validate_docx_template(path)
    resolved = Path(package_facts["path"])
    with zipfile.ZipFile(resolved) as package:
        names = _package_names(package)
        document = _xml_root(package.read("word/document.xml"), "word/document.xml")
        structure_sha, structural_parts = _structure_fingerprint(package, names)
        relationships = _document_relationships(package, names)
        sections = _section_contracts(document)
        media = _media(package, names, relationships)
        drawing = _drawing_summary(document)
        settings = _xml_root(package.read("word/settings.xml"), "word/settings.xml") if "word/settings.xml" in names else None
        even_odd = bool(settings is not None and settings.find("w:evenAndOddHeaders", namespaces=NS) is not None)
        payload: dict[str, Any] = {
            "schema_version": IR_SCHEMA_VERSION,
            "artifact_type": "DocxTemplateIRV3",
            "generated_at": utc_now(),
            "analyzer": {"id": ANALYZER_ID, "version": ANALYZER_VERSION},
            "source_template": {
                "path": str(resolved),
                "sha256": package_facts["sha256"],
                "container": package_facts["container"],
                "member_count": package_facts["member_count"],
            },
            "fingerprint": {
                "byte_sha256": package_facts["sha256"],
                "normalized_structure_sha256": structure_sha,
                "visual_proxy_sha256": _visual_proxy_hash(sections, media, drawing),
                "visual_method": "page-geometry-media-proxy-v1",
                "structural_parts": structural_parts,
            },
            "page_contract": {
                "sections": sections,
                "different_even_odd_headers": even_odd,
                "regions": _regions(sections),
            },
            "styles": _styles(package, names),
            "numbering": _numbering(package, names),
            "theme": _theme_summary(package, names),
            "media": media,
            "detected_slots": _detected_slots(document),
            "drawing": drawing,
            "unsupported_objects": _unsupported_objects(names, document),
            "recognition": {
                "source": "deterministic",
                "review_state": "REVIEW_REQUIRED",
                "approved_scope": None,
            },
        }
    payload["ir_sha256"] = artifact_sha256(payload, "ir_sha256")
    return payload


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def validate_template_program(program: dict[str, Any]) -> None:
    if not isinstance(program, dict):
        raise TemplateProgramError("Template Program 必须是 JSON object")
    allowed_top_level = {
        "schema_version",
        "artifact_type",
        "template_pack",
        "template_sha256",
        "template_ir_sha256",
        "compiler",
        "adapter",
        "operations",
        "program_sha256",
    }
    required_top_level = allowed_top_level
    missing_top = sorted(required_top_level - set(program))
    if missing_top:
        raise TemplateProgramError(f"Template Program 缺少必需顶层字段: {', '.join(missing_top)}")
    unknown_top = sorted(set(program) - allowed_top_level)
    if unknown_top:
        raise TemplateProgramError(f"Template Program 包含未允许的顶层字段: {', '.join(unknown_top)}")
    forbidden = sorted({key.lower() for key in _walk_keys(program)} & _FORBIDDEN_PROGRAM_KEYS)
    if forbidden:
        raise TemplateProgramError(f"Template Program 包含禁止的代码字段: {', '.join(forbidden)}")
    if program.get("schema_version") != PROGRAM_SCHEMA_VERSION:
        raise TemplateProgramError("Template Program schema_version 不是 template-program-v3")
    compiler = program.get("compiler")
    if (
        not isinstance(compiler, dict)
        or compiler.get("id") != COMPILER_ID
        or compiler.get("version") != COMPILER_VERSION
        or set(compiler) != {"id", "version"}
    ):
        raise TemplateProgramError("未知或未批准的固定编译器")
    operations = program.get("operations")
    if not isinstance(operations, list) or not operations:
        raise TemplateProgramError("Template Program operations 必须是非空数组")
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise TemplateProgramError(f"operations[{index}] 必须是 object")
        name = operation.get("op")
        if name not in ALLOWED_DSL_OPERATIONS:
            raise TemplateProgramError(f"operations[{index}] 使用未允许操作: {name}")
        required_by_operation = {
            "retain_part": {"op", "part", "required"},
            "bind_slot": {"op", "slot_id", "source", "required"},
            "bind_asset_role": {"op", "role", "part", "required"},
            "apply_style_ref": {"op", "slot_id", "style_id"},
            "repeat_block": {"op", "slot_id", "source", "min_items", "max_items"},
            "insert_text": {"op", "slot_id", "source"},
            "insert_image": {"op", "slot_id", "source", "fit"},
            "insert_table": {"op", "slot_id", "source", "geometry_id"},
            "bind_header_footer": {"op", "section", "kind", "part"},
            "section_break": {"op", "type"},
            "page_break": {"op"},
            "validate_invariant": {"op", "invariant", "expected"},
        }[str(name)]
        missing = sorted(required_by_operation - set(operation))
        if missing:
            raise TemplateProgramError(f"operations[{index}] 缺少必需字段: {', '.join(missing)}")
        unknown = sorted(set(operation) - _OPERATION_KEYS[str(name)])
        if unknown:
            raise TemplateProgramError(f"operations[{index}] 包含未允许字段: {', '.join(unknown)}")
    expected = program.get("program_sha256")
    if expected is not None and expected != artifact_sha256(program, "program_sha256"):
        raise TemplateProgramError("Template Program 哈希不匹配")


def finalize_template_program(program: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(program)
    payload["program_sha256"] = artifact_sha256(payload, "program_sha256")
    validate_template_program(payload)
    return payload


def build_conservative_slot_contract(template_ir: dict[str, Any]) -> dict[str, Any]:
    slots = []
    for detected in template_ir.get("detected_slots", []):
        slots.append(
            {
                "slot_id": detected["slot_id"],
                "kind": detected["kind"],
                "required": False,
                "location": detected["location"],
                "growth": "review_required",
                "recognition_source": detected["recognition_source"],
                "review_required": True,
            }
        )
    payload: dict[str, Any] = {
        "schema_version": SLOT_SCHEMA_VERSION,
        "artifact_type": "SlotContractV3",
        "template_sha256": template_ir["source_template"]["sha256"],
        "template_ir_sha256": template_ir["ir_sha256"],
        "slots": slots,
        "review_state": "REVIEW_REQUIRED",
    }
    payload["slot_contract_sha256"] = artifact_sha256(payload, "slot_contract_sha256")
    return payload


def build_conservative_program(template_ir: dict[str, Any], pack_id: str, version: str) -> dict[str, Any]:
    operations: list[dict[str, Any]] = [
        {"op": "retain_part", "part": member, "required": True}
        for member in template_ir["fingerprint"]["structural_parts"]
        if member in {"word/styles.xml", "word/numbering.xml", "word/theme/theme1.xml"}
    ]
    operations.append(
        {
            "op": "validate_invariant",
            "invariant": "normalized_structure_sha256",
            "expected": template_ir["fingerprint"]["normalized_structure_sha256"],
        }
    )
    for detected in template_ir.get("detected_slots", []):
        operations.append(
            {
                "op": "bind_slot",
                "slot_id": detected["slot_id"],
                "source": f"content_plan.items[decision.target_slot={detected['slot_id']}]",
                "required": False,
            }
        )
    return finalize_template_program(
        {
            "schema_version": PROGRAM_SCHEMA_VERSION,
            "artifact_type": "TemplateProgramV3",
            "template_pack": {"id": pack_id, "version": version},
            "template_sha256": template_ir["source_template"]["sha256"],
            "template_ir_sha256": template_ir["ir_sha256"],
            "compiler": {"id": COMPILER_ID, "version": COMPILER_VERSION},
            "adapter": "declarative-docx-v3",
            "operations": operations,
        }
    )
