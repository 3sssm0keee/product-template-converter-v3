from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from pipeline_common import finish, result, sha256_file
from powershell_host_v3 import resolve_powershell_host


SUPPORTED = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}


def office_convert(source: Path, output: Path, kind: str, visual_evidence_dir: Path | None = None) -> tuple[bool, str, dict]:
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        powershell = resolve_powershell_host()
        script = Path(__file__).resolve().with_name("normalize_legacy_office.ps1")
        command = [
            str(powershell.path), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-Kind", kind, "-InputPath", str(source.resolve()), "-OutputPath", str(output.resolve()),
        ]
        if visual_evidence_dir is not None:
            visual_evidence_dir.mkdir(parents=True, exist_ok=True)
            command.extend([
                "-OriginalPdfPath", str((visual_evidence_dir / "original.pdf").resolve()),
                "-NormalizedPdfPath", str((visual_evidence_dir / "normalized.pdf").resolve()),
            ])
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if completed.returncode != 0:
            return False, completed.stderr.strip() or completed.stdout.strip() or "Office COM conversion failed", {}
        try:
            provenance = json.loads(completed.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            provenance = {}
        required = ["application", "application_version"]
        if visual_evidence_dir is not None:
            required.extend(["original_pdf", "normalized_pdf"])
        missing = [name for name in required if not str(provenance.get(name) or "").strip()]
        if missing:
            return False, f"NORMALIZATION_PROVENANCE_INCOMPLETE: missing {', '.join(missing)}", provenance
        if visual_evidence_dir is not None:
            expected_pdfs = {
                "original_pdf": (visual_evidence_dir / "original.pdf").resolve(),
                "normalized_pdf": (visual_evidence_dir / "normalized.pdf").resolve(),
            }
            for field, expected in expected_pdfs.items():
                actual = Path(str(provenance[field])).resolve()
                if actual != expected or not actual.is_file():
                    return False, f"NORMALIZATION_PROVENANCE_INCOMPLETE: invalid {field}", provenance
        if not output.is_file():
            return False, "NORMALIZATION_OUTPUT_MISSING: Office conversion reported success without output", provenance
        return True, "Microsoft Office COM via PowerShell", provenance
    except Exception as exc:
        return False, str(exc), {}


def main() -> int:
    parser = argparse.ArgumentParser(description="标准化产品资料源文件")
    parser.add_argument("source", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--detected-format", choices=["DOC", "DOCX", "PPT", "PPTX", "PDF"], help="来自 FormatProbeV3 的真实格式")
    parser.add_argument("--visual-evidence-dir", type=Path, help="DOC/PPT 归一化前后 PDF 与页面差异证据目录")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    if not source.is_file():
        return finish(result("BLOCKED", "normalize_source", findings=[{"code": "SOURCE_MISSING", "message": "源文件不存在"}], source=str(source)), args.report)
    suffix = f".{args.detected_format.lower()}" if args.detected_format else source.suffix.lower()
    if suffix not in SUPPORTED:
        return finish(result("BLOCKED", "normalize_source", findings=[{"code": "UNSUPPORTED_FORMAT", "message": suffix}], source=str(source)), args.report)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    target_suffix = {".doc": ".docx", ".ppt": ".pptx"}.get(suffix, suffix)
    target = args.out_dir / f"{source.stem}.normalized{target_suffix}"
    method = "byte-copy"
    findings = []
    if suffix in {".doc", ".ppt"}:
        if args.visual_evidence_dir is None:
            return finish(result(
                "BLOCKED",
                "normalize_source",
                findings=[{
                    "code": "NORMALIZATION_VISUAL_EVIDENCE_DIR_REQUIRED",
                    "message": "DOC/PPT 归一化必须同时保存原件与归一化文件的 PDF 视觉证据",
                }],
                source=str(source),
            ), args.report)
        ok, detail, provenance = office_convert(
            source,
            target,
            "word" if suffix == ".doc" else "powerpoint",
            args.visual_evidence_dir.resolve() if args.visual_evidence_dir else None,
        )
        method = detail
        if not ok:
            return finish(result("BLOCKED", "normalize_source", findings=[{"code": "OFFICE_CONVERSION_FAILED", "message": detail}], source=str(source)), args.report)
    else:
        provenance = {}
        shutil.copy2(source, target)
    source_sha256 = sha256_file(source)
    normalized_sha256 = sha256_file(target)
    if suffix != target_suffix:
        findings.append({
            "code": "FORMAT_NORMALIZED",
            "source_format": suffix.lstrip(".").upper(),
            "normalized_format": target_suffix.lstrip(".").upper(),
            "source_sha256": source_sha256,
            "normalized_sha256": normalized_sha256,
        })
    payload = result(
        "PASS",
        "normalize_source",
        findings=findings,
        source=str(source),
        source_sha256=source_sha256,
        normalized=str(target),
        normalized_sha256=normalized_sha256,
        method=method,
        format_changed=suffix != target_suffix,
        source_format=suffix.lstrip(".").upper(),
        normalized_format=target_suffix.lstrip(".").upper(),
        application=provenance.get("application", ""),
        application_version=provenance.get("application_version", ""),
        original_render_pdf=provenance.get("original_pdf", ""),
        normalized_render_pdf=provenance.get("normalized_pdf", ""),
    )
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
