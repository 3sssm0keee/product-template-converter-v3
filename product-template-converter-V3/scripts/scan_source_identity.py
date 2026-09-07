from __future__ import annotations

import argparse
import os
import re
import subprocess
import zipfile
from pathlib import Path

from lxml import etree

from pipeline_common import finish, read_json, result
from powershell_host_v3 import WINRT_OCR_POWERSHELL_HOST_ENV, resolve_powershell_host


def package_text(path: Path) -> list[tuple[str, str]]:
    values = []
    if path.suffix.lower() not in {".docx", ".pptx"}:
        return values
    with zipfile.ZipFile(path) as package:
        for name in package.namelist():
            if name.lower().endswith((".xml", ".rels")):
                raw = package.read(name)
                try:
                    root = etree.fromstring(
                        raw,
                        parser=etree.XMLParser(resolve_entities=False, no_network=True, recover=True),
                    )
                    # Relationship targets and drawing metadata (title/descr/alt)
                    # live in attributes rather than XML text nodes.
                    text_values = list(root.itertext())
                    text_values.extend(
                        value
                        for element in root.iter()
                        for value in element.attrib.values()
                    )
                    text = " ".join(text_values)
                except Exception:
                    text = raw.decode("utf-8", errors="ignore")
                values.append((name, text))
    return values


def run_windows_ocr(image_path: Path) -> str:
    powershell = resolve_powershell_host(os.environ.get(WINRT_OCR_POWERSHELL_HOST_ENV))
    script = Path(__file__).resolve().parent / "ocr_windows.ps1"
    completed = subprocess.run(
        [
            str(powershell.path),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-ImagePath",
            str(image_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or f"Windows OCR exit {completed.returncode}")
    return completed.stdout


def locate_tesseract() -> str:
    candidates = [
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ]
    return str(next((path for path in candidates if path.is_file()), ""))


def ocr_text(image_path: Path) -> tuple[str, str]:
    try:
        return run_windows_ocr(image_path), "windows-media-ocr"
    except Exception as windows_exc:
        try:
            import pytesseract
            from PIL import Image

            tesseract = locate_tesseract()
            if tesseract:
                pytesseract.pytesseract.tesseract_cmd = tesseract
            return pytesseract.image_to_string(Image.open(image_path), lang="chi_sim+eng"), "tesseract"
        except Exception as tesseract_exc:
            raise RuntimeError(f"Windows OCR: {windows_exc}; Tesseract OCR: {tesseract_exc}") from tesseract_exc


PAGE_NAME_RE = re.compile(r"^(?:page[-_]?)?(\d+)\.png$", re.IGNORECASE)


def page_number(image_path: Path) -> int | None:
    match = PAGE_NAME_RE.fullmatch(image_path.name)
    return int(match.group(1)) if match else None


def expected_ocr_pages(document: Path, image_paths: list[Path], expected_page_count: int | None) -> list[int]:
    if expected_page_count is not None:
        if expected_page_count < 1:
            raise ValueError("--expected-page-count must be at least 1")
        return list(range(1, expected_page_count + 1))
    if document.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return list(range(1, len(PdfReader(document).pages) + 1))
    numbered = [number for path in image_paths if (number := page_number(path)) is not None]
    return list(range(1, max(numbered, default=0) + 1))


def no_source_identity_review(data: object) -> dict | None:
    if not isinstance(data, dict):
        return None
    review = data.get("identity_review")
    if not isinstance(review, dict) or review.get("status") != "NO_SOURCE_IDENTITY_FOUND":
        return None
    reviewer = review.get("reviewer")
    required = (review.get("source_sha256"), review.get("reviewed_at"), review.get("scope"))
    if not isinstance(reviewer, dict) or not reviewer.get("id") or not reviewer.get("role") or not all(required):
        return None
    return review


def verified_identity_review(data: object) -> dict | None:
    if not isinstance(data, dict):
        return None
    review = data.get("identity_review")
    if not isinstance(review, dict) or review.get("status") != "VERIFIED":
        return None
    terms = [str(term).strip() for term in review.get("manufacturer_terms", []) if str(term).strip()]
    evidence_ids = [str(value).strip() for value in review.get("evidence_ids", []) if str(value).strip()]
    if not terms or not evidence_ids:
        return None
    return {
        "status": "VERIFIED",
        "manufacturer_terms": terms,
        "evidence_ids": evidence_ids,
        "notes": str(review.get("notes") or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描来源厂家文字、链接和元数据")
    parser.add_argument("document", type=Path)
    parser.add_argument("--terms", type=Path, required=True, help="JSON数组或content_map.json")
    parser.add_argument("--template", type=Path, help="排除固定模板中原本就存在的文字命中")
    parser.add_argument("--ocr-dir", type=Path)
    parser.add_argument("--expected-page-count", type=int, help="OCR 页图应覆盖的页数")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    data = read_json(args.terms)
    terms = data if isinstance(data, list) else data.get("manufacturer_terms", [])
    terms = [str(term).strip() for term in terms if str(term).strip()]
    approved_identity_review = verified_identity_review(data)
    empty_terms_review = None
    if not terms:
        empty_terms_review = no_source_identity_review(data)
        if empty_terms_review and not args.ocr_dir:
            return finish(result(
                "PASS",
                "scan_source_identity",
                findings=[],
                term_count=0,
                identity_review={
                    "status": empty_terms_review["status"],
                    "source_sha256": empty_terms_review["source_sha256"],
                    "reviewed_at": empty_terms_review["reviewed_at"],
                    "scope": empty_terms_review["scope"],
                    "reviewer": empty_terms_review["reviewer"],
                },
                ocr_scanned=False,
                ocr_backends=[],
                ocr_expected_pages=[],
                ocr_scanned_pages=[],
                ocr_failed_pages=[],
            ), args.report)
        if not empty_terms_review:
            return finish(result("HUMAN_REVIEW", "scan_source_identity", findings=[{"code": "IDENTITY_TERMS_EMPTY"}]), args.report)
    hits = []
    try:
        baseline = {
            (part, term.casefold())
            for part, text in package_text(args.template)
            for term in terms
            if term.casefold() in text.casefold()
        } if args.template and args.template.is_file() else set()
        for part, text in package_text(args.document):
            for term in terms:
                if term.casefold() in text.casefold() and (part, term.casefold()) not in baseline:
                    hits.append({"term": term, "location": part, "method": "package-text"})
    except Exception as exc:
        return finish(result(
            "FAIL",
            "scan_source_identity",
            findings=[{"code": "PACKAGE_SCAN_FAILED", "message": str(exc)}],
            term_count=len(terms),
        ), args.report)

    ocr_scanned = False
    ocr_backends = set()
    ocr_failures = []
    ocr_scanned_pages = []
    ocr_expected_pages = []
    if args.ocr_dir:
        if not args.ocr_dir.is_dir():
            ocr_failures.append({"code": "OCR_DIR_MISSING", "location": str(args.ocr_dir)})
        else:
            image_paths = sorted(args.ocr_dir.glob("*.png"), key=lambda path: (page_number(path) or 0, path.name.casefold()))
            try:
                ocr_expected_pages = expected_ocr_pages(args.document, image_paths, args.expected_page_count)
            except Exception as exc:
                ocr_failures.append({"code": "OCR_EXPECTED_PAGE_COUNT_FAILED", "message": str(exc)})
            numbered_pages = [page_number(path) for path in image_paths]
            unnumbered = [str(path) for path, number in zip(image_paths, numbered_pages) if number is None]
            if unnumbered:
                ocr_failures.append({"code": "OCR_PAGE_SEQUENCE_UNVERIFIABLE", "pages": unnumbered})
            page_numbers = [number for number in numbered_pages if number is not None]
            duplicates = sorted({number for number in page_numbers if page_numbers.count(number) > 1})
            if duplicates:
                ocr_failures.append({"code": "OCR_PAGE_DUPLICATE", "pages": duplicates})
            if not image_paths:
                ocr_failures.append({
                    "code": "OCR_PAGES_MISSING",
                    "missing_pages": ocr_expected_pages or [1],
                })
            else:
                expected = set(ocr_expected_pages)
                actual = set(page_numbers)
                missing_pages = sorted(expected - actual)
                extra_pages = sorted(actual - expected) if ocr_expected_pages else []
                if missing_pages or extra_pages:
                    ocr_failures.append({
                        "code": "OCR_PAGES_MISSING" if missing_pages else "OCR_PAGES_UNEXPECTED",
                        "missing_pages": missing_pages,
                        "unexpected_pages": extra_pages,
                    })
            for image_path in image_paths:
                number = page_number(image_path)
                try:
                    text, backend = ocr_text(image_path)
                    ocr_scanned = True
                    ocr_backends.add(backend)
                    if number is not None:
                        ocr_scanned_pages.append(number)
                    for term in terms:
                        if term.casefold() in str(text).casefold():
                            hits.append({"term": term, "location": str(image_path), "method": "ocr"})
                except Exception as exc:
                    ocr_failures.append({
                        "code": "OCR_FAILED",
                        "page": number,
                        "location": str(image_path),
                        "message": str(exc),
                    })

    approved_identity_hits = []
    if approved_identity_review:
        approved_terms = {term.casefold() for term in approved_identity_review["manufacturer_terms"]}
        pending_hits = []
        for hit in hits:
            if str(hit.get("term") or "").casefold() in approved_terms:
                approved_identity_hits.append(hit)
            else:
                pending_hits.append(hit)
        hits = pending_hits
    definite = list(hits) + ocr_failures
    status = "FAIL" if definite else "PASS"
    identity_review = None
    if empty_terms_review:
        identity_review = {
            "status": empty_terms_review["status"],
            "source_sha256": empty_terms_review["source_sha256"],
            "reviewed_at": empty_terms_review["reviewed_at"],
            "scope": empty_terms_review["scope"],
            "reviewer": empty_terms_review["reviewer"],
        }
    elif approved_identity_review:
        identity_review = approved_identity_review
    return finish(result(
        status,
        "scan_source_identity",
        findings=hits + ocr_failures,
        term_count=len(terms),
        ocr_scanned=ocr_scanned,
        ocr_backends=sorted(ocr_backends),
        ocr_expected_pages=ocr_expected_pages,
        ocr_scanned_pages=sorted(set(ocr_scanned_pages)),
        ocr_failed_pages=sorted({item["page"] for item in ocr_failures if item.get("page") is not None}),
        approved_identity_hits=approved_identity_hits,
        **({"identity_review": identity_review} if identity_review else {}),
    ), args.report)


if __name__ == "__main__":
    raise SystemExit(main())
