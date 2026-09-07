# V3 黄金案例逐页复核

本流程只在两模板 16 条正式矩阵全部 `PASS + deliverable=true` 后执行。它生成两份面向审核人的离线 HTML，不会自动填写审核结论，也不会用负责人验收替代独立盲验。

## 1. 选择五条黄金案例

从 `release-matrix-evidence-v3.json` 中明确选择恰好五个 `case_id`。五条共同必须覆盖：

- `DOC`、`DOCX`、`PPT`、`PPTX`、`PDF`；
- 两份已发布模板包；
- `desktop` 和 `mobile`。

生成器会重新运行矩阵验证，并检查每条流水线的 WPS/Word 渲染页是否从 1 连续、非空、数量一致且文件真实存在。

## 2. 负责人逐页验收

```powershell
<workspace-python> scripts/golden_visual_review_v3.py `
  --matrix <release-matrix-evidence-v3.json> `
  --case-id <case-1> `
  --case-id <case-2> `
  --case-id <case-3> `
  --case-id <case-4> `
  --case-id <case-5> `
  --mode responsible `
  --output-dir <responsible-review-dir> `
  --report <prepare-responsible-review.json>
```

打开 `responsible-review.html`。负责人必须填写 reviewer ID/role，逐页勾选 WPS 和 Word 页面，为每个案例选择通过或不通过，然后导出 `responsible-engineer-visual-review-v3.json`。

## 3. 无上下文独立盲验

使用同一冻结矩阵和相同五个案例再次运行生成器，只把生成的盲验页面交给独立审核人：

```powershell
<workspace-python> scripts/golden_visual_review_v3.py `
  --matrix <release-matrix-evidence-v3.json> `
  --case-id <case-1> `
  --case-id <case-2> `
  --case-id <case-3> `
  --case-id <case-4> `
  --case-id <case-5> `
  --mode blind `
  --output-dir <blind-review-dir> `
  --report <prepare-blind-review.json>
```

独立审核人不得读取负责人结论、日常 prompt、修复历史或旧盲验报告。打开 `blind-review.html` 后逐页检查并导出 `independent-blind-visual-review-v3.json`。独立审核人的 reviewer ID 必须与负责人不同。

## 4. 装配最终盲验报告

```powershell
<workspace-python> scripts/assemble_blind_report_v3.py `
  --matrix <release-matrix-evidence-v3.json> `
  --manifest <manifest.json> `
  --case-id <case-1> `
  --case-id <case-2> `
  --case-id <case-3> `
  --case-id <case-4> `
  --case-id <case-5> `
  --responsible-review <responsible-engineer-visual-review-v3.json> `
  --blind-review <independent-blind-visual-review-v3.json> `
  --output <blind-report-v3.json>
```

装配器会校验两种严格 JSON Schema、manifest/matrix SHA、案例集合、pipeline 报告 SHA、逐引擎页码、页面总数和审核人独立性。任一不一致都会生成 `status=FAIL`，不得用于发布。

## 5. 最终发布核验

将 5 条黄金 pipeline 报告、16 案例矩阵验证报告和最终 blind report 一并传给 `scripts/verify_release.py`。只有总发布报告为 `PASS` 才允许进入正式目录。
