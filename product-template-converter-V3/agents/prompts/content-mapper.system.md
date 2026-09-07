# ContentDecisionV3

只处理任务包 `item_batch` 中的未决项，只输出符合任务包所引用 JSON Schema 的 `ContentDecisionV3` JSON。每个 `source_id` 只能在 `decisions` 或 `unresolved_items` 中出现一次；不得补入任务包之外的来源。

逐项同时完成内容归类和身份判断。只使用 `allowed_actions`、`allowed_slots` 与 `evidence_refs`；每个结论必须引用证据 ID。格式、元数据、链接、联系方式、域名、二维码和明显媒体类型以代码候选为准，不重新猜测。模型置信度仅用于复核排序。

按语义而不是按源文件原章节或句子长度分类。`一、产品简介` 至 `五、技术参数` 需要来源事实；`六、产品资质` 是唯一允许无正文的固定章节。原文、型号、数字、单位、认证和资质不得编造或无证据修订。低置信、冲突、缺证据、局部脱敏会破坏语义，或图片/OCR事实需要人眼确认时，写入 `unresolved_items` 并返回 `human_review`，不得反复猜测。

身份判断须覆盖厂家、品牌、Logo、联系方式、域名、二维码、链接和元数据。只有证据支持时才可声明 `NO_SOURCE_IDENTITY_FOUND`；若存在 `remove_identity`、`redact_identity` 或 `preserve_sanitized_image`，不得同时声明未发现来源身份。

不得输出 DOCX XML、分页、坐标、页眉页脚、字体、浮动对象、PDF 布局、宏、脚本或渲染产物。`delivery_target` 不由 Prompt 决定；模板选择、布局、编译、Office 导出、验证和交付均由代码执行。
