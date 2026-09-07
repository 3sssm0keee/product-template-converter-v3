from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

from lxml import etree

from pipeline_common import write_json


EXPECTED_SHA256 = "90B04F4C802A3821234B7A47789CCB47904C7D8E5C486087EC54953A39D19E70"
EXPECTED_RELATION_PARTS = {"word/header1.xml", "word/header2.xml", "word/footer1.xml", "word/footer2.xml"}
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 zxty 固定模板的字节与 OOXML 不变量")
    parser.add_argument("template", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    path = args.template
    facts: dict[str, object] = {"path": str(path)}
    errors: list[str] = []
    if not path.is_file():
        errors.append("模板不存在")
    else:
        digest = file_hash(path)
        facts["sha256"] = digest
        if digest != EXPECTED_SHA256:
            errors.append("模板SHA-256不匹配")
        try:
            with zipfile.ZipFile(path) as package:
                names = set(package.namelist())
                missing = sorted(EXPECTED_RELATION_PARTS - names)
                if missing:
                    errors.append("缺少页眉页脚部件：" + ", ".join(missing))
                root = etree.fromstring(package.read("word/document.xml"))
                sections = root.xpath(".//w:sectPr", namespaces={"w": W})
                facts["section_count"] = len(sections)
                if len(sections) != 2:
                    errors.append("模板分节数不是2")
                media = []
                for name in sorted(names):
                    if re.fullmatch(r"word/media/[^/]+", name):
                        data = package.read(name)
                        if data:
                            media.append(hashlib.sha256(data).hexdigest().upper())
                facts["nonempty_media_count"] = len(media)
                facts["media_sha256"] = media
                if len(media) != 4:
                    errors.append("固定非空媒体数量不是4")
        except Exception as exc:
            errors.append("OOXML读取失败：" + str(exc))
    facts["status"] = "PASS" if not errors else "FAIL"
    facts["stage"] = "verify_fixed_template"
    facts["findings"] = [{"code": "FIXED_TEMPLATE_INVARIANT_FAILED", "message": value} for value in errors]
    facts["errors"] = errors
    if args.report:
        write_json(args.report, facts)
    print(json.dumps(facts, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
