# V3 DOCX 模板识别、代码化与注册合同

V3 首版只把 DOCX 作为目标模板。DOC、PPT/PPTX 和 PDF 可以作为内容来源，但作为目标模板时返回 `BLOCKED / UNSUPPORTED_TEMPLATE_FORMAT`。

## 选择规则

- 显式 `--template-pack <id@version>`：只加载目录中状态为 `RELEASED` 的准确版本。
- 显式 `--template <docx>`：SHA 与已批准模板准确一致时自动命中；结构或视觉相似只作为候选，不能自动认定为相同模板。
- 未指定模板时只运行已注册的确定性路由。恰好一个合格 pack 可自动选择；零个或多个并列候选为 `HUMAN_REVIEW / TEMPLATE_SELECTION_REQUIRED`。
- 禁止隐藏默认模板和模型自行决定模板。

## 代码化边界

模板由 `DocxTemplateIRV3 + TemplateProgramV3` 表达，编译器只接受白名单操作：

```text
retain_part
bind_slot
bind_asset_role
apply_style_ref
repeat_block
insert_text
insert_image
insert_table
bind_header_footer
section_break
page_break
validate_invariant
```

不得执行模型生成的 Python、shell、宏或原始 OOXML。编译器统一负责关系 ID、Drawing ID、分节、分页、表格几何和媒体写入。关键对象无法安全表达时 `BLOCKED`；可以保留但语义不明时 `HUMAN_REVIEW`。

已批准的通用 DOCX pack 使用固定的 `declarative-docx-v3` adapter。首版可确定性执行审核文本写入、固定布局表格以及带本地路径和 SHA-256 证据的图片写入；`contain` 和 `native` 图片适配可执行，未知几何下的 `cover` 裁剪必须 `BLOCKED`。模板原有部件、固定媒体、页眉页脚、分节和分页由候选重建保留，并由 pack/core validator 复核，内容 adapter 不接受任意 OOXML 操作。`RenderPlanV3` 在模板 pack 已解析时同时绑定 `template.json` 的路径和 SHA-256，防止只锁模板字节却替换 DSL、slot contract 或 validator 配置。

## 审批与生命周期

```text
DRAFT
→ REVIEW_REQUIRED
→ APPROVED | REJECTED | REVISION_REQUIRED
→ COMPILED
→ VALIDATED
→ RELEASED
```

回执必须绑定源模板、IR、DSL、编译器、候选成品、diff 和静态资源的 SHA-256。任一内容或版本变化，旧回执失效并返回 `HUMAN_REVIEW / REVIEW_RECEIPT_STALE`。

`template_onboard.py` 必须实际生成规范化重建候选 DOCX、Candidate IR 与严格 diff 报告，不允许以 `pending.json` 作为候选。结构 diff 至少比较规范化 OOXML、页面合同、样式、编号、主题、媒体视觉指纹、Drawing 和全部 DSL invariant。

模板审批前必须串行完成原模板与候选的 WPS/Word 双引擎导出，并逐页保存原页、候选页和差异叠图。`template_render_evidence_v3.py` 只接受位于草案目录内、身份/进程所有权/COM 清理和页数均通过的证据；附加后必须重新计算 diff、Queue、review ID 和 draft 哈希。机器比较通过后状态仍为 `HUMAN_REVIEW`。

只有 `APPROVED + COMPILED + VALIDATED` 且没有未关闭 finding 的 pack 才能注册到正式 catalog。第二份真实 DOCX 模板及其有效审批回执缺失时，不得声明多模板正式发布完成。
