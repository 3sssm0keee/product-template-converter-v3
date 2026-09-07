from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from format_probe import probe_format
from pipeline_common import finish, result


SUPPORTED = {"doc", "docx", "ppt", "pptx", "pdf"}


def _expectations(values: list[str]) -> dict[str, int]:
    expected: dict[str, int] = {}
    for value in values:
        name, separator, raw_count = value.partition("=")
        name = name.strip().lower()
        if separator != "=" or name not in SUPPORTED:
            raise ValueError(f"invalid --expect value: {value}")
        expected[name] = int(raw_count)
    return expected


def verify_matrix(root: Path, expected: dict[str, int] | None = None) -> dict:
    root = root.resolve()
    if not root.is_dir():
        return result("BLOCKED", "real_format_matrix_v3", findings=[{
            "code": "SAMPLE_ROOT_NOT_FOUND",
            "path": str(root),
        }])
    paths = sorted(
        (
            path for path in root.rglob("*")
            if path.is_file() and path.suffix.lower().lstrip(".") in SUPPORTED
        ),
        key=lambda path: str(path.relative_to(root)).casefold(),
    )
    cases = []
    counts: Counter[str] = Counter()
    findings = []
    for path in paths:
        probe = probe_format(path)
        detected = str(probe.get("detected_format") or "")
        if probe.get("status") == "PASS" and detected:
            counts[detected] += 1
        else:
            findings.append({
                "code": "REAL_SAMPLE_FORMAT_PROBE_FAILED",
                "path": str(path.relative_to(root)),
                "probe_status": probe.get("status"),
                "probe_findings": probe.get("findings", []),
            })
        cases.append({
            "path": str(path.relative_to(root)),
            "extension": path.suffix.lower().lstrip("."),
            "detected_format": detected,
            "family": probe.get("family"),
            "status": probe.get("status"),
            "sha256": probe.get("source", {}).get("sha256"),
            "findings": probe.get("findings", []),
        })
    for name, expected_count in sorted((expected or {}).items()):
        actual = counts.get(name, 0)
        if actual != expected_count:
            findings.append({
                "code": "REAL_SAMPLE_COUNT_MISMATCH",
                "format": name,
                "expected": expected_count,
                "actual": actual,
            })
    return result(
        "FAIL" if findings else "PASS",
        "real_format_matrix_v3",
        findings=findings,
        sample_root=str(root),
        file_count=len(paths),
        counts={name: counts.get(name, 0) for name in sorted(SUPPORTED)},
        cases=cases,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="用真实签名批量核验 V3 五种输入格式样本")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--expect", action="append", default=[], help="可重复，例如 --expect doc=3")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        expected = _expectations(args.expect)
        payload = verify_matrix(args.root, expected)
    except (OSError, ValueError) as exc:
        payload = result("FAIL", "real_format_matrix_v3", findings=[{
            "code": "REAL_SAMPLE_MATRIX_INVALID",
            "message": str(exc),
        }])
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
