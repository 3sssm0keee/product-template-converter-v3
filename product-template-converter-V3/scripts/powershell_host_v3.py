from __future__ import annotations

import os
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


POWERSHELL_HOST_ENV = "PTC_POWERSHELL_HOST"
WINRT_OCR_POWERSHELL_HOST_ENV = "PTC_WINRT_OCR_POWERSHELL_HOST"


@dataclass(frozen=True)
class PowerShellHost:
    path: Path
    source: str
    host_type: str

    def report(self) -> dict[str, str]:
        return {
            "path": str(self.path),
            "source": self.source,
            "host_type": self.host_type,
            "executable": self.path.name,
        }


class PowerShellHostError(FileNotFoundError):
    def __init__(self, source: str, candidate: str, message: str) -> None:
        self.source = source
        self.candidate = candidate
        super().__init__(f"{source} PowerShell host is unavailable: {candidate}. {message}")


def _host_type(path: Path) -> str:
    name = path.name.casefold()
    if name == "pwsh.exe":
        return "powershell7"
    if name == "powershell.exe":
        return "windows-powershell"
    return "unknown"


def _checked_host(value: str | os.PathLike[str], source: str) -> PowerShellHost:
    text = str(value).strip().strip('"')
    if not text:
        raise PowerShellHostError(source, text, "path is empty")
    path = Path(text).expanduser()
    if not path.is_file():
        raise PowerShellHostError(source, text, "file does not exist")
    host_type = _host_type(path)
    if host_type == "unknown":
        raise PowerShellHostError(source, text, "not a PowerShell executable name; expected pwsh.exe or powershell.exe")
    return PowerShellHost(path=path.resolve(), source=source, host_type=host_type)


def probe_powershell_host(
    host: PowerShellHost,
    *,
    timeout: int = 10,
    runner: Callable[[list[str], int], subprocess.CompletedProcess[str] | None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    payload: dict[str, Any] = host.report()
    command = (
        "$payload=[ordered]@{"
        "version=[string]$PSVersionTable.PSVersion;"
        "edition=[string]$PSVersionTable.PSEdition;"
        "bitness=if([Environment]::Is64BitProcess){64}else{32};"
        "process_path=[Diagnostics.Process]::GetCurrentProcess().MainModule.FileName"
        "};"
        "$payload|ConvertTo-Json -Compress"
    )
    invocation = [str(host.path), "-NoProfile", "-Command", command]
    try:
        completed = runner(invocation, timeout) if runner is not None else subprocess.run(
            invocation,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        completed = None
        payload["probe_error"] = str(exc) or "PowerShell host could not be started"

    if not completed or completed.returncode != 0:
        payload["available"] = False
        if completed:
            payload["probe_error"] = (completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}")[-1000:]
        else:
            payload.setdefault("probe_error", "PowerShell host could not be started")
        return payload, {
            "code": "POWERSHELL_HOST_UNAVAILABLE",
            "source": host.source,
            "path": str(host.path),
            "message": payload["probe_error"],
        }

    try:
        identity = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        identity = {}
    if not isinstance(identity, dict) or not identity.get("edition") or not identity.get("version"):
        payload["available"] = False
        payload["probe_error"] = "PowerShell host did not return edition/version identity"
        return payload, {
            "code": "POWERSHELL_HOST_UNAVAILABLE",
            "source": host.source,
            "path": str(host.path),
            "message": payload["probe_error"],
        }

    payload.update({key: value for key, value in identity.items() if value not in ("", None)})
    payload["available"] = True
    return payload, None


def resolve_powershell_host(explicit: str | os.PathLike[str] | None = None) -> PowerShellHost:
    if explicit is not None:
        return _checked_host(explicit, "explicit")

    configured = os.environ.get(POWERSHELL_HOST_ENV)
    if configured is not None:
        return _checked_host(configured, "environment")

    candidates: list[tuple[Path, str]] = []
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append((Path(program_files) / "PowerShell" / "7" / "pwsh.exe", "program-files"))

    discovered_pwsh = shutil.which("pwsh")
    if discovered_pwsh:
        candidates.append((Path(discovered_pwsh), "path"))

    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidates.append((Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe", "system-root"))

    seen: set[str] = set()
    for candidate, source in candidates:
        key = str(candidate).casefold()
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return PowerShellHost(path=candidate.resolve(), source=source, host_type=_host_type(candidate))

    raise PowerShellHostError("auto", "", "no candidate file was found")
