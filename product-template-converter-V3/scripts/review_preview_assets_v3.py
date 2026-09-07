from __future__ import annotations

import hashlib
import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from decision_bundle_v3 import _source_items
from pipeline_common import sha256_file


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _preview_blocks(preview: dict[str, Any]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for page in preview.get("pages") or []:
        if not isinstance(page, dict):
            continue
        for section in page.get("sections") or []:
            if not isinstance(section, dict):
                continue
            blocks.extend(block for block in section.get("blocks") or [] if isinstance(block, dict))
    return blocks


def _safe_asset_name(source_id: str, name: str, package_path: str) -> str:
    suffix = Path(name).suffix.lower() or PurePosixPath(package_path).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        suffix = ".png"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).stem or source_id).strip("._-") or "image"
    safe_source_id = re.sub(r"[^A-Za-z0-9._-]+", "_", source_id).strip("._-") or "IMAGE"
    return f"{safe_source_id}-{base}{suffix}"


def _safe_package_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix.lower() not in IMAGE_EXTENSIONS
    ):
        raise ValueError(f"unsafe image package path: {value}")
    return normalized


def _try_safe_package_path(value: str, names: set[str]) -> str | None:
    try:
        package_path = _safe_package_path(value)
    except ValueError:
        return None
    return package_path if package_path in names else None


def _pptx_relationship_target(archive: zipfile.ZipFile, names: set[str], slide: int, relationship_id: str) -> str | None:
    rels_path = f"ppt/slides/_rels/slide{slide}.xml.rels"
    if rels_path not in names:
        return None
    root = ET.fromstring(archive.read(rels_path))
    for relationship in root:
        if relationship.attrib.get("Id") != relationship_id:
            continue
        if relationship.attrib.get("TargetMode") == "External":
            return None
        target = relationship.attrib.get("Target") or ""
        package_path = posixpath.normpath(posixpath.join("ppt/slides", target))
        if package_path.startswith("../") or package_path.startswith("/"):
            return None
        return package_path if package_path in names else None
    return None


def _resolve_image_package_path(
    archive: zipfile.ZipFile,
    names: set[str],
    image: dict[str, Any],
    source_by_id: dict[str, dict[str, Any]],
) -> str:
    source_id = str(image.get("source_id") or "")
    source_item = source_by_id.get(source_id, {})
    payload = source_item.get("payload") if isinstance(source_item.get("payload"), dict) else {}
    for value in (
        image.get("package_path"),
        payload.get("package_path"),
        payload.get("path"),
        source_item.get("location"),
    ):
        package_path = _try_safe_package_path(str(value or ""), names)
        if package_path:
            return package_path

    location = str(source_item.get("location") or image.get("package_path") or "")
    context = source_item.get("context") if isinstance(source_item.get("context"), dict) else {}
    match = re.search(r"slide:(\d+)/media:(rId\d+)", location)
    slide = int(match.group(1)) if match else int(context.get("slide") or 0)
    relationship_id = match.group(2) if match else str(context.get("relationship_id") or "")
    if slide > 0 and relationship_id:
        package_path = _pptx_relationship_target(archive, names, slide, relationship_id)
        if package_path:
            return package_path

    expected_sha = str(image.get("sha256") or payload.get("sha256") or "").strip().upper()
    if expected_sha:
        for name in sorted(names):
            if PurePosixPath(name).suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            if hashlib.sha256(archive.read(name)).hexdigest().upper() == expected_sha:
                return name
    raise ValueError(f"content preview image missing from normalized source: {image.get('package_path')}")


def _pdf_image_data(pdf_path: Path, location: str) -> bytes:
    match = re.fullmatch(r"page:(\d+)/image:(\d+)", location)
    if not match:
        raise ValueError(f"unsupported PDF preview image location: {location}")
    page_index = int(match.group(1)) - 1
    image_index = int(match.group(2)) - 1
    from pypdf import PdfReader

    pages = PdfReader(pdf_path).pages
    if page_index < 0 or page_index >= len(pages):
        raise ValueError(f"PDF preview image page out of range: {location}")
    images = list(pages[page_index].images)
    if image_index < 0 or image_index >= len(images):
        raise ValueError(f"PDF preview image index out of range: {location}")
    return images[image_index].data


def _png_bytes(data: bytes) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


def _materialize_pdf_preview_images(
    normalized_path: Path,
    image_blocks: list[dict[str, Any]],
    output_dir: Path,
) -> None:
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    for block in image_blocks:
        image = block["image"]
        raw = _pdf_image_data(normalized_path, str(image.get("package_path") or ""))
        expected_sha = str(image.get("sha256") or "").strip().upper()
        if expected_sha and hashlib.sha256(raw).hexdigest().upper() != expected_sha:
            raise ValueError(f"content preview PDF image sha256 mismatch: {image.get('package_path')}")
        asset_name = _safe_asset_name(
            str(image.get("source_id") or ""),
            f"{Path(str(image.get('name') or 'image')).stem}.png",
            "image.png",
        )
        target = assets_dir / asset_name
        target.write_bytes(_png_bytes(raw))
        image["path"] = f"assets/{asset_name}"
        image["materialized_sha256"] = sha256_file(target)


def materialize_preview_images(source_document: dict[str, Any], preview: dict[str, Any], output_dir: Path) -> None:
    normalized_path = Path(str(source_document.get("normalized", {}).get("path") or ""))
    image_blocks = [
        block
        for block in _preview_blocks(preview)
        if block.get("kind") == "image" and isinstance(block.get("image"), dict)
    ]
    if not image_blocks:
        return
    if not normalized_path.is_file():
        raise ValueError("content preview image requires a readable normalized source package")
    if normalized_path.suffix.lower() == ".pdf":
        _materialize_pdf_preview_images(normalized_path, image_blocks, output_dir)
        return

    assets_dir = output_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    source_by_id = {str(item.get("source_id") or item.get("id") or ""): item for item in _source_items(source_document)}
    with zipfile.ZipFile(normalized_path) as archive:
        names = set(archive.namelist())
        for block in image_blocks:
            image = block["image"]
            package_path = _resolve_image_package_path(archive, names, image, source_by_id)
            asset_name = _safe_asset_name(
                str(image.get("source_id") or ""),
                str(image.get("name") or ""),
                package_path,
            )
            target = assets_dir / asset_name
            target.write_bytes(archive.read(package_path))
            actual_sha = sha256_file(target)
            expected_sha = str(image.get("sha256") or "").strip().upper()
            if expected_sha and actual_sha != expected_sha:
                raise ValueError(f"content preview image sha256 mismatch: {package_path}")
            image["path"] = f"assets/{asset_name}"
            image["materialized_sha256"] = actual_sha
