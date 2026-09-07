from __future__ import annotations

import argparse
import hashlib
import zipfile
from collections import Counter
from pathlib import Path

from lxml import etree

from pipeline_common import finish, result, sha256_file


TEMPLATE_SHA256 = "90B04F4C802A3821234B7A47789CCB47904C7D8E5C486087EC54953A39D19E70"


def xml_signature(element):
    """比较 XML 实义，忽略序列化引号和命名空间声明顺序。"""
    return (element.tag, tuple(sorted(element.attrib.items())), element.text or "",
            tuple(xml_signature(child) for child in element))


def approved_declarative_pack(reference: str) -> dict:
    from template_catalog_v3 import load_template_pack_v3, resolve_pack_reference

    root = Path(__file__).resolve().parents[1]
    resolved = resolve_pack_reference(root, reference)
    if resolved.get("status") != "PASS":
        raise ValueError(f"模板包未获有效批准: {reference}")
    pack = load_template_pack_v3(root, Path(resolved["template_pack"]["template_path"]).parent)
    if pack["compiler"].get("adapter") != "declarative-docx-v3":
        raise ValueError("首模板必须保留其专用不变量校验")
    return pack


def declared_template_findings(template: Path, document: Path, pack: dict) -> list[dict]:
    findings = []
    if sha256_file(template) != pack["template_sha256"]:
        return [{"code": "TEMPLATE_HASH_MISMATCH", "expected": pack["template_sha256"]}]
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with zipfile.ZipFile(template) as baseline, zipfile.ZipFile(document) as output:
        before = etree.fromstring(baseline.read("word/document.xml"))
        after = etree.fromstring(output.read("word/document.xml"))
        sections_before = [xml_signature(x) for x in before.xpath(".//w:sectPr", namespaces=ns)]
        sections_after = [xml_signature(x) for x in after.xpath(".//w:sectPr", namespaces=ns)]
        if sections_before != sections_after:
            findings.append({"code": "TEMPLATE_SECTION_PROPERTIES_CHANGED"})
        preserved = {op["part"] for op in pack["program_payload"]["operations"] if op["op"] == "retain_part"}
        preserved.update(name for name in baseline.namelist() if name.startswith((
            "word/header", "word/footer", "word/_rels/header", "word/_rels/footer", "word/media/")))
        for name in sorted(preserved):
            if name not in output.namelist():
                findings.append({"code": "TEMPLATE_PART_MISSING", "part": name})
                continue
            left, right = baseline.read(name), output.read(name)
            equal = (xml_signature(etree.fromstring(left)) == xml_signature(etree.fromstring(right))
                     if name.endswith((".xml", ".rels")) else left == right)
            if not equal:
                findings.append({"code": "TEMPLATE_PART_CHANGED", "part": name})
        # 原关系必须保留；允许内容插入增加新的图片关系。
        name = "word/_rels/document.xml.rels"
        original_rels = {xml_signature(x) for x in etree.fromstring(baseline.read(name))}
        current_rels = {xml_signature(x) for x in etree.fromstring(output.read(name))}
        if not original_rels.issubset(current_rels):
            findings.append({"code": "TEMPLATE_RELATIONSHIP_CHANGED"})
    return findings


def package_facts(path: Path) -> dict:
    with zipfile.ZipFile(path) as package:
        names = set(package.namelist())
        media = {}
        for name in sorted(item for item in names if item.startswith("word/media/") and not item.endswith("/")):
            data = package.read(name)
            if data:
                media[name] = hashlib.sha256(data).hexdigest().upper()
        document = etree.fromstring(package.read("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        section_count = document.xpath("count(.//w:sectPr)", namespaces=ns)
        body = document.find("w:body", ns)
        body_blocks = list(body)
        section_break_positions = [
            index for index, block in enumerate(body_blocks)
            if block.xpath("./w:pPr/w:sectPr", namespaces=ns)
        ]
        final_section_start = section_break_positions[-1] + 1 if section_break_positions else 0
        closing_body_blocks = [
            block for block in body_blocks[final_section_start:]
            if etree.QName(block).localname != "sectPr"
        ]
        closing_body_text = "".join(
            block.xpath(".//w:t/text()", namespaces=ns)[index]
            for block in closing_body_blocks
            for index in range(len(block.xpath(".//w:t/text()", namespaces=ns)))
        ).strip()
        closing_body_drawings = sum(
            int(block.xpath("count(.//w:drawing)", namespaces=ns))
            for block in closing_body_blocks
        )
        closing_body_tables = sum(
            1 for block in closing_body_blocks
            if etree.QName(block).localname == "tbl"
        )
        relationships = []
        relationships_by_id = {}
        rel_name = "word/_rels/document.xml.rels"
        if rel_name in names:
            root = etree.fromstring(package.read(rel_name))
            for node in root:
                relationship_id = node.get("Id", "")
                relationship_type = node.get("Type", "").rsplit("/", 1)[-1]
                relationship_target = node.get("Target", "")
                relationships.append((relationship_type, relationship_target))
                relationships_by_id[relationship_id] = (relationship_type, relationship_target)

        drawing_ns = {
            "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
            "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
            "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
        }
        body_floating_anchors = int(document.xpath("count(.//w:body//wp:anchor)", namespaces=drawing_ns))
        section_properties = document.xpath("./w:body/w:sectPr | ./w:body/w:p/w:pPr/w:sectPr", namespaces=drawing_ns)
        closing_section = section_properties[-1]
        main_section = section_properties[0]
        main_default_header_targets = []
        for reference in main_section.xpath("./w:headerReference[@w:type='default']", namespaces=drawing_ns):
            relation_id = reference.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
            relation = relationships_by_id.get(relation_id)
            if relation and relation[0] == "header":
                main_default_header_targets.append("word/" + relation[1].lstrip("/"))
        main_default_header_drawings = 0
        for target in main_default_header_targets:
            if target in names:
                header = etree.fromstring(package.read(target))
                main_default_header_drawings += int(header.xpath("count(.//w:drawing)", namespaces=drawing_ns))
        closing_title_page = bool(closing_section.xpath("./w:titlePg", namespaces=drawing_ns))
        closing_first_header_targets = []
        for reference in closing_section.xpath("./w:headerReference[@w:type='first']", namespaces=drawing_ns):
            relation_id = reference.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
            relation = relationships_by_id.get(relation_id)
            if relation and relation[0] == "header":
                closing_first_header_targets.append("word/" + relation[1].lstrip("/"))
        closing_first_header_drawings = 0
        closing_first_header_text = ""
        for target in closing_first_header_targets:
            if target not in names:
                continue
            header = etree.fromstring(package.read(target))
            closing_first_header_drawings += int(header.xpath("count(.//w:drawing)", namespaces=drawing_ns))
            closing_first_header_text += "".join(header.xpath(".//w:t/text()", namespaces=drawing_ns))
        return {
            "sha256": sha256_file(path),
            "sections": int(section_count),
            "media_hashes": media,
            "relationship_types": Counter(kind for kind, _ in relationships),
            "relationship_targets": relationships,
            "has_document": "word/document.xml" in names,
            "closing_body_text": closing_body_text,
            "closing_body_drawings": closing_body_drawings,
            "closing_body_tables": closing_body_tables,
            "body_floating_anchors": body_floating_anchors,
            "main_default_header_targets": main_default_header_targets,
            "main_default_header_drawings": main_default_header_drawings,
            "closing_title_page": closing_title_page,
            "closing_first_header_targets": closing_first_header_targets,
            "closing_first_header_drawings": closing_first_header_drawings,
            "closing_first_header_text": closing_first_header_text.strip(),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="核验内置模板固定对象及成品模板不变量")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--template-pack", help="已批准的声明式模板 id@version；省略时保留首模板规则")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    findings = []
    if not args.template.is_file() or not args.document.is_file():
        return finish(result("BLOCKED", "verify_template_invariants", findings=[{"code": "INPUT_MISSING"}]), args.report)
    if args.template_pack:
        try:
            findings = declared_template_findings(args.template, args.document, approved_declarative_pack(args.template_pack))
        except Exception as exc:
            findings = [{"code": "DECLARED_TEMPLATE_VALIDATION_FAILED", "message": str(exc)}]
        return finish(result("FAIL" if findings else "PASS", "verify_template_invariants", findings=findings,
                             template_reference=args.template_pack), args.report)
    if sha256_file(args.template) != TEMPLATE_SHA256:
        findings.append({"code": "TEMPLATE_HASH_MISMATCH", "actual": sha256_file(args.template), "expected": TEMPLATE_SHA256})
        return finish(result("BLOCKED", "verify_template_invariants", findings=findings), args.report)
    try:
        template = package_facts(args.template)
        document = package_facts(args.document)
    except Exception as exc:
        return finish(result("FAIL", "verify_template_invariants", findings=[{"code": "OOXML_INVALID", "message": str(exc)}]), args.report)
    if document["sections"] != 2:
        findings.append({"code": "SECTION_COUNT_MISMATCH", "expected": 2, "actual": document["sections"]})
    if document["closing_body_text"] or document["closing_body_drawings"] or document["closing_body_tables"]:
        findings.append({
            "code": "CLOSING_BODY_CONTENT_PRESENT",
            "text": bool(document["closing_body_text"]),
            "drawings": document["closing_body_drawings"],
            "tables": document["closing_body_tables"],
        })
    if document["body_floating_anchors"]:
        findings.append({"code": "BODY_FLOATING_ANCHOR_PRESENT", "count": document["body_floating_anchors"]})
    if len(document["main_default_header_targets"]) != 1 or document["main_default_header_drawings"] != 1:
        findings.append({
            "code": "BODY_PAGE_BACKGROUND_MISSING",
            "header_targets": document["main_default_header_targets"],
            "drawings": document["main_default_header_drawings"],
        })
    if not document["closing_title_page"] or len(document["closing_first_header_targets"]) != 1:
        findings.append({
            "code": "CLOSING_FIRST_PAGE_HEADER_MISSING",
            "title_page": document["closing_title_page"],
            "header_targets": document["closing_first_header_targets"],
        })
    if document["closing_first_header_drawings"] != 1 or document["closing_first_header_text"]:
        findings.append({
            "code": "CLOSING_HEADER_CONTENT_INVALID",
            "drawings": document["closing_first_header_drawings"],
            "text": bool(document["closing_first_header_text"]),
        })
    template_hashes = Counter(template["media_hashes"].values())
    output_hashes = Counter(document["media_hashes"].values())
    missing_media = list((template_hashes - output_hashes).elements())
    if missing_media:
        findings.append({"code": "TEMPLATE_MEDIA_MISSING", "hashes": missing_media})
    for kind in ("header", "footer"):
        if document["relationship_types"].get(kind, 0) < template["relationship_types"].get(kind, 0):
            findings.append({"code": "TEMPLATE_RELATIONSHIP_MISSING", "kind": kind})
    status = "FAIL" if findings else "PASS"
    return finish(result(status, "verify_template_invariants", findings=findings, template=template, document=document), args.report)


if __name__ == "__main__":
    raise SystemExit(main())
