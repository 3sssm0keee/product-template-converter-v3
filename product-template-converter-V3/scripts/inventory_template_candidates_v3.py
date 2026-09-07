from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from pipeline_common import finish, result, utc_now, write_json
from template_ir_v3 import TemplateIRError, build_docx_template_ir


def _known_templates(catalog: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not catalog.is_dir():
        return records
    for config_path in sorted(catalog.glob("*/template.json")):
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
            ir_path = config_path.parent / str(config["template_ir"])
            ir = json.loads(ir_path.read_text(encoding="utf-8-sig"))
            records.append(
                {
                    "id": str(config["id"]),
                    "version": str(config["version"]),
                    "reference": f"{config['id']}@{config['version']}",
                    "template_sha256": str(config["template_sha256"]),
                    "normalized_structure_sha256": ir["fingerprint"]["normalized_structure_sha256"],
                    "visual_proxy_sha256": ir["fingerprint"]["visual_proxy_sha256"],
                }
            )
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return records


def _classification(template_ir: dict[str, Any], known: list[dict[str, Any]]) -> tuple[str, list[str]]:
    sha256 = template_ir["source_template"]["sha256"]
    structure = template_ir["fingerprint"]["normalized_structure_sha256"]
    visual = template_ir["fingerprint"]["visual_proxy_sha256"]
    exact = [value["reference"] for value in known if value["template_sha256"] == sha256]
    if exact:
        return "EXACT_APPROVED_TEMPLATE", exact
    structural = [value["reference"] for value in known if value["normalized_structure_sha256"] == structure]
    if structural:
        return "STRUCTURAL_CANDIDATE_ONLY", structural
    visual_matches = [value["reference"] for value in known if value["visual_proxy_sha256"] == visual]
    if visual_matches:
        return "VISUAL_CANDIDATE_ONLY", visual_matches
    return "UNKNOWN_TEMPLATE_CANDIDATE", []


def inventory_template_candidates(sample_root: Path, catalog: Path) -> dict[str, Any]:
    sample_root = sample_root.resolve()
    catalog = catalog.resolve()
    if not sample_root.is_dir():
        return result("BLOCKED", "inventory_template_candidates_v3", findings=[{"code": "SAMPLE_ROOT_MISSING", "path": str(sample_root)}])
    known = _known_templates(catalog)
    cases: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for path in sorted(sample_root.rglob("*.docx"), key=lambda value: str(value).casefold()):
        if path.name.startswith("~$"):
            continue
        try:
            template_ir = build_docx_template_ir(path)
        except (TemplateIRError, OSError) as exc:
            findings.append({"code": "DOCX_TEMPLATE_INVENTORY_FAILED", "path": str(path.relative_to(sample_root)), "message": str(exc)})
            continue
        classification, matches = _classification(template_ir, known)
        cases.append(
            {
                "path": str(path.relative_to(sample_root)),
                "sha256": template_ir["source_template"]["sha256"],
                "normalized_structure_sha256": template_ir["fingerprint"]["normalized_structure_sha256"],
                "visual_proxy_sha256": template_ir["fingerprint"]["visual_proxy_sha256"],
                "section_count": len(template_ir.get("page_contract", {}).get("sections", [])),
                "media_count": len(template_ir.get("media", [])),
                "drawing": template_ir.get("drawing", {}),
                "detected_slot_count": len(template_ir.get("detected_slots", [])),
                "classification": classification,
                "matching_packs": matches,
                "requires_user_designation": classification != "EXACT_APPROVED_TEMPLATE",
            }
        )

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        grouped[(case["normalized_structure_sha256"], case["visual_proxy_sha256"])].append(case)
    clusters = []
    for index, ((structure, visual), members) in enumerate(sorted(grouped.items()), 1):
        classifications = sorted({value["classification"] for value in members})
        clusters.append(
            {
                "cluster_id": f"DOCX-CLUSTER-{index:03d}",
                "normalized_structure_sha256": structure,
                "visual_proxy_sha256": visual,
                "member_count": len(members),
                "classifications": classifications,
                "members": [value["path"] for value in members],
                "second_template_candidate": "UNKNOWN_TEMPLATE_CANDIDATE" in classifications,
                "approval_state": "USER_DESIGNATION_REQUIRED" if "EXACT_APPROVED_TEMPLATE" not in classifications else "KNOWN_PACK_PRESENT",
            }
        )
    second_candidates = [
        {
            "cluster_id": cluster["cluster_id"],
            "member_count": cluster["member_count"],
            "members": cluster["members"],
            "reason": "结构与视觉代理均未命中已批准模板；仅作为目标模板候选，不代表已获得用户指定或审批。",
        }
        for cluster in clusters
        if cluster["second_template_candidate"]
    ]
    status = "HUMAN_REVIEW" if second_candidates else ("FAIL" if findings else "PASS")
    if second_candidates:
        findings.append({"code": "SECOND_TEMPLATE_USER_DESIGNATION_REQUIRED", "candidate_cluster_count": len(second_candidates)})
    return {
        "status": status,
        "stage": "inventory_template_candidates_v3",
        "generated_at": utc_now(),
        "findings": findings,
        "sample_root": str(sample_root),
        "catalog": str(catalog),
        "known_templates": known,
        "docx_count": len(cases),
        "cluster_count": len(clusters),
        "cases": cases,
        "clusters": clusters,
        "second_template_candidates": second_candidates,
        "policy": {
            "auto_register": False,
            "auto_treat_finished_document_as_template": False,
            "requires_explicit_user_designation": True,
            "requires_hash_bound_human_receipt": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="盘点真实 DOCX 结构簇并列出第二目标模板候选，不自动注册")
    parser.add_argument("--sample-root", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, default=Path(__file__).resolve().parents[1] / "template_packs")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload = inventory_template_candidates(args.sample_root, args.catalog)
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
