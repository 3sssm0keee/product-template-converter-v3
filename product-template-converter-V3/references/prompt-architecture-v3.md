# V3 Prompt 架构与可行性

## V2 现场架构

V2 仓库内没有模型 SDK、API 调用、prompt composer、结构化响应解析或模型重试。代码生成 `content_map v1` 骨架后以 `HUMAN_REVIEW` 停止，由仓库外部的人或 agent 填写并再次运行。

V2 的两份 system prompt 文本本身不长；主要低效来自外部 agent 需要同时理解 Skill、六份合同、Markdown schema、prompt、Python validator 和 renderer 中重复或漂移的规则。V2 prompt 还声称输出 `ContentPlan v2`，而总控实际接收 `content_map v1`。

## V3 可行方案

V3 不内置供应商 SDK、API Key、网络重试或模型配置。生产代码只生成供应商中立的 `DecisionTaskBundleV3`，并接收严格校验的 `ContentDecisionV3`。

固定过程如下：

1. 代码先完成真实格式识别、元数据、链接、联系方式、域名、邮箱、二维码、重复背景、显然表格/图片类型和确定性候选提取。
2. 只有代码不能确定的 item 进入任务包。
3. 同一个 `source_id` 的内容归类与身份判断合并为一份 decision，避免两次加载完整源件。
4. 每包最多 40 个 item 或 16,000 个内联证据字符，以先达到者为准；单个 item 不拆包。
5. 大文本、原图和页面只保存本地相对路径、页码和 SHA-256 引用。
6. system instruction 不超过 2,000 字符；授权、Office、交付、发布和修复规则不进入语义任务。
7. JSON Schema 使用 Draft 2020-12、`additionalProperties=false`、枚举、条件必填和证据约束。
8. 模型置信度只用于复核排序，不能代替证据或审批。
9. 无 decision 时返回 `HUMAN_REVIEW / DECISION_BUNDLE_REQUIRED`；schema 非法为 `FAIL / DECISION_SCHEMA_INVALID`；缺证据、冲突、低置信或未覆盖 source ID 为 `HUMAN_REVIEW`。

独立盲验 prompt 与日常 DecisionTaskBundle 保持完全隔离。

## 负载验收

- 日常 decision 任务不得要求读取完整 Skill 和六份交付合同。
- 固定自然语言指令不超过 2,000 字符。
- 同一 `source_id` 在一个任务包中只出现一次。
- 同一证据不重复内联。
- 与 V2 完整启动上下文相比，固定规则文本负载至少降低 70%。
