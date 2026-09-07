from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any, Sequence

from pipeline_common import finish, read_json, result, sha256_file, write_json
from verify_release_matrix_v3 import verify_matrix


ROOT = Path(__file__).resolve().parents[1]
MODES = {"responsible", "blind"}
REQUIRED_FORMATS = {"DOC", "DOCX", "PPT", "PPTX", "PDF"}
REQUIRED_TARGETS = {"desktop", "mobile"}


def _resolve(base: Path, value: Any) -> Path:
    path = Path(str(value))
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _stage_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(stage.get("stage")): stage
        for stage in report.get("stages", [])
        if isinstance(stage, dict) and stage.get("stage")
    }


def _selected_cases(matrix: dict[str, Any], case_ids: Sequence[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    requested = [str(value).strip() for value in case_ids if str(value).strip()]
    if len(requested) != 5 or len(set(requested)) != 5:
        return [], [{"code": "GOLDEN_CASE_SELECTION_COUNT_INVALID", "expected": 5, "actual": requested}]
    by_id = {str(case.get("case_id")): case for case in matrix.get("cases", []) if isinstance(case, dict)}
    missing = [case_id for case_id in requested if case_id not in by_id]
    if missing:
        return [], [{"code": "GOLDEN_CASE_SELECTION_MISSING", "case_ids": missing}]
    selected = [by_id[case_id] for case_id in requested]
    formats = {str(case["source"]["format"]).upper() for case in selected}
    targets = {str(case["delivery_target"]).lower() for case in selected}
    packs = {f"{case['template_pack']['id']}@{case['template_pack']['version']}" for case in selected}
    if formats != REQUIRED_FORMATS:
        findings.append({"code": "GOLDEN_CASE_FORMAT_COVERAGE_INVALID", "expected": sorted(REQUIRED_FORMATS), "actual": sorted(formats)})
    if targets != REQUIRED_TARGETS:
        findings.append({"code": "GOLDEN_CASE_TARGET_COVERAGE_INVALID", "expected": sorted(REQUIRED_TARGETS), "actual": sorted(targets)})
    if len(packs) != 2:
        findings.append({"code": "GOLDEN_CASE_TEMPLATE_COVERAGE_INVALID", "expected_count": 2, "actual": sorted(packs)})
    return selected, findings


def _render_pages(
    *,
    case_id: str,
    report: dict[str, Any],
    report_base: Path,
    findings: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    stage = _stage_map(report).get("analyze_rendered_pages", {})
    engines: dict[str, list[dict[str, Any]]] = {}
    for engine in ("wps", "word"):
        details = stage.get(engine) if isinstance(stage.get(engine), dict) else {}
        raw_pages = details.get("pages") if isinstance(details.get("pages"), list) else []
        pages: list[dict[str, Any]] = []
        for index, item in enumerate(raw_pages, start=1):
            if not isinstance(item, dict) or item.get("page") != index or not item.get("png"):
                findings.append({"code": "GOLDEN_RENDER_PAGE_ENTRY_INVALID", "case_id": case_id, "engine": engine, "index": index})
                continue
            path = _resolve(report_base, item["png"])
            if not path.is_file():
                findings.append({"code": "GOLDEN_RENDER_PAGE_MISSING", "case_id": case_id, "engine": engine, "page": index, "path": str(path)})
                continue
            pages.append({"page": index, "path": str(path), "sha256": sha256_file(path), "uri": path.as_uri()})
        if details.get("page_count") != len(raw_pages) or not raw_pages:
            findings.append(
                {
                    "code": "GOLDEN_RENDER_PAGE_COUNT_INVALID",
                    "case_id": case_id,
                    "engine": engine,
                    "reported": details.get("page_count"),
                    "actual": len(raw_pages),
                }
            )
        engines[engine] = pages
    if len(engines["wps"]) != len(engines["word"]) or not engines["wps"]:
        findings.append(
            {
                "code": "GOLDEN_RENDER_ENGINE_PAGE_COUNT_MISMATCH",
                "case_id": case_id,
                "wps": len(engines["wps"]),
                "word": len(engines["word"]),
            }
        )
    return engines


def build_review_spec(matrix_path: Path, case_ids: Sequence[str], mode: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    if mode not in MODES:
        return None, [{"code": "GOLDEN_REVIEW_MODE_INVALID", "actual": mode}]
    try:
        matrix = read_json(matrix_path)
    except Exception as exc:
        return None, [{"code": "GOLDEN_MATRIX_INVALID", "path": str(matrix_path), "message": str(exc)}]
    if not isinstance(matrix, dict):
        return None, [{"code": "GOLDEN_MATRIX_INVALID", "path": str(matrix_path), "message": "matrix must be an object"}]
    verification = verify_matrix(matrix, base=matrix_path.resolve().parent)
    if verification.get("status") != "PASS":
        return None, [{"code": "GOLDEN_MATRIX_NOT_PASS", "verification": verification}]
    selected, selection_findings = _selected_cases(matrix, case_ids)
    findings.extend(selection_findings)
    cases: list[dict[str, Any]] = []
    for case in selected:
        case_id = str(case["case_id"])
        report_ref = case["artifacts"]["pipeline_report"]
        report_path = _resolve(matrix_path.resolve().parent, report_ref["path"])
        try:
            report = read_json(report_path)
        except Exception as exc:
            findings.append({"code": "GOLDEN_PIPELINE_REPORT_INVALID", "case_id": case_id, "path": str(report_path), "message": str(exc)})
            continue
        engines = _render_pages(case_id=case_id, report=report, report_base=report_path.parent, findings=findings)
        # 身份判断需要中性参照；模板的实际字节必须匹配本批矩阵，不能按品牌名称全局豁免。
        pack_dir = _resolve(matrix_path.resolve().parent, case["artifacts"]["template_program"]["path"]).parent
        try:
            config_path = pack_dir / "template.json"
            config = read_json(config_path) if config_path.is_file() else {"template": "template.docx"}
            template_path = _resolve(pack_dir, config["template"])
            template_sha = sha256_file(template_path)
            if template_sha != str(case["template_pack"]["template_sha256"]).upper():
                raise ValueError("template reference SHA does not match the matrix")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            findings.append({"code": "GOLDEN_TEMPLATE_REFERENCE_INVALID", "case_id": case_id, "message": str(exc)})
            continue
        source_path = _resolve(matrix_path.resolve().parent, case["source"]["path"])
        cases.append(
            {
                "id": case_id,
                "source_format": str(case["source"]["format"]).upper(),
                "template_pack": f"{case['template_pack']['id']}@{case['template_pack']['version']}",
                "delivery_target": str(case["delivery_target"]).lower(),
                "pipeline_report": {"path": str(report_path), "sha256": str(report_ref["sha256"]).upper()},
                "output_sha256": str(case["artifacts"]["output"]["sha256"]).upper(),
                "template_reference": {"path": str(template_path), "sha256": template_sha, "uri": template_path.as_uri()},
                "source_reference": {"path": str(source_path), "sha256": case["source"]["sha256"], "uri": source_path.as_uri()},
                "engines": engines,
            }
        )
    if findings:
        return None, findings
    spec = {
        "schema_version": "golden-visual-review-spec-v3",
        "mode": mode,
        "manifest_sha256": str(matrix["manifest"]["sha256"]).upper(),
        "matrix": {"path": str(matrix_path.resolve()), "sha256": sha256_file(matrix_path.resolve())},
        "cases": cases,
    }
    return spec, findings


def _page_pairs(case: dict[str, Any]) -> str:
    cards = []
    for wps, word in zip(case["engines"]["wps"], case["engines"]["word"], strict=True):
        page = int(wps["page"])
        if int(word["page"]) != page:
            raise ValueError("golden review requires matching engine page numbers")
        cards.append('<div class="page-pair">'
                     f'<h3>第 {page} 页 · 同页对照</h3>'
                     '<label class="zoom-control">两侧同步缩放 '
                     '<input type="range" class="pair-zoom" min="100" max="250" step="25" value="100">'
                     '<output>100%</output></label><div class="paired-pages">')
        for engine, item in (("wps", wps), ("word", word)):
            cards.append(
                '<label class="page-card">'
                f'<strong>{engine.upper()}</strong><div class="page-viewport">'
                f'<img src="{html.escape(item["uri"], quote=True)}" alt="{engine.upper()} 第 {page} 页">'
                '</div>'
                f'<span><input type="checkbox" class="page-check" data-engine="{engine}" data-page="{page}"> '
                f'确认已检查 {engine.upper()} 第 {page} 页</span>'
                '</label>'
            )
        cards.append('</div></div>')
    return "".join(cards)


def _identity_references(case: dict[str, Any]) -> str:
    links = []
    for key, label in (("template_reference", "目标模板原件"), ("source_reference", "来源原件")):
        ref = case.get(key)
        if isinstance(ref, dict) and ref.get("uri"):
            links.append(f'<a href="{html.escape(ref["uri"], quote=True)}" target="_blank" rel="noopener">{label}</a>')
    return ('<aside class="identity-reference"><strong>身份归属核对</strong><p>'
            + (' · '.join(links) if links else '未提供身份参照，归属不明时请选择待核验。')
            + '</p><p>先记录可见标识，再对照目标模板与来源原件判断归属。'
              '目标模板原有页眉、页脚和背景身份须与对应位置核对；同名品牌出现在正文图片中不自动豁免。'
              '没有足够证据时标记待核验，不能仅凭品牌可见判定残留或清理完成。</p></aside>')


def render_review_html(spec: dict[str, Any]) -> str:
    mode = str(spec["mode"])
    responsible = mode == "responsible"
    title = "V3 黄金案例负责人逐页验收" if responsible else "V3 黄金案例无上下文独立盲验"
    intro = (
        "逐页比较 WPS 与 Microsoft Word 渲染，确认缺图、遮挡、裁切、空白页、异常留白、孤立标题、表格断裂和模板漂移。"
        if responsible
        else "依据当前冻结页面及中性的目标模板、来源参照逐页检查，不读取负责人结论、实现、修复历史或其他审阅结论。"
    )
    sections = []
    for index, case in enumerate(spec["cases"], start=1):
        label = f"案例 {index}" if not responsible else f"{case['id']} · {case['source_format']} → {case['delivery_target']} · {case['template_pack']}"
        blind_fields = "" if responsible else (
            '<label>来源身份核对<select class="brand-assessment"><option value="">请选择</option>'
            '<option value="clear">已核对，未发现应清除的来源身份</option><option value="found">确认发现来源身份残留</option>'
            '<option value="unknown">品牌可见，归属待核验</option></select></label>'
            '<label>双引擎差异<select class="engine-assessment"><option value="">请选择</option>'
            '<option value="clear">已逐页对照，无实质差异</option><option value="found">存在实质差异</option>'
            '<option value="unknown">差异是否可接受仍待核验</option></select></label>'
        )
        sections.append(
            f'<section class="case" data-case-id="{html.escape(case["id"], quote=True)}" '
            f'data-pipeline-sha="{case["pipeline_report"]["sha256"]}">'
            f'<h2>{html.escape(label)}</h2>'
            + _identity_references(case)
            + _page_pairs(case)
            + '<div class="decision">'
            + blind_fields
            + '<label>本案例结论<select class="case-decision"><option value="">请选择</option><option value="PASS">通过</option><option value="FAIL">不通过</option><option value="HUMAN_REVIEW">待核验，保持交付暂停</option></select></label>'
            + '<label>问题或待核验项（每行记录引擎、页码、区域、可见事实；推测单独注明）<textarea class="case-findings" rows="3"></textarea></label>'
            + ('<label>负责人备注<textarea class="case-notes" rows="2"></textarea></label>' if responsible else '')
            + '</div></section>'
        )
    spec_json = json.dumps(
        {
            "mode": mode,
            "manifest_sha256": spec["manifest_sha256"],
            "matrix_sha256": spec["matrix"]["sha256"],
            "cases": [
                {
                    "id": case["id"],
                    "pipeline_report_sha256": case["pipeline_report"]["sha256"],
                    "expected_pages_per_engine": len(case["engines"]["wps"]),
                }
                for case in spec["cases"]
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{margin:0;background:#eef1f5;color:#18212b;font-family:"Microsoft YaHei",Arial,sans-serif}}main{{max-width:1480px;margin:auto;padding:24px}}
.hero,.case{{background:#fff;border-radius:14px;box-shadow:0 4px 18px #25364a18;padding:24px;margin-bottom:22px}}h1{{margin:0 0 10px}}h2{{border-bottom:2px solid #d9e2ec;padding-bottom:12px}}
.reviewer{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:18px}}label{{display:block;font-weight:600}}input[type=text],select,textarea{{box-sizing:border-box;width:100%;margin-top:7px;padding:10px;border:1px solid #aebdca;border-radius:7px;font:inherit}}
.page-pair{{margin:24px 0}}.paired-pages{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}}.page-card{{border:1px solid #cbd5df;border-radius:10px;padding:10px;background:#fafbfd}}
.page-viewport{{overflow:auto;max-height:85vh;margin:10px 0}}.page-card img{{display:block;width:var(--page-zoom,100%);max-width:none;height:auto;background:#fff;border:1px solid #e1e7ed}}.zoom-control{{display:flex;align-items:center;gap:12px;margin-bottom:12px}}.decision{{display:grid;gap:12px;background:#f5f8fb;border-radius:10px;padding:16px}}
.flag{{font-weight:500}}button{{background:#155eef;color:#fff;border:0;border-radius:8px;padding:13px 24px;font:700 16px inherit;cursor:pointer}}button:hover{{background:#0d47bf}}
.notice{{color:#7a2e0b;background:#fff3e8;border-left:4px solid #e36b21;padding:12px 14px}}@media(max-width:700px){{.reviewer{{grid-template-columns:1fr}}main{{padding:10px}}}}
</style></head><body><main><header class="hero"><h1>{title}</h1><p>{intro}</p>
<p class="notice">页面不会预选“通过”。必须填写审核人身份、勾选每一页并为每个案例作出结论后，才能导出回执。</p>
<div class="reviewer"><label>审核人 ID<input id="reviewer-id" type="text" autocomplete="off"></label><label>审核人角色<input id="reviewer-role" type="text" autocomplete="off"></label></div></header>
{''.join(sections)}<section class="hero"><button id="export">导出绑定当前证据的审核回执 JSON</button></section></main>
<script>const SPEC={spec_json};
const lines=v=>v.split(/\\r?\\n/).map(x=>x.trim()).filter(Boolean);
document.querySelectorAll('.page-pair').forEach(pair=>{{
 const slider=pair.querySelector('.pair-zoom'),views=[...pair.querySelectorAll('.page-viewport')];
 slider.addEventListener('input',()=>{{pair.style.setProperty('--page-zoom',slider.value+'%');pair.querySelector('output').textContent=slider.value+'%';}});
 views.forEach(view=>view.addEventListener('scroll',()=>{{
  const peer=views.find(other=>other!==view);
  for(const [position,extent,client] of [['scrollLeft','scrollWidth','clientWidth'],['scrollTop','scrollHeight','clientHeight']]){{
   const available=view[extent]-view[client],target=available>0?view[position]/available*(peer[extent]-peer[client]):0;
   if(Math.abs(peer[position]-target)>1)peer[position]=target;
  }}
 }}));
}});
document.getElementById('export').addEventListener('click',()=>{{
 const reviewerId=document.getElementById('reviewer-id').value.trim(), reviewerRole=document.getElementById('reviewer-role').value.trim();
 if(!reviewerId||!reviewerRole){{alert('请填写审核人 ID 和角色。');return;}}
 const rows=[...document.querySelectorAll('.case')], cases=[]; let allPass=true,totalExpected=0,totalReviewed=0;
 for(const row of rows){{const id=row.dataset.caseId, checks=[...row.querySelectorAll('.page-check')];
  if(checks.some(x=>!x.checked)){{alert('案例 '+id+' 仍有页面未勾选。');return;}}
  const selected=row.querySelector('.case-decision').value;if(!selected){{alert('请选择案例 '+id+' 的结论。');return;}}
  let decision=selected==='HUMAN_REVIEW'?'FAIL':selected;
  const findings=lines(row.querySelector('.case-findings').value);const wps=checks.filter(x=>x.dataset.engine==='wps').map(x=>Number(x.dataset.page));const word=checks.filter(x=>x.dataset.engine==='word').map(x=>Number(x.dataset.page));
  if(selected==='HUMAN_REVIEW')findings.push('待核验：本案例尚不能形成通过结论。');
  if(selected!=='PASS'&&!row.querySelector('.case-findings').value.trim()){{alert('请为案例 '+id+' 记录页码、区域和问题或待核验事项。');return;}}
  totalExpected+=wps.length+word.length;totalReviewed+=wps.length+word.length;if(decision!=='PASS'||findings.length)allPass=false;
  if(SPEC.mode==='responsible'){{cases.push({{id,pipeline_report_sha256:row.dataset.pipelineSha,wps_pages_reviewed:wps,word_pages_reviewed:word,expected_pages_per_engine:wps.length,findings,decision,notes:row.querySelector('.case-notes').value.trim()}});}}
  else{{const identity=row.querySelector('.brand-assessment').value,engine=row.querySelector('.engine-assessment').value;
   if(!identity||!engine){{alert('请完成案例 '+id+' 的身份与双引擎核对。');return;}}
   const brand=identity==='found',diff=engine==='found';
   if(identity==='unknown')findings.push('待核验：可见品牌的身份归属尚未确定。');
   if(engine==='unknown')findings.push('待核验：双引擎差异尚未裁决。');
   if(identity!=='clear'||engine!=='clear'||findings.length){{allPass=false;decision='FAIL';}}
   cases.push({{id,wps_pages_reviewed:wps,word_pages_reviewed:word,pages_expected:wps.length+word.length,pages_reviewed:wps.length+word.length,complete:true,source_brand_residual_found:brand,substantive_engine_difference_found:diff,findings,decision}});}}
 }}
 const common={{status:allPass?'PASS':'FAIL',reviewer_id:reviewerId,reviewer_role:reviewerRole,reviewed_at:new Date().toISOString(),manifest_sha256:SPEC.manifest_sha256,matrix_sha256:SPEC.matrix_sha256,cases}};
 const payload=SPEC.mode==='responsible'?{{schema_version:'responsible-visual-review-v3',stage:'responsible_engineer_visual_review',...common,coverage:{{output_pages_reviewed:totalReviewed,output_pages_expected:totalExpected,complete:totalReviewed===totalExpected}},release_decision:allPass?'PROCEED_TO_INDEPENDENT_BLIND_VALIDATION':'RELEASE_HOLD'}}:{{schema_version:'independent-blind-visual-review-v3',stage:'independent_blind_visual_review',...common,total_pages_expected:totalExpected,total_pages_reviewed:totalReviewed,complete:totalReviewed===totalExpected,findings:[],decision:allPass?'PASS':'FAIL'}};
 const blob=new Blob([JSON.stringify(payload,null,2)+'\\n'],{{type:'application/json'}}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=SPEC.mode==='responsible'?'responsible-engineer-visual-review-v3.json':'independent-blind-visual-review-v3.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);
}});</script></body></html>"""


def prepare_review(matrix_path: Path, case_ids: Sequence[str], mode: str, output_dir: Path) -> dict[str, Any]:
    spec, findings = build_review_spec(matrix_path.resolve(), case_ids, mode)
    if spec is None or findings:
        return result("FAIL", "prepare_golden_visual_review_v3", findings=findings)
    output_dir.mkdir(parents=True, exist_ok=True)
    spec_path = output_dir / f"{mode}-review-spec.json"
    html_path = output_dir / f"{mode}-review.html"
    write_json(spec_path, spec)
    html_path.write_text(render_review_html(spec), encoding="utf-8")
    return result(
        "PASS",
        "prepare_golden_visual_review_v3",
        findings=[],
        mode=mode,
        case_count=len(spec["cases"]),
        manifest_sha256=spec["manifest_sha256"],
        matrix_sha256=spec["matrix"]["sha256"],
        review_spec={"path": str(spec_path.resolve()), "sha256": sha256_file(spec_path)},
        review_html={"path": str(html_path.resolve()), "sha256": sha256_file(html_path)},
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 V3 正式矩阵生成负责人或独立盲验的离线逐页 HTML")
    parser.add_argument("--matrix", type=Path, required=True, help="release-matrix-evidence-v3.json")
    parser.add_argument("--case-id", action="append", required=True, help="黄金案例 ID，必须恰好重复 5 次")
    parser.add_argument("--mode", choices=sorted(MODES), required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    payload = prepare_review(args.matrix, args.case_id, args.mode, args.output_dir)
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
