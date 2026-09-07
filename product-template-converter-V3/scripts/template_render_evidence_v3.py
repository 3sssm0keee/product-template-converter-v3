from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from pipeline_common import finish, result, sha256_file, write_json
from review_preview_v3 import build_template_document_preview
from review_v3 import build_review_queue, write_review_html
from schema_validation_v3 import load_schema, validate_instance
from template_ir_v3 import artifact_sha256


ROOT = Path(__file__).resolve().parents[1]
DIFF_SCHEMA = ROOT / "references" / "schemas" / "template-diff-report-v3.schema.json"


class RenderEvidenceError(ValueError):
    pass


def _read_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderEvidenceError(f"无法读取 JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RenderEvidenceError(f"JSON 顶层必须是 object: {path}")
    return payload


def _inside(draft_dir: Path, path: Path, label: str) -> tuple[Path, str]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(draft_dir.resolve())
    except ValueError as exc:
        raise RenderEvidenceError(f"{label} 必须位于 onboarding 草案目录内: {resolved}") from exc
    if not resolved.is_file():
        raise RenderEvidenceError(f"{label} 不存在: {resolved}")
    return resolved, relative.as_posix()


def _engine(report: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [value for value in report.get("engines", []) if isinstance(value, dict) and value.get("engine") == name]
    if len(matches) != 1:
        raise RenderEvidenceError(f"导出报告必须恰好包含一个 {name} 引擎")
    value = matches[0]
    if value.get("status") != "PASS" or value.get("identity_verified") is not True or value.get("ownership_verified") is not True:
        raise RenderEvidenceError(f"{name} 引擎身份、进程所有权或导出状态未通过")
    cleanup = value.get("cleanup", {})
    if cleanup.get("exit_observed") is not True or cleanup.get("com_released") is not True:
        raise RenderEvidenceError(f"{name} 引擎 COM 清理证据未通过")
    return value


def _verify_visual_report(
    engine_name: str,
    visual: dict[str, Any],
    original_engine: dict[str, Any],
    candidate_engine: dict[str, Any],
) -> dict[str, Any]:
    if visual.get("status") != "PASS" or visual.get("findings"):
        raise RenderEvidenceError(f"{engine_name} 原模板与候选的逐页视觉比较未通过")
    if visual.get("original_pdf", {}).get("sha256") != original_engine.get("sha256"):
        raise RenderEvidenceError(f"{engine_name} 原模板 PDF 与视觉报告哈希不一致")
    if visual.get("normalized_pdf", {}).get("sha256") != candidate_engine.get("sha256"):
        raise RenderEvidenceError(f"{engine_name} 候选 PDF 与视觉报告哈希不一致")
    original_count = visual.get("original_page_count")
    candidate_count = visual.get("normalized_page_count")
    pages = visual.get("pages")
    if not isinstance(original_count, int) or original_count < 1 or original_count != candidate_count:
        raise RenderEvidenceError(f"{engine_name} 页面计数无效或发生变化")
    if not isinstance(pages, list) or len(pages) != original_count:
        raise RenderEvidenceError(f"{engine_name} 页面证据不完整")
    threshold = float(visual.get("mad_threshold", 0))
    differences = [float(value.get("mean_absolute_difference", threshold + 1)) for value in pages if isinstance(value, dict)]
    if len(differences) != original_count or any(value > threshold for value in differences):
        raise RenderEvidenceError(f"{engine_name} 页面差异超过阈值")
    return {
        "engine": engine_name,
        "original_page_count": original_count,
        "candidate_page_count": candidate_count,
        "max_mean_absolute_difference": max(differences, default=0.0),
    }


def _evidence_ref(evidence_id: str, label: str, kind: str, path: Path, draft_dir: Path) -> dict[str, Any]:
    resolved, relative = _inside(draft_dir, path, label)
    return {"evidence_id": evidence_id, "label": label, "kind": kind, "path": relative, "sha256": sha256_file(resolved)}


def attach_render_evidence(
    draft_dir: Path,
    original_export_report: Path,
    candidate_export_report: Path,
    wps_visual_report: Path,
    word_visual_report: Path,
) -> dict[str, Any]:
    draft_dir = draft_dir.resolve()
    try:
        original_path, original_relative = _inside(draft_dir, original_export_report, "原模板导出报告")
        candidate_path, candidate_relative = _inside(draft_dir, candidate_export_report, "候选导出报告")
        wps_path, wps_relative = _inside(draft_dir, wps_visual_report, "WPS 视觉报告")
        word_path, word_relative = _inside(draft_dir, word_visual_report, "Word 视觉报告")
        draft = _read_object(draft_dir / "draft.json")
        queue = _read_object(draft_dir / "review-queue.json")
        diff_report = _read_object(draft_dir / "diff-report.json")
        original_export = _read_object(original_path)
        candidate_export = _read_object(candidate_path)
        wps_visual = _read_object(wps_path)
        word_visual = _read_object(word_path)
        if original_export.get("status") != "PASS" or candidate_export.get("status") != "PASS":
            raise RenderEvidenceError("原模板或候选双引擎导出未通过")
        template_path, _ = _inside(draft_dir, draft_dir / draft["artifacts"]["template"], "原模板")
        candidate_docx, _ = _inside(draft_dir, draft_dir / draft["artifacts"]["candidate_output"], "候选 DOCX")
        if original_export.get("input") != str(template_path) or candidate_export.get("input") != str(candidate_docx):
            raise RenderEvidenceError("导出报告未绑定当前原模板或候选 DOCX")
        original_engines = {name: _engine(original_export, name) for name in ("wps", "word")}
        candidate_engines = {name: _engine(candidate_export, name) for name in ("wps", "word")}
        engine_summaries = [
            _verify_visual_report("wps", wps_visual, original_engines["wps"], candidate_engines["wps"]),
            _verify_visual_report("word", word_visual, original_engines["word"], candidate_engines["word"]),
        ]
    except (KeyError, RenderEvidenceError) as exc:
        return result("BLOCKED", "template_render_evidence_v3", findings=[{"code": "TEMPLATE_RENDER_EVIDENCE_INVALID", "message": str(exc)}])

    report_evidence = [
        {"kind": "original_export_report", "path": original_relative, "sha256": sha256_file(original_path)},
        {"kind": "candidate_export_report", "path": candidate_relative, "sha256": sha256_file(candidate_path)},
        {"kind": "wps_visual_diff", "path": wps_relative, "sha256": sha256_file(wps_path)},
        {"kind": "word_visual_diff", "path": word_relative, "sha256": sha256_file(word_path)},
    ]
    diff_report["render_validation"] = {"status": "PASS", "engines": engine_summaries, "evidence": report_evidence}
    diff_report["diff_report_sha256"] = artifact_sha256(diff_report, "diff_report_sha256")
    schema_errors = validate_instance(diff_report, load_schema(DIFF_SCHEMA))
    if schema_errors:
        return result("FAIL", "template_render_evidence_v3", findings=[{"code": "TEMPLATE_DIFF_SCHEMA_INVALID", "errors": schema_errors[:10]}])
    write_json(draft_dir / "diff-report.json", diff_report)

    added_evidence = [
        _evidence_ref("ORIGINAL-EXPORT-REPORT", "原模板 WPS/Word 导出报告", "office_export_report", original_path, draft_dir),
        _evidence_ref("CANDIDATE-EXPORT-REPORT", "候选 WPS/Word 导出报告", "office_export_report", candidate_path, draft_dir),
        _evidence_ref("WPS-VISUAL-REPORT", "WPS 原模板/候选逐页差异报告", "visual_diff_report", wps_path, draft_dir),
        _evidence_ref("WORD-VISUAL-REPORT", "Word 原模板/候选逐页差异报告", "visual_diff_report", word_path, draft_dir),
    ]
    preview_paths: list[str] = []
    overlay_paths: list[str] = []
    for engine_name, visual in (("WPS", wps_visual), ("WORD", word_visual)):
        for page in visual["pages"]:
            page_number = int(page["page"])
            for role, field, label in (
                ("ORIGINAL", "original_png", "原模板页"),
                ("CANDIDATE", "normalized_png", "候选页"),
                ("DIFF", "diff_overlay", "差异叠图"),
            ):
                evidence_id = f"{engine_name}-{role}-PAGE-{page_number:02d}"
                ref = _evidence_ref(evidence_id, f"{engine_name} {label} {page_number}", "rendered_page", Path(page[field]), draft_dir)
                added_evidence.append(ref)
                if role == "DIFF":
                    overlay_paths.append(ref["path"])
                else:
                    preview_paths.append(ref["path"])

    slot_contract = _read_object(draft_dir / draft["artifacts"]["slot_contract"])
    program = _read_object(draft_dir / draft["artifacts"]["program"])
    document_preview = build_template_document_preview(template_path, slot_contract, program)
    preview_path = draft_dir / str(draft.get("artifacts", {}).get("document_preview") or "document-preview-v3.json")
    write_json(preview_path, document_preview)

    existing_evidence = {value["evidence_id"]: value for value in queue.get("evidence_refs", [])}
    # 附加真实渲染后 diff-report 已改变，证据引用必须与新的绑定同时更新。
    existing_evidence["DIFF-REPORT"] = _evidence_ref(
        "DIFF-REPORT", "模板结构及渲染差异报告", "diff_report", draft_dir / "diff-report.json", draft_dir,
    )
    existing_evidence["DOCUMENT-PREVIEW"] = _evidence_ref(
        "DOCUMENT-PREVIEW",
        "面向业务用户的拟生成 DOCX 结构预览",
        "document_preview",
        preview_path,
        draft_dir,
    )
    for value in added_evidence:
        existing_evidence[value["evidence_id"]] = value
    items = deepcopy(queue["items"])
    page_summary = "；".join(f"{value['engine'].upper()} {value['candidate_page_count']} 页" for value in engine_summaries)
    for item in items:
        if item["review_item_id"] == "TEMPLATE-SEMANTIC-SLOTS":
            item["evidence_ids"] = list(dict.fromkeys(item["evidence_ids"] + ["DOCUMENT-PREVIEW"]))
        if item["review_item_id"] == "TEMPLATE-COMPILED-PREVIEW":
            item["evidence_ids"] = list(dict.fromkeys(item["evidence_ids"] + [value["evidence_id"] for value in added_evidence]))
            item["conclusion"] = f"WPS 与 Word 均已真实导出原模板和候选；{page_summary}，逐页机器差异均在阈值内，仍须人眼批准。"
            item["details"]["recognized_conclusion"] = "双引擎页面集合完整，逐页结构与视觉比较通过。"
            item["details"]["preview_paths"] = preview_paths
            item["details"]["diff_overlay_paths"] = overlay_paths
            item["details"]["notes"] = "机器差异 PASS 不替代人工确认；任何制品变化都会使本 Queue 与回执失效。"

    binding = deepcopy(queue["binding"])
    binding["diff_report_sha256"] = sha256_file(draft_dir / "diff-report.json")
    rebuilt_queue = build_review_queue(
        queue["review_type"],
        binding,
        items,
        evidence_refs=list(existing_evidence.values()),
        document_preview=document_preview,
        created_at=queue.get("created_at"),
    )
    write_json(draft_dir / "review-queue.json", rebuilt_queue)
    write_review_html(rebuilt_queue, draft_dir / "review.html")

    draft["artifacts"].update(
        {
            "document_preview": preview_path.relative_to(draft_dir).as_posix(),
            "render_original_report": original_relative,
            "render_candidate_report": candidate_relative,
            "visual_diff_wps_report": wps_relative,
            "visual_diff_word_report": word_relative,
        }
    )
    draft["review_id"] = rebuilt_queue["review_id"]
    draft["review_binding"] = rebuilt_queue["binding"]
    draft["validation"] = {
        "status": "PASS",
        "stage": "template_render_validation_v3",
        "open_findings": [],
        "engines": engine_summaries,
        "evidence": report_evidence,
    }
    draft["draft_sha256"] = artifact_sha256(draft, "draft_sha256")
    write_json(draft_dir / "draft.json", draft)
    return result(
        "HUMAN_REVIEW",
        "template_render_evidence_v3",
        findings=[{"code": "TEMPLATE_HUMAN_APPROVAL_REQUIRED", "review_id": rebuilt_queue["review_id"]}],
        validation=draft["validation"],
        review_queue=str(draft_dir / "review-queue.json"),
        review_html=str(draft_dir / "review.html"),
        review_id=rebuilt_queue["review_id"],
        diff_report_sha256=binding["diff_report_sha256"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="把 WPS/Word 原模板与候选逐页差异证据绑定到 onboarding Queue")
    parser.add_argument("--draft", type=Path, required=True)
    parser.add_argument("--original-export-report", type=Path, required=True)
    parser.add_argument("--candidate-export-report", type=Path, required=True)
    parser.add_argument("--wps-visual-report", type=Path, required=True)
    parser.add_argument("--word-visual-report", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload = attach_render_evidence(
        args.draft.resolve(),
        args.original_export_report.resolve(),
        args.candidate_export_report.resolve(),
        args.wps_visual_report.resolve(),
        args.word_visual_report.resolve(),
    )
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
