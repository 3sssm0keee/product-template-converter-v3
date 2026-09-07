from __future__ import annotations

import argparse
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from pipeline_common import finish, read_json, sha256_file


def _release_report_findings(report: dict[str, Any], manifest_sha256: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if report.get("status") != "PASS" or report.get("stage") != "verify_release":
        findings.append({"code": "PUBLISH_RELEASE_REPORT_NOT_PASS", "status": report.get("status"), "stage": report.get("stage")})
    manifest = report.get("manifest") if isinstance(report.get("manifest"), dict) else {}
    if str(manifest.get("sha256") or "").upper() != manifest_sha256:
        findings.append({"code": "PUBLISH_RELEASE_MANIFEST_SHA_MISMATCH", "expected": manifest_sha256, "actual": manifest.get("sha256")})
    matrix = report.get("release_matrix_report") if isinstance(report.get("release_matrix_report"), dict) else {}
    if matrix.get("status") != "PASS" or matrix.get("case_count") != 16:
        findings.append({"code": "PUBLISH_RELEASE_MATRIX_NOT_PASS", "status": matrix.get("status"), "case_count": matrix.get("case_count")})
    pipelines = report.get("pipeline_reports") if isinstance(report.get("pipeline_reports"), list) else []
    if len(pipelines) != 5 or any(
        not isinstance(item, dict) or item.get("status") != "PASS" or item.get("deliverable") is not True
        for item in pipelines
    ):
        findings.append({"code": "PUBLISH_GOLDEN_PIPELINES_INVALID", "actual_count": len(pipelines)})
    blind = report.get("blind_report") if isinstance(report.get("blind_report"), dict) else {}
    if blind.get("status") != "PASS" or len(blind.get("cases", [])) != 5:
        findings.append({"code": "PUBLISH_BLIND_REPORT_INVALID", "status": blind.get("status"), "case_count": len(blind.get("cases", [])) if isinstance(blind.get("cases"), list) else 0})
    responsible = report.get("responsible_engineer_visual_review") if isinstance(report.get("responsible_engineer_visual_review"), dict) else {}
    independent = report.get("independent_blind_visual_review") if isinstance(report.get("independent_blind_visual_review"), dict) else {}
    if responsible.get("status") != "PASS" or responsible.get("case_count") != 5:
        findings.append({"code": "PUBLISH_RESPONSIBLE_REVIEW_INVALID"})
    if independent.get("decision") != "PASS" or independent.get("complete") is not True or independent.get("case_count") != 5:
        findings.append({"code": "PUBLISH_INDEPENDENT_REVIEW_INVALID"})
    if report.get("findings") != []:
        findings.append({"code": "PUBLISH_RELEASE_FINDINGS_NOT_EMPTY", "actual": report.get("findings")})
    return findings


def _verify_source_manifest(source_root: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    manifest_path = source_root / "manifest.json"
    if not manifest_path.is_file():
        return None, [{"code": "PUBLISH_MANIFEST_MISSING", "path": str(manifest_path)}]
    try:
        manifest = read_json(manifest_path)
    except Exception as exc:
        return None, [{"code": "PUBLISH_MANIFEST_INVALID", "path": str(manifest_path), "message": str(exc)}]
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        return None, [{"code": "PUBLISH_MANIFEST_INVALID", "path": str(manifest_path)}]
    paths: set[str] = set()
    for index, entry in enumerate(manifest["files"]):
        if not isinstance(entry, dict):
            findings.append({"code": "PUBLISH_MANIFEST_ENTRY_INVALID", "index": index})
            continue
        relative = str(entry.get("path") or "")
        path = Path(relative)
        if not relative or path.is_absolute() or ".." in path.parts or relative in paths:
            findings.append({"code": "PUBLISH_MANIFEST_PATH_UNSAFE", "index": index, "path": relative})
            continue
        paths.add(relative)
        source = (source_root / path).resolve()
        try:
            source.relative_to(source_root.resolve())
        except ValueError:
            findings.append({"code": "PUBLISH_MANIFEST_PATH_UNSAFE", "index": index, "path": relative})
            continue
        if not source.is_file():
            findings.append({"code": "PUBLISH_SOURCE_FILE_MISSING", "path": str(source)})
            continue
        actual_bytes = source.stat().st_size
        actual_sha = sha256_file(source)
        if entry.get("bytes") != actual_bytes or str(entry.get("sha256") or "").upper() != actual_sha:
            findings.append(
                {
                    "code": "PUBLISH_SOURCE_FILE_MISMATCH",
                    "path": relative,
                    "expected_bytes": entry.get("bytes"),
                    "actual_bytes": actual_bytes,
                    "expected_sha256": entry.get("sha256"),
                    "actual_sha256": actual_sha,
                }
            )
    return manifest, findings


def publish(source_root: Path, destination: Path, release_report_path: Path) -> dict[str, Any]:
    source_root = source_root.resolve()
    destination = destination.resolve()
    findings: list[dict[str, Any]] = []
    manifest, manifest_findings = _verify_source_manifest(source_root)
    findings.extend(manifest_findings)
    manifest_path = source_root / "manifest.json"
    manifest_sha = sha256_file(manifest_path) if manifest_path.is_file() else ""
    try:
        release_report = read_json(release_report_path)
    except Exception as exc:
        release_report = {}
        findings.append({"code": "PUBLISH_RELEASE_REPORT_INVALID", "path": str(release_report_path), "message": str(exc)})
    if isinstance(release_report, dict):
        findings.extend(_release_report_findings(release_report, manifest_sha))
    else:
        findings.append({"code": "PUBLISH_RELEASE_REPORT_INVALID", "path": str(release_report_path)})
    if destination.exists():
        findings.append({"code": "PUBLISH_DESTINATION_EXISTS", "path": str(destination)})
    try:
        destination.relative_to(source_root)
    except ValueError:
        pass
    else:
        findings.append({"code": "PUBLISH_DESTINATION_INSIDE_SOURCE", "path": str(destination)})
    staging = destination.with_name(f"{destination.name}.staging-{manifest_sha[:12]}")
    if staging.exists():
        findings.append({"code": "PUBLISH_STAGING_EXISTS", "path": str(staging)})
    if findings or manifest is None:
        return {
            "status": "BLOCKED",
            "stage": "publish_v3",
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "deliverable": False,
            "findings": findings,
            "source_root": str(source_root),
            "destination": str(destination),
            "manifest_sha256": manifest_sha or None,
        }

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=False, exist_ok=False)
    copied: list[dict[str, Any]] = []
    try:
        for entry in manifest["files"]:
            relative = Path(entry["path"])
            source = source_root / relative
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append({"path": relative.as_posix(), "bytes": target.stat().st_size, "sha256": sha256_file(target)})
        shutil.copy2(manifest_path, staging / "manifest.json")
        for expected, actual in zip(manifest["files"], copied, strict=True):
            if (
                expected["path"] != actual["path"]
                or expected["bytes"] != actual["bytes"]
                or str(expected["sha256"]).upper() != actual["sha256"]
            ):
                raise ValueError(f"staging verification failed: {expected['path']}")
        if sha256_file(staging / "manifest.json") != manifest_sha:
            raise ValueError("staging manifest hash mismatch")
        staging.replace(destination)
    except Exception as exc:
        return {
            "status": "BLOCKED",
            "stage": "publish_v3",
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "deliverable": False,
            "findings": [{"code": "PUBLISH_COPY_OR_VERIFY_FAILED", "message": str(exc), "staging": str(staging)}],
            "source_root": str(source_root),
            "destination": str(destination),
            "manifest_sha256": manifest_sha,
        }
    return {
        "status": "PASS",
        "stage": "publish_v3",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "deliverable": True,
        "findings": [],
        "source_root": str(source_root),
        "destination": str(destination),
        "manifest_sha256": manifest_sha,
        "release_report": {"path": str(release_report_path.resolve()), "sha256": sha256_file(release_report_path)},
        "published_file_count": len(copied),
        "files": copied,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通过全部 V3 发布门禁后，将 manifest 白名单文件原子发布到正式目录")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--release-report", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    payload = publish(args.source_root, args.destination, args.release_report)
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
