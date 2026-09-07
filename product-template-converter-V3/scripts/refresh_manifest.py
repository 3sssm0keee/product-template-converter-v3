from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


PUBLISH_ROOTS = {"README.md", "agents", "assets", "references", "scripts", "template_packs"}
EXCLUDED_DIRECTORY_NAMES = {".codegraph", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}
EXCLUDED_FILE_SUFFIXES = {".pyc", ".pyd", ".pyo"}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def is_publishable(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if not path.is_file() or relative == Path("manifest.json"):
        return False
    if relative.parts[0] not in PUBLISH_ROOTS:
        return False
    if relative.parts[0] == "agents" and relative.parts[1:2] != ("prompts",):
        return False
    if any(part in EXCLUDED_DIRECTORY_NAMES for part in relative.parts[:-1]):
        return False
    return path.suffix.lower() not in EXCLUDED_FILE_SUFFIXES


def collect_manifest_entries(root: Path) -> list[dict[str, object]]:
    entries = []
    for path in sorted(root.rglob("*")):
        if not is_publishable(path, root):
            continue
        entries.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size, "sha256": digest(path)})
    return entries


def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新独立项目 manifest.json 的文件清单")
    parser.add_argument("project_dir", type=Path)
    args = parser.parse_args()
    root = args.project_dir.resolve()
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {
        "project": root.name,
        "generated_at": "",
        "template_sha256": "",
        "files": [],
    }
    template = root / "assets" / "ZXTY-XX_产品全称-产品介绍-模板.docx"
    manifest["project"] = manifest.pop("skill", manifest.get("project", root.name))
    manifest["generated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    manifest["template_sha256"] = digest(template) if template.is_file() else ""
    manifest["files"] = collect_manifest_entries(root)
    write_manifest(manifest_path, manifest)
    print(json.dumps({"status": "PASS", "manifest": str(manifest_path), "file_count": len(manifest["files"])}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
