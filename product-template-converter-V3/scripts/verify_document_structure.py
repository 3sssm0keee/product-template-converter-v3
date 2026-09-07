from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

from lxml import etree

from pipeline_common import PLACEHOLDERS, SECTIONS, finish, read_json, result


NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}


def paragraphs(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as package:
        root = etree.fromstring(package.read("word/document.xml"))
    output = []
    for index, paragraph in enumerate(root.xpath(".//w:body/w:p", namespaces=NS), 1):
        text = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS)).strip()
        outline = paragraph.xpath("string(./w:pPr/w:outlineLvl/@w:val)", namespaces=NS)
        output.append({"index": index, "text": text, "outline": int(outline) if outline.isdigit() else None})
    return output


def cover_text_color_findings(path: Path, model: str | None, full_name: str | None) -> list[dict]:
    if not model or not full_name:
        return []
    expected_texts = {normalized_inline_text(f"{model} {full_name}"), normalized_inline_text("产 品 介 绍")}
    findings = []
    with zipfile.ZipFile(path) as package:
        root = etree.fromstring(package.read("word/document.xml"))
    for paragraph in root.xpath(".//w:body/w:p", namespaces=NS):
        text = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS)).strip()
        if normalized_title(text) == SECTIONS[0]:
            break
        if normalized_inline_text(text) not in expected_texts:
            continue
        colors = [
            value.upper()
            for value in paragraph.xpath(".//w:r[w:t]/w:rPr/w:color/@w:val", namespaces=NS)
        ]
        if not colors or any(color != "FFFFFF" for color in colors):
            findings.append({"code": "COVER_TEXT_COLOR_INVALID", "text": text, "expected": "FFFFFF", "actual": colors})
    return findings


def has_page_break_before_first_section(path: Path) -> bool:
    with zipfile.ZipFile(path) as package:
        root = etree.fromstring(package.read("word/document.xml"))
    for paragraph in root.xpath(".//w:body/w:p", namespaces=NS):
        text = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS)).strip()
        if normalized_title(text) == SECTIONS[0]:
            return False
        if paragraph.xpath('.//w:br[@w:type="page"]', namespaces=NS):
            return True
    return False


def duplicate_drawing_id_findings(path: Path) -> list[dict]:
    drawing_parts: dict[str, list[str]] = {}
    with zipfile.ZipFile(path) as package:
        for name in package.namelist():
            if not name.startswith("word/") or not name.endswith(".xml"):
                continue
            root = etree.fromstring(package.read(name))
            for node in root.xpath(".//wp:docPr", namespaces=NS):
                drawing_id = str(node.get("id") or "")
                if drawing_id:
                    drawing_parts.setdefault(drawing_id, []).append(name)
    return [
        {"code": "DRAWING_ID_DUPLICATE", "id": drawing_id, "parts": parts}
        for drawing_id, parts in sorted(drawing_parts.items())
        if len(parts) > 1
    ]


def pagination_binding_findings(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as package:
        root = etree.fromstring(package.read("word/document.xml"))
    findings = []
    body = root.find("w:body", namespaces=NS)
    if body is None:
        return [{"code": "DOCUMENT_BODY_MISSING"}]
    children = list(body)
    body_started = False
    for index, node in enumerate(children):
        if node.tag != f"{{{NS['w']}}}p":
            continue
        text = "".join(node.xpath(".//w:t/text()", namespaces=NS)).strip()
        keep_next_nodes = node.xpath("./w:pPr/w:keepNext", namespaces=NS)
        keep_next = False
        if keep_next_nodes:
            value = keep_next_nodes[0].get(f"{{{NS['w']}}}val")
            keep_next = value is None or str(value).strip().lower() not in {"0", "false", "no", "off"}
        normalized = normalized_title(text)
        if normalized in SECTIONS and not keep_next:
            findings.append({"code": "SECTION_KEEP_NEXT_MISSING", "section": normalized})
        if normalized == SECTIONS[0]:
            body_started = True
        if not body_started or not text or index + 1 >= len(children):
            continue
        following = children[index + 1]
        if following.xpath(".//w:drawing", namespaces=NS) and not keep_next:
            findings.append({"code": "IMAGE_LABEL_KEEP_NEXT_MISSING", "text": text})
    return findings


def normalized_title(text: str) -> str:
    return re.sub(r"^[▌▎|丨\s]+", "", text).strip()


def normalized_inline_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def expected_section_title_counts(content_map: Path | None) -> dict[str, int]:
    if content_map is not None:
        read_json(content_map)
    return {section: 1 for section in SECTIONS}


def main() -> int:
    parser = argparse.ArgumentParser(description="检查六篇章、重复标题、占位符及导航大纲")
    parser.add_argument("document", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--full-name")
    parser.add_argument("--content-map", type=Path)
    parser.add_argument("--allow-intermediate-name", action="store_true")
    parser.add_argument("--template-pack", help="按已批准声明式模板的非编辑段落及格式核验")
    parser.add_argument("--content-plan", type=Path, help="绑定内容计划，仅用于已批准的空补充章节省略")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.document.is_file():
        return finish(result("BLOCKED", "verify_document_structure", findings=[{"code": "DOCUMENT_MISSING"}]), args.report)
    try:
        values = paragraphs(args.document)
    except Exception as exc:
        return finish(result("FAIL", "verify_document_structure", findings=[{"code": "OOXML_INVALID", "message": str(exc)}]), args.report)
    findings = []
    if args.template_pack:
        from verify_template_invariants import approved_declarative_pack, xml_signature
        try:
            pack = approved_declarative_pack(args.template_pack)
            with zipfile.ZipFile(pack["template_path"]) as original, zipfile.ZipFile(args.document) as output:
                baseline = etree.fromstring(original.read("word/document.xml"))
                current = etree.fromstring(output.read("word/document.xml"))
            editable = {int(slot["location"].rsplit(":", 1)[1]) for slot in pack["slot_contract_payload"]["slots"]}
            if args.content_plan:
                from declarative_docx_adapter_v3 import EMPTY_SUPPLEMENT_TEMPLATE_SHA, REPAIRED_SECOND_TEMPLATE_SHA, supplement_has_content
                from v3_common import canonical_json_sha256
                plan = read_json(args.content_plan)
                digest = canonical_json_sha256({key: value for key, value in plan.items() if key != "canonical_sha256"})
                if plan.get("canonical_sha256") != digest or plan.get("template_pack") != pack["program_payload"]["template_pack"]:
                    raise ValueError("空章节省略的内容计划哈希或模板绑定失效")
                template_sha = pack["program_payload"]["template_sha256"]
                if template_sha == REPAIRED_SECOND_TEMPLATE_SHA:
                    # 仅允许新版已知隐藏标题及紧邻空段的 1pt 行高，仍逐项比较其他格式。
                    parameters = baseline.xpath('.//w:body/w:p[w:r/w:t="五、技术参数"]', namespaces=NS)[0]
                    for node in (parameters, parameters.getnext()):
                        p_pr = node.find("w:pPr", namespaces=NS)
                        if p_pr is None:
                            p_pr = etree.Element(f"{{{NS['w']}}}pPr")
                            node.insert(0, p_pr)
                        spacing = p_pr.find("w:spacing", namespaces=NS)
                        if spacing is None:
                            spacing = etree.SubElement(p_pr, f"{{{NS['w']}}}spacing")
                        spacing.set(f"{{{NS['w']}}}line", "20")
                        spacing.set(f"{{{NS['w']}}}lineRule", "exact")
                if template_sha in {EMPTY_SUPPLEMENT_TEMPLATE_SHA, REPAIRED_SECOND_TEMPLATE_SHA}:
                    # 已批准的分页折叠不豁免非空补充标题，空章节省略仍单独检查。
                    if not supplement_has_content(plan):
                        editable.add(26)
                if template_sha == EMPTY_SUPPLEMENT_TEMPLATE_SHA:
                    # 新版模板已在原件中去除硬分页；此折叠兼容仅适用于旧版。
                    folded = current.xpath('.//w:body//w:p[w:r/w:t="五、技术参数"]/w:pPr/w:pageBreakBefore', namespaces=NS)
                    if folded:
                        editable.add(30)
                        parameters = baseline.xpath(".//w:body//w:p", namespaces=NS)[28]
                        p_pr = parameters.find("w:pPr", namespaces=NS)
                        p_pr.insert(0, etree.Element(f"{{{NS['w']}}}pageBreakBefore"))
            expected = [xml_signature(p) for index, p in enumerate(baseline.xpath(".//w:body//w:p", namespaces=NS), 1)
                        if index not in editable]
            actual = iter(xml_signature(p) for p in current.xpath(".//w:body//w:p", namespaces=NS))
            # 仅内容槽可变；固定章节、格式和分页段落须按原顺序完整存在。
            for signature in expected:
                if not any(value == signature for value in actual):
                    findings.append({"code": "TEMPLATE_STATIC_PARAGRAPH_CHANGED_OR_MISSING"})
                    break
            findings.extend(duplicate_drawing_id_findings(args.document))
            complete_text = "\n".join(current.xpath(".//w:t/text()", namespaces=NS))
            for placeholder in PLACEHOLDERS:
                if re.search(re.escape(placeholder), complete_text, flags=re.IGNORECASE):
                    findings.append({"code": "PLACEHOLDER_VISIBLE", "value": placeholder})
        except Exception as exc:
            findings.append({"code": "DECLARED_STRUCTURE_VALIDATION_FAILED", "message": str(exc)})
        return finish(result("FAIL" if findings else "PASS", "verify_document_structure", findings=findings,
                             template_reference=args.template_pack), args.report)
    try:
        expected_counts = expected_section_title_counts(args.content_map)
    except Exception as exc:
        return finish(result("FAIL", "verify_document_structure", findings=[{"code": "CONTENT_MAP_INVALID", "message": str(exc)}]), args.report)
    titles = [normalized_title(item["text"]) for item in values]
    positions = []
    for section in SECTIONS:
        matches = [index for index, title in enumerate(titles) if title == section]
        expected_count = expected_counts[section]
        if len(matches) != expected_count:
            findings.append({"code": "SECTION_TITLE_COUNT", "section": section, "actual": len(matches), "expected": expected_count})
        elif expected_count == 1:
            positions.append(matches[0])
            if values[matches[0]]["outline"] != 0:
                findings.append({"code": "SECTION_OUTLINE_INVALID", "section": section, "actual": values[matches[0]]["outline"]})
    expected_position_count = sum(expected_counts.values())
    if len(positions) == expected_position_count and positions != sorted(positions):
        findings.append({"code": "SECTION_ORDER_INVALID", "positions": positions})
    if not has_page_break_before_first_section(args.document):
        findings.append({"code": "COVER_BODY_PAGE_BREAK_MISSING", "section": SECTIONS[0]})
    findings.extend(duplicate_drawing_id_findings(args.document))
    findings.extend(pagination_binding_findings(args.document))
    complete_text = "\n".join(item["text"] for item in values)
    for placeholder in PLACEHOLDERS:
        if re.search(re.escape(placeholder), complete_text, flags=re.IGNORECASE):
            findings.append({"code": "PLACEHOLDER_VISIBLE", "value": placeholder})
    function_aliases = [
        normalized_title(item["text"])
        for item in values
        if item.get("outline") == 0
        and re.fullmatch(r"(?:二、)?(?:设备功能|系统功能|产品功能|功能介绍)(?:\s*\(续\))?", normalized_title(item["text"]))
    ]
    if len(function_aliases) != 1:
        findings.append({"code": "FUNCTION_SECTION_DUPLICATED", "titles": function_aliases, "actual": len(function_aliases)})
    if "（续）" in complete_text or "(续)" in complete_text:
        findings.append({"code": "CONTINUATION_LABEL_VISIBLE"})
    if args.model and args.full_name:
        findings.extend(cover_text_color_findings(args.document, args.model, args.full_name))
        expected = f"{args.model}_{args.full_name}-产品介绍.docx"
        if not args.allow_intermediate_name and args.document.name != expected:
            findings.append({"code": "OUTPUT_NAME_MISMATCH", "expected": expected, "actual": args.document.name})
    status = "FAIL" if findings else "PASS"
    return finish(result(status, "verify_document_structure", findings=findings, paragraph_count=len(values), sections=positions), args.report)


if __name__ == "__main__":
    raise SystemExit(main())
