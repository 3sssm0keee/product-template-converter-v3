from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat
from pypdf import PdfReader

from pipeline_common import finish, result, sha256_file


DEFAULT_MAD_THRESHOLD = 18.0


def _render(pdf: Path, output: Path, pdftoppm: str) -> list[Path]:
    output.mkdir(parents=True, exist_ok=True)
    prefix = output / "page"
    completed = subprocess.run(
        [pdftoppm, "-png", "-r", "110", str(pdf), str(prefix)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "pdftoppm failed")
    return sorted(output.glob("page-*.png"))


def _visual_difference(left: Path, right: Path, overlay: Path | None = None) -> float:
    with Image.open(left) as first, Image.open(right) as second:
        first_rgb = first.convert("RGB")
        second_rgb = second.convert("RGB")
        if first_rgb.size != second_rgb.size:
            second_rgb = second_rgb.resize(first_rgb.size)
        difference = ImageChops.difference(first_rgb, second_rgb)
        means = ImageStat.Stat(difference).mean
        if overlay is not None:
            overlay.parent.mkdir(parents=True, exist_ok=True)
            # 红色表示变化强度；保留浅灰原页背景，便于离线人工定位差异。
            grayscale = difference.convert("L")
            red = Image.new("RGB", first_rgb.size, (225, 38, 38))
            base = first_rgb.convert("L").convert("RGB").point(lambda value: min(255, int(value * 0.35 + 165)))
            composed = Image.composite(red, base, grayscale)
            composed.save(overlay, format="PNG", optimize=True)
        return round(sum(means) / len(means), 6)


def compare_legacy_pdfs(
    original_pdf: Path,
    normalized_pdf: Path,
    *,
    output_dir: Path | None = None,
    mad_threshold: float = DEFAULT_MAD_THRESHOLD,
) -> dict[str, Any]:
    if not original_pdf.is_file() or not normalized_pdf.is_file():
        return result("BLOCKED", "legacy_visual_compare_v3", findings=[{"code": "LEGACY_RENDER_EVIDENCE_MISSING"}])
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        return result("BLOCKED", "legacy_visual_compare_v3", findings=[{"code": "PDFTOTPPM_UNAVAILABLE"}])
    original_count = len(PdfReader(original_pdf).pages)
    normalized_count = len(PdfReader(normalized_pdf).pages)
    if original_count != normalized_count:
        return result(
            "HUMAN_REVIEW",
            "legacy_visual_compare_v3",
            findings=[{
                "code": "LEGACY_NORMALIZATION_PAGE_COUNT_CHANGED",
                "original": original_count,
                "normalized": normalized_count,
            }],
            original_page_count=original_count,
            normalized_page_count=normalized_count,
        )
    temporary_context = tempfile.TemporaryDirectory() if output_dir is None else None
    root = output_dir or Path(temporary_context.name)
    try:
        original_pages = _render(original_pdf, root / "original", pdftoppm)
        normalized_pages = _render(normalized_pdf, root / "normalized", pdftoppm)
        if len(original_pages) != original_count or len(normalized_pages) != normalized_count:
            return result("BLOCKED", "legacy_visual_compare_v3", findings=[{"code": "LEGACY_RENDER_PAGE_SET_INCOMPLETE"}])
        persist_paths = output_dir is not None
        pages: list[dict[str, Any]] = []
        for index, (left, right) in enumerate(zip(original_pages, normalized_pages), 1):
            overlay = root / "diff" / f"page-{index:02d}.png" if persist_paths else None
            page = {
                "page": index,
                "mean_absolute_difference": _visual_difference(left, right, overlay),
            }
            if persist_paths:
                page.update(
                    {
                        "original_png": str(left),
                        "original_png_sha256": sha256_file(left),
                        "normalized_png": str(right),
                        "normalized_png_sha256": sha256_file(right),
                        "diff_overlay": str(overlay),
                        "diff_overlay_sha256": sha256_file(overlay),
                    }
                )
            pages.append(page)
        exceeded = [page for page in pages if page["mean_absolute_difference"] > mad_threshold]
        status = "HUMAN_REVIEW" if exceeded else "PASS"
        findings = [{
            "code": "LEGACY_NORMALIZATION_VISUAL_DIFF_EXCEEDED",
            "pages": [page["page"] for page in exceeded],
            "threshold": mad_threshold,
        }] if exceeded else []
        return result(
            status,
            "legacy_visual_compare_v3",
            findings=findings,
            original_pdf={"path": str(original_pdf), "sha256": sha256_file(original_pdf)},
            normalized_pdf={"path": str(normalized_pdf), "sha256": sha256_file(normalized_pdf)},
            original_page_count=original_count,
            normalized_page_count=normalized_count,
            mad_threshold=mad_threshold,
            pages=pages,
        )
    finally:
        if temporary_context is not None:
            temporary_context.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="比较旧 Office 原件与归一化文件的渲染证据")
    parser.add_argument("--original-pdf", type=Path, required=True)
    parser.add_argument("--normalized-pdf", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--mad-threshold", type=float, default=DEFAULT_MAD_THRESHOLD)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        payload = compare_legacy_pdfs(
            args.original_pdf.resolve(),
            args.normalized_pdf.resolve(),
            output_dir=args.output_dir.resolve() if args.output_dir else None,
            mad_threshold=args.mad_threshold,
        )
    except Exception as exc:
        payload = result("BLOCKED", "legacy_visual_compare_v3", findings=[{"code": "LEGACY_VISUAL_COMPARE_FAILED", "message": str(exc)}])
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
