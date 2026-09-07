# VALIDATION_REPORT

本报告用于开发/发布盲验，或用户明确要求的额外独立验收；不是每次转换的强制交付物。

- `STATUS`: `PASS | FAIL | HUMAN_REVIEW | BLOCKED`
- `INPUT_FACTS`: 三项输入路径、SHA-256、格式、页数、打开与渲染引擎
- `DELIVERY`: `target`、`delivery_format`、正式 `path`、SHA-256、实际 `renderer`、`validation_profile` 与 Word/WPS 双渲染要求；正式交付仅在 `PASS + deliverable=true` 时成立。
- `INTERMEDIATE_ARTIFACTS`: 基础 DOCX 与带变更说明的中间修复文件；它们可检查、修改和回滚，但不属于正式交付物。
- `SOURCE_DOCUMENT`、`CONTENT_PLAN`、`RENDER_PLAN`: 三层中间表示的路径和版本，用于追溯来源事实、内容语义与模板渲染决定。
- `CONTENT_COVERAGE`: 参数、功能、条件、图片和表格逐项覆盖证据
- `SOURCE_IDENTITY`: 厂家词、Logo、联系方式、链接和元数据扫描证据
- `TEMPLATE_FIDELITY`: 固定媒体、六篇章、分节、页眉页脚、页码、封底和命名证据
- `VISUAL_QA`: WPS 页数、导出身份和逐页检查结果
- `FINDINGS`: 严重级别、位置、证据、影响和判定
- `HUMAN_REVIEW_ITEMS`: 仅列无法从源件判断的事实
- `SKILL_PATCH_CANDIDATES`: 可复现失败的触发条件、风险、建议检查和通过标准
