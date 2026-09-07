"""Deterministic, local-only raster image sanitization candidate builder.

This module deliberately does not make an approval decision.  It materializes a
candidate and the evidence needed for a human to review it.  The only
transformation currently implemented is ``edge_median_fill``: each requested
rectangle is filled with the per-channel median of its one-pixel outside ring.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
from html import escape as html_escape
import io
import json
import re
import sys
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from PIL import Image, UnidentifiedImageError


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from schema_validation_v3 import load_schema, validate_instance


SCHEMA_VERSION = "image-sanitization-v3"
QUEUE_SCHEMA_VERSION = "image-sanitization-review-queue-v3"
RECEIPT_SCHEMA_VERSION = "image-sanitization-review-receipt-v3"
REVIEW_UI_VERSION = "image-sanitization-review-1.6.0"
METHOD = "edge_median_fill"
SCRIPT_PATH = Path(__file__).resolve()
SCHEMA_ROOT = SCRIPT_PATH.parents[1] / "references" / "schemas"
REQUEST_SCHEMA_PATH = SCHEMA_ROOT / "image-sanitization-request-v3.schema.json"
REPORT_SCHEMA_PATH = SCHEMA_ROOT / "image-sanitization-report-v3.schema.json"
QUEUE_SCHEMA_PATH = SCHEMA_ROOT / "image-sanitization-review-queue-v3.schema.json"
RECEIPT_SCHEMA_PATH = SCHEMA_ROOT / "image-sanitization-review-receipt-v3.schema.json"
SHA_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
SUPPORTED_MODES = {"L", "LA", "RGB", "RGBA"}

# Keep the review controls in one static fragment.  Region-specific markup is
# assembled separately below; putting the controls in that loop (or appending
# another copy after the template) makes a multi-region review page silently
# render duplicate form controls.
_REVIEW_FORM_HTML = (
    '<section id="review-form"><h2>人工动作</h2>'
    '<p>未选择任何默认动作。请填写复核人信息、选择动作并说明依据后再导出：</p>'
    '<p><label>复核人 ID<br><input id="reviewer-id" autocomplete="off" '
    'style="width:100%;box-sizing:border-box;margin-top:8px;padding:8px"></label></p>'
    '<p><label>复核角色<br><input id="reviewer-role" autocomplete="off" '
    'style="width:100%;box-sizing:border-box;margin-top:8px;padding:8px"></label></p>'
    '<div id="actions"><button type="button" data-action="APPROVE">批准候选</button>'
    '<button type="button" data-action="REJECT">驳回候选</button>'
    '<button type="button" data-action="REQUEST_REVISION">要求修订</button>'
    '<button type="button" data-action="FACT_PENDING">标记事实待确认</button></div>'
    '<p><label>复核意见（必填；说明去除了什么，以及产品事实是否保持不变）<br>'
    '<textarea id="comment" rows="4" style="width:100%;box-sizing:border-box;'
    'margin-top:8px"></textarea></label></p>'
    '<button id="export" type="button">导出哈希绑定 JSON</button><span id="message"></span></section>'
)


def _review_form_html(hidden_reviewer: dict[str, Any] | None) -> str:
    if hidden_reviewer is None:
        return _REVIEW_FORM_HTML
    reviewer_id = str(hidden_reviewer.get("reviewer_id") or "").strip()
    reviewer_role = str(hidden_reviewer.get("reviewer_role") or "").strip()
    if not reviewer_id or not reviewer_role:
        raise SanitizationError("hidden reviewer requires non-empty reviewer_id and reviewer_role")
    return (
        '<section id="review-form"><h2>人工动作</h2>'
        '<p>未选择任何默认动作。请选择动作并说明依据后再导出：</p>'
        '<p>已绑定本地复核身份；身份信息只写入审批回执，不进入业务预览或交付文件。</p>'
        f'<input id="reviewer-id" type="hidden" value="{html_escape(reviewer_id, quote=True)}">'
        f'<input id="reviewer-role" type="hidden" value="{html_escape(reviewer_role, quote=True)}">'
        '<div id="actions"><button type="button" data-action="APPROVE">批准候选</button>'
        '<button type="button" data-action="REJECT">驳回候选</button>'
        '<button type="button" data-action="REQUEST_REVISION">要求修订</button>'
        '<button type="button" data-action="FACT_PENDING">标记事实待确认</button></div>'
        '<p><label>复核意见（必填；说明去除了什么，以及产品事实是否保持不变）<br>'
        '<textarea id="comment" rows="4" style="width:100%;box-sizing:border-box;'
        'margin-top:8px"></textarea></label></p>'
        '<button id="export" type="button">导出哈希绑定 JSON</button><span id="message"></span></section>'
    )


class SanitizationError(ValueError):
    """An input or deterministic invariant failed closed."""


def _normalise_hidden_reviewer(value: dict[str, Any] | None) -> dict[str, str] | None:
    if value is None:
        return None
    reviewer_id = str(value.get("reviewer_id") or "").strip()
    reviewer_role = str(value.get("reviewer_role") or "").strip()
    if not reviewer_id or not reviewer_role:
        raise SanitizationError("hidden reviewer requires non-empty reviewer_id and reviewer_role")
    if any(ord(character) < 32 for character in reviewer_id + reviewer_role):
        raise SanitizationError("hidden reviewer contains control characters")
    return {"reviewer_id": reviewer_id, "reviewer_role": reviewer_role}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SanitizationError(f"{label} must be an integer")
    return value


def _normalise_region(value: Any, index: int) -> dict[str, Any]:
    """Accept the documented object form and a compact [x,y,width,height]."""
    region_id = f"REGION-{index:04d}"
    if isinstance(value, (list, tuple)):
        if len(value) != 4:
            raise SanitizationError(f"region {index} must have four values")
        x, y, width, height = (_integer(item, f"region {index}") for item in value)
    elif isinstance(value, dict):
        if value.get("region_id") is not None or value.get("id") is not None:
            raw_id = value.get("region_id", value.get("id"))
            if not isinstance(raw_id, str) or not raw_id.strip():
                raise SanitizationError(f"region {index} id must be a non-empty string")
            region_id = raw_id.strip()
        if all(key in value for key in ("x", "y", "width", "height")):
            x = _integer(value["x"], f"region {index}.x")
            y = _integer(value["y"], f"region {index}.y")
            width = _integer(value["width"], f"region {index}.width")
            height = _integer(value["height"], f"region {index}.height")
        elif all(key in value for key in ("left", "top", "right", "bottom")):
            left = _integer(value["left"], f"region {index}.left")
            top = _integer(value["top"], f"region {index}.top")
            right = _integer(value["right"], f"region {index}.right")
            bottom = _integer(value["bottom"], f"region {index}.bottom")
            x, y, width, height = left, top, right - left, bottom - top
        elif all(key in value for key in ("x1", "y1", "x2", "y2")):
            x = _integer(value["x1"], f"region {index}.x1")
            y = _integer(value["y1"], f"region {index}.y1")
            right = _integer(value["x2"], f"region {index}.x2")
            bottom = _integer(value["y2"], f"region {index}.y2")
            width, height = right - x, bottom - y
        else:
            raise SanitizationError(f"region {index} must specify x,y,width,height")
    else:
        raise SanitizationError(f"region {index} must be an object or four-value array")
    if width < 1 or height < 1:
        raise SanitizationError(f"region {index} must contain at least one pixel")
    return {"region_id": region_id, "x": x, "y": y, "width": width, "height": height}


def normalise_regions(payload: Any, size: tuple[int, int]) -> tuple[list[dict[str, Any]], str | None]:
    expected_sha: str | None = None
    if isinstance(payload, dict):
        values = payload.get("regions")
        if values is None:
            raise SanitizationError("regions JSON object must contain regions")
        for key in ("source_sha256", "source_sha"):
            if key in payload:
                expected_sha = str(payload[key]).strip().upper()
                if not SHA_RE.fullmatch(expected_sha):
                    raise SanitizationError("source SHA-256 is malformed")
                break
    else:
        values = payload
    if not isinstance(values, list) or not values:
        raise SanitizationError("regions must be a non-empty array")
    width, height = size
    regions = [_normalise_region(value, index) for index, value in enumerate(values, 1)]
    for index, region in enumerate(regions, 1):
        x2 = region["x"] + region["width"]
        y2 = region["y"] + region["height"]
        if region["x"] < 0 or region["y"] < 0 or x2 > width or y2 > height:
            raise SanitizationError(f"region {index} is outside image bounds")
    for index, first in enumerate(regions):
        for second in regions[index + 1 :]:
            if (first["x"] < second["x"] + second["width"] and second["x"] < first["x"] + first["width"] and
                    first["y"] < second["y"] + second["height"] and second["y"] < first["y"] + first["height"]):
                raise SanitizationError("regions must not overlap")
    ids = [region["region_id"] for region in regions]
    if len(ids) != len(set(ids)):
        raise SanitizationError("region IDs must be unique")
    return regions, expected_sha


def _components(pixel: Any, mode: str) -> tuple[Any, ...]:
    if mode in {"1", "L", "P", "I", "I;16"}:
        return (pixel,)
    return tuple(pixel)


def _pixel(components: Iterable[Any], mode: str) -> Any:
    values = tuple(components)
    if mode in {"1", "L", "P", "I", "I;16"}:
        return values[0]
    return values


def _median_pixel(values: list[Any], mode: str) -> Any:
    component_values = list(zip(*(_components(value, mode) for value in values)))
    medians: list[Any] = []
    for values_for_channel in component_values:
        value = median(values_for_channel)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        # Pixel channels must be integral for all supported PNG modes.  For an
        # even sample count, round halves down consistently and transparently.
        if isinstance(value, float):
            value = int(value)
        medians.append(value)
    return _pixel(medians, mode)


def _ring_coordinates(region: dict[str, Any], width: int, height: int) -> list[tuple[int, int]]:
    x0, y0 = region["x"], region["y"]
    x1, y1 = x0 + region["width"], y0 + region["height"]
    coordinates: list[tuple[int, int]] = []
    for y in range(max(0, y0 - 1), min(height, y1 + 1)):
        for x in range(max(0, x0 - 1), min(width, x1 + 1)):
            if not (x0 <= x < x1 and y0 <= y < y1):
                coordinates.append((x, y))
    if not coordinates:
        raise SanitizationError(f"region {region['region_id']} has no outside ring")
    return coordinates


def edge_median_fill(image: Image.Image, regions: list[dict[str, Any]]) -> tuple[Image.Image, Image.Image, list[dict[str, Any]], int, bool]:
    """Return sanitized image, same-mode diff image, region evidence, count."""
    if image.mode not in SUPPORTED_MODES:
        raise SanitizationError(f"unsupported raster mode for PNG output: {image.mode}")
    source = image.copy()
    candidate = image.copy()
    width, height = image.size
    pixels = source.load()
    out = candidate.load()
    changed: set[tuple[int, int]] = set()
    evidence: list[dict[str, Any]] = []
    for region in regions:
        ring = _ring_coordinates(region, width, height)
        fill = _median_pixel([pixels[coordinate] for coordinate in ring], image.mode)
        for y in range(region["y"], region["y"] + region["height"]):
            for x in range(region["x"], region["x"] + region["width"]):
                if out[x, y] != fill:
                    changed.add((x, y))
                out[x, y] = fill
        evidence.append({
            **region,
            "right": region["x"] + region["width"],
            "bottom": region["y"] + region["height"],
            "ring_pixel_count": len(ring),
            "fill_value": list(fill) if isinstance(fill, tuple) else fill,
        })

    # Diff preserves the source canvas and mode.  Brightening changed pixels is
    # intentionally conservative for uncommon modes, while RGB/RGBA get a
    # visible red marker without altering unmodified pixels.
    diff = source.copy()
    diff_pixels = diff.load()
    for x, y in changed:
        old = diff_pixels[x, y]
        if image.mode == "RGB":
            diff_pixels[x, y] = (255, 255, 0)
        elif image.mode == "RGBA":
            diff_pixels[x, y] = (255, 0, 0, 255)
        elif image.mode in {"L", "1", "P", "I", "I;16"}:
            diff_pixels[x, y] = 255
        elif image.mode == "LA":
            diff_pixels[x, y] = (255, old[1])
        elif image.mode == "CMYK":
            diff_pixels[x, y] = (0, 255, 255, 0)
    outside_unchanged = all(
        source.getpixel((x, y)) == candidate.getpixel((x, y))
        for y in range(height) for x in range(width)
        if (x, y) not in changed
    )
    return candidate, diff, evidence, len(changed), outside_unchanged


def _read_regions(path_or_json: Path | str) -> Any:
    raw = str(path_or_json)
    path = Path(raw)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SanitizationError(f"cannot read regions JSON: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SanitizationError("regions-json must be a JSON file path or JSON value") from exc


def _open_raster(path: Path) -> tuple[Image.Image, bytes]:
    if not path.is_file():
        raise SanitizationError(f"source image does not exist: {path}")
    try:
        raw = path.read_bytes()
        with Image.open(io.BytesIO(raw)) as opened:
            if opened.format is None:
                raise SanitizationError("source is not a recognized raster image")
            opened.load()
            image = opened.copy()
    except (OSError, UnidentifiedImageError, SyntaxError) as exc:
        raise SanitizationError(f"source is not a valid raster image: {exc}") from exc
    if not image.size[0] or not image.size[1]:
        raise SanitizationError("source image has an empty canvas")
    return image, raw


def _json_write(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SanitizationError(f"cannot read {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise SanitizationError(f"{label} JSON must be an object")
    return value


def _schema_check(value: dict[str, Any], schema_path: Path, label: str) -> None:
    try:
        schema = load_schema(schema_path)
        errors = validate_instance(value, schema)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise SanitizationError(f"cannot load {label} schema: {exc}") from exc
    if errors:
        first = errors[0]
        raise SanitizationError(f"{label} schema invalid at {first['path']}: {first['message']}")


def _non_blank_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SanitizationError(f"{label} must be non-empty after stripping whitespace")
    return value.strip()


def _review_id(source_id: str, binding: dict[str, Any], regions: list[dict[str, Any]]) -> str:
    return "IMAGE-REVIEW-" + canonical_json_sha256({
        "source_id": source_id,
        "binding": binding,
        "regions": regions,
    })


def _canonical_request_from_queue(queue: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the request from independently bound queue fields.

    The serialized ``request`` is an artifact, not an authority.  Rebuilding
    it prevents a queue tamperer from making the request self-consistent by
    changing only that nested object and its hash.
    """
    source = queue.get("source")
    if not isinstance(source, dict):
        raise SanitizationError("review queue source is missing")
    regions = queue.get("regions")
    if not isinstance(regions, list):
        raise SanitizationError("review queue regions are missing")
    request_regions = [
        {key: region[key] for key in ("region_id", "x", "y", "width", "height")}
        for region in regions
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "source_id": queue.get("source_id"),
        "source_sha256": source.get("sha256"),
        "width": source.get("width"),
        "height": source.get("height"),
        "mode": source.get("mode"),
        "method": METHOD,
        "regions": request_regions,
    }


def _queue_artifact(queue_path: Path, item: dict[str, Any], expected_sha: str, label: str) -> Path:
    relative = item.get("path")
    if not isinstance(relative, str) or not relative or Path(relative).name != relative:
        raise SanitizationError(f"{label} path must be one local filename")
    path = (queue_path.parent / relative).resolve()
    if path.parent != queue_path.parent.resolve() or not path.is_file():
        raise SanitizationError(f"{label} artifact is missing or escapes the review directory")
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha:
        raise SanitizationError(f"{label} SHA-256 mismatch")
    image, _ = _open_raster(path)
    if image.width != item["width"] or image.height != item["height"] or image.mode != item["mode"]:
        raise SanitizationError(f"{label} dimensions or mode changed")
    return path


def _png_bytes(image: Image.Image) -> bytes:
    """Serialize a candidate exactly as ``build`` does, without touching disk."""
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def validate_receipt(queue_path: Path, receipt_path: Path) -> dict[str, Any]:
    queue_path = queue_path.resolve()
    receipt_path = receipt_path.resolve()
    queue = _json_object(queue_path, "review queue")
    receipt = _json_object(receipt_path, "review receipt")
    _schema_check(queue, QUEUE_SCHEMA_PATH, "review queue")
    _schema_check(receipt, RECEIPT_SCHEMA_PATH, "review receipt")
    binding = queue["binding"]
    request = queue["request"]
    _schema_check(request, REQUEST_SCHEMA_PATH, "review queue request")
    canonical_request = _canonical_request_from_queue(queue)
    _schema_check(canonical_request, REQUEST_SCHEMA_PATH, "canonical review queue request")
    if request != canonical_request:
        raise SanitizationError("review queue request is not canonical")
    request_sha = canonical_json_sha256(canonical_request)
    if request_sha != binding["request_sha256"]:
        raise SanitizationError("review queue request SHA-256 mismatch")
    if request["source_id"] != queue["source_id"]:
        raise SanitizationError("review queue request source ID is stale")
    if request["source_sha256"] != binding["source_sha256"]:
        raise SanitizationError("review queue request source SHA-256 is stale")
    source = queue["source"]
    if source["sha256"] != binding["source_sha256"]:
        raise SanitizationError("review queue source SHA-256 is stale")
    if not isinstance(source["width"], int) or not isinstance(source["height"], int):
        raise SanitizationError("review queue source dimensions are invalid")
    request_metadata = (request["width"], request["height"], request["mode"])
    source_metadata = (source["width"], source["height"], source["mode"])
    if request_metadata != source_metadata:
        raise SanitizationError("review queue request dimensions or mode are stale")
    if request["method"] != METHOD:
        raise SanitizationError("review queue request method is stale")
    request_regions = [
        {key: region[key] for key in ("region_id", "x", "y", "width", "height")}
        for region in queue["regions"]
    ]
    if request["regions"] != request_regions:
        raise SanitizationError("review queue request regions are stale")
    if receipt["review_id"] != queue["review_id"]:
        raise SanitizationError("review receipt ID does not match the queue")
    if receipt["binding"] != queue["binding"]:
        raise SanitizationError("review receipt binding is stale")
    expected_regions = [region["region_id"] for region in queue["regions"]]
    selected_regions = receipt["scope"]["regions"]
    selected_region_set = set(selected_regions)
    expected_region_set = set(expected_regions)
    selected_images = receipt["selected_images"]
    selected_image_region_set = {item["region_id"] for item in selected_images}
    candidate_region_set = {
        item["region_id"] for item in selected_images if item["image_role"] == "candidate"
    }
    if (
        receipt["scope"]["source_id"] != queue["source_id"]
        or not selected_region_set
        or not selected_region_set.issubset(expected_region_set)
        or selected_image_region_set != selected_region_set
    ):
        raise SanitizationError("review receipt scope does not match the queue")
    if queue["review_id"] != _review_id(queue["source_id"], binding, queue["regions"]):
        raise SanitizationError("review queue ID does not match its canonical contents")
    for field in ("reviewer_id", "reviewer_role", "comment"):
        _non_blank_text(receipt[field], f"review receipt {field}")
    try:
        reviewed_at_value = receipt["reviewed_at"]
        if "T" not in reviewed_at_value:
            raise ValueError("date-time must contain a date/time separator")
        reviewed_at = datetime.fromisoformat(reviewed_at_value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise SanitizationError("reviewed_at is not an ISO-8601 date-time") from exc
    if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        raise SanitizationError("reviewed_at must include a timezone")
    if binding["script_sha256"] != sha256_file(SCRIPT_PATH):
        raise SanitizationError("image sanitization script changed after review generation")
    if binding["receipt_schema_sha256"] != sha256_file(RECEIPT_SCHEMA_PATH):
        raise SanitizationError("image sanitization receipt schema changed after review generation")
    if binding["review_ui_version"] != REVIEW_UI_VERSION:
        raise SanitizationError("image sanitization review UI version changed")
    review_path = queue_path.parent / "review.html"
    try:
        actual_review_html = review_path.read_bytes()
    except OSError as exc:
        raise SanitizationError(f"cannot read review HTML: {exc}") from exc
    hidden_reviewer = queue.get("hidden_reviewer")
    if hidden_reviewer is not None:
        normalised_hidden_reviewer = _normalise_hidden_reviewer(hidden_reviewer)
        if binding.get("hidden_reviewer_sha256") != canonical_json_sha256(normalised_hidden_reviewer):
            raise SanitizationError("review queue hidden reviewer binding is stale")
    elif "hidden_reviewer_sha256" in binding:
        raise SanitizationError("review queue hidden reviewer binding is unexpected")
    expected_review_html = _review_html(queue).encode("utf-8")
    if actual_review_html != expected_review_html:
        raise SanitizationError("review HTML is missing or does not match the deterministic queue rendering")
    for label in ("source_preview", "candidate", "diff"):
        item = queue[label]
        if (item["width"], item["height"], item["mode"]) != (source["width"], source["height"], source["mode"]):
            raise SanitizationError(f"review queue {label} dimensions or mode do not match source")
    for label, binding_key in (("source_preview", "source_preview_sha256"), ("candidate", "candidate_sha256"), ("diff", "diff_sha256")):
        if queue[label]["sha256"] != binding[binding_key]:
            raise SanitizationError(f"review queue {label} SHA-256 is stale")
    source_asset = queue.get("source_asset")
    if not isinstance(source_asset, dict):
        raise SanitizationError("review queue is missing a verifiable source asset")
    if source_asset.get("sha256") != binding["source_sha256"]:
        raise SanitizationError("review queue source asset SHA-256 is stale")
    source_asset_path = _queue_artifact(queue_path, source_asset, binding["source_sha256"], "source asset")
    source_image, source_bytes = _open_raster(source_asset_path)
    if sha256_bytes(source_bytes) != binding["source_sha256"]:
        raise SanitizationError("review queue source asset SHA-256 is stale")
    if (source_image.width, source_image.height, source_image.mode) != source_metadata:
        raise SanitizationError("review queue source asset dimensions or mode changed")
    source_preview_path = _queue_artifact(queue_path, queue["source_preview"], binding["source_preview_sha256"], "source preview")
    source_preview_image, _ = _open_raster(source_preview_path)
    if source_preview_image.tobytes() != source_image.tobytes():
        raise SanitizationError("review queue source preview does not match the source asset")
    candidate_path = _queue_artifact(queue_path, queue["candidate"], binding["candidate_sha256"], "candidate")
    diff_path = _queue_artifact(queue_path, queue["diff"], binding["diff_sha256"], "diff")
    request_regions = [{key: region[key] for key in ("region_id", "x", "y", "width", "height")} for region in queue["regions"]]
    expected_candidate, expected_diff, expected_evidence, _, _ = edge_median_fill(source_image, request_regions)
    if expected_evidence != queue["regions"]:
        raise SanitizationError("review queue region evidence is stale")
    actual_candidate, _ = _open_raster(candidate_path)
    if actual_candidate.tobytes() != expected_candidate.tobytes():
        raise SanitizationError("review queue candidate does not match the canonical request")
    actual_diff, _ = _open_raster(diff_path)
    if actual_diff.tobytes() != expected_diff.tobytes():
        raise SanitizationError("review queue diff does not match the canonical request")
    if (
        receipt["action"] != "APPROVE"
        or selected_region_set != expected_region_set
        or candidate_region_set != expected_region_set
    ):
        return {
            "status": "HUMAN_REVIEW",
            "stage": "image_sanitization_receipt_v3",
            "review_id": queue["review_id"],
            "action": receipt["action"],
            "selected_regions": selected_regions,
            "selected_images": selected_images,
            "deliverable": False,
        }
    return {
        "status": "PASS",
        "stage": "image_sanitization_receipt_v3",
        "review_id": queue["review_id"],
        "action": "APPROVE",
        "deliverable": False,
        "replacement_ref": {
            "path": str(candidate_path),
            "sha256": binding["candidate_sha256"],
            "review_note": receipt["comment"],
        },
    }


def _review_html(
    queue: dict[str, Any],
    hidden_reviewer: dict[str, Any] | None = None,
) -> str:
    data = json.dumps(queue, ensure_ascii=False, sort_keys=True).replace("<", "\\u003c")
    effective_hidden_reviewer = queue.get("hidden_reviewer") if hidden_reviewer is None else hidden_reviewer
    form_html = _review_form_html(effective_hidden_reviewer)
    source = queue["source"]
    viewbox_images = (
        ("source_preview", "源图", "源图"),
        ("candidate", "脱敏候选", "脱敏候选"),
        ("diff", "差异图", "差异图（红色标记变更像素）"),
    )
    zoom_regions: list[str] = []
    for region in queue["regions"]:
        region_id = html_escape(str(region["region_id"]), quote=True)
        x, y = int(region["x"]), int(region["y"])
        width, height = int(region["width"]), int(region["height"])
        view_box = f"{x} {y} {width} {height}"
        # A four-pixel vector scale keeps a 270x50 request region legible while
        # retaining the original PNGs as the only image sources.  The frame is
        # horizontally scrollable for unusually wide requests.
        zoom_width, zoom_height = width * 4, height * 4
        panels: list[str] = []
        for key, label, alt in viewbox_images:
            item = queue[key]
            path = html_escape(str(item["path"]), quote=True)
            panel_body = (
                f'<figcaption>{label}</figcaption>'
                f'<div class="zoom-frame"><svg class="zoom-svg" role="img" aria-label="{html_escape(alt, quote=True)}，{region_id}" '
                f'viewBox="{view_box}" width="{zoom_width}" height="{zoom_height}" preserveAspectRatio="none">'
                f'<image href="{path}" x="0" y="0" width="{source["width"]}" height="{source["height"]}" '
                f'preserveAspectRatio="none"></image></svg></div>'
            )
            role = "source_preview" if key == "source_preview" else key
            safe_label = html_escape(label, quote=True)
            panels.append(
                f'<figure class="zoom-panel" data-region-id="{region_id}" data-image-role="{role}" '
                f'data-image-label="{safe_label}" data-image-selectable="true" '
                f'tabindex="0" role="button" aria-pressed="false">{panel_body}'
                f'<p class="image-controls"><span class="select-state">未选中</span><span>单击选中/取消，双击添加注释。</span></p>'
                f'<label class="image-note-label">图片注释<br><textarea class="image-note" '
                f'data-region-id="{region_id}" data-image-role="{role}" data-image-label="{safe_label}" '
                f'rows="3" placeholder="双击这张{label}后在这里填写注释；会并入 receipt.comment"></textarea></label></figure>'
            )
        zoom_regions.append(
            f'<article class="zoom-region" data-region-id="{region_id}">'
            f'<h3>{region_id}：变更区域放大对照</h3>'
            f'<p class="zoom-meta">左上（{x},{y}），{width}×{height} 像素；以下视图使用同一源图、脱敏候选和差异图裁取该区域并放大 4 倍。</p>'
            f'<div class="zoom-gallery">{"".join(panels)}</div></article>'
        )
    zoom_markup = "".join(zoom_regions)
    html = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>图片局部脱敏人工复核</title>
 <style>
 body{{margin:0;background:#f3f6f8;color:#263746;font:16px system-ui,"Microsoft YaHei",sans-serif}}header{{padding:22px 6%;background:#166b5c;color:white}}main{{max-width:1100px;margin:24px auto;padding:0 20px 48px}}section{{background:white;border:1px solid #ccd8df;border-radius:10px;padding:18px;margin:16px 0}}.gallery{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}}figure{{margin:0;border:1px solid #dde5e9;padding:10px;border-radius:8px}}img{{display:block;width:100%;max-height:620px;object-fit:contain;background:#fafafa}}figcaption{{font-size:13px;color:#5d6d78;margin-top:8px}}.zoom-region{{margin-top:18px;padding:14px;border:2px solid #e0a832;border-radius:9px;background:#fffdf5}}.zoom-region h3{{margin:0;color:#8c5410}}.zoom-meta{{margin:8px 0 14px;color:#5d6d78;font-size:14px}}.select-state{{display:inline-block;min-width:64px;padding:4px 8px;border-radius:6px;background:#e7edf1;color:#263746;font-weight:700;text-align:center}}.zoom-gallery{{display:grid;grid-template-columns:repeat(3,minmax(260px,1fr));gap:12px}}.zoom-panel{{border-color:#e0c98e;background:white;min-width:0}}.zoom-panel[data-image-selectable="true"]{{cursor:pointer}}.zoom-panel.selected{{border-color:#166b5c;background:#f0faf6;box-shadow:0 0 0 3px rgba(22,107,92,.16)}}.zoom-panel:focus{{outline:3px solid #89b6d4;outline-offset:2px}}.zoom-panel.selected .select-state{{background:#166b5c;color:white}}.zoom-panel figcaption{{font-weight:700;color:#263746}}.zoom-frame{{overflow-x:auto;background:#f7f9fa;border:1px solid #ccd8df}}.zoom-svg{{display:block;width:auto;min-width:100%;max-width:none;height:auto;image-rendering:auto}}.image-controls{{display:flex;gap:12px;align-items:center;margin:10px 0 0;color:#5d6d78;font-size:14px}}.image-note-label{{display:block;margin-top:12px;color:#263746;font-weight:700}}.image-note{{display:block;width:100%;box-sizing:border-box;margin-top:8px;padding:8px;border:1px solid #aebdc6;border-radius:6px;font:15px system-ui,"Microsoft YaHei",sans-serif;background:white}}button{{margin:6px 8px 6px 0;padding:10px 15px;border:0;border-radius:7px;background:#166b5c;color:white;font-weight:700;cursor:pointer}}button.selected{{outline:3px solid #efb44d}}#export{{background:#345b83}}#message{{color:#a44940;margin-left:8px}}.warning{{border-left:5px solid #db9a28;line-height:1.7}}@media(max-width:900px){{.zoom-gallery{{grid-template-columns:1fr}}}}
 </style></head><body><header><h1>图片局部脱敏人工复核</h1><div id="meta"></div></header><main>
 <section class="warning"><strong>候选结果，不是批准结果。</strong> 请检查原图、候选图和差异图。四个动作都不会直接修改正式 decision；只有点击导出后才会下载一份与哈希绑定的 JSON。</section>
 <section><h2>图像对照</h2><div class="gallery"><figure><img id="source" alt="源图"><figcaption>源图</figcaption></figure><figure><img src="sanitized.png" alt="脱敏候选"><figcaption>脱敏候选</figcaption></figure><figure><img src="diff.png" alt="差异图"><figcaption>差异图（红色标记变更像素）</figcaption></figure></div></section>
 <section><h2>处理区域</h2><div id="regions"></div>{zoom_markup}</section>
 {form_html}
 </main><script id="queue" type="application/json">{data}</script><script>
'use strict';
const queue=JSON.parse(document.getElementById('queue').textContent);let selectedAction='';
document.getElementById('meta').textContent='图片脱敏候选 · '+queue.review_id;
document.getElementById('source').src=queue.source_preview.path;
document.getElementById('regions').textContent=queue.regions.map(r=>`${{r.region_id}}：左上（${{r.x}},${{r.y}}），${{r.width}}×${{r.height}} 像素，填充值 ${{JSON.stringify(r.fill_value)}}`).join('；');
document.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',()=>{{selectedAction=button.dataset.action;document.querySelectorAll('[data-action]').forEach(item=>item.classList.toggle('selected',item===button));}}));
const selectedImagePanels=()=>Array.from(document.querySelectorAll('.zoom-panel.selected[data-image-selectable="true"]'));
const selectedRegionIds=selectedPanels=>Array.from(new Set(selectedPanels.map(panel=>panel.dataset.regionId)));
const selectedImages=selectedPanels=>selectedPanels.map(panel=>({{region_id:panel.dataset.regionId,image_role:panel.dataset.imageRole,label:panel.dataset.imageLabel}}));
function setImageSelected(panel,selected){{panel.classList.toggle('selected',selected);panel.setAttribute('aria-pressed',selected?'true':'false');const state=panel.querySelector('.select-state');if(state){{state.textContent=selected?'已选中':'未选中';}}}}
function toggleImageSelection(panel){{setImageSelected(panel,!panel.classList.contains('selected'));}}
function focusImageNote(panel){{setImageSelected(panel,true);const note=panel.querySelector('.image-note');if(note){{note.focus();}}}}
function collectImageNotes(){{return Array.from(document.querySelectorAll('.image-note')).map(note=>({{region_id:note.dataset.regionId,image_label:note.dataset.imageLabel,comment:note.value.trim()}})).filter(item=>item.comment);}}
function buildComment(comment, imageNotes){{const parts=[];if(comment){{parts.push('[复核意见] '+comment);}}if(imageNotes.length){{parts.push('[图片注释]\\n'+imageNotes.map(item=>`- ${{item.region_id}} / ${{item.image_label}}：${{item.comment}}`).join('\\n'));}}return parts.join('\\n\\n');}}
document.querySelectorAll('.zoom-panel[data-image-selectable="true"]').forEach(panel=>panel.addEventListener('click',event=>{{if(event.target.closest('textarea')){{return;}}const clickCount = event.detail;if(clickCount === 1){{toggleImageSelection(panel);}}if(clickCount === 2){{focusImageNote(panel);}}}}));
document.querySelectorAll('.zoom-panel[data-image-selectable="true"]').forEach(panel=>panel.addEventListener('keydown',event=>{{if(event.key==='Enter'||event.key===' '){{event.preventDefault();toggleImageSelection(panel);}}}}));
document.getElementById('export').addEventListener('click',()=>{{const reviewerId=document.getElementById('reviewer-id').value.trim();const reviewerRole=document.getElementById('reviewer-role').value.trim();const comment=document.getElementById('comment').value.trim();const imageNotes=collectImageNotes();const finalComment=buildComment(comment, imageNotes);const selectedPanels=selectedImagePanels();if(!reviewerId||!reviewerRole||!selectedAction||!finalComment){{document.getElementById('message').textContent='请填写复核人 ID、角色、动作和复核意见或图片注释。';return;}}if(selectedPanels.length<1){{document.getElementById('message').textContent='请至少单击选中一张对照图片。';return;}}const output={{schema_version:'image-sanitization-review-receipt-v3',review_id:queue.review_id,reviewer_id:reviewerId,reviewer_role:reviewerRole,reviewed_at:new Date().toISOString(),action:selectedAction,comment:buildComment(comment, imageNotes),selected_images:selectedImages(selectedPanels),scope:{{source_id:queue.source_id,regions:selectedRegionIds(selectedPanels)}},binding:queue.binding}};const blob=new Blob([JSON.stringify(output,null,2)],{{type:'application/json'}});const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download=queue.review_id+'-review.json';link.click();URL.revokeObjectURL(link.href);document.getElementById('message').textContent='已导出；正式 decision 未被修改。';}});
 </script></body></html>'''
    _validate_review_html_structure(html, form_html)
    return html


def _validate_review_html_structure(html: str, form_html: str = _REVIEW_FORM_HTML) -> None:
    """Fail closed if deterministic composition ever reintroduces controls."""
    # Only inspect the static document body.  Queue values are serialized into
    # a later JSON script and must not affect structural counts if a legitimate
    # source identifier happens to contain one of these marker strings.
    static_body = html.split('<script id="queue"', 1)[0]
    if static_body.count(form_html) != 1:
        raise SanitizationError("review HTML must contain one review form fragment")
    runtime_script = html.rsplit("<script>", 1)[-1]
    if runtime_script.count("let selectedAction='';") != 1:
        raise SanitizationError("review HTML must initialize one unselected action state")
    exact_once = (
        'id="reviewer-id"',
        'id="reviewer-role"',
        '<textarea id="comment"',
        '<button id="export"',
        '<section id="review-form">',
    )
    for marker in exact_once:
        if static_body.count(marker) != 1:
            raise SanitizationError(f"review HTML structure marker must occur once: {marker}")
    action_markers = (
        'data-action="APPROVE"',
        'data-action="REJECT"',
        'data-action="REQUEST_REVISION"',
        'data-action="FACT_PENDING"',
    )
    if static_body.count('data-action=') != 4 or any(static_body.count(marker) != 1 for marker in action_markers):
        raise SanitizationError("review HTML must contain exactly four unique action buttons")
    if 'class="selected"' in form_html:
        raise SanitizationError("review HTML must not select an action by default")


def build(
    source_image: Path,
    source_id: str,
    regions_json: Path | str,
    output_dir: Path,
    hidden_reviewer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_image = source_image.resolve()
    output_dir = output_dir.resolve()
    source_id = source_id.strip()
    hidden_reviewer = _normalise_hidden_reviewer(hidden_reviewer)
    if not source_id:
        raise SanitizationError("source-id must be non-empty")
    for schema_path in (REQUEST_SCHEMA_PATH, REPORT_SCHEMA_PATH, QUEUE_SCHEMA_PATH, RECEIPT_SCHEMA_PATH):
        if not schema_path.is_file():
            raise SanitizationError(f"image sanitization schema is missing: {schema_path}")
    if output_dir.exists() and not output_dir.is_dir():
        raise SanitizationError("output directory path is not a directory")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SanitizationError("output directory must be empty")
    image, source_bytes = _open_raster(source_image)
    source_sha = sha256_bytes(source_bytes)
    regions_payload = _read_regions(regions_json)
    regions, expected_sha = normalise_regions(regions_payload, image.size)
    if expected_sha and expected_sha != source_sha:
        raise SanitizationError(f"source SHA-256 mismatch: expected={expected_sha} actual={source_sha}")
    request = {"schema_version": SCHEMA_VERSION, "source_id": source_id, "source_sha256": source_sha,
               "width": image.width, "height": image.height, "mode": image.mode, "method": METHOD,
               "regions": regions}
    _schema_check(request, REQUEST_SCHEMA_PATH, "sanitization request")
    request_sha = canonical_json_sha256(request)
    candidate, diff, region_evidence, changed_count, outside_unchanged = edge_median_fill(image, regions)
    if changed_count < 1:
        raise SanitizationError("sanitization request changed no pixels")
    if not outside_unchanged:
        raise SanitizationError("pixels outside the approved regions changed")
    output_dir.mkdir(parents=True, exist_ok=True)
    source_asset_path = output_dir / "source-original.bin"
    source_preview_path = output_dir / "source.png"
    sanitized_path, diff_path = output_dir / "sanitized.png", output_dir / "diff.png"
    try:
        # Keep the exact source bytes beside the review queue.  A re-encoded
        # preview is useful for the UI, but cannot prove the original
        # source_sha256 (PNG metadata and compression may legitimately differ).
        source_asset_path.write_bytes(source_bytes)
        image.save(source_preview_path, format="PNG")
        candidate.save(sanitized_path, format="PNG")
        diff.save(diff_path, format="PNG")
    except (OSError, ValueError) as exc:
        raise SanitizationError(f"cannot write PNG candidate: {exc}") from exc
    source_preview_sha = sha256_file(source_preview_path)
    candidate_sha, diff_sha = sha256_file(sanitized_path), sha256_file(diff_path)
    binding = {"script_sha256": sha256_file(SCRIPT_PATH), "request_sha256": request_sha,
               "receipt_schema_sha256": sha256_file(RECEIPT_SCHEMA_PATH),
               "source_sha256": source_sha, "source_preview_sha256": source_preview_sha,
               "candidate_sha256": candidate_sha, "diff_sha256": diff_sha,
               "review_ui_version": REVIEW_UI_VERSION}
    if hidden_reviewer is not None:
        binding["hidden_reviewer_sha256"] = canonical_json_sha256(hidden_reviewer)
    report = {"schema_version": SCHEMA_VERSION, "source_id": source_id, "method": METHOD, "request": request,
              "source": {"sha256": source_sha, "width": image.width, "height": image.height, "mode": image.mode},
              "source_preview": {"sha256": source_preview_sha, "width": image.width, "height": image.height, "mode": image.mode, "path": "source.png"},
              "candidate": {"sha256": candidate_sha, "width": candidate.width, "height": candidate.height, "mode": candidate.mode, "path": "sanitized.png"},
              "diff": {"sha256": diff_sha, "width": diff.width, "height": diff.height, "mode": diff.mode, "path": "diff.png"},
              "request_sha256": request_sha, "binding": binding, "regions": region_evidence,
              "changed_pixel_count": changed_count, "outside_regions_unchanged": outside_unchanged}
    if hidden_reviewer is not None:
        report["hidden_reviewer"] = hidden_reviewer
    _schema_check(report, REPORT_SCHEMA_PATH, "sanitization report")
    report_path = output_dir / "report.json"
    _json_write(report_path, report)
    review_id = _review_id(source_id, binding, region_evidence)
    queue = {"schema_version": QUEUE_SCHEMA_VERSION, "review_id": review_id, "status": "REVIEW_REQUIRED",
             "review_type": "image_sanitization", "source_id": source_id,
             "request": request, "binding": binding, "source": report["source"],
             "source_asset": {"sha256": source_sha, "width": image.width, "height": image.height, "mode": image.mode, "path": source_asset_path.name},
             "source_preview": report["source_preview"], "candidate": report["candidate"], "diff": report["diff"],
             "regions": region_evidence, "actions": ["APPROVE", "REJECT", "REQUEST_REVISION", "FACT_PENDING"],
             "instruction": "人工选择动作；导出的 JSON 仅为哈希绑定复核意见，不修改正式 decision。"}
    if hidden_reviewer is not None:
        queue["hidden_reviewer"] = hidden_reviewer
    _schema_check(queue, QUEUE_SCHEMA_PATH, "review queue")
    _json_write(output_dir / "review-queue.json", queue)
    # Write bytes so the deterministic LF-only rendering is identical on
    # Windows and POSIX; validate-receipt compares this exact representation.
    (output_dir / "review.html").write_bytes(_review_html(queue).encode("utf-8"))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建确定性位图局部脱敏候选")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--source-image", type=Path, required=True)
    build_parser.add_argument("--source-id", required=True)
    build_parser.add_argument("--regions-json", required=True)
    build_parser.add_argument("--output-dir", type=Path, required=True)
    build_parser.add_argument("--hidden-reviewer-id")
    build_parser.add_argument("--hidden-reviewer-role")
    validate_parser = subparsers.add_parser("validate-receipt")
    validate_parser.add_argument("--queue", type=Path, required=True)
    validate_parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            hidden_values = (args.hidden_reviewer_id, args.hidden_reviewer_role)
            if any(value is not None for value in hidden_values) and not all(hidden_values):
                raise SanitizationError("--hidden-reviewer-id and --hidden-reviewer-role must be supplied together")
            hidden_reviewer = (
                {"reviewer_id": args.hidden_reviewer_id, "reviewer_role": args.hidden_reviewer_role}
                if all(hidden_values)
                else None
            )
            report = build(args.source_image, args.source_id, args.regions_json, args.output_dir, hidden_reviewer=hidden_reviewer)
            print(json.dumps({"status": "HUMAN_REVIEW", "stage": "image_sanitization_v3", "report": report}, ensure_ascii=False, indent=2))
            return 3
        result = validate_receipt(args.queue, args.receipt)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 3
    except (SanitizationError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "stage": "image_sanitization_v3", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
