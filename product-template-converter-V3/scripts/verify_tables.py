from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

from lxml import etree

from pipeline_common import finish, result


NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def main() -> int:
    parser = argparse.ArgumentParser(description="检查DOCX表格固定网格、表头重复和禁止拆行")
    parser.add_argument("document", type=Path)
    parser.add_argument("--template-pack", help="只保留已批准模板中未改变的装饰表格规则")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.document.is_file():
        return finish(result("BLOCKED", "verify_tables", findings=[{"code": "DOCUMENT_MISSING"}]), args.report)
    try:
        with zipfile.ZipFile(args.document) as package:
            root = etree.fromstring(package.read("word/document.xml"))
    except Exception as exc:
        return finish(result("FAIL", "verify_tables", findings=[{"code": "OOXML_INVALID", "message": str(exc)}]), args.report)
    findings = []
    facts = []
    tables = root.xpath(".//w:tbl", namespaces=NS)
    reference_tables = []
    if args.template_pack:
        from verify_template_invariants import approved_declarative_pack, xml_signature
        try:
            pack = approved_declarative_pack(args.template_pack)
            with zipfile.ZipFile(pack["template_path"]) as package:
                baseline = etree.fromstring(package.read("word/document.xml"))
            reference_tables = [xml_signature(t) for t in baseline.xpath(".//w:tbl", namespaces=NS)]
        except Exception as exc:
            return finish(result("FAIL", "verify_tables", findings=[{"code": "TABLE_REFERENCE_INVALID", "message": str(exc)}]), args.report)
    for table_index, table in enumerate(tables, 1):
        rows = table.xpath("./w:tr", namespaces=NS)
        grid = table.xpath("./w:tblGrid/w:gridCol", namespaces=NS)
        if args.template_pack and xml_signature(table) in reference_tables:
            reference_tables.remove(xml_signature(table))
            facts.append({"table": table_index, "rows": len(rows), "columns": len(grid), "unchanged_template_table": True})
            continue
        if not grid:
            findings.append({"code": "TABLE_GRID_MISSING", "table": table_index})
        for row_index, row in enumerate(rows, 1):
            if not row.xpath("./w:trPr/w:cantSplit", namespaces=NS):
                findings.append({"code": "ROW_CAN_SPLIT", "table": table_index, "row": row_index})
            cells = row.xpath("./w:tc", namespaces=NS)
            for cell_index, cell in enumerate(cells, 1):
                if not cell.xpath("./w:tcPr/w:tcW", namespaces=NS):
                    findings.append({"code": "CELL_WIDTH_MISSING", "table": table_index, "row": row_index, "cell": cell_index})
        if rows and not rows[0].xpath("./w:trPr/w:tblHeader", namespaces=NS):
            findings.append({"code": "REPEAT_HEADER_MISSING", "table": table_index})
        exact_heights = table.xpath("./w:tr/w:trPr/w:trHeight[@w:hRule='exact']", namespaces=NS)
        if exact_heights:
            findings.append({"code": "FIXED_ROW_HEIGHT_PRESENT", "table": table_index, "count": len(exact_heights)})
        facts.append({"table": table_index, "rows": len(rows), "columns": len(grid)})
    if reference_tables:
        findings.append({"code": "TEMPLATE_TABLE_CHANGED_OR_MISSING", "count": len(reference_tables)})
    status = "FAIL" if findings else "PASS"
    return finish(result(status, "verify_tables", findings=findings, table_count=len(tables), tables=facts), args.report)


if __name__ == "__main__":
    raise SystemExit(main())
