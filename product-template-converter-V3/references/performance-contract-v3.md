# V3 性能与缓存合同

性能优化不能跳过任何 V2 质量门。`StageTelemetryV3` 分别记录机器耗时和人工等待，不把人工等待混入转换速度。

## 内容寻址缓存

| 阶段 | 缓存键 |
|---|---|
| 归一化 | 源 SHA + adapter/Office 身份与版本 |
| inventory/OCR | 规范化 SHA + extractor/OCR 后端/语言包版本 + 页面图哈希 |
| Template IR/Program | 模板 SHA + analyzer/compiler/schema 版本 |
| decision | SourceDocument SHA + pack 版本 + policy/schema SHA |
| 最终成品 | 源、模板、decision、compiler、交付目标、验证配置、WPS/Word 身份与版本的完整组合 |

哈希或版本变化即失效。缓存缺失或损坏只能重算，不能转成成功。复用已 PASS 的最终成品前仍须重新核对成品 SHA、manifest、审批回执和当前引擎身份；漂移时完整重跑。

静态 validator、页面栅格化、两引擎页面分析和 OCR 可以在受控 worker 池并行。WPS 与 Word COM 必须保持串行和单一进程所有权。

## 验收

- V2 与 V3 使用同机、同运行时、同源件、同目标、同 decision、同模板和同 Office 版本。
- V2 基线至少 3 次；V3 冷路径清空自身缓存后至少 3 次；V3 热路径预热后至少 5 次。
- V3 冷路径机器耗时中位数不高于 V2 的 70%。
- V3 热路径机器耗时中位数不高于 V2 冷路径的 40%。
- 每个 `duration_ms` 必须存在、可转换为有限数值且不小于零；缺失、文本、负数、`NaN` 或正负无穷大均为 `PERFORMANCE_SAMPLE_DURATION_INVALID`，该样本不得进入中位数或比例计算，整个性能门禁保持 `FAIL`。
- 任一状态、deliverable、内容覆盖、页面数、身份扫描或质量签名不一致，样本按质量失败处理，不能用于证明提速。
- 显式 `--work-root` 必须为空；基准工具不会读取或覆盖旧运行目录中的报告。
- 每次计时前必须删除该次运行预期的 pipeline report；命令失败但残留旧 `PASS` 报告时不得计入样本。
- 所有被统计的子进程退出码必须为 `0`。预热运行本身也必须 `PASS + deliverable=true`，否则热路径整体失败。
- 每一个热路径样本都必须在 pipeline report 中证明 `final_artifact_cache.status=HIT` 且最终交付阶段 `cache_reused=true`；仅仅共用同一 cache 目录不能证明完全命中有效缓存。
- 冷路径样本一旦出现最终成品缓存 `HIT` 即失败，不能混入冷路径中位数。
