# 运行依赖与安装后复检

安装、升级或迁移后先运行：

```powershell
<python> scripts/post_install_check.py --report <可写目录>/post_install_dependencies.json
```

报告 `status=PASS` 才能开始正式任务。`BLOCKED` 时向用户展示 `message` 与 `actions`；补全后必须重跑，不能凭“软件已安装”主观判定。

必需：

- Windows 10/11；
- Microsoft Word、Microsoft PowerPoint（处理旧 `.doc/.ppt`）；
- WPS Writer（COM ProgID `kwps.Application` 或 `KWps.Application`），作为主引擎；
- Microsoft Word 机器级安装，作为备用兼容引擎；静态预检从 HKLM `Software\Classes` 的 Registry64 与 Registry32 读取 `Word.Application` 的 CLSID/LocalServer32，并只接受能解析到有效 `WINWORD.EXE` 的候选。两个视图若指向不同有效可执行文件，状态为 `BLOCKED`；
- Windows PowerShell 5.1；
- Python 3.10+；
- Python 包：`python-docx`、`python-pptx`、`lxml`、`Pillow`、`pypdf`；
- Poppler `pdftoppm`，用于 Word/WPS PDF 全页渲染。
- Pillow 必须具备 OpenJPEG/JPEG2000 解码能力；构建器会把 JP2、CMYK 等 Word/WPS 不稳定图片确定性转成 PNG 后嵌入。
- OCR 后端至少一个可用：
  - 首选 Windows Media OCR，系统必须安装 `zh-Hans-CN` 与英文识别语言；或
  - Tesseract 5 + `chi_sim`、`eng` 语言包，并在当前隔离 Python 安装 `pytesseract`。

按输入类型附加：

- `.ppt/.pptx`：PowerPoint COM 注册可用；
- `.pdf`：JPEG2000 解码可用；
- 旧 `.doc`：Word 或 WPS 能完成标准化。

Codex 桌面端优先调用工作区依赖加载器，使用其 Python 和 Poppler 路径。运行 `run_pipeline.py` 时可用 `--python <路径>` 固定所有 Python 子关卡的解释器。

若工作区依赖加载器不可用，在任务输出目录创建隔离虚拟环境并安装 Python 包，再将虚拟环境解释器传给 `--python`。不得修改系统 Python：

```powershell
<python> -m venv <输出目录>/.venv
<输出目录>/.venv/Scripts/python.exe -m pip install python-docx python-pptx lxml Pillow pypdf pytesseract
```

若使用 Windows Media OCR，不要求安装 `pytesseract`，但依赖自检必须确认简体中文与英文语言均可用。若使用 Tesseract，只有 `eng` 没有 `chi_sim` 仍视为缺失。

Office 静态预检只读取 ProgID/注册表和 LocalServer32，不启动 GUI 或 Office COM 实例。报告固定声明 `wps.role=primary`、`word.role=backup_compatibility`、`effective_word_server`、`effective_word_owner`、`word_machine_candidates` 和 `runtime_identity_required_at_export=true`。`Word.Application` 被 WPS 覆盖时记录 effective owner，不把它单独报告为 Word 缺失；但有效 WPS 覆盖不能替代 HKLM 中有效的 Microsoft Word 机器级备用安装。沙箱或服务会话看不到真实桌面 HKCU 时报告 `probe_context` 并暂不把 WPS 判定为缺失，必须在交互式桌面用户会话复检；真正打开/导出的身份和失败仍由导出阶段判定。

授权状态固定写入 `%LOCALAPPDATA%\ZXTY\zxty-product-template-converter-v2\license_state.json` 与 `HKCU\Software\ZXTY\ProductTemplateConverterV2\LicenseState`，并绑定本机系统指纹。两个副本都必须写入并回读一致；沙箱、服务账户或受限执行环境无法访问真实用户配置时应直接 `BLOCKED`，不得改写到任务输出目录，也不存在 `--state-file` 绕过参数。

所有 PowerShell 脚本保持 ASCII 源码以兼容 Windows PowerShell 5；中文路径通过参数传入。

脚本退出码：`0` 表示 `PASS`；`2` 表示 `FAIL/BLOCKED`；`3` 表示流程正常停在 `HUMAN_REVIEW`。外层调度必须同时读取 JSON `status`，不得把退出码 3 当成脚本崩溃。
