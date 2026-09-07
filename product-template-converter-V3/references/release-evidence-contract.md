# Release Evidence Contract

`scripts/verify_release.py` 是发布门禁脚本。它只做证据核验，不修复文件。

## 输入

- `--manifest <manifest.json>`: 由 `scripts/refresh_manifest.py` 生成的发布清单。
- `--post-install-report <report.json>`: 安装或升级后的依赖自检报告，必须 `status=PASS`。
- `--pipeline-report <report.json>`: 一个或多个流水线报告，必须全部 `status=PASS` 且 `deliverable=true`。
- `--blind-report <report.json>`: 独立盲验报告，必须 `status=PASS`，且 `manifest_sha256` 必须与 `manifest.json` 的 SHA-256 完全一致。
- `--release-matrix-report <verify-release-matrix-v3.json>`: V3 必需。由 `verify_release_matrix_v3.py` 生成的 16 案例验证报告，必须绑定当前 manifest。
- `--require-target desktop|mobile`: 可重复。要求全部流水线报告集合覆盖指定交付目标。
- `--require-source-format DOCX|PDF|...`: 可重复。要求全部流水线报告集合覆盖指定源文件格式。
- `--require-template-pack <id@version>`: V3 必须恰好重复两次，明确声明两份真实且已发布的模板包。
- `--report <path>`: 可选。把结构化 JSON 报告写入磁盘。

## 通过条件

1. `manifest.json` 中的每个文件条目都要和磁盘上的实际文件一致，包含 `bytes` 与 `sha256`。
2. `manifest.json.template_sha256` 必须和模板文件 `assets/ZXTY-XX_产品全称-产品介绍-模板.docx` 的实际 SHA-256 一致。
3. 清单中的发布文件集合必须与仓库里实际可发布资源集合一致。
4. `post_install_report` 必须是 `PASS`。
5. 每个 `pipeline_report` 必须满足：
   - 顶层 `status=PASS`
   - 顶层 `deliverable=true`
   - `output` 文件存在，且 `output_sha256` 与实际 SHA-256 一致
   - `delivery_target` 必须是 `desktop` 或 `mobile`
   - `delivery_format` 必须分别对应 `DOCX` 或 `PDF`
   - `stages` 中必须包含 `verify_document_structure`、`export_word_wps`、`analyze_rendered_pages`、`scan_source_identity_rendered_word`、`scan_source_identity_rendered_wps`、`final_delivery`
   - `verify_document_structure` 必须是 `PASS`，确保章节、分页绑定、封面文字、分节和 Drawing ID 等结构门禁已经执行并通过
   - `export_word_wps` 必须恰好包含 `word`、`wps` 各一条且均为 `PASS`，不得重复或以两条相同引擎冒充双引擎
   - WPS 条目必须是 `role=primary`、`required_identity=wps-writer`、`identity_verified=true`、`ownership_verified=true`；Word 条目必须是 `role=backup_compatibility`、`required_identity=microsoft-word`、`identity_verified=true`、`ownership_verified=true`
   - 两份 PDF 的路径、实际字节数和实际 SHA-256 必须分别与报告一致，且两份路径、字节数、SHA-256 均不得重复
   - `analyze_rendered_pages` 必须是 `PASS`
   - Word/WPS 两套渲染身份扫描都必须是 `PASS`；存在来源身份词时，OCR 必须实际执行，扫描页必须完整覆盖对应渲染页，且失败页为空
   - 来源身份词确认为空时，两套渲染身份扫描都必须携带完整的 `NO_SOURCE_IDENTITY_FOUND` 人工复核声明
   - `final_delivery` 必须是 `PASS`
   - `delivery.required_renderers` 必须同时包含 `word` 与 `wps`
   - `mobile` 的正式输出 renderer 必须是 `wps_pdf_export`，且输出 SHA-256 必须等于 WPS PDF 的实际 SHA-256；不得绑定 Word PDF
6. `blind_report` 必须是 `PASS`，并且其 `manifest_sha256` 必须和清单 SHA 完全一致。
7. 如果声明了 `--require-target` 或 `--require-source-format`，则所有 pipeline 报告集合必须覆盖这些要求。

## V3 附加发布门禁

V3 通过模板包版本字段或 `--require-template-pack` 触发。除上述通用条件外，还必须全部满足：

1. `--release-matrix-report` 必须存在，且报告同时满足：
   - `status=PASS`、`stage=verify_release_matrix_v3`、`findings=[]`；
   - `manifest_sha256` 与当前发布 manifest 完全一致；
   - `case_count=16` 且实际案例摘要恰好 16 条；
   - 覆盖 `DOC/DOCX/PPT/PPTX/PDF`、`desktop/mobile` 和两份明确声明的模板包。
   - 每条案例的内容复核回执必须通过 `ReviewReceiptV3` schema，并把源文件、模板、Template IR、Template DSL、当前编译器、候选 decision 和 decision validation 报告的 SHA-256 全部绑定到该案例；`ContentPlanV3.review_approval` 的动作与绑定必须和回执完全一致。缺字段、旧回执、裸 `APPROVED`、任一绑定漂移或 schema 非法都必须保持 `RELEASE_HOLD`。
2. 传给总发布门禁的 pipeline 报告必须恰好 5 条，它们就是负责人逐页验收和无上下文独立盲验使用的黄金案例。
3. 5 条黄金案例必须一对一覆盖五种源格式；集合必须同时覆盖两份模板和两个交付目标。每条案例都以源格式、源 SHA、模板 ID/版本、交付目标、pipeline 报告 SHA、成品 SHA 计算 `case_sha256`。
4. 每条黄金案例必须在 16 案例矩阵报告中找到唯一对应项，pipeline 报告 SHA 和成品 SHA 必须完全一致，禁止把自动矩阵和盲验矩阵分别计算后合并成假通过。
5. 独立盲验 `visual_review` 必须：
   - 恰好包含相同的 5 个案例；
   - 每个案例分别列出从 1 开始连续的 WPS 和 Word 页面，两个列表都非空；
   - `pages_expected/pages_reviewed` 等于两套页面数之和；
   - `complete=true`、`decision=PASS`、`findings=[]`，且未发现来源厂家残留或实质性双引擎差异；
   - 顶层页面合计与逐案例合计完全一致且大于零。
6. `evidence.responsible_engineer_visual_review` 必须绑定一份实际 JSON 文件的字节数和 SHA。该文件必须 `status=PASS`、`stage=responsible_engineer_visual_review`，提供非空 reviewer ID/role，并对同一 5 个案例逐项绑定 pipeline 报告 SHA、WPS/Word 页码和结果。其总页数必须与独立盲验一致。
7. 负责人验收只作为前置支持证据，`used_as_substitute_for_blind_visual_review` 必须明确为 `false`；不得代替独立盲验。

## 输出

脚本输出结构化 JSON。成功时：

- `status = PASS`
- 退出码 `0`

任一证据缺失、SHA 不匹配、状态不是 `PASS`、目标覆盖不足或源格式覆盖不足时：

- `status = RELEASE_HOLD`
- 退出码 `2`

推荐字段：

- `manifest`
- `post_install`
- `blind_report`
- `pipeline_reports`
- `coverage`
- `findings`

## 正式复制

只有 `verify_release.py` 输出 `status=PASS` 后，才可调用：

```powershell
<workspace-python> scripts/publish_v3.py `
  --source-root <product-template-converter-V3-work> `
  --release-report <verify-release-v3.json> `
  --destination D:\document-coverter-skill\product-template-converter-V3 `
  --report <publish-v3.json>
```

发布器只复制当前 manifest 白名单中的文件和 manifest 本身。它会重新核对源文件大小/SHA、16 案例矩阵、5 条黄金流水线、负责人复核和独立盲验摘要，在同一目标父目录创建绑定 manifest SHA 的 staging 目录，复制后逐文件复核，再原子改名为正式目录。

正式目标或同名 staging 目录已经存在时一律 `BLOCKED`；发布器不会覆盖、合并或自动删除已有目录。复制或核验中断时保留 staging 供人工检查，不把半成品当成正式版本。
