# 内容映射文件

`content_map.json` 是人工分类与确定性脚本之间的唯一契约。

```json
{
  "schema_version": 1,
  "product": {"model": "ZXTY-型号", "full_name": "产品全称"},
  "manufacturer_terms": ["来源厂家名称", "来源域名", "来源电话"],
  "mappings": [
    {
      "source_id": "TEXT-稳定ID",
      "action": "reviewed_text",
      "reviewed_text": "PX1 手持式背散射检查仪",
      "target_section": "一、产品简介",
      "target_order": 1,
      "source_evidence": {
        "original_ocr_source_id": "TEXT-稳定ID",
        "page_or_position": "page 1 / upper half",
        "review_reason": "对照原图纠正 OCR 字符并合并误插入空格",
        "reviewer": {"id": "qa-reviewer-1", "role": "content reviewer"},
        "evidence_source_ids": ["TEXT-稳定ID"]
      },
      "notes": ""
    }
  ]
}
```

当源清单经过复核且确实没有来源厂家名称、Logo、联系方式、网址、二维码或品牌标语时，`manufacturer_terms` 可以为空，但必须提供顶层 `identity_review`：`status` 固定为 `NO_SOURCE_IDENTITY_FOUND`，并记录源文件 SHA-256、复核时间、复核范围以及含 `id`、`role` 的复核者。空词表且缺少完整声明时仍为 `HUMAN_REVIEW`。

`NO_SOURCE_IDENTITY_FOUND` 与 `remove_identity`、`redact_identity`、`preserve_sanitized_image` 互斥。映射中只要出现任一来源身份处理动作，就不得声明“未发现来源身份”；必须把已知厂商/品牌文字及 OCR 变体写入 `manufacturer_terms`，使 Word/WPS 最终渲染实际执行逐页身份扫描。

动作：

- `preserve_exact`：原文或原表格逐字保留。
- `redact_identity`：仅删除本条 `redactions` 中逐项列出的来源厂家字面词，其余字符逐字保留。每个词必须在源对象中真实存在，且必须同时列入顶层 `manufacturer_terms`。
- `reviewed_text`：仅用于 `extraction=ocr` 的文本源。人工必须对照原图核准并填写 `reviewed_text`，用于修正 OCR 的型号、数值、单位或关键术语误识别；原始 OCR 文本仍保留在源清单中。该块必须带 `source_evidence`，至少包含 `original_ocr_source_id`、`page_or_position`、`review_reason`、含 `id` 与 `role` 的 `reviewer`，以及包含原 OCR ID 的 `evidence_source_ids`。
- 同一 OCR 页同时包含多个一级篇章时，使用 `reviewed_blocks` 替代单个 `reviewed_text`。该映射仍只有一个 `source_id`；每个块必须包含 `text`、`target_section`、`target_order` 和独立的 `source_evidence`，不能通过复制 `source_id` 解决。`source_evidence.evidence_source_ids` 可列出提供交叉事实的其他源内容 ID。
- `preserve_image`：原字节图片保留；封面图加 `"use_on_cover": true`，只上封面加 `"use_on_cover_only": true`。
- `preserve_sanitized_image`：源图片含来源厂家标识但产品主体必须保留时使用。该动作只允许用于 `kind=image`，必须提供 `replacement_path`、`replacement_sha256`、非空 `sanitization_review` 和 `target_section`；替换图是唯一允许插入成品的图片，封面字段同样支持 `use_on_cover` 与 `use_on_cover_only`。`replacement_path` 为相对路径时相对于 `content_map.json` 所在目录，验证时必须实际存在且 SHA-256 匹配。`sanitization_review` 必须说明去除了什么，并记录人工确认未改变产品事实。
- `remove_identity`：来源厂家名称、Logo、联系方式、网址、二维码或品牌标语。
- `remove_template_background`：源 PPT/PDF 的整页背景、导航条或重复版式对象。
- `human_review`：无法安全判断，禁止最终交付。

局部脱敏示例：

```json
{
  "source_id": "TEXT-稳定ID",
  "action": "redact_identity",
  "redactions": ["来源厂家全称", "来源品牌简称"],
  "target_section": "一、产品简介",
  "target_order": 2
}
```

局部脱敏不得改写句子、补词或概述，只允许删除指定字面词并清理其相邻的多余空白。若删除后造成语义不完整或病句，将该项标为 `human_review`，不得自动交付。

`preserve_exact`、`preserve_image` 与 `preserve_sanitized_image` 必须指定六篇章之一：一、产品简介；二、功能介绍；三、产品优势；四、应用场景；五、技术参数；六、产品资质。

映射应按语义分类：能力、模式、接口、联动和检测行为归入功能；可靠性、安全性、误报率、抗干扰和性能亮点归入优势；适用对象、通行条件和检查对象归入场景；尺寸、环境等级、频率、容量等可量化规格归入技术参数。不能因源文件名为“参数”或条目含数字，就把所有条目集中到技术参数。前五章任一章没有来源支撑映射时，验证结果为 `HUMAN_REVIEW`；不得复制同一来源、拆改普通 DOCX/PPTX 文本或编造内容。只有“六、产品资质”在无来源资质事实时允许空正文。

`preserve_sanitized_image` 是人工审定输入，不是模型可以自动生成、判断或批准的脱敏结论。模型只能提出待审材料；只有人工完成图片脱敏、核对产品主体与技术事实未被改变，并填写 `sanitization_review` 后，映射才可进入确定性验证和构建。缺字段、路径不存在、SHA-256 不匹配或用于非图片源对象均为 `FAIL`。成品覆盖验证使用替换图的 `replacement_sha256` 或可视哈希，不使用带来源标识源图的哈希。

`reviewed_text` 同样必须指定六篇章之一，且不得用于普通 DOCX/PPTX 文本层。OCR 明显误识别但未人工核准时必须使用 `human_review`，不得用 `preserve_exact` 静默交付。

使用 `reviewed_blocks` 时，父映射不填写 `target_section` 或 `reviewed_text`；每个内容块独立指定篇章、顺序和证据。块文本来自人工对原图的逐项核准，不得补写源件没有的技术事实。型号样式、数字+单位、认证/资质关键词等敏感事实 token 若不在原 OCR 归一化文本中，必须由 `evidence_source_ids` 中的其他源内容提供同一事实；否则校验结果为 `FAIL`，不得交付。允许有限的常见 OCR 字符纠正和空格归一化，但不允许借此改变事实。

不得为资质、证书、型号、检测机构或日期添加“待核实”占位。源件无资质事实时仍保留固定标题“六、产品资质”，但不编造正文内容，并在交付说明中单独提示。
