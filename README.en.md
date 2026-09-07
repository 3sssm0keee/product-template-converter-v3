# Codex / AI Agent Product Document Template Converter V3

[简体中文](README.md) | English

**A document conversion tool for Codex and other AI agents: turn product materials in different formats into product documents that follow a consistent company template.**

The tool extracts text, images, and specification tables from Word, PowerPoint, or PDF files and maps them to sections in an approved DOCX template. After the necessary content review, it produces an editable DOCX or a PDF for mobile reading and sharing.

It is intended for teams preparing product introductions, sales materials, and technical specifications. It reduces manual copying, image placement, table preparation, and formatting work.

Example: supplier PDF manual → content extraction and section mapping → content review → product introduction in the company’s DOCX/PDF format.

This is a **local Python command-line tool for Windows**, not a standalone desktop installer. Content mapping requires a person or an external model; arbitrary documents are not guaranteed to convert automatically without review.

## Working with Codex and other AI agents

This project is designed for agent-assisted document workflows. Codex, or another AI agent with local file access, command execution, and structured JSON capabilities, can invoke the Python scripts, prepare content decisions, and explain validation results. A user reviews business content and template approvals. Experienced users can also run the commands directly.

Typical workflow:

1. Give the agent the source material, target template, and delivery target (DOCX or PDF).
2. The agent checks the environment using the requirements below, then runs `run_pipeline.py` to extract content and generate task bundles.
3. The agent prepares `ContentDecisionV3` from the source and task bundles, referring uncertain content to the user.
4. The user reviews the generated review materials and obtains a valid receipt. The agent resumes the pipeline with the decision file and receipt.
5. The agent checks the result and delivers files only when `status=PASS` and `deliverable=true`.

The agent provides orchestration and content assistance; the Python scripts perform conversion and validation. The project does not bundle an agent client, model service, or API key, and is not a packaged MCP server. Other agents need their own local file and command execution capabilities.

## Inputs and outputs

| Item | Supported scope |
| --- | --- |
| Source files | DOC, DOCX, PPT, PPTX, PDF |
| Target template | A registered and approved DOCX template |
| `desktop` | Editable DOCX for further editing and document delivery |
| `mobile` | PDF exported by WPS for reading and sharing |

Images retain their original display size where possible and are scaled down when necessary to fit the template. Empty sections retain headings according to template rules. Uncertain content placement, OCR results, or template structures require review.

The distribution includes two templates:

- `zxty-fixed-v1@1.0.0`: the first company template.
- `second-template-portable-v3@1.0.0`: the second, portable template.

Custom templates must go through template onboarding. Replacing a template file does not preserve the validity of an earlier approval.

## Download

Download the ZIP and `SHA256SUMS.txt` from the [v3.0.0 release](https://github.com/3sssm0keee/product-template-converter-v3/releases/tag/v3.0.0). This release has been updated in place at the maintainer’s request, so use the checksum supplied with the current download.

The ZIP contains scripts and resources needed for V3 conversion, content review, and template onboarding. It excludes obsolete standalone entry points, development benchmarks, batch acceptance matrices, release maintenance scripts, customer source files, and development screenshots. Compatibility modules still used by V3, such as `build_fixed_docx.py` and `validate_content_map.py`, remain included.

Extract the archive and open PowerShell in the `product-template-converter-V3` directory before running the commands below. Software and Python dependencies must be installed separately.

## Software requirements

| Software | Requirement | Purpose |
| --- | --- | --- |
| Windows 10/11 | Required | Office COM automation and Windows OCR |
| Python 3.10+ | Required | Runs the processing scripts; distribution checks used Python 3.12 |
| Windows PowerShell 5.1 | Keep installed | Used by Office and system helper workflows; PowerShell 7 may coexist |
| WPS Office Writer | Required | Primary PDF export engine; Writer COM automation must be available and correctly registered |
| Microsoft Word desktop | Required | Compatibility checks and the second export engine; the web version is not sufficient |
| Microsoft PowerPoint desktop | Required for PPT/PPTX inputs | Presentation handling and legacy PPT normalization; PowerPoint COM must be available |
| Poppler: `pdftoppm` | Required | Renders PDFs into page images for inspection; the executable must be on PATH |
| Windows Media OCR | Preferred OCR backend | Image/scanned-content recognition; requires Simplified Chinese and English OCR capabilities |
| Tesseract 5 | Alternative OCR backend | Requires `chi_sim` and `eng` language data plus the `pytesseract` Python package |

The delivery workflow uses both WPS and Word. Installing only one of them, or using LibreOffice alone, does not satisfy the current workflow. Run under a signed-in Windows desktop user; service accounts and restricted sandboxes may not see the correct COM registrations.

## Python packages

| pip package | Requirement | Purpose |
| --- | --- | --- |
| `python-docx` | Required | Read, generate, and inspect DOCX files |
| `python-pptx` | Required | Read PowerPoint content |
| `lxml` | Required | Process document XML |
| `Pillow` | Required | Image conversion and page comparison; JP2 images require OpenJPEG/JPEG2000 support |
| `pypdf` | Required | Read and inspect PDFs |
| `pytesseract` | Only for Tesseract OCR | Python interface to Tesseract; does not include the Tesseract executable |

Create an isolated environment in the extracted runtime directory. Activation is not required:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install python-docx python-pptx lxml Pillow pypdf

# Install only when using Tesseract OCR
.\.venv\Scripts\python.exe -m pip install pytesseract
```

Third-party package versions are not currently pinned. Arbitrary combinations of newer versions have not been verified; run the project’s preflight checks after installation. `pip install` does not install Word, WPS, PowerPoint, Poppler, or the Tesseract application.

Check image and OCR capabilities:

```powershell
pdftoppm -v
.\.venv\Scripts\python.exe -c "from PIL import features; print(features.check('jpg_2000'))"

# For Tesseract users: the output should include chi_sim and eng
tesseract --list-langs
```

## Getting started

### 1. Check the environment

```powershell
.\.venv\Scripts\python.exe scripts/post_install_check.py --report post-install.json
.\.venv\Scripts\python.exe scripts/dependency_bindings_v3.py --output dependency-bindings.json

# Replace this example path with your actual input file
.\.venv\Scripts\python.exe scripts/preflight_dependencies.py --source "D:\materials\product.pdf" --report source-preflight.json
```

If a report returns `BLOCKED`, follow its `message` and `actions`. A basic preflight without an input file does not prove that OCR, PowerPoint, or actual exports have all been verified.

### 2. Check local access authorization

```powershell
.\.venv\Scripts\python.exe scripts/access_gate.py check
```

The current code retains a local 30-day trial and renewal mechanism. The first authorization check starts the trial; renewal requires a method provided by the maintainer. Making the repository public does not remove this runtime restriction. Authorization state is stored in the current Windows user’s directory and registry and requires write and read-back access.

### 3. Select a template and start conversion

```powershell
.\.venv\Scripts\python.exe scripts/run_pipeline.py `
  --source "D:\materials\product.pdf" `
  --output-dir "D:\conversion-output\product-a" `
  --template-pack "second-template-portable-v3@1.0.0" `
  --dependency-bindings dependency-bindings.json `
  --python .\.venv\Scripts\python.exe `
  --delivery-target desktop
```

Use `--delivery-target mobile` for PDF output. The first run may stop at `HUMAN_REVIEW`. This means the generated content tasks and review materials need attention; it does not mean a finished document has been delivered.

### 4. Complete content mapping and review

Use the task bundle to confirm the destination sections for text, images, and specifications. Prepare a valid `ContentDecisionV3`, review it through the corresponding local HTML queue, and export a valid review receipt. Then run the complete command with those files:

```powershell
.\.venv\Scripts\python.exe scripts/run_pipeline.py `
  --source "D:\materials\product.pdf" `
  --output-dir "D:\conversion-output\product-a" `
  --template-pack "second-template-portable-v3@1.0.0" `
  --dependency-bindings dependency-bindings.json `
  --python .\.venv\Scripts\python.exe `
  --decision-bundle "D:\review\content-decision-v3.json" `
  --review-receipt "D:\review\review-receipt-v3.json" `
  --delivery-target desktop
```

Only outputs with both `status=PASS` and `deliverable=true` are ready for delivery. `HUMAN_REVIEW` means review is needed, `BLOCKED` indicates missing dependencies or unsupported conditions, and `FAIL` indicates a failed check.

The project does not include a model service, provider SDK, or API key. Content decisions can be supplied by a person or an externally configured model. Codex is not a runtime dependency.

## Limitations and verification scope

- Changes to templates, source files, or content decisions can invalidate earlier receipts. Historical tasks are not automatically migrated.
- Scanned documents, complex layouts, and uncertain images or slots may require manual work.
- Included templates retain company branding approved for this public distribution. Public repository access does not automatically grant an open-source license for all code and assets.
- The original V3 baseline recorded 510 passing tests and 16 passing conversion cases. Distribution changes were checked through dependency analysis, template loading, command-entry checks, and file hashes. The complete conversion matrix and independent blind review were not rerun; historical acceptance is not presented as a fresh acceptance run for this package.

The runtime’s `references/` directory contains content review, template onboarding, and dependency rules. Development tests and release maintenance tools are not included in the runtime package. This English README documents the existing tool; it does not translate its Chinese templates or review interface.

## Contributors

- **3sssm0keee**: Project maintainer responsible for requirements, business review, and release decisions.
- **Codex (OpenAI AI coding agent)**: AI development collaborator contributing to implementation, troubleshooting, validation assistance, and Chinese and English documentation. Business approvals and release decisions remain with the project maintainer.
