# V2 交付合同

调用时在对话中提供两个标准版本供客户选择；这些提示不得写入产品 DOCX、PDF、页眉页脚、表格或正文。

| 目标 | 正式输出 | 使用场景 | 风险提示（仅对话） |
|---|---|---|---|
| `desktop`（默认） | DOCX | 电脑端 WPS 主用，Microsoft Word 备用兼容性验证、编辑与打印 | 微信或手机 DOCX 预览可能重排分页、节和背景 |
| `mobile` | PDF | 微信 Android/iOS 阅读 | PDF 固定版式，不作为直接编辑对象 |

`mobile` 的正式 PDF 必须来自最终中间 DOCX 的 WPS 导出（`wps_pdf_export`），并同时具备 WPS 主引擎与 Microsoft Word 备用兼容性引擎的两份导出及逐页比较证据。WPS 或 Word 任一环境缺失、身份/所有权证据缺失、导出失败、PDF 路径/字节/SHA-256 不真实或重复、页面分析失败、`HUMAN_REVIEW` 或其他非 `PASS` 状态时，必须 `BLOCKED` 或 `deliverable=false`，不得正式交付。

桌面端正式交付仍是可编辑 DOCX：WPS 是主使用环境，Microsoft Word 只作备用兼容性验证；双引擎均为硬门禁，任一缺失或验证失败都不能正式交付。

正式输出目录只保存所选目标的最终文件。基础 DOCX 位于 `work/`，重名中间产物自动追加 `-v2`、`-v3`。
