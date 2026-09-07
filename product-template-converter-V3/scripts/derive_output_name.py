from __future__ import annotations

import argparse
import re
import sys


INVALID = re.compile(r'[\\/:*?"<>|]')


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", INVALID.sub("-", value.strip())).rstrip(". ")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成固定产品介绍模板成品名")
    parser.add_argument("--model", default="")
    parser.add_argument("--full-name", default="")
    parser.add_argument("--draft-if-missing", action="store_true")
    args = parser.parse_args()
    model = clean(args.model)
    full_name = clean(args.full_name)
    missing = [name for name, value in (("产品型号", model), ("产品全称", full_name)) if not value]
    if missing and not args.draft_if_missing:
        print("缺少：" + "、".join(missing) + "；不能生成最终文件名。", file=sys.stderr)
        return 2
    model = model or "待核实型号"
    full_name = full_name or "待核实产品全称"
    suffix = "-草稿" if missing else ""
    print(f"{model}_{full_name}-产品介绍{suffix}.docx")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
