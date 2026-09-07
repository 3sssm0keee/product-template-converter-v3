# ContentPlan v2

## 边界

ContentPlan 只描述内容语义。模型或人工映射不得生成 DOCX XML、分页、坐标、页眉页脚、浮动对象、字体尺寸或 PDF 布局指令。`delivery_target` 由调用方和渲染代码决定，不由 Prompt 或 ContentPlan 决定。

## 最小结构

```json
{
  "schema_version": "v2",
  "source_document": "work/source_document.json",
  "product": {"model": "", "full_name": ""},
  "mappings": [],
  "review_items": []
}
```

## 映射合同

- `mappings` 必须覆盖输入中的每个 `source_id`，且每个 `source_id` 只出现一次。一个 OCR 源页可在单条 `reviewed_text` 映射内提供多个 `reviewed_blocks`，以不同目标篇章保存经人工核准的内容块；不得通过重复 `source_id` 拆分内容。
- `reviewed_text` 或每个 `reviewed_blocks[]` 内容块必须包含 `source_evidence`：`original_ocr_source_id`、`page_or_position`、`review_reason`、`reviewer.id`、`reviewer.role` 和 `evidence_source_ids[]`。原 OCR ID 必须与映射 `source_id` 相同，并列入证据 ID 列表；列表中的其他 ID 只能引用实际输入来源。
- 每条映射保留来源证据位置与置信度；不得用渲染坐标、分页或交付格式替代内容含义。对于审核文字，型号样式、数字+单位、认证/资质关键词属于敏感事实 token：不在原 OCR 归一化文本中的 token，必须在列出的其他来源内容中出现同一事实，否则进入 `FAIL`/`HUMAN_REVIEW`，不得猜测或自动交付。空格和有限常见 OCR 字符纠正属于允许的非事实变更。
- 低置信、证据冲突，或局部脱敏会破坏语义时，必须创建 `human_review` 的 `review_items`；`review_items` 非空即进入 `HUMAN_REVIEW`，不得猜测补全。
- 前五个业务篇章必须各有至少一条来源支撑映射；任一章为空即进入 `HUMAN_REVIEW`。仅“六、产品资质”允许在无来源资质事实时保留空正文。
