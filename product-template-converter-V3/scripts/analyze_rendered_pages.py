from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image, ImageChops, ImageStat
from pypdf import PdfReader

from pipeline_common import finish, result


class RenderValidationError(RuntimeError):
    def __init__(self, finding: dict):
        self.finding = finding
        super().__init__(finding.get("message", finding.get("code", "render validation failed")))


PAGE_PNG_RE = re.compile(r"^page-(\d+)\.png$", re.IGNORECASE)
BLANK_PAGE_GRAY_STDDEV_MAX = 1.0


def is_blank_page(page: dict) -> bool:
    return (
        page["text_blocks"] == 0
        and page["content_objects"] == 0
        and page["gray_stddev"] <= BLANK_PAGE_GRAY_STDDEV_MAX
    )


def needs_large_gap_review(page: dict, is_edge: bool) -> bool:
    return (
        not is_edge
        and not is_blank_page(page)
        and page.get("image_count", 0) == 0
        and page["content_bottom_ratio"] < 0.55
    )


def render_and_measure(pdf_path: Path, output_dir: Path, dpi: int = 120) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob("page-*.png"):
        stale.unlink()
    try:
        document = PdfReader(pdf_path)
        pdf_page_count = len(document.pages)
    except Exception as exc:
        raise RenderValidationError({"code": "PDF_READ_FAILED", "message": str(exc)}) from exc
    if pdf_page_count < 1:
        raise RenderValidationError({"code": "PDF_PAGE_COUNT_ZERO", "pdf_page_count": pdf_page_count})
    pdftoppm = shutil.which("pdftoppm")
    if not pdftoppm:
        raise RenderValidationError({"code": "PDFTOTPPM_NOT_FOUND", "message": "pdftoppm not found"})
    candidate = Path(pdftoppm)
    if candidate.suffix.lower() == ".cmd":
        for parent in candidate.parents:
            direct = parent / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe"
            if direct.is_file():
                pdftoppm = str(direct)
                break
    prefix = output_dir / "page"
    completed = subprocess.run([pdftoppm, "-png", "-r", str(dpi), str(pdf_path), str(prefix)], capture_output=True, text=True, timeout=600)
    if completed.returncode:
        raise RenderValidationError({
            "code": "PDFTOTPPM_FAILED",
            "returncode": completed.returncode,
            "message": completed.stderr.strip() or completed.stdout.strip() or "pdftoppm failed",
        })
    png_files = sorted(output_dir.glob("*.png"), key=lambda path: path.name.casefold())
    parsed = [(path, PAGE_PNG_RE.fullmatch(path.name)) for path in png_files]
    invalid_names = [str(path) for path, match in parsed if match is None]
    rendered = sorted(
        ((path, int(match.group(1))) for path, match in parsed if match is not None),
        key=lambda item: item[1],
    )
    png_page_numbers = [number for _, number in rendered]
    expected_pages = list(range(1, pdf_page_count + 1))
    missing_pages = sorted(set(expected_pages) - set(png_page_numbers))
    extra_pages = sorted(set(png_page_numbers) - set(expected_pages))
    duplicate_pages = sorted({number for number in png_page_numbers if png_page_numbers.count(number) > 1})
    if invalid_names or len(png_files) != pdf_page_count or png_page_numbers != expected_pages or duplicate_pages:
        raise RenderValidationError({
            "code": "PDF_PNG_PAGE_MISMATCH",
            "message": "PDF 页数、PNG 数量或页码序列不一致",
            "pdf_page_count": pdf_page_count,
            "png_count": len(png_files),
            "expected_pages": expected_pages,
            "actual_pages": png_page_numbers,
            "missing_pages": missing_pages,
            "extra_pages": extra_pages,
            "duplicate_pages": duplicate_pages,
            "invalid_png_names": invalid_names,
        })

    pages = []
    for number, page in enumerate(document.pages, 1):
        png = rendered[number - 1][0]
        try:
            with Image.open(png) as opened:
                gray = opened.convert("L")
                gray_stddev = float(ImageStat.Stat(gray).stddev[0])
        except Exception as exc:
            raise RenderValidationError({
                "code": "PNG_READ_FAILED",
                "page": number,
                "png": str(png),
                "message": str(exc),
            }) from exc
        width = float(page.mediabox.width)
        height = float(page.mediabox.height)
        text_positions = []
        def visitor(text, cm, tm, font_dict, font_size):
            if text.strip():
                y_from_bottom = float(tm[5])
                top = height - y_from_bottom - max(float(font_size or 10), 1)
                bottom = height - y_from_bottom + max(float(font_size or 10), 1)
                text_positions.append((top, bottom))
        try:
            page.extract_text(visitor_text=visitor)
        except Exception:
            text_positions = []
        content_positions = [position for position in text_positions if position[0] > height * 0.08 and position[1] < height * 0.94]
        content_bottom = max((position[1] for position in content_positions), default=0)
        text = page.extract_text() or ""
        image_count = len(list(page.images))
        pages.append({
            "page": number,
            "width_pt": round(width, 2),
            "height_pt": round(height, 2),
            "text_blocks": len([line for line in text.splitlines() if line.strip()]),
            "image_count": image_count,
            "content_objects": len(content_positions) + image_count,
            "content_bottom_ratio": round(content_bottom / max(height, 1), 4),
            "gray_stddev": round(gray_stddev, 4),
            "png": str(png),
        })
    return {
        "pdf": str(pdf_path),
        "page_count": len(pages),
        "pdf_page_count": pdf_page_count,
        "png_count": len(png_files),
        "page_numbers": expected_pages,
        "pages": pages,
    }


def compare_pages(left: dict, right: dict) -> list[dict]:
    left_numbers = left.get("page_numbers", list(range(1, left["page_count"] + 1)))
    right_numbers = right.get("page_numbers", list(range(1, right["page_count"] + 1)))
    if left_numbers != right_numbers:
        raise ValueError(f"page sequences differ: left={left_numbers}, right={right_numbers}")
    left_by_number = {item["page"]: item for item in left["pages"]}
    right_by_number = {item["page"]: item for item in right["pages"]}
    comparisons = []
    for number in left_numbers:
        left_page = left_by_number[number]
        right_page = right_by_number[number]
        with Image.open(left_page["png"]) as left_opened, Image.open(right_page["png"]) as right_opened:
            a_image = left_opened.convert("L")
            b_image = right_opened.convert("L")
            if a_image.size != b_image.size:
                b_image = b_image.resize(a_image.size, Image.Resampling.LANCZOS)
            difference = float(ImageStat.Stat(ImageChops.difference(a_image, b_image)).mean[0]) / 255.0
        comparisons.append({"page": number, "mean_absolute_difference": round(difference, 5)})
    return comparisons


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染并比较Word/WPS导出的全部页面")
    parser.add_argument("--word-pdf", type=Path, required=True)
    parser.add_argument("--wps-pdf", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not args.word_pdf.is_file() or not args.wps_pdf.is_file():
        return finish(result("BLOCKED", "analyze_rendered_pages", findings=[{"code": "PDF_INPUT_MISSING"}]), args.report)
    if args.word_pdf.resolve() == args.wps_pdf.resolve():
        return finish(result("FAIL", "analyze_rendered_pages", findings=[{"code": "ENGINE_PDF_NOT_DISTINCT", "pdf": str(args.word_pdf.resolve())}]), args.report)
    try:
        # 两套 Office COM 导出仍在上游串行完成；这里只并行处理已落盘 PDF 的
        # 栅格化与页面分析，避免并发操纵 Office 进程。
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="v3-pdf-analysis") as pool:
            word_future = pool.submit(render_and_measure, args.word_pdf, args.output_dir / "word")
            wps_future = pool.submit(render_and_measure, args.wps_pdf, args.output_dir / "wps")
            word = word_future.result()
            wps = wps_future.result()
    except RenderValidationError as exc:
        return finish(result(
            "FAIL",
            "analyze_rendered_pages",
            findings=[exc.finding, {"code": "RENDER_FAILED", "message": str(exc)}],
        ), args.report)
    except Exception as exc:
        return finish(result("FAIL", "analyze_rendered_pages", findings=[{"code": "RENDER_FAILED", "message": str(exc)}]), args.report)
    findings = []
    if word["page_count"] != wps["page_count"]:
        findings.append({
            "code": "ENGINE_PAGE_COUNT_MISMATCH",
            "word": word["page_count"],
            "wps": wps["page_count"],
            "word_pages": word["page_numbers"],
            "wps_pages": wps["page_numbers"],
        })
    comparisons = compare_pages(word, wps) if not findings else []
    for item in comparisons:
        if item["mean_absolute_difference"] > 0.12:
            findings.append({"code": "ENGINE_VISUAL_DIFFERENCE", **item})
    engine_facts = [("word", word), ("wps", wps)]
    for engine, facts in engine_facts:
        blank_pages = [page["page"] for page in facts["pages"] if is_blank_page(page)]
        if len(blank_pages) == facts["page_count"]:
            findings.append({
                "code": "DOCUMENT_ALL_PAGES_BLANK",
                "engine": engine,
                "pages": blank_pages,
                "gray_stddev_max": BLANK_PAGE_GRAY_STDDEV_MAX,
            })
        edge_pages = {1, facts["page_count"]}
        blank_edge_pages = sorted(page for page in blank_pages if page in edge_pages)
        if blank_edge_pages:
            findings.append({
                "code": "EDGE_PAGE_BLANK",
                "engine": engine,
                "pages": blank_edge_pages,
                "gray_stddev_max": BLANK_PAGE_GRAY_STDDEV_MAX,
            })
        for page in facts["pages"]:
            is_edge = page["page"] in {1, facts["page_count"]}
            if not is_edge and is_blank_page(page):
                findings.append({"code": "BODY_PAGE_WITHOUT_CONTENT", "engine": engine, "page": page["page"]})
            elif needs_large_gap_review(page, is_edge):
                findings.append({"code": "POSSIBLE_LARGE_GAP", "engine": engine, "page": page["page"], "bottom_ratio": page["content_bottom_ratio"]})
    hard_codes = {
        "ENGINE_PAGE_COUNT_MISMATCH",
        "BODY_PAGE_WITHOUT_CONTENT",
        "DOCUMENT_ALL_PAGES_BLANK",
        "EDGE_PAGE_BLANK",
    }
    hard = [item for item in findings if item["code"] in hard_codes]
    status = "FAIL" if hard else "HUMAN_REVIEW" if findings else "PASS"
    render_payload = {"word": word, "wps": wps, "comparisons": comparisons}
    return finish(result(status, "analyze_rendered_pages", findings=findings, **render_payload), args.report)


if __name__ == "__main__":
    raise SystemExit(main())
