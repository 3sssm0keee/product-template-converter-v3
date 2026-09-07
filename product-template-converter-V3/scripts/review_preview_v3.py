from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from docx import Document
from docx.text.paragraph import Paragraph


PREVIEW_SCHEMA_VERSION = "document-preview-v3"
NON_RENDER_ACTIONS = {"remove_identity", "remove_template_background", "exclude_from_output"}
SECTION_PATTERN = re.compile(r"^([一二三四五六七八九十]+、[^\s]+)")
STYLE_HINT_PATTERN = re.compile(r"\s*(?:主标题|副标题|正文)\s*微软雅黑.*$", re.IGNORECASE)
CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DEFAULT_CHAPTERS = (
    "一、产品简介",
    "二、功能介绍",
    "三、产品优势",
    "四、应用场景",
    "五、技术参数",
    "六、产品资质",
)
SLOT_DISPLAY_TITLES = {
    "cover.title": "产品名称",
    "cover.model": "产品型号",
    "body.overview": "一、产品简介",
    "body.overview.images": "一、产品简介",
    "body.features": "二、功能介绍",
    "body.features.images": "二、功能介绍",
    "body.advantages": "三、产品优势",
    "body.advantages.images": "三、产品优势",
    "body.scenarios": "四、应用场景",
    "body.scenarios.images": "四、应用场景",
    "parameters.text": "五、技术参数",
    "parameters.table": "五、技术参数",
    "parameters.images": "五、技术参数",
    "body.qualifications": "六、产品资质",
    "body.qualifications.images": "六、产品资质",
    "body.supplement": "补充资料",
    "body.supplement.images": "补充资料",
    "section.introduction": "一、产品简介",
    "section.functions": "二、功能介绍",
    "section.advantages": "三、产品优势",
    "section.scenarios": "四、应用场景",
    "section.parameters": "五、技术参数",
    "section.qualifications": "六、产品资质",
    "cover.primary_image": "封面主图",
    "closing.fixed_background": "固定封底",
}


def normalize_preview_text(value: Any, *, maximum: int = 1200) -> str:
    text = CONTROL_PATTERN.sub("", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    compact: list[str] = []
    for line in lines:
        if line or (compact and compact[-1]):
            compact.append(line)
    while compact and not compact[-1]:
        compact.pop()
    rendered = "\n".join(compact).strip()
    return rendered if len(rendered) <= maximum else rendered[: maximum - 15].rstrip() + "……（完整内容见证据）"


def _paragraphs(template: Path) -> list[tuple[int, str]]:
    document = Document(template)
    return [
        (index, normalize_preview_text(Paragraph(element, document._body).text))
        for index, element in enumerate(document._element.xpath(".//w:p"), 1)
    ]


def _business_placeholder(raw: str, section_title: str, order: int) -> tuple[str, str, str]:
    clean = STYLE_HINT_PATTERN.sub("", normalize_preview_text(raw)).strip()
    if "型号" in clean and "产品名称" in clean:
        return "产品型号与名称", "【产品型号】　【产品名称】", "替换模板中的型号和产品名称占位文字"
    if "产品示例图片" in clean:
        return "产品主图", "【审核后的产品主图】", "使用已绑定证据的产品图片替换示例图片"
    if clean.startswith("产品名称："):
        return "资质产品名称", "产品名称：【与正文一致的审核值】", "从已审核产品信息填充"
    if clean.startswith("产品型号："):
        return "资质产品型号", "产品型号：【与封面一致的审核值】", "从已审核产品信息填充"
    if clean.startswith("证书编号："):
        return "证书编号", "证书编号：【仅填入有证据的编号】", "无证据时留待事实复核"
    placeholder_only = not clean or not re.sub(r"[XxＸ▎|\s]", "", clean)
    if placeholder_only or clean in {"XXX", "XX"}:
        if raw.lstrip().startswith("▎") or order % 2 == 1 and section_title in {"三、产品优势", "四、应用场景"}:
            return "内容小标题", "【经审核的小标题】", "去除 XX/XXX 技术占位符，显示业务含义"
        return "正文内容", f"【{section_title.split('、', 1)[-1]}的审核正文】", "去除 XX/XXX 技术占位符，填入审核内容"
    return "模板文字", clean or "【待审核内容】", "保留可读业务文字，隐藏字体和颜色代码"


def _block(
    *,
    kind: str,
    label: str,
    preview_text: str,
    source_rule: str,
    transformation: str,
    growth: str,
    slot_ids: list[str] | None = None,
    table_rows: list[list[str]] | None = None,
    image: dict[str, Any] | None = None,
) -> dict[str, Any]:
    block = {
        "kind": kind,
        "label": label,
        "preview_text": normalize_preview_text(preview_text),
        "source_rule": source_rule,
        "transformation": transformation,
        "growth": growth,
        "slot_ids": slot_ids or [],
        "table_rows": table_rows or [],
    }
    if image:
        block["image"] = image
    return block


def build_template_document_preview(
    template: Path,
    slot_contract: dict[str, Any],
    program: dict[str, Any],
) -> dict[str, Any]:
    paragraphs = _paragraphs(template)
    text_by_index = dict(paragraphs)
    headings = [(index, match.group(1)) for index, text in paragraphs if (match := SECTION_PATTERN.match(text))]
    slots = [value for value in slot_contract.get("slots", []) if isinstance(value, dict)]
    slot_by_index: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for slot in slots:
        location = str(slot.get("location") or "")
        match = re.search(r"#paragraph:(\d+)$", location)
        if match:
            slot_by_index[int(match.group(1))].append(slot)

    cover_blocks = [
        _block(
            kind="title",
            label="封面标题",
            preview_text="【产品型号】　【产品名称】",
            source_rule="来自审核后的产品型号和完整名称",
            transformation="替换封面技术占位字符串，不显示内部槽位 ID",
            growth="优先单行；超长时进入版式复核",
            slot_ids=[value.get("slot_id") for value in slot_by_index.get(8, []) if value.get("slot_id")],
        ),
        _block(
            kind="image",
            label="产品主图",
            preview_text="【审核后的产品主图将在此处按比例显示】",
            source_rule="只接受带本地路径和 SHA-256 的已审核图片",
            transformation="示例图片不会进入最终成品",
            growth="保持模板图片区边界；未知裁剪几何时停止",
        ),
    ]

    article_sections: list[dict[str, Any]] = []
    for heading_index, (paragraph_index, title) in enumerate(headings):
        next_index = headings[heading_index + 1][0] if heading_index + 1 < len(headings) else 10**9
        section_slots = [
            (index, slot)
            for index, values in slot_by_index.items()
            if paragraph_index < index < next_index
            for slot in values
        ]
        blocks: list[dict[str, Any]] = []
        if "技术参数" in title:
            blocks.append(_block(
                kind="table",
                label="技术参数表",
                preview_text="参数将按“序号 / 技术项目 / 技术参数”组织",
                source_rule="只使用已审核的参数名称、数值和单位",
                transformation="合并连续参数项并使用固定列宽；不凭空补齐参数",
                growth="表格向下增加行，允许跨页但不得截断",
                table_rows=[["序号", "技术项目", "技术参数"], ["1", "【参数名称】", "【审核值与单位】"]],
            ))
        elif "产品资质" in title:
            blocks.extend([
                _block(
                    kind="fact",
                    label="资质事实",
                    preview_text="产品名称：【审核值】\n产品型号：【审核值】\n证书编号：【有证据时填写】\n检验机构：【有证据时填写】\n报告日期：【有证据时填写】",
                    source_rule="来自证书、检测报告或负责人确认的事实证据",
                    transformation="厂家名称和无证据事实不会自动带入",
                    growth="字段纵向排列；证书图片在下方按比例追加",
                    slot_ids=[slot.get("slot_id") for _index, slot in section_slots if slot.get("slot_id")],
                ),
                _block(
                    kind="image",
                    label="资质图片",
                    preview_text="【审核后的证书或检测报告图片】",
                    source_rule="使用已通过事实和身份复核的图片",
                    transformation="保留可读区域，必要时先脱敏",
                    growth="图片纵向追加，禁止覆盖固定页脚",
                ),
            ])
        else:
            for order, (slot_index, slot) in enumerate(sorted(section_slots), 1):
                raw = text_by_index.get(slot_index, "")
                label, preview_text, transformation = _business_placeholder(raw, title, order)
                blocks.append(_block(
                    kind="subtitle" if "小标题" in label else "text",
                    label=label,
                    preview_text=preview_text,
                    source_rule="按 ContentDecisionV3 的目标章节和顺序填入已审核内容",
                    transformation=transformation,
                    growth="正文向下增长；超过页面容量时自然分页",
                    slot_ids=[str(slot.get("slot_id"))] if slot.get("slot_id") else [],
                ))
            if not blocks:
                blocks.append(_block(
                    kind="text",
                    label="章节正文",
                    preview_text=f"【{title.split('、', 1)[-1]}的审核内容】",
                    source_rule="按目标章节汇总已审核来源内容",
                    transformation="清理控制字符和技术占位说明，不改写事实",
                    growth="正文向下增长并自然分页",
                ))
        article_sections.append({
            "title": title,
            "purpose": f"承载{title.split('、', 1)[-1]}相关的已审核内容",
            "layout": "沿用模板标题样式和页面边距",
            "blocks": blocks,
        })

    retained_parts = [value for value in program.get("operations", []) if isinstance(value, dict) and value.get("op") == "retain_part"]
    return {
        "schema_version": PREVIEW_SCHEMA_VERSION,
        "preview_kind": "template_structure",
        "title": "拟生成的产品介绍 DOCX",
        "subtitle": "这是给业务审核人员看的内容结构预览；方括号内容表示将由已审核资料填充。",
        "format": "DOCX",
        "summary": f"预计保留模板视觉资产，形成封面、{len(article_sections)} 个正文篇章和固定封底。检测到 {len(slots)} 个待确认内容槽位。",
        "string_rules": [
            {"label": "技术占位符", "before": "XX / XXX / 内部槽位 ID", "after": "可读的业务占位说明", "reason": "普通用户不需要理解实现代码"},
            {"label": "样式说明", "before": "微软雅黑字号、颜色代码等模板文字", "after": "由模板样式执行，不进入成品正文", "reason": "避免把排版指令当作文章内容"},
            {"label": "事实文本", "before": "型号、数字、单位、证书与资质", "after": "仅展示有证据且已人工确认的值", "reason": "保持事实准确性"},
        ],
        "pages": [
            {
                "page_role": "cover",
                "title": "封面",
                "badge": "固定视觉 + 审核内容",
                "summary": "保留封面背景和品牌化版式，只替换产品型号、名称和主图。",
                "sections": [{"title": "封面内容", "purpose": "让读者快速识别产品", "layout": "沿用模板封面几何", "blocks": cover_blocks}],
            },
            {
                "page_role": "body",
                "title": "正文文章结构",
                "badge": "按内容自然分页",
                "summary": "以下顺序就是最终 DOCX 的业务文章结构，具体文字来自后续 ContentDecisionV3。",
                "sections": article_sections,
            },
            {
                "page_role": "back_cover",
                "title": "封底",
                "badge": "固定资产",
                "summary": "封底固定背景、媒体和页脚按模板原样保留，不接受模型自由生成。",
                "sections": [{
                    "title": "固定封底",
                    "purpose": "保持模板完整性",
                    "layout": "原样保留",
                    "blocks": [_block(
                        kind="fixed",
                        label="固定模板资产",
                        preview_text="【封底视觉按原模板保留】",
                        source_rule=f"由固定编译器保留 {len(retained_parts)} 个显式声明部件及模板其他安全部件",
                        transformation="不生成新的品牌、地址或联系方式",
                        growth="不可增长",
                    )],
                }],
            },
        ],
    }


def _source_items(source_document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    candidates = source_document.get("items")
    if not isinstance(candidates, list) and isinstance(source_document.get("content"), dict):
        candidates = source_document["content"].get("items")
    return {
        str(value.get("source_id") or value.get("id") or ""): value
        for value in candidates or []
        if isinstance(value, dict) and (value.get("source_id") or value.get("id"))
    }


def _image_material(item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    image = {
        "source_id": str(item.get("source_id") or item.get("id") or ""),
        "name": normalize_preview_text(payload.get("name") or item.get("source_id")),
        "package_path": str(item.get("location") or ""),
    }
    for key in ("sha256", "visual_sha256"):
        value = str(payload.get(key) or "").strip().upper()
        if value:
            image[key] = value
    return image


def _source_material(item: dict[str, Any]) -> tuple[str, str, list[list[str]], dict[str, Any] | None]:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    payload_type = str(payload.get("type") or "text")
    if payload_type == "table":
        rows = [[normalize_preview_text(cell, maximum=240) for cell in row] for row in payload.get("rows", []) if isinstance(row, list)]
        return "table", "表格内容见下方预览", rows[:20], None
    if payload_type == "asset":
        return "image", f"【图片：{normalize_preview_text(payload.get('name') or item.get('source_id'))}】", [], _image_material(item, payload)
    text = payload.get("text") or payload.get("description") or item.get("text") or ""
    return "text", normalize_preview_text(text), [], None


def _location_label(item: dict[str, Any]) -> str:
    location = str(item.get("location") or "")
    paragraph = re.search(r"paragraph:(\d+)", location)
    if paragraph:
        return f"原文件第 {paragraph.group(1)} 段"
    slide = re.search(r"slide:(\d+)", location)
    if slide:
        return f"原文件第 {slide.group(1)} 页幻灯片"
    page = re.search(r"page:(\d+)", location)
    if page:
        return f"原文件第 {page.group(1)} 页"
    return "已绑定的原文件证据位置"


def build_content_document_preview(
    source_document: dict[str, Any],
    decision: dict[str, Any],
    template_pack: dict[str, Any],
) -> dict[str, Any]:
    by_id = _source_items(source_document)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cover_blocks: list[dict[str, Any]] = []
    excluded_by_action: dict[str, list[str]] = defaultdict(list)
    action_labels = {
        "preserve_exact": "原文保留，仅清理不可见控制字符和换行",
        "preserve_image": "保留审核图片并验证路径和哈希",
        "preserve_sanitized_image": "使用已完成人工脱敏的替代图片",
        "redact_identity": "按审核结果移除厂家身份文字",
        "reviewed_text": "使用人工校对后的文字，不使用模型草稿",
        "remove_identity": "从成品中删除厂家身份内容",
        "remove_template_background": "删除来源文件的重复背景，不影响目标模板背景",
        "exclude_from_output": "按审核选择不写入成品，原始源项与证据保留",
    }
    for value in decision.get("decisions", []):
        if not isinstance(value, dict):
            continue
        source_id = str(value.get("source_id") or "")
        action = str(value.get("action") or "")
        if action in NON_RENDER_ACTIONS:
            excluded_by_action[action].append(_location_label(by_id.get(source_id, {})))
            continue
        source = by_id.get(source_id, {})
        target_slot = str(value.get("target_slot") or value.get("target_section") or "未指定章节")
        target = SLOT_DISPLAY_TITLES.get(target_slot, target_slot)
        if target_slot in {"product.model", "product.full_name", "closing.fixed_background"}:
            # Product metadata is already shown in the cover title, while the
            # closing background comes from the approved target template.
            continue
        reviewed_blocks = value.get("reviewed_blocks")
        if isinstance(reviewed_blocks, list):
            for block_index, reviewed in enumerate(reviewed_blocks, 1):
                if not isinstance(reviewed, dict):
                    continue
                block_slot = str(reviewed.get("target_slot") or target_slot)
                block_target = SLOT_DISPLAY_TITLES.get(block_slot, block_slot)
                grouped[block_target].append({
                    "order": int(reviewed.get("target_order") or value.get("target_order") or block_index),
                    "block": _block(
                        kind="text",
                        label="人工校对后的内容",
                        preview_text=reviewed.get("text") or "",
                        source_rule=_location_label(source),
                        transformation="使用人工拆分并确认后的文本块",
                        growth="按审核顺序向下排列",
                        slot_ids=[block_slot],
                    ),
                })
            continue
        kind, text, rows, image = _source_material(source)
        if isinstance(value.get("reviewed_text"), str):
            text = normalize_preview_text(value["reviewed_text"])
        rendered_block = _block(
            kind=kind,
            label="待写入内容",
            preview_text=text or "【该内容将在证据核验后显示】",
            source_rule=_location_label(source),
            transformation=action_labels.get(action, "按 ContentDecisionV3 的审核动作处理"),
            growth="按目标章节和 target_order 排列",
            slot_ids=[target_slot] if target_slot else [],
            table_rows=rows,
            image=image,
        )
        if target_slot == "cover.primary_image":
            rendered_block["label"] = "待确认的产品主图"
            cover_blocks.append(rendered_block)
            continue
        grouped[target].append({
            "order": int(value.get("target_order") or 0),
            "block": rendered_block,
        })

    excluded: list[str] = []
    for action, locations in sorted(excluded_by_action.items()):
        label = action_labels.get(action, "按审核要求不写入成品")
        if action == "remove_template_background":
            excluded.append(f"已识别并移除 {len(locations)} 个来源演示文稿的母版、版式或重复背景项；目标 DOCX 模板背景不受影响。")
        elif len(locations) <= 5:
            excluded.extend(f"{location}：{label}" for location in locations)
        else:
            examples = "、".join(locations[:3])
            excluded.append(f"{label}：共 {len(locations)} 项；示例位置为 {examples}。")

    configured_chapters = template_pack.get("invariants", {}).get("chapter_names", []) if isinstance(template_pack.get("invariants"), dict) else []
    chapters = [str(value) for value in configured_chapters if str(value)] or list(DEFAULT_CHAPTERS)
    section_titles = chapters + sorted(title for title in grouped if title not in chapters)
    sections = []
    populated_section_count = 0
    for title in section_titles:
        values = grouped.get(title, [])
        ordered = [value["block"] for value in sorted(values, key=lambda item: (item["order"], item["block"]["label"]))]
        if ordered:
            populated_section_count += 1
            for index, block in enumerate(ordered, 1):
                block["label"] = f"{'图片' if block['kind'] == 'image' else '表格' if block['kind'] == 'table' else '内容'} {index}"
        else:
            ordered = [_block(
                kind="warning",
                label="本次无待写入内容",
                preview_text="【当前来源资料没有分配到本章节的内容】",
                source_rule="当前 ContentDecisionV3",
                transformation="不让模型凭空补写；可由负责人补充资料或批准保持为空",
                growth="不产生额外页面",
            )]
        sections.append({
            "title": title,
            "purpose": "展示当前审核决策实际准备写入成品的内容",
            "layout": "沿用目标模板对应章节样式",
            "blocks": ordered,
        })
    product = decision.get("product") if isinstance(decision.get("product"), dict) else {}
    product_title = " ".join(str(product.get(key) or "").strip() for key in ("model", "full_name")).strip() or "待审核产品"
    pack_id = str(template_pack.get("id") or "未指定模板")
    return {
        "schema_version": PREVIEW_SCHEMA_VERSION,
        "preview_kind": "content_render",
        "title": f"{product_title} · 拟生成 DOCX 内容",
        "subtitle": "这是依据当前 ContentDecisionV3 生成的业务预览；只有本页审批通过后才会进入正式编译。",
        "format": "DOCX",
        "summary": f"目标模板 {pack_id}；封面含 {len(cover_blocks)} 个待确认主图，正文有 {sum(len(value) for value in grouped.values())} 个内容块进入 {populated_section_count} 个章节；空章节不凭空补写。",
        "string_rules": [
            {"label": "换行与控制字符", "before": "源文件中的混合换行、不可见控制字符", "after": "规范化为可读段落", "reason": "避免 DOCX 出现乱码和不可见字符"},
            {"label": "人工修订优先", "before": "模型草稿或未经确认的 OCR 文本", "after": "人工校对并确认的文字", "reason": "最终文字以人工审核结果为准"},
            {"label": "事实与身份", "before": "型号、数字、单位、厂家身份和资质", "after": "只保留证据充分且本页批准的内容", "reason": "保持质量和身份零命中门禁"},
        ],
        "pages": [
            {
                "page_role": "cover",
                "title": "封面",
                "badge": "产品信息预览",
                "summary": "沿用目标模板封面，只将审核后的产品名称、型号和图片写入对应位置。",
                "sections": [{
                    "title": "封面内容",
                    "purpose": "确认成品识别信息",
                    "layout": "沿用模板封面几何",
                    "blocks": [_block(
                        kind="title",
                        label="产品型号与名称",
                        preview_text=product_title,
                        source_rule="当前人工审核决策中的产品信息",
                        transformation="清理首尾空格，不改写型号",
                        growth="超长时进入版式复核",
                    ), *cover_blocks],
                }],
            },
            {
                "page_role": "body",
                "title": "拟写入的文章内容",
                "badge": "等待人工批准",
                "summary": "按最终六篇章顺序展示文字、表格和图片占位；没有来源内容的章节不会由模型凭空补写。",
                "sections": sections,
            },
            {
                "page_role": "back_cover",
                "title": "封底",
                "badge": "固定模板资产",
                "summary": "封底视觉按已批准模板保留，不从源文件复制厂家名称、地址或联系方式。",
                "sections": [{
                    "title": "固定封底",
                    "purpose": "保持模板完整性",
                    "layout": "原样保留",
                    "blocks": [_block(
                        kind="fixed",
                        label="封底视觉",
                        preview_text="【封底按目标模板保留】",
                        source_rule="已批准模板包",
                        transformation="不添加未经审核的身份信息",
                        growth="不可增长",
                    )],
                }],
            },
        ],
        "excluded_items": excluded,
    }
