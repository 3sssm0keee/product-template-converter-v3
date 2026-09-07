# 多模板转换 V3

V3 以冻结的 V2 为兼容基线，把 DOC、DOCX、PPT、PPTX、PDF 作为内容来源，把已批准的 DOCX 作为目标模板。正式结果仍只有两种：`desktop` 交付可编辑 DOCX，`mobile` 交付 WPS 主引擎导出的 PDF。只有 `status=PASS` 且 `deliverable=true` 才能交付。

## 必读边界

每个转换任务只读取：

1. `references/access-policy.md`
2. `references/v3-execution-contract.md`
3. `references/delivery-contract.md`

本项目通过本地 Python CLI 运行，不需要注册为 Skill。日常语义映射任务无需加载完整项目文档、发布合同或修复合同；遇到特定阶段时再按“按需参考”读取对应文件。

## 运行顺序

1. 使用完整运行时做依赖自检。安装、升级或迁移后先运行：

```powershell
<python> scripts/post_install_check.py --report <可写目录>/post_install_dependencies.json
<python> scripts/dependency_bindings_v3.py --output <可写目录>/dependency-bindings.v3.json
```

报告不是 `PASS` 时停止，不生成半成品。依赖路径可通过 `PTC_DEPENDENCY_<NAME>_PATH` 显式配置；Python 应绑定具备所需模块的完整运行时。机器专属绑定不进入发布包。正式任务仍由总控按真实格式复检 Word、PowerPoint、WPS、OCR、Poppler 和图片解码依赖。

2. 在创建输出目录和读取源件前通过访问门槛：

```powershell
<python> scripts/access_gate.py check
```

只有 `AUTHORIZED` 才继续。到期时只能通过标准输入续期；不得把密码写入命令行、环境变量、日志或成品。状态冲突、损坏、机器不匹配或双副本持久化失败均停止。

3. 运行总控。优先显式指定已发布模板包：

```powershell
<python> scripts/run_pipeline.py `
  --source <源文件> `
  --output-dir <工作目录> `
  --template-pack <id@version> `
  --dependency-bindings <dependency-bindings.v3.json> `
  --delivery-target <desktop|mobile>
```

也可使用 `--template <目标模板.docx>`。模板 SHA 未精确命中已批准版本、模板字节发生变化、没有唯一候选或候选并列时，必须进入模板 onboarding/人工复核；不得隐藏回退到默认模板，也不得让模型选择业务模板。DOC、PPT/PPTX、PDF 作为目标模板时返回 `BLOCKED / UNSUPPORTED_TEMPLATE_FORMAT`。

4. 第一次运行通常会生成 `DecisionTaskBundleV3` 和离线复核材料，并以 `HUMAN_REVIEW` 停止。外部人或模型只处理任务包中的未决 `source_id`，只返回严格 JSON Schema 的 `ContentDecisionV3`；代码不内置模型供应商 SDK、API Key 或网络重试。

5. 人工在本地 HTML 中逐项复核，导出与当前队列及七项 SHA-256 绑定的 `ReviewReceiptV3`。不得自动伪造批准。使用外部 decision 后重新运行：

```powershell
<python> scripts/run_pipeline.py `
  --source <源文件> `
  --output-dir <工作目录> `
  --template-pack <id@version> `
  --decision-bundle <content-decision-v3.json> `
  --review-receipt <review-receipt-v3.json> `
  --dependency-bindings <dependency-bindings.v3.json> `
  --delivery-target <desktop|mobile>
```

已有且已批准的 V2 `content_map v1` 只能通过显式 `--content-map` 迁移为 `ContentDecisionV3`；V3 renderer 不得直接读取旧文件。迁移后仍需有效人工回执。

6. 总控把已批准 decision 物化为 `ContentPlanV3`，再生成绑定模板程序、模板包配置 SHA 和交付目标的 `RenderPlanV3`。固定编译器只能消费这两个已校验制品，不能回读原始 inventory、旧 content map 或任意模型输出，也不能执行模型生成的 Python、shell、宏或 OOXML。已发布的通用 pack 使用 `declarative-docx-v3` adapter 执行白名单文本、固定布局表格和哈希绑定图片操作；未知槽位、缺失必需内容、无唯一图片证据或无法安全确定的 `cover` 裁剪均 fail-closed。

7. DOC/PPT 先通过真实 Office COM 归一化；原件与归一化件的页数、关键页或视觉差异超阈值时进入 `HUMAN_REVIEW`。随后执行模板/core 静态校验、WPS 与 Word 串行导出、全页渲染、OCR、内容覆盖和厂家身份扫描。任一质量门失败都不能被缓存、单引擎成功或模型置信度覆盖。

## 模板入库

未知或变化的 DOCX 模板先生成草稿与离线对照页面：

```powershell
<python> scripts/template_onboard.py --template <目标模板.docx> --output-dir <草稿目录>
```

该步骤会生成真实的确定性重建候选、Candidate IR 和结构/视觉代理 diff，不再写入占位成品。正式审批前，必须依次用 `scripts/export_word_wps.ps1` 串行导出原模板与候选，分别生成 WPS/Word 逐页对照，再用下列命令把双引擎报告、原页、候选页和差异叠图绑定到新的 ReviewQueueV3：

```powershell
<python> scripts/template_render_evidence_v3.py --draft <草稿目录> --original-export-report <原模板导出报告.json> --candidate-export-report <候选导出报告.json> --wps-visual-report <WPS逐页差异报告.json> --word-visual-report <Word逐页差异报告.json>
```

绑定任何新证据都会生成新的 review ID 和哈希；此前导出的回执自动失效。机器 diff `PASS` 只说明重建没有检测到结构/视觉漂移，不替代人眼确认业务槽位语义。

保留部件、固定媒体、页眉页脚、分节/分页和 invariant 由已批准模板本体及 pack validator 约束；内容写入阶段不会把这些声明解释为任意 OOXML 修改。若模板需要通用 adapter 尚不能安全表达的浮动对象、未知锚点或裁剪几何，必须返回 `BLOCKED` 或重新进入模板修订，不得降级猜测。

人工批准后再注册：

```powershell
<python> scripts/template_register.py --draft <草稿目录> --receipt <模板审批回执.json> --catalog <template_packs目录>
```

只有 `APPROVED + COMPILED + VALIDATED` 且没有未关闭 finding 的 pack 才能进入正式目录。第二份结构明显不同的真实 DOCX 模板及其有效回执缺失时，可以验证框架和单模板兼容性，但不得声称多模板正式发布完成。

## 状态与提速边界

- `PASS`：所有关卡通过；只有同时 `deliverable=true` 才可交付。
- `HUMAN_REVIEW`：模板选择、未知/变化模板、决策、OCR 事实、归一化差异或审批仍需人工确认。
- `FAIL`：schema、哈希、证据或确定性不变量明确不一致。
- `BLOCKED`：加密/损坏文件、不支持目标模板、硬依赖缺失、编译器无法安全表达关键对象或双引擎失败。

V3 使用内容寻址缓存、阶段遥测和受控 worker 池提速。WPS 与 Word COM 保持串行。缓存仅能复用哈希、版本、审批、引擎身份和完整验证证据均未漂移的 `PASS` 成品；任何漂移都完整重跑。

## 按需参考

- 模板识别、IR、DSL、生命周期与审批：`references/template-onboarding-contract-v3.md`
- 外部语义任务与 Prompt 负载：`references/prompt-architecture-v3.md` 和 `references/schemas/decision-task-bundle-v3.schema.json`
- V3 decision、执行计划与回执结构：`references/schemas/` 下对应 JSON Schema
- 运行时安装和 OCR 回退：`references/runtime-dependencies.md`
- 性能基准：`references/performance-contract-v3.md`
- 正式矩阵、黄金盲验与发布：`references/release-matrix-v3.md`、`references/golden-visual-review-v3.md`、`references/release-evidence-contract.md`
- 独立盲验：仅在版本发布、重大规则/模板变化或用户明确要求时读取 `references/blind-validator-prompt.md`
- 已知失败与最小修复：只有实际失败后再读取 `references/failure-patterns.md`、`references/repair-policy.md`

版本目录更新前运行 `<python> scripts/refresh_manifest.py <项目目录>`。`.codegraph/`、测试、开发证据、缓存、`__pycache__/` 与 `*.pyc` 不进入正式 manifest。`agents/prompts/` 是运行时资源，必须随项目发布。

发布门禁通过后，显式指定新的项目部署目录：

```powershell
<python> scripts/publish_v3.py --source-root <项目目录> --destination <新部署目录> --release-report <已通过的发布报告.json> --report <发布结果.json>
```

不再默认部署到历史 Skill 目录；目标目录已存在时停止，不覆盖旧版本。开发分支、单元测试或单个转换案例通过均不代表正式版本已发布。
