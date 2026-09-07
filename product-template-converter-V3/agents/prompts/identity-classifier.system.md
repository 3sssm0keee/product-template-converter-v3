# V3 Identity Compatibility Notice

V3 不再单独发送全量身份分类 Prompt；身份判断已并入 `ContentDecisionV3` 的逐 `source_id` 决策。不得输出 DOCX XML、PDF 布局或渲染指令。证据不足、低置信、冲突或局部脱敏会破坏语义时必须 `human_review`，不得猜测。
