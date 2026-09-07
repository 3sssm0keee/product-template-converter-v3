# ReviewAnnotationPortV3

V3 仅在离线复核 HTML 中提供默认禁用、机器可读的 V4 批注端口描述及稳定目标锚点。端口不提供选区、线程、状态回复、完成标签、本地持久化或远程同步交互。

端口描述使用独立接口版本 `1.0.0`，根对象必须包含与当前队列一致的 `review_id`。该字段只用于定位当前 review，不包含隐藏复核身份，也不进入 V3 receipt、binding、review identity hash 或 DOCX/PDF 交付。未来 V4 可通过独立 sidecar 和 `load`、`save`、`export`、`import`、`subscribe` 存储适配器实现批注。

目标锚点仅在同一 `review_id` 内解释：`review-item:<id>`，以及从 1 开始的 `preview-<kind>:<page>:<section>:<block>`。端口只声明当前 V3 HTML 实际会生成 `data-annotation-target-id` / `data-annotation-target-kind` 的目标；没有 `image.path` 的图片块和 `table_rows` 为空的表格块不声明对应图片或表格目标。
