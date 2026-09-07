# V3 执行总线合同

V3 保留 V2 的业务顺序和 fail-closed 状态：

```text
访问授权
→ 依赖预检
→ 真实格式探测
→ 归一化与源内容盘点
→ 内容/身份人工决策
→ 确定性 DOCX 构建
→ 静态与模板专用校验
→ WPS 主引擎 + Word 备用兼容引擎导出
→ 全页分析、OCR 与厂家身份扫描
→ PASS + deliverable=true 才交付
```

V3 在上述位置加入可审计制品，实际执行链固定为：

```text
FormatProbeResultV3
→ Normalize
→ SourceDocumentV3
→ TemplateResolverV3 / TemplateOnboarding
→ TemplatePackV3
→ DeterministicCandidateEngineV3
→ DecisionTaskBundleV3
→ ContentDecisionV3 + ReviewReceiptV3
→ ContentPlanV3
→ TemplateProgramV3
→ RenderPlanV3
→ FixedTemplateCompilerV3
→ Core Validators + Pack Validators
→ WPS/Word Render
→ Visual/OCR/Identity Gates
→ Delivery
```

## 强制边界

- renderer 只允许读取已经哈希锁定的 `ContentPlanV3` 与 `RenderPlanV3`。
- 旧 `.doc/.ppt` 归一化必须同时提供完整 Office 应用身份/版本、原件与归一化件视觉证据路径，以及绑定源/输出 SHA-256 的 `FORMAT_NORMALIZED` finding；任一缺失均不得进入 `SourceDocumentV3`。
- `ContentPlanV3` 必须物化每个唯一 `source_id` 对应的源内容和 decision；不能让 renderer 回读原始 inventory 或任意模型输出。
- `ContentPlanV3` 必须绑定已批准且仍有效的 `ReviewReceiptV3`；公共构建接口必须核对当前 source/template/IR/DSL/compiler/decision/diff 七项 binding、当前 decision 字节 SHA，并确认内存回执与回执文件完全一致。代码不得生成、推定或旁路人工批准。
- `RenderPlanV3` 必须绑定 SourceDocument、ContentPlan、TemplateProgram、模板包、编译器版本、交付目标和 validator dispatch 的路径与 SHA-256。
- V2 `content_map v1` 只能经显式迁移器生成 `ContentDecisionV3`；V3 renderer 不直接消费旧 schema。
- 状态只允许 `PASS / FAIL / HUMAN_REVIEW / BLOCKED`。`PASS` 以外均为 `deliverable=false`。
- desktop 只正式交付可编辑 DOCX；mobile 只正式交付 WPS 主引擎导出的 PDF，并同时保留 Word 备用兼容证据。
- WPS/Word 双引擎、全页 OCR、厂家身份零命中、模板不变量和审批回执不因缓存或性能优化而减少。
- `content_review_v3`、`content_plan_v3`、`render_plan_v3` 与 `final_delivery` 必须记录真实开始/结束时间、持续时间、输入/输出哈希、状态和 finding 数；不得用事后合成的零耗时记录证明性能。

## 哈希与版本

以下制品均须保存 `schema_version`、上游 SHA-256、策略/生成器/编译器版本：

- SourceDocumentV3
- ContentDecisionV3
- ContentPlanV3
- TemplateEnvelopeV3 / DocxTemplateIRV3 / TemplateProgramV3
- RenderPlanV3
- ReviewReceiptV3
- StageTelemetryV3

任一上游哈希或版本变化，所有下游缓存、回执和发布证据自动失效。
