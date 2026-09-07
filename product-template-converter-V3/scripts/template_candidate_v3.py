from __future__ import annotations

import argparse
import zipfile
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any

from pipeline_common import finish, result, sha256_file, utc_now, write_json
from template_ir_v3 import (
    COMPILER_ID,
    COMPILER_VERSION,
    TemplateIRError,
    artifact_sha256,
    build_docx_template_ir,
    validate_docx_template,
    validate_template_program,
)


DIFF_SCHEMA_VERSION = "template-diff-report-v3"
CANONICAL_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class TemplateCandidateError(ValueError):
    """表示模板候选不能由固定编译器安全、确定地重建。"""


def _safe_member_name(name: str) -> None:
    if not name or "\\" in name or name.startswith("/"):
        raise TemplateCandidateError(f"DOCX 包含不安全成员路径: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise TemplateCandidateError(f"DOCX 包含不安全成员路径: {name!r}")


def canonical_repack_docx(source: Path, output: Path) -> None:
    """按固定成员顺序、时间戳和压缩级别重建 OOXML，不执行宏或外部代码。"""
    validate_docx_template(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(source) as package:
            infos = package.infolist()
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise TemplateCandidateError("DOCX 包含重复 ZIP 成员")
            for info in infos:
                _safe_member_name(info.filename)
                if info.flag_bits & 0x1:
                    raise TemplateCandidateError("DOCX 容器已加密或受密码保护")
            material = [(info.filename, package.read(info.filename), info.is_dir()) for info in infos]
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as candidate:
            for name, data, is_dir in sorted(material, key=lambda value: value[0]):
                info = zipfile.ZipInfo(name, CANONICAL_ZIP_TIMESTAMP)
                info.create_system = 0
                info.compress_type = zipfile.ZIP_STORED if is_dir else zipfile.ZIP_DEFLATED
                info.external_attr = 0x10 if is_dir else 0
                candidate.writestr(info, data, compress_type=info.compress_type, compresslevel=9)
    except zipfile.BadZipFile as exc:
        raise TemplateCandidateError("DOCX 容器损坏") from exc
    validate_docx_template(output)


def _comparison(name: str, expected: Any, actual: Any) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if expected == actual else "FAIL", "expected": expected, "actual": actual}


def _media_contract(template_ir: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "part": value.get("part"),
            "visual_sha256": value.get("visual_sha256"),
            "content_type": value.get("content_type"),
            "relationship_ids": value.get("relationship_ids", []),
        }
        for value in template_ir.get("media", [])
    ]


def compare_template_irs(source_ir: dict[str, Any], candidate_ir: dict[str, Any], program: dict[str, Any]) -> dict[str, Any]:
    comparisons = [
        _comparison(
            "normalized_structure_sha256",
            source_ir["fingerprint"]["normalized_structure_sha256"],
            candidate_ir["fingerprint"]["normalized_structure_sha256"],
        ),
        _comparison(
            "visual_proxy_sha256",
            source_ir["fingerprint"]["visual_proxy_sha256"],
            candidate_ir["fingerprint"]["visual_proxy_sha256"],
        ),
        _comparison("structural_parts", source_ir["fingerprint"]["structural_parts"], candidate_ir["fingerprint"]["structural_parts"]),
        _comparison("page_contract", source_ir.get("page_contract"), candidate_ir.get("page_contract")),
        _comparison("styles", source_ir.get("styles"), candidate_ir.get("styles")),
        _comparison("numbering", source_ir.get("numbering"), candidate_ir.get("numbering")),
        _comparison("theme", source_ir.get("theme"), candidate_ir.get("theme")),
        _comparison("media_visuals", _media_contract(source_ir), _media_contract(candidate_ir)),
        _comparison("drawing", source_ir.get("drawing"), candidate_ir.get("drawing")),
        _comparison("unsupported_objects", source_ir.get("unsupported_objects"), candidate_ir.get("unsupported_objects")),
    ]
    invariant_operations = [value for value in program.get("operations", []) if value.get("op") == "validate_invariant"]
    for index, operation in enumerate(invariant_operations, 1):
        name = str(operation.get("invariant"))
        actual = candidate_ir.get("fingerprint", {}).get(name)
        comparisons.append(_comparison(f"dsl_invariant_{index}:{name}", operation.get("expected"), actual))
    findings = [
        {"code": "TEMPLATE_RECONSTRUCTION_DIFFERENCE", "comparison": value["name"]}
        for value in comparisons
        if value["status"] != "PASS"
    ]
    payload: dict[str, Any] = {
        "schema_version": DIFF_SCHEMA_VERSION,
        "artifact_type": "TemplateDiffReportV3",
        "generated_at": utc_now(),
        "status": "PASS" if not findings else "FAIL",
        "compiler": {"id": COMPILER_ID, "version": COMPILER_VERSION},
        "source": {
            "sha256": source_ir["source_template"]["sha256"],
            "ir_sha256": source_ir["ir_sha256"],
        },
        "candidate": {
            "sha256": candidate_ir["source_template"]["sha256"],
            "ir_sha256": candidate_ir["ir_sha256"],
        },
        "program_sha256": program["program_sha256"],
        "comparisons": comparisons,
        "findings": findings,
    }
    payload["diff_report_sha256"] = artifact_sha256(payload, "diff_report_sha256")
    return payload


def compile_template_candidate(
    template: Path,
    template_ir: dict[str, Any],
    program: dict[str, Any],
    candidate_output: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    validate_template_program(program)
    source_facts = validate_docx_template(template)
    if source_facts["sha256"] != template_ir.get("source_template", {}).get("sha256"):
        raise TemplateCandidateError("模板 SHA 与 Template IR 不一致")
    if program.get("template_sha256") != source_facts["sha256"]:
        raise TemplateCandidateError("Template Program 未绑定当前模板")
    if program.get("template_ir_sha256") != template_ir.get("ir_sha256"):
        raise TemplateCandidateError("Template Program 未绑定当前 Template IR")
    compiler = program.get("compiler", {})
    if compiler != {"id": COMPILER_ID, "version": COMPILER_VERSION}:
        raise TemplateCandidateError("Template Program 使用未知固定编译器")
    canonical_repack_docx(template, candidate_output)
    candidate_ir = build_docx_template_ir(candidate_output)
    diff_report = compare_template_irs(template_ir, candidate_ir, program)
    return candidate_ir, diff_report


def build_candidate_artifacts(
    template: Path,
    template_ir_path: Path,
    program_path: Path,
    candidate_output: Path,
    candidate_ir_output: Path,
    diff_output: Path,
) -> dict[str, Any]:
    import json

    try:
        template_ir = json.loads(template_ir_path.read_text(encoding="utf-8-sig"))
        program = json.loads(program_path.read_text(encoding="utf-8-sig"))
        candidate_ir, diff_report = compile_template_candidate(template, template_ir, program, candidate_output)
        write_json(candidate_ir_output, candidate_ir)
        write_json(diff_output, diff_report)
    except (OSError, ValueError, TemplateIRError, TemplateCandidateError) as exc:
        return result("BLOCKED", "template_candidate_v3", findings=[{"code": "TEMPLATE_CANDIDATE_COMPILE_FAILED", "message": str(exc)}])
    status = "PASS" if diff_report["status"] == "PASS" else "FAIL"
    return result(
        status,
        "template_candidate_v3",
        findings=deepcopy(diff_report["findings"]),
        candidate_output={"path": str(candidate_output), "sha256": sha256_file(candidate_output)},
        candidate_ir={"path": str(candidate_ir_output), "sha256": sha256_file(candidate_ir_output), "ir_sha256": candidate_ir["ir_sha256"]},
        diff_report={"path": str(diff_output), "sha256": sha256_file(diff_output), "artifact_sha256": diff_report["diff_report_sha256"]},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="用固定编译器生成未知 DOCX 模板的确定性重建候选和差异报告")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--template-ir", type=Path, required=True)
    parser.add_argument("--program", type=Path, required=True)
    parser.add_argument("--candidate-output", type=Path, required=True)
    parser.add_argument("--candidate-ir-output", type=Path, required=True)
    parser.add_argument("--diff-output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload = build_candidate_artifacts(
        args.template.resolve(),
        args.template_ir.resolve(),
        args.program.resolve(),
        args.candidate_output.resolve(),
        args.candidate_ir_output.resolve(),
        args.diff_output.resolve(),
    )
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
