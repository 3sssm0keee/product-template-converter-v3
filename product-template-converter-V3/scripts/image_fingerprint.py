from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageChops


def trim_connected_edge_background(image: Image.Image, threshold: int = 248) -> Image.Image:
    """Crop the white/transparent edge background without Python pixel walks.

    The old implementation flood-filled every background pixel from the four
    edges and then materialized every remaining ``(x, y)`` coordinate.  For a
    large mostly-white product image this took several seconds per image.

    The crop rectangle is identical to the bounding box of all opaque pixels
    for which at least one RGB channel is below ``threshold``.  A white region
    not connected to an edge can only be enclosed by those foreground pixels,
    so it cannot enlarge that bounding box.  Pillow evaluates the channel
    masks and bounding box in native code, preserving the canonical hash while
    avoiding the Python flood fill.
    """
    rgba = image.convert("RGBA")
    width, height = rgba.size
    if width == 0 or height == 0:
        return rgba

    red, green, blue, alpha = rgba.split()
    dark_lut = [255 if value < threshold else 0 for value in range(256)]
    opaque_lut = [255 if value > 0 else 0 for value in range(256)]
    color_foreground = ImageChops.lighter(red.point(dark_lut), green.point(dark_lut))
    color_foreground = ImageChops.lighter(color_foreground, blue.point(dark_lut))
    foreground = ImageChops.multiply(color_foreground, alpha.point(opaque_lut))
    bbox = foreground.getbbox()
    if bbox is None:
        return rgba
    return rgba.crop(bbox)


def canonical_visual_sha256(data: bytes) -> str:
    try:
        with Image.open(io.BytesIO(data)) as image:
            normalized = trim_connected_edge_background(image)
            digest = hashlib.sha256()
            digest.update(f"{normalized.width}x{normalized.height}|RGBA".encode("ascii"))
            digest.update(normalized.tobytes())
            return digest.hexdigest().upper()
    except Exception:
        return ""
