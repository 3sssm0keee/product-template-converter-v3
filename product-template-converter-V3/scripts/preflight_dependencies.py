from __future__ import annotations

import argparse
import json
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Callable

from pipeline_common import finish, result
from powershell_host_v3 import PowerShellHost, PowerShellHostError, WINRT_OCR_POWERSHELL_HOST_ENV, probe_powershell_host, resolve_powershell_host


MODULES = {
    "docx": "python-docx",
    "pptx": "python-pptx",
    "lxml": "lxml",
    "PIL": "Pillow",
    "pypdf": "pypdf",
}
STANDARD_TESSERACT = (
    Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
)
WORD_PROGID = "Word.Application"
WPS_PROGIDS = ("kwps.Application", "KWps.Application")
REGISTRY_VIEWS = ("Registry64", "Registry32")


def run_command(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def powershell_host_report(host: PowerShellHost) -> tuple[dict[str, Any], dict[str, Any] | None]:
    return probe_powershell_host(host, timeout=10, runner=run_command)


def com_registered(progid: str) -> bool:
    """只检查 COM 注册，避免预检阶段启动 Office 造成假超时。"""
    command = "if([type]::GetTypeFromProgID('%s')){exit 0}else{exit 2}" % progid
    powershell = resolve_powershell_host()
    completed = run_command([str(powershell.path), "-NoProfile", "-Command", command], timeout=15)
    return bool(completed and completed.returncode == 0)


def com_server_command(progid: str) -> str:
    command = (
        "$type=[type]::GetTypeFromProgID('%s');"
        "if($null -eq $type){exit 2};"
        "$clsid='{'+$type.GUID.ToString().ToUpper()+'}';"
        "$key='Registry::HKEY_CLASSES_ROOT\\CLSID\\'+$clsid+'\\LocalServer32';"
        "if(-not(Test-Path -LiteralPath $key)){exit 3};"
        "$value=(Get-ItemProperty -LiteralPath $key).'(default)';"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "[Console]::Write($value)"
    ) % progid
    powershell = resolve_powershell_host()
    completed = run_command([str(powershell.path), "-NoProfile", "-Command", command], timeout=15)
    return completed.stdout.strip() if completed and completed.returncode == 0 else ""


def server_executable(command: str) -> str:
    """从 LocalServer32 的带引号或不带引号命令中取出 exe 路径。"""
    value = (command or "").strip()
    if not value:
        return ""
    if value.startswith('"'):
        closing_quote = value.find('"', 1)
        return value[1:closing_quote] if closing_quote > 1 else ""
    match = re.search(r"\.exe(?=\s|$)", value, flags=re.IGNORECASE)
    return value[: match.end()] if match else ""


def _windows_basename(path: str) -> str:
    return re.split(r"[\\/]", path.strip().strip('"'))[-1]


def office_server_kind(command: str) -> str:
    executable = server_executable(command)
    name = _windows_basename(executable).casefold()
    if name == "winword.exe":
        return "microsoft-word"
    if name == "wps.exe":
        return "wps-writer"
    return "unknown"


def office_server_owner(command: str) -> str:
    return {
        "microsoft-word": "microsoft_word",
        "wps-writer": "wps",
    }.get(office_server_kind(command), "unknown")


def _candidate_is_valid(
    candidate: dict[str, Any],
    expected_kind: str,
    path_exists: Callable[[str], bool] = lambda value: Path(value).is_file(),
) -> bool:
    command = str(candidate.get("server") or candidate.get("command") or "")
    executable = str(candidate.get("executable") or server_executable(command))
    if "valid" in candidate:
        return bool(candidate["valid"]) and office_server_kind(command or executable) == expected_kind
    return bool(executable) and office_server_kind(command or executable) == expected_kind and path_exists(executable)


def _normalise_registry_records(output: str) -> list[dict[str, Any]]:
    if not output.strip():
        return []
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        return [payload]
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _reg_default_value(key: str) -> str:
    completed = run_command(["reg.exe", "query", key, "/ve"], timeout=10)
    if not completed or completed.returncode != 0:
        return ""
    for line in completed.stdout.splitlines():
        match = re.match(r"^\s+\(Default\)\s+REG_\w+\s+(.*)$", line)
        if match:
            return match.group(1).strip()
    return ""


def _registry_candidates_via_reg(hive: str, progids: tuple[str, ...]) -> list[dict[str, Any]]:
    """通过 reg.exe 读取合并后的用户注册表视图，作为 .NET 视图读取的后备。"""
    hive_name = {"LocalMachine": "HKLM", "CurrentUser": "HKCU"}[hive]
    records: list[dict[str, Any]] = []
    for progid in progids:
        root = f"{hive_name}\\Software\\Classes\\{progid}"
        clsid = _reg_default_value(root + "\\CLSID")
        if not clsid:
            direct = _reg_default_value(root)
            clsid = direct if re.fullmatch(r"\{[0-9A-Fa-f-]{36}\}", direct) else ""
        if not clsid:
            curver = _reg_default_value(root + "\\CurVer")
            if curver:
                clsid = _reg_default_value(f"{hive_name}\\Software\\Classes\\{curver}\\CLSID")
        if not re.fullmatch(r"\{[0-9A-Fa-f-]{36}\}", clsid):
            continue
        server = _reg_default_value(f"{hive_name}\\Software\\Classes\\CLSID\\{clsid}\\LocalServer32")
        if server:
            records.append({"view": "reg.exe", "source": "reg.exe", "progid": progid, "clsid": clsid, "server": server})
    return records


def registry_candidates(hive: str, progids: tuple[str, ...]) -> list[dict[str, Any]]:
    """只读指定注册表 hive 的 ProgID -> CLSID -> LocalServer32 链路。"""
    if hive not in {"LocalMachine", "CurrentUser"}:
        raise ValueError(f"unsupported registry hive: {hive}")
    progid_literals = ",".join('"' + progid.replace('"', '""') + '"' for progid in progids)
    command = rf'''
$ErrorActionPreference = 'Stop'
$views = @('Registry64', 'Registry32')
$progidNames = @({progid_literals})
$records = @()
foreach ($viewName in $views) {{
  $base = $null
  try {{
    $view = [Microsoft.Win32.RegistryView]$viewName
    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::{hive}, $view)
    foreach ($progid in $progidNames) {{
      $progidKey = $null
      $progidClsidKey = $null
      $curVerKey = $null
      $curVerClsidKey = $null
      $serverKey = $null
      try {{
        $progidKey = $base.OpenSubKey(('Software\Classes\' + $progid))
        if ($null -eq $progidKey) {{ continue }}

        $progidClsidKey = $progidKey.OpenSubKey('CLSID')
        $clsid = if ($null -ne $progidClsidKey) {{ [string]$progidClsidKey.GetValue('') }} else {{ '' }}
        if ([string]::IsNullOrWhiteSpace($clsid)) {{
          $directValue = [string]$progidKey.GetValue('')
          if ($directValue -match '^\{{[0-9A-Fa-f-]{{36}}\}}$') {{ $clsid = $directValue }}
        }}
        if ([string]::IsNullOrWhiteSpace($clsid)) {{
          $curVerKey = $progidKey.OpenSubKey('CurVer')
          $curVer = if ($null -ne $curVerKey) {{ [string]$curVerKey.GetValue('') }} else {{ '' }}
          if (-not [string]::IsNullOrWhiteSpace($curVer)) {{
            $curVerClsidKey = $base.OpenSubKey(('Software\Classes\' + $curVer + '\CLSID'))
            if ($null -ne $curVerClsidKey) {{ $clsid = [string]$curVerClsidKey.GetValue('') }}
          }}
        }}
        if ([string]::IsNullOrWhiteSpace($clsid)) {{ continue }}

        $serverKey = $base.OpenSubKey(('Software\Classes\CLSID\' + $clsid + '\LocalServer32'))
        if ($null -eq $serverKey) {{ continue }}
        $server = [string]$serverKey.GetValue('')
        if ([string]::IsNullOrWhiteSpace($server)) {{ continue }}
        $records += [pscustomobject]@{{ view = $viewName; progid = $progid; clsid = $clsid; server = $server }}
      }} finally {{
        if ($null -ne $serverKey) {{ $serverKey.Dispose() }}
        if ($null -ne $curVerClsidKey) {{ $curVerClsidKey.Dispose() }}
        if ($null -ne $curVerKey) {{ $curVerKey.Dispose() }}
        if ($null -ne $progidClsidKey) {{ $progidClsidKey.Dispose() }}
        if ($null -ne $progidKey) {{ $progidKey.Dispose() }}
      }}
    }}
  }} finally {{
    if ($null -ne $base) {{ $base.Dispose() }}
  }}
}}
if ($records.Count -gt 0) {{ $records | ConvertTo-Json -Compress -Depth 4 }}
'''
    powershell = resolve_powershell_host()
    completed = run_command([str(powershell.path), "-NoProfile", "-Command", command], timeout=20)
    records = _normalise_registry_records(completed.stdout if completed and completed.returncode == 0 else "")
    if not records and hive == "CurrentUser":
        records = _registry_candidates_via_reg(hive, progids)
    return records


def _with_candidate_validity(records: list[dict[str, Any]], expected_kind: str) -> list[dict[str, Any]]:
    candidates = []
    for record in records:
        candidate = dict(record)
        candidate["executable"] = server_executable(str(candidate.get("server") or ""))
        candidate["kind"] = office_server_kind(str(candidate.get("server") or ""))
        candidate["valid"] = _candidate_is_valid(candidate, expected_kind)
        candidates.append(candidate)
    return candidates


def machine_word_candidates() -> list[dict[str, Any]]:
    return _with_candidate_validity(registry_candidates("LocalMachine", (WORD_PROGID,)), "microsoft-word")


def current_user_wps_candidates() -> list[dict[str, Any]]:
    return _with_candidate_validity(registry_candidates("CurrentUser", (*WPS_PROGIDS, WORD_PROGID)), "wps-writer")


def assess_machine_word_candidates(
    candidates: list[dict[str, Any]],
    path_exists: Callable[[str], bool] = lambda value: Path(value).is_file(),
) -> dict[str, Any]:
    valid = []
    for candidate in candidates:
        item = dict(candidate)
        item["executable"] = str(item.get("executable") or server_executable(str(item.get("server") or "")))
        item["kind"] = office_server_kind(str(item.get("server") or item["executable"]))
        item["valid"] = _candidate_is_valid(item, "microsoft-word", path_exists)
        if item["valid"]:
            valid.append(item)
    executable_paths = {item["executable"].casefold() for item in valid}
    return {
        "candidates": candidates,
        "valid_candidates": valid,
        "available": bool(valid),
        "view_conflict": len(executable_paths) > 1,
    }


def probe_context() -> dict[str, Any]:
    """标记当前进程是否能代表用户桌面；不访问 COM，也不改变系统状态。"""
    command = "if([Environment]::UserInteractive){'true'}else{'false'}"
    powershell = resolve_powershell_host()
    completed = run_command([str(powershell.path), "-NoProfile", "-Command", command], timeout=10)
    interactive: bool | None = None
    if completed and completed.returncode == 0:
        text = completed.stdout.strip().casefold()
        interactive = True if text == "true" else False if text == "false" else None
    session_name = os.environ.get("SESSIONNAME", "")
    if interactive is None and session_name:
        interactive = session_name.casefold() not in {"services", "service"}
    codex_shell = os.environ.get("CODEX_SHELL") == "1" or bool(os.environ.get("CODEX_SESSION_ID"))
    desktop_hkcu_visible = bool(interactive) and not codex_shell
    return {
        "session_name": session_name,
        "user_name": os.environ.get("USERNAME", ""),
        "user_interactive": interactive,
        "desktop_hkcu_visible": desktop_hkcu_visible,
        "codex_shell": codex_shell,
        "probe_scope": "desktop_user" if desktop_hkcu_visible else "codex_sandbox_or_non_desktop",
    }


def _valid_wps_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid = []
    seen = set()
    for candidate in candidates:
        item = dict(candidate)
        item["executable"] = str(item.get("executable") or server_executable(str(item.get("server") or "")))
        item["kind"] = office_server_kind(str(item.get("server") or item["executable"]))
        item["valid"] = _candidate_is_valid(item, "wps-writer")
        key = (str(item.get("progid") or "").casefold(), item["executable"].casefold())
        if item["valid"] and key not in seen:
            seen.add(key)
            valid.append(item)
    return valid


def assess_office(
    *,
    effective_word_registered: bool,
    effective_word_server: str,
    wps_registered: bool,
    wps_candidates: list[dict[str, Any]],
    machine_candidates: list[dict[str, Any]],
    context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    context = dict(context or probe_context())
    machine = assess_machine_word_candidates(machine_candidates)
    valid_wps = _valid_wps_candidates(wps_candidates)
    owner = office_server_owner(effective_word_server)
    effective_wps = {
        "progid": WORD_PROGID,
        "source": "effective_com_registration",
        "server": effective_word_server,
    }
    if owner == "wps" and _candidate_is_valid(effective_wps, "wps-writer"):
        valid_wps = _valid_wps_candidates([effective_wps, *valid_wps])
    wps_server = valid_wps[0]["server"] if valid_wps else (wps_candidates[0].get("server", "") if wps_candidates else "")
    office = {
        "servers": {"word": effective_word_server, "wps": wps_server},
        "effective_word_server": effective_word_server,
        "effective_word_owner": owner,
        "word_machine_candidates": machine_candidates,
        "runtime_identity_required_at_export": True,
        "probe_context": context,
        "wps": {
            "role": "primary",
            "registered": wps_registered,
            "available": bool(valid_wps),
            "server": wps_server,
            "candidates": valid_wps,
        },
        "word": {
            "role": "backup_compatibility",
            "effective_registered": effective_word_registered,
            "effective_server": effective_word_server,
            "effective_owner": owner,
            "machine_installed": machine["available"],
            "machine_candidates": machine["valid_candidates"],
        },
    }
    findings: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    if not valid_wps:
        if context.get("desktop_hkcu_visible") is True:
            findings.append({
                "code": "WPS_COM_UNAVAILABLE",
                "role": "primary",
                "server": wps_server,
                "probe_context": context,
            })
            actions.append(action("INSTALL_WPS", "安装或修复 WPS Writer，并确认当前用户的 kwps.Application 或 KWps.Application COM 注册及 WPS 可执行文件有效。"))
        else:
            office["wps"]["availability"] = "unknown"
            office["probe_context"]["note"] = "当前运行上下文无法证明真实桌面 HKCU；未将 WPS 判定为缺失，请在交互式桌面用户会话复检。"
            findings.append({
                "code": "WPS_COM_PROBE_CONTEXT_UNVERIFIED",
                "role": "primary",
                "server": wps_server,
                "probe_context": context,
            })
            actions.append(action(
                "RERUN_OFFICE_PREFLIGHT_IN_DESKTOP_CONTEXT",
                "当前 Codex/非桌面上下文无法证明 WPS Writer COM 状态；请在真实桌面用户会话和项目指定 PowerShell 版本下重新运行预检。",
            ))
    if not machine["available"]:
        findings.append({
            "code": "WORD_MACHINE_INSTALLATION_UNAVAILABLE",
            "role": "backup_compatibility",
            "candidates": machine_candidates,
        })
        actions.append(action("INSTALL_WORD", "安装或修复机器级 Microsoft Word，并确认 HKLM\\Software\\Classes 的 Registry64/Registry32 均能解析到有效 WINWORD.EXE。"))
    elif machine["view_conflict"]:
        findings.append({
            "code": "WORD_MACHINE_VIEW_CONFLICT",
            "role": "backup_compatibility",
            "candidates": machine["valid_candidates"],
        })
        actions.append(action("REPAIR_WORD_REGISTRATION", "Registry64 与 Registry32 指向不同的有效 WINWORD.EXE；统一 Microsoft Word 安装或注册后再复检。"))
    return office, findings, actions


def locate_tesseract() -> str:
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in STANDARD_TESSERACT:
        if candidate.is_file():
            return str(candidate)
    return ""


def tesseract_languages(executable: str) -> list[str]:
    if not executable:
        return []
    completed = run_command([executable, "--list-langs"], timeout=20)
    if not completed or completed.returncode:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip() and not line.startswith("List of")]


def windows_ocr_languages() -> list[str]:
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Runtime.WindowsRuntime;"
        "[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null;"
        "[Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages|ForEach-Object{$_.LanguageTag}"
    )
    powershell = resolve_powershell_host(os.environ.get(WINRT_OCR_POWERSHELL_HOST_ENV))
    completed = run_command([str(powershell.path), "-NoProfile", "-Command", command], timeout=20)
    if not completed or completed.returncode:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def pillow_jpeg2000() -> bool:
    try:
        from PIL import features

        return bool(features.check("jpg_2000"))
    except Exception:
        return False


def source_has_images(source: Path | None, source_format: str | None = None) -> bool:
    if source is None or not source.is_file():
        return False
    suffix = f".{source_format.lower().lstrip('.')}" if source_format else source.suffix.lower()
    if suffix in {".doc", ".ppt"}:
        # 旧二进制文件在标准化前无法安全盘点，按可能含图片处理。
        return True
    if suffix in {".docx", ".pptx"}:
        try:
            with zipfile.ZipFile(source) as package:
                return any(name.startswith(("word/media/", "ppt/media/")) for name in package.namelist())
        except Exception:
            return True
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            return any(len(list(page.images)) > 0 for page in PdfReader(source).pages)
        except Exception:
            return True
    return False


def pdf_has_jpeg2000(source: Path | None, source_format: str | None = None) -> bool:
    suffix = f".{source_format.lower().lstrip('.')}" if source_format else (source.suffix.lower() if source else "")
    if source is None or not source.is_file() or suffix != ".pdf":
        return False
    try:
        from pypdf import PdfReader

        for page in PdfReader(source).pages:
            for image in page.images:
                name = str(getattr(image, "name", "")).lower()
                if name.endswith((".jp2", ".jpx", ".jpf")) or image.data[:12].startswith(b"\x00\x00\x00\x0cjP  \r\n\x87\n"):
                    return True
    except Exception:
        return True
    return False


def action(code: str, message: str, commands: list[str] | None = None) -> dict:
    return {"code": code, "message": message, "commands": commands or []}


def main() -> int:
    parser = argparse.ArgumentParser(description="安装后及每次任务前检查固定模板转换运行依赖")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--source-format", choices=["doc", "docx", "ppt", "pptx", "pdf"])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    findings = []
    actions = []
    try:
        powershell_host = resolve_powershell_host()
    except PowerShellHostError as exc:
        return finish(result(
            "BLOCKED",
            "preflight_dependencies",
            findings=[{
                "code": "POWERSHELL_HOST_UNAVAILABLE",
                "source": exc.source,
                "candidate": exc.candidate,
                "message": str(exc),
            }],
            actions=[action("SET_POWERSHELL_HOST", "提供有效的 PowerShell 7 或 Windows PowerShell 可执行文件路径，并通过 --powershell-host 或 PTC_POWERSHELL_HOST 传入。")],
            powershell_host={"source": exc.source, "path": exc.candidate, "available": False},
            message="PowerShell host 不可用；未执行 COM/OCR 预检。",
        ), args.report)
    powershell_report, powershell_finding = powershell_host_report(powershell_host)
    if powershell_finding is not None:
        return finish(result(
            "BLOCKED",
            "preflight_dependencies",
            findings=[powershell_finding],
            actions=[action("SET_POWERSHELL_HOST", "提供可启动的 PowerShell 7 或 Windows PowerShell 可执行文件路径，并通过 --powershell-host 或 PTC_POWERSHELL_HOST 传入。")],
            powershell_host=powershell_report,
            message="PowerShell host 无法启动；未执行 COM/OCR 预检。",
        ), args.report)
    suffix = f".{args.source_format}" if args.source_format else (args.source.suffix.lower() if args.source else "")
    facts = {
        "python": sys.executable,
        "python_version": list(sys.version_info[:3]),
        "powershell_host": powershell_report,
        "source_type": suffix,
        "modules": {},
        "executables": {},
        "office": {},
        "ocr": {},
        "image_decoders": {},
    }
    if sys.version_info < (3, 10):
        findings.append({"code": "PYTHON_VERSION_UNSUPPORTED", "required": ">=3.10"})
        actions.append(action("INSTALL_PYTHON", "安装 Python 3.10 或更高版本，或使用 Codex 工作区依赖加载器返回的 Python。"))
    missing_packages = []
    for module, package in MODULES.items():
        available = importlib.util.find_spec(module) is not None
        facts["modules"][module] = available
        if not available:
            findings.append({"code": "PYTHON_MODULE_MISSING", "module": module, "package": package})
            missing_packages.append(package)
    if missing_packages:
        package_list = " ".join(sorted(set(missing_packages)))
        actions.append(action(
            "INSTALL_PYTHON_PACKAGES",
            "在任务输出目录创建隔离虚拟环境后安装缺失包，不要改系统 Python。",
            [
                '"<python>" -m venv "<output-dir>\\.venv"',
                '"<output-dir>\\.venv\\Scripts\\python.exe" -m pip install ' + package_list,
            ],
        ))

    pdftoppm = shutil.which("pdftoppm") or ""
    facts["executables"]["pdftoppm"] = pdftoppm
    if not pdftoppm:
        findings.append({"code": "EXECUTABLE_MISSING", "name": "pdftoppm"})
        actions.append(action(
            "INSTALL_POPPLER",
            "调用 Codex 工作区依赖加载器使用内置 Poppler，或安装 Poppler 并把 pdftoppm 加入 PATH。",
        ))

    effective_word_registered = com_registered(WORD_PROGID)
    effective_word_server = com_server_command(WORD_PROGID) if effective_word_registered else ""
    wps_registered = False
    wps_servers = []
    for progid in WPS_PROGIDS:
        registered = com_registered(progid)
        wps_registered = wps_registered or registered
        if registered:
            server = com_server_command(progid)
            if server:
                wps_servers.append({"progid": progid, "source": "effective_com_registration", "server": server})
    wps_servers.extend(current_user_wps_candidates())
    office, office_findings, office_actions = assess_office(
        effective_word_registered=effective_word_registered,
        effective_word_server=effective_word_server,
        wps_registered=wps_registered,
        wps_candidates=wps_servers,
        machine_candidates=machine_word_candidates(),
        context=probe_context(),
    )
    facts["office"] = office
    findings.extend(office_findings)
    actions.extend(office_actions)
    if args.source and suffix in {".ppt", ".pptx"}:
        facts["office"]["powerpoint"] = com_registered("PowerPoint.Application")
        if not facts["office"]["powerpoint"]:
            findings.append({"code": "POWERPOINT_COM_UNAVAILABLE"})
            actions.append(action("INSTALL_POWERPOINT", "安装或修复 Microsoft PowerPoint，并确认 PowerPoint.Application COM 已注册。"))

    images_expected = source_has_images(args.source, args.source_format)
    facts["images_expected"] = images_expected
    jpeg2000 = pillow_jpeg2000()
    facts["image_decoders"]["pillow_jpeg2000"] = jpeg2000
    if pdf_has_jpeg2000(args.source, args.source_format) and not jpeg2000:
        findings.append({"code": "JPEG2000_DECODER_MISSING", "required_for": "PDF JP2 images"})
        actions.append(action(
            "INSTALL_JPEG2000_SUPPORT",
            "安装带 OpenJPEG/JPEG2000 支持的 Pillow；否则 PDF 中 JP2 图片不能可靠转换为 Word 兼容 PNG。",
            ['"<python>" -m pip install --upgrade Pillow'],
        ))

    tesseract = locate_tesseract()
    tess_languages = tesseract_languages(tesseract)
    pytesseract_available = importlib.util.find_spec("pytesseract") is not None
    win_languages = windows_ocr_languages()
    windows_ocr_ready = any(language.lower().startswith("zh-hans") for language in win_languages) and any(
        language.lower().startswith("en") for language in win_languages
    )
    tesseract_ready = bool(tesseract and pytesseract_available and {"chi_sim", "eng"}.issubset(set(tess_languages)))
    facts["ocr"] = {
        "windows_media_ocr_languages": win_languages,
        "windows_media_ocr_ready": windows_ocr_ready,
        "tesseract": tesseract,
        "tesseract_languages": tess_languages,
        "pytesseract": pytesseract_available,
        "tesseract_ready": tesseract_ready,
        "selected_backend": "windows-media-ocr" if windows_ocr_ready else "tesseract" if tesseract_ready else "",
    }
    if images_expected and not (windows_ocr_ready or tesseract_ready):
        findings.append({"code": "OCR_BACKEND_UNAVAILABLE", "required_languages": ["zh-Hans-CN/chi_sim", "en-US/eng"]})
        actions.append(action(
            "ENABLE_OCR",
            "启用 Windows 中文(简体)+英文 OCR 语言，或安装 Tesseract 5、chi_sim/eng 语言包及 pytesseract。图片任务缺少 OCR 时禁止宣称厂家零命中。",
            ['"<python>" -m pip install pytesseract'],
        ))
    elif not images_expected and not (windows_ocr_ready or tesseract_ready):
        facts["ocr"]["note"] = "当前源文件未发现图片，OCR 仅作为后续含图片任务的安装提示。"

    status = "BLOCKED" if findings else "PASS"
    message = "运行依赖完整，可以开始转换。" if status == "PASS" else "运行依赖不完整；请先按 actions 补全，再重新运行本检查。"
    return finish(result(status, "preflight_dependencies", findings=findings, actions=actions, message=message, **facts), args.report)


if __name__ == "__main__":
    raise SystemExit(main())
