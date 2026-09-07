# 产品资料模板转换工具 V3

简体中文 | [English](README.en.md)

**把不同格式的产品资料，整理成统一公司模板的产品介绍文档。**

输入 Word、PPT 或 PDF 产品资料，提取文字、图片和参数表，将内容对应到指定模板的章节。完成必要复核后，输出可编辑 DOCX，或适合手机阅读、微信发送的 PDF。

适用于产品介绍、销售资料和技术参数的标准化整理，减少人工复制文字、搬运图片、整理表格和调整排版。例如：供应商 PDF 说明书 → 内容提取与章节映射 → 确认内容 → 公司模板的产品介绍 DOCX/PDF。

这是 **Windows 本地 Python 命令行工具**，没有独立桌面安装程序。内容映射需要人工或外部模型参与，不承诺任意资料一键无人工转换。

## 输入与输出

| 项目 | 支持范围 |
| --- | --- |
| 源资料 | DOC、DOCX、PPT、PPTX、PDF |
| 目标模板 | 已注册并批准的 DOCX 模板 |
| `desktop` | 可编辑 DOCX，用于继续编辑及文档交付 |
| `mobile` | WPS 导出的 PDF，用于阅读与发送 |

图片优先保留原显示尺寸，超出模板区域时缩小适配；空章节按模板规则保留标题。无法确定的内容归属、OCR 结果或模板结构会进入待复核状态。

当前提供两套模板：`zxty-fixed-v1@1.0.0`（第一套公司固定模板）和 `second-template-portable-v3@1.0.0`（第二套可移植模板）。自定义模板需要按模板入库流程处理，不能直接替换文件并沿用旧批准。

## 下载

到 [v3.0.0 Release](https://github.com/3sssm0keee/product-template-converter-v3/releases/tag/v3.0.0) 下载 ZIP 和 `SHA256SUMS.txt`。该版本按维护要求覆盖更新，下载时请使用同一批次的校验和。

ZIP 仅包含 V3 转换、复核、模板入库所需的脚本及资源。不包含旧独立入口、开发基准测试、批量验收矩阵、发布维护脚本、客户源件和开发截图。仍被 V3 调用的兼容模块会保留：例如 `build_fixed_docx.py` 和 `validate_content_map.py`，不能仅按名称判断它们已经废弃。

解压后进入 `product-template-converter-V3` 目录，再执行下文命令。所有软件与 Python 包均需另行安装。

## 必需软件与条件依赖

| 软件 | 要求 | 用途 |
| --- | --- | --- |
| Windows 10/11 | 必需 | Office COM 自动化及 Windows OCR 的运行平台 |
| Python 3.10+ | 必需 | 执行处理脚本；本次分发核验使用 Python 3.12 |
| Windows PowerShell 5.1 | 必需保留 | Office/系统辅助流程使用；可同时安装 PowerShell 7 |
| WPS Office 文字组件 | 必需 | PDF 主导出引擎；必须支持并正常注册 Writer COM 自动化 |
| Microsoft Word 桌面版 | 必需 | 双引擎兼容校验与导出；需有效本机安装，网页版不适用 |
| Microsoft PowerPoint 桌面版 | PPT/PPTX 输入时需要 | 演示文稿处理及旧 PPT 归一化，要求 PowerPoint COM 可用 |
| Poppler：`pdftoppm` | 必需 | 把 PDF 渲染成页面图片，供逐页检查；可执行文件须在 PATH 中 |
| Windows Media OCR | OCR 后端二选一，优先使用 | 图片/扫描内容识别，需要简体中文和英文 OCR 语言能力 |
| Tesseract 5 | Windows OCR 不可用时选用 | 需 `chi_sim`、`eng` 语言包，并安装 `pytesseract` |

正式流程需要 WPS 与 Word 两套引擎；仅安装一种或只使用 LibreOffice，不能满足当前流程。在已登录的 Windows 桌面用户环境运行，服务账户或受限沙箱可能看不到正确的 COM 注册信息。

## Python 包

| pip 包名 | 类型 | 用途 |
| --- | --- | --- |
| `python-docx` | 必需 | DOCX 读取、生成和检查 |
| `python-pptx` | 必需 | PowerPoint 内容读取 |
| `lxml` | 必需 | 文档 XML 处理 |
| `Pillow` | 必需 | 图片转换与页面比较；JP2 图片需要 OpenJPEG/JPEG2000 支持 |
| `pypdf` | 必需 | PDF 读取与检查 |
| `pytesseract` | 条件依赖 | Tesseract Python 调用接口；不包含 Tesseract 软件本体 |

在运行目录创建独立环境，无需执行激活脚本：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install python-docx python-pptx lxml Pillow pypdf

# 仅使用 Tesseract OCR 时安装
.\.venv\Scripts\python.exe -m pip install pytesseract
```

发行包暂未锁定第三方包版本，不能保证任意新版本组合都已测试。安装后应执行项目预检；`pip install` 不会替你安装 Word、WPS、PowerPoint、Poppler 或 Tesseract 软件。

可先检查图片与 OCR 能力：

```powershell
pdftoppm -v
.\.venv\Scripts\python.exe -c "from PIL import features; print(features.check('jpg_2000'))"
# 仅使用 Tesseract 时：应能列出 chi_sim 和 eng
tesseract --list-langs
```

## 使用流程

### 1. 检查环境

```powershell
.\.venv\Scripts\python.exe scripts/post_install_check.py --report post-install.json
.\.venv\Scripts\python.exe scripts/dependency_bindings_v3.py --output dependency-bindings.json
# 把路径换成实际源文件，进一步检查该格式和图片/OCR要求
.\.venv\Scripts\python.exe scripts/preflight_dependencies.py --source "D:\资料\产品说明.pdf" --report source-preflight.json
```

检查报告为 `BLOCKED` 时，按 `message`、`actions` 补齐依赖。未指定源件的基础预检 PASS 不代表 OCR、PowerPoint 或实际导出已全部验证。

### 2. 检查本地使用授权

```powershell
.\.venv\Scripts\python.exe scripts/access_gate.py check
```

当前代码保留本地 30 天试用/续期机制：首次检查启动试用，到期需要维护方提供续期方式；仓库公开不会移除运行限制。授权状态保存于当前用户目录和注册表，需要正常写入、回读权限。

### 3. 选择模板并开始转换

```powershell
.\.venv\Scripts\python.exe scripts/run_pipeline.py `
  --source "D:\资料\产品说明.pdf" `
  --output-dir "D:\转换结果\产品A" `
  --template-pack "second-template-portable-v3@1.0.0" `
  --dependency-bindings dependency-bindings.json `
  --python .\.venv\Scripts\python.exe `
  --delivery-target desktop
```

输出 PDF 时使用 `--delivery-target mobile`。第一次运行可能停在 `HUMAN_REVIEW`，表示需要处理生成的内容任务和复核材料，不是成品已完成。

### 4. 完成内容映射和复核

依据任务包确认原文、图片和参数的目标章节，生成符合约定的 `ContentDecisionV3`，在对应本地 HTML 队列复核后导出有效回执。重新运行上面的命令时，追加实际文件参数：

```powershell
  --decision-bundle "D:\复核\content-decision-v3.json" `
  --review-receipt "D:\复核\review-receipt-v3.json"
```

上述两行是参数片段，需接到完整命令中，不能独立执行。仅 `status=PASS` 且 `deliverable=true` 的输出可交付。`HUMAN_REVIEW` 是待复核，`BLOCKED` 是依赖或支持条件不足，`FAIL` 是检查未通过。

本项目不内置大模型服务、供应商 SDK 或 API Key。可由人工或自行配置的外部模型完成内容判断；Codex 不是工具运行的必需依赖。

## 限制与验证范围

- 新增模板、源件或内容决策变化可能使旧回执失效，历史任务不自动迁移。
- 扫描件、复杂布局和未知图片/槽位可能需要人工处理。
- 模板保留公司品牌内容，用户已允许公开；仓库公开不等于自动授予所有代码和素材的开源许可。
- 原 V3 基线记录了 510 项测试和 16 个转换案例通过。本次分发调整采用依赖范围核验、模板加载、入口检查及文件哈希验证，没有重跑完整矩阵或独立盲验，不将历史验收冒称为新包的新一轮验收。

运行目录的 `references/` 保存内容复核、模板入库和依赖规则。开发测试与发布维护工具不属于此运行包。
