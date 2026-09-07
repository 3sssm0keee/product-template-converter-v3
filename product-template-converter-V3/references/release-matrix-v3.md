# V3 真实回归与发布矩阵

框架和非 COM 测试通过不等于 V3 已发布。正式发布至少需要：

- `zxty-fixed-v1`：DOC、DOCX、PPT、PPTX、PDF × desktop/mobile，共 10 条真实流水线。
- 第 2 份真实 DOCX 模板：DOCX、PPTX、PDF × desktop/mobile，共 6 条真实流水线。
- 3 个现场 DOC 与 2 个现场 PPT 全部完成原件/归一化文件页数、关键页和视觉差异检查。
- 至少一个 DOC、一个 PPT 完成 desktop + mobile 端到端回归。
- 5 条黄金案例由负责人逐页验收并完成无上下文独立盲验；五条合计覆盖五种源格式、两个模板和两个交付目标。

每个案例都必须绑定：

```text
源件 SHA
模板包 ID/版本/模板 SHA
ContentDecision SHA
ReviewReceipt SHA
ContentPlan/RenderPlan/TemplateProgram SHA
成品 SHA
流水线报告 SHA
盲验 case SHA
manifest SHA
```

先按 `schemas/release-matrix-run-spec-v3.schema.json` 准备运行规格。每条案例只填写经过哈希核对的源件、模板包、ContentDecision、ReviewReceipt 和交付目标；执行总控会严格按清单顺序串行运行 Office 流水线：

```powershell
python scripts/run_release_matrix_v3.py `
  --spec <release-matrix-run-spec-v3.json> `
  --output-root <new-empty-matrix-directory> `
  --python <workspace-complete-python.exe> `
  --report <run-release-matrix-v3.json>
```

默认遇到首个非 `PASS + deliverable=true` 案例即停止；只有排查阶段明确需要收集其余失败时才使用 `--continue-on-failure`。输出根目录必须是新建或空目录，避免旧报告混入正式证据。Word/WPS 调用始终逐案例串行；`--workers` 只传给单条流水线内部允许并行的静态校验、页面和 OCR 工作。

全部 16 条真实流水线成功后，总控自动生成符合 `schemas/release-matrix-evidence-v3.schema.json` 的单一矩阵账本，并立即再次调用独立验证器。也可单独复核账本：

```powershell
python scripts/verify_release_matrix_v3.py `
  --matrix <release-matrix-evidence-v3.json> `
  --report <verify-release-matrix-v3.json>
```

已经完成且保存了不可变阶段证据时，不得为了适配当前消费端而重跑或改写历史矩阵。先对现有第一模板证据执行只读兼容性检查：

```powershell
python scripts/merge_release_matrix_evidence_v3.py `
  --first-template-evidence <first-template-validation-evidence-v3.json>
```

检查会重算历史 case SHA、全部引用文件 SHA，并从每条 PipelineReport、ContentPlan 中解析本次运行实际消费的 ContentDecision 和 ReviewReceipt。历史记录保存的是审批输入路径、运行报告保存的是内容相同的隔离工作副本时，只在内存中形成规范化合并视图，不修改原证据；内容 SHA、回执绑定或质量阶段不一致仍然失败关闭。

第二模板 6 条阶段证据齐备后，才允许输出新的 16 条合并账本：

```powershell
python scripts/merge_release_matrix_evidence_v3.py `
  --first-template-evidence <first-template-validation-evidence-v3.json> `
  --second-template-evidence <second-template-validation-evidence-v3.json> `
  --output <release-matrix-evidence-v3.json> `
  --report <merge-release-matrix-evidence-v3.json>
```

合并不是发布豁免：输出前仍调用同一个 `verify_release_matrix_v3` 做 16 维覆盖、双模板、全链哈希和必要阶段校验；任一阶段不合格时不写入合并账本。

验证器只接受精确的 16 维组合，并逐案重算所有本地制品 SHA、case SHA，交叉核对 PipelineReport、ContentPlan、RenderPlan 与模板解析阶段的路径、模板 ID/版本和绑定哈希。任何文件缺失、制品被替换、案例维度重复或必要质量阶段非 `PASS`，矩阵总状态均为 `FAIL`。

性能基准使用 `scripts/benchmark_v3.py`。除质量签名一致外，每个样本还必须提供完整且相同的机器、Python、WPS 和 Word 二进制指纹；环境漂移的样本不得用于证明提速。

`verify_release.py` 必须交叉检查模板 ID、版本、源格式、交付目标、流水线报告 SHA、成品 SHA 与 blind case SHA。自动流水线覆盖与盲验覆盖不能分别计算后合并成假通过。

矩阵通过后，按 `golden-visual-review-v3.md` 从 16 条案例中明确选择五条黄金案例，分别生成负责人验收 HTML 和无上下文独立盲验 HTML。两份页面都不会预选通过；最终由 `assemble_blind_report_v3.py` 校验两份人工回执并装配哈希绑定的盲验报告。

每次正式回归都必须重新执行宿主桌面依赖预检；历史上的 WPS/Word 可用或不可用记录都不能代替当前事实。任一引擎在当前案例不可用、身份不符或导出失败时，该案例保持 `BLOCKED / RELEASE_HOLD`。
