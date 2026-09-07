from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any, Sequence

from pipeline_common import finish, read_json, result, sha256_file, write_json
from schema_validation_v3 import load_schema, validate_instance


ROOT = Path(__file__).resolve().parents[1]
DESIGNATION_SCHEMA = ROOT / "references" / "schemas" / "template-designation-v3.schema.json"


def _candidate_payload(case: dict[str, Any]) -> dict[str, str]:
    return {
        "path": str(case["path"]),
        "sha256": str(case["sha256"]).upper(),
        "normalized_structure_sha256": str(case["normalized_structure_sha256"]).upper(),
        "visual_proxy_sha256": str(case["visual_proxy_sha256"]).upper(),
    }


def build_selector(inventory_path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    try:
        inventory = read_json(inventory_path)
    except Exception as exc:
        return None, [{"code": "TEMPLATE_CANDIDATE_INVENTORY_INVALID", "path": str(inventory_path), "message": str(exc)}]
    if not isinstance(inventory, dict) or not isinstance(inventory.get("cases"), list):
        return None, [{"code": "TEMPLATE_CANDIDATE_INVENTORY_INVALID", "path": str(inventory_path)}]
    sample_root = Path(str(inventory.get("sample_root") or "")).resolve()
    candidates = []
    for case in inventory["cases"]:
        if not isinstance(case, dict) or case.get("requires_user_designation") is not True:
            continue
        relative = Path(str(case.get("path") or ""))
        source = (sample_root / relative).resolve()
        if not source.is_file():
            findings.append({"code": "TEMPLATE_CANDIDATE_FILE_MISSING", "path": str(source)})
            continue
        actual_sha = sha256_file(source)
        if actual_sha != str(case.get("sha256") or "").upper():
            findings.append({"code": "TEMPLATE_CANDIDATE_FILE_SHA_MISMATCH", "path": str(source), "expected": case.get("sha256"), "actual": actual_sha})
            continue
        candidates.append(
            {
                **_candidate_payload(case),
                "absolute_path": str(source),
                "uri": source.as_uri(),
                "name": source.name,
                "section_count": int(case.get("section_count") or 0),
                "media_count": int(case.get("media_count") or 0),
                "drawing_count": int(case.get("drawing", {}).get("drawing_count") or 0),
                "floating_count": int(case.get("drawing", {}).get("floating_anchor_count") or 0),
                "slot_count": int(case.get("detected_slot_count") or 0),
            }
        )
    if not candidates:
        findings.append({"code": "TEMPLATE_CANDIDATE_SET_EMPTY"})
    return {
        "schema_version": "template-candidate-selector-v3",
        "inventory": {"path": str(inventory_path.resolve()), "sha256": sha256_file(inventory_path)},
        "sample_root": str(sample_root),
        "candidates": candidates,
    }, findings


def render_selector_html(selector: dict[str, Any]) -> str:
    cards = []
    for index, case in enumerate(selector["candidates"], start=1):
        data = html.escape(json.dumps(_candidate_payload(case), ensure_ascii=False, separators=(",", ":")), quote=True)
        cards.append(
            f'<label class="card"><input type="radio" name="candidate" value="{index - 1}" data-candidate="{data}">'
            f'<div><h2>{index}. {html.escape(case["name"])}</h2>'
            f'<p class="path">{html.escape(case["path"])}</p>'
            '<div class="metrics">'
            f'<span>分节 {case["section_count"]}</span><span>图片/媒体 {case["media_count"]}</span>'
            f'<span>绘图对象 {case["drawing_count"]}</span><span>浮动对象 {case["floating_count"]}</span><span>候选槽位 {case["slot_count"]}</span>'
            '</div>'
            f'<p><a href="{html.escape(case["uri"], quote=True)}">打开原 DOCX 查看实际版式</a></p>'
            '<details><summary>工程证据</summary>'
            f'<p>文件 SHA-256：{case["sha256"]}</p><p>结构指纹：{case["normalized_structure_sha256"]}</p><p>视觉代理：{case["visual_proxy_sha256"]}</p>'
            '</details></div></label>'
        )
    selector_json = json.dumps(
        {"inventory": selector["inventory"], "candidates": [_candidate_payload(case) for case in selector["candidates"]]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V3 第二目标模板选择</title><style>
body{{margin:0;background:#eef2f6;color:#1d2733;font-family:"Microsoft YaHei",Arial,sans-serif}}main{{max-width:1100px;margin:auto;padding:24px}}.panel,.card{{background:#fff;border-radius:14px;box-shadow:0 4px 16px #21334d18;padding:22px;margin-bottom:18px}}
.notice{{background:#fff4e8;border-left:5px solid #df6c20;padding:13px 16px}}.card{{display:grid;grid-template-columns:28px 1fr;gap:12px;cursor:pointer;border:2px solid transparent}}.card:has(input:checked){{border-color:#155eef;background:#f4f8ff}}
.card input{{margin-top:8px}}h1{{margin-top:0}}h2{{margin:0 0 8px;font-size:20px}}.path{{color:#526476;word-break:break-all}}.metrics{{display:flex;flex-wrap:wrap;gap:9px}}.metrics span{{background:#eaf0f6;border-radius:999px;padding:5px 10px}}
input[type=text],textarea{{box-sizing:border-box;width:100%;padding:10px;margin-top:6px;border:1px solid #aab8c5;border-radius:7px;font:inherit}}.reviewer{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}button{{background:#155eef;color:#fff;border:0;border-radius:8px;padding:13px 22px;font:700 16px inherit;cursor:pointer}}a{{color:#155eef}}
@media(max-width:700px){{main{{padding:10px}}.reviewer{{grid-template-columns:1fr}}}}
</style></head><body><main><section class="panel"><h1>V3 第二目标模板选择</h1>
<p>这里列出的是“结构与现有模板不同的 DOCX 文件”，不是系统认定的模板。很多文件可能只是已经生成的产品成品。只有当你明确希望以后反复复用它的页面、配色、封面、封底和槽位结构时，才选择它作为第二目标模板。</p>
<p class="notice">系统不会因为文件看起来像模板就自动注册。你也可以选择“以上都不是”，随后另行提供一份真实 DOCX 模板。</p>
<div class="reviewer"><label>指定人 ID<input id="reviewer-id" type="text"></label><label>指定人角色<input id="reviewer-role" type="text"></label></div></section>
{''.join(cards)}
<label class="card"><input type="radio" name="candidate" value="external"><div><h2>以上都不是</h2><p>我将另行提供一份结构明显不同的真实 DOCX 目标模板。</p></div></label>
<section class="panel"><label>补充说明<textarea id="comment" rows="3"></textarea></label><p><button id="export">导出模板指定回执 JSON</button></p></section></main>
<script>const DATA={selector_json};document.getElementById('export').addEventListener('click',()=>{{
const id=document.getElementById('reviewer-id').value.trim(),role=document.getElementById('reviewer-role').value.trim(),selected=document.querySelector('input[name=candidate]:checked');
if(!id||!role){{alert('请填写指定人 ID 和角色。');return}}if(!selected){{alert('请选择一个候选，或选择“以上都不是”。');return}}
const external=selected.value==='external';const payload={{schema_version:'template-designation-v3',action:external?'PROVIDE_EXTERNAL_TEMPLATE':'SELECT_CANDIDATE',reviewer:{{id,role}},reviewed_at:new Date().toISOString(),inventory:DATA.inventory,selected_candidate:external?null:DATA.candidates[Number(selected.value)],comment:document.getElementById('comment').value.trim()}};
const blob=new Blob([JSON.stringify(payload,null,2)+'\n'],{{type:'application/json'}}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='template-designation-v3.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);
}});</script></body></html>"""


def prepare_selector(inventory_path: Path, output_dir: Path) -> dict[str, Any]:
    selector, findings = build_selector(inventory_path.resolve())
    if selector is None or findings:
        return result("FAIL", "prepare_template_candidate_selector_v3", findings=findings)
    output_dir.mkdir(parents=True, exist_ok=True)
    data_path = output_dir / "selector-data.json"
    html_path = output_dir / "select-second-template.html"
    write_json(data_path, selector)
    html_path.write_text(render_selector_html(selector), encoding="utf-8")
    return result(
        "PASS",
        "prepare_template_candidate_selector_v3",
        findings=[],
        candidate_count=len(selector["candidates"]),
        selector_data={"path": str(data_path.resolve()), "sha256": sha256_file(data_path)},
        selector_html={"path": str(html_path.resolve()), "sha256": sha256_file(html_path)},
    )


def validate_designation(inventory_path: Path, designation_path: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    selector, selector_findings = build_selector(inventory_path.resolve())
    findings.extend(selector_findings)
    try:
        designation = read_json(designation_path)
    except Exception as exc:
        return result("FAIL", "validate_template_designation_v3", findings=[{"code": "TEMPLATE_DESIGNATION_INVALID", "message": str(exc)}])
    errors = validate_instance(designation, load_schema(DESIGNATION_SCHEMA)) if isinstance(designation, dict) else ["designation must be an object"]
    if errors:
        findings.append({"code": "TEMPLATE_DESIGNATION_SCHEMA_INVALID", "errors": errors})
    if selector is None:
        return result("FAIL", "validate_template_designation_v3", findings=findings)
    if designation.get("inventory") != selector["inventory"]:
        findings.append({"code": "TEMPLATE_DESIGNATION_INVENTORY_STALE"})
    action = designation.get("action")
    if action == "PROVIDE_EXTERNAL_TEMPLATE":
        return result(
            "HUMAN_REVIEW" if not findings else "FAIL",
            "validate_template_designation_v3",
            findings=findings or [{"code": "EXTERNAL_TEMPLATE_REQUIRED"}],
            action=action,
        )
    selected = designation.get("selected_candidate")
    candidates = {_candidate_payload(case)["sha256"]: case for case in selector["candidates"]}
    match = candidates.get(str(selected.get("sha256") or "").upper()) if isinstance(selected, dict) else None
    if match is None or _candidate_payload(match) != selected:
        findings.append({"code": "TEMPLATE_DESIGNATION_CANDIDATE_INVALID"})
    return result(
        "PASS" if not findings else "FAIL",
        "validate_template_designation_v3",
        findings=findings,
        action=action,
        template_path=match.get("absolute_path") if match and not findings else None,
        template_sha256=match.get("sha256") if match and not findings else None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成或校验 V3 第二目标模板的用户可读选择页")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--inventory", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--report", type=Path)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--inventory", type=Path, required=True)
    validate.add_argument("--designation", type=Path, required=True)
    validate.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    payload = prepare_selector(args.inventory, args.output_dir) if args.command == "prepare" else validate_designation(args.inventory, args.designation)
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
