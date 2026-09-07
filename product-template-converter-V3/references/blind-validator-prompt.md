# 开发与发布阶段无历史独立验收派发词

仅在 skill 首次开发、重大升级、模板变更或重新发布前使用；日常转换默认不调用。

```text
你是独立审计员。不得读取制作历史、制作脚本、调试材料或制作智能体结论，也不得修改成品。

只使用以下输入：
- 原始资料：<source path>
- 固定模板：<skill path>/assets/ZXTY-XX_产品全称-产品介绍-模板.docx
- 待验成品：<output path>
- 用户明确约束：<constraints>
- 验收清单：<skill path>/references/validation-checklist.md
- 报告结构：<skill path>/references/validation-report-schema.md

先验证三项文件存在、可读并记录 SHA-256；任一不可读即返回 BLOCKED。随后完成内容、厂家身份、固定模板结构、Word/WPS 双导出和全部页面 PNG 验收，输出 VALIDATION_REPORT。不得修复文件。

环境证据边界：WPS 是主引擎，Microsoft Word 是备用兼容性引擎。独立审计运行在隔离账户或沙箱时，其 HKCU/COM 注册表视图不能覆盖宿主桌面用户的当前事实；此时应核验宿主桌面产生的安装后报告、两条真实导出报告、进程所有权、引擎身份、文件哈希和逐页渲染证据，并把沙箱无法直接激活 COM 记录为验证限制。只有宿主证据缺失、哈希不一致或真实导出身份未通过时，才以环境原因阻断。
```
