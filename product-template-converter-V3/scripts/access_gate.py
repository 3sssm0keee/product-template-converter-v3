from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from powershell_host_v3 import PowerShellHostError, resolve_powershell_host

try:
    import winreg
except ImportError:  # pragma: no cover - Windows is the supported runtime
    winreg = None


PASSWORD_SHA256 = "81ff815a55ada2c7bc29c1341ac64bd47f3b7c11bed09cc747af7416ecd08324"
STATE_SALT = b"zxty-product-template-converter-v2-machine-license"
STATE_VERSION = 3
VALID_DAYS = 30
CLOCK_SKEW = timedelta(minutes=5)
REGISTRY_KEY = r"Software\ZXTY\ProductTemplateConverterV2"
REGISTRY_VALUE = "LicenseState"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def default_state_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    return base / "ZXTY" / "zxty-product-template-converter-v2" / "license_state.json"


def password_ok(password: str) -> bool:
    digest = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, PASSWORD_SHA256)


def _machine_guid() -> str:
    if winreg is None:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value).strip()
    except OSError:
        return ""


def _bios_uuid() -> str:
    try:
        powershell = resolve_powershell_host()
    except PowerShellHostError:
        return ""
    try:
        completed = subprocess.run(
            [str(powershell.path), "-NoProfile", "-Command", "(Get-CimInstance -ClassName Win32_ComputerSystemProduct -ErrorAction Stop).UUID"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    value = completed.stdout.strip().upper()
    compact = value.replace("-", "")
    if completed.returncode != 0 or not compact or set(compact) <= {"0"} or set(compact) <= {"F"}:
        return ""
    return value


def machine_fingerprint() -> str:
    machine_guid = _machine_guid()
    if machine_guid:
        material = f"machine-guid:{machine_guid}"
    else:
        bios_uuid = _bios_uuid()
        if not bios_uuid:
            return ""
        material = f"bios-uuid:{bios_uuid}"
    return hashlib.sha256(f"zxty-machine-v1|{material}".encode("utf-8")).hexdigest().upper()


def canonical_payload(state: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in state.items() if key != "signature"}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def signing_key(fingerprint: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", fingerprint.encode("ascii"), STATE_SALT, 120_000)


def sign(state: dict[str, Any], fingerprint: str) -> str:
    return hmac.new(signing_key(fingerprint), canonical_payload(state), hashlib.sha256).hexdigest()


def signed_state(state: dict[str, Any], fingerprint: str) -> dict[str, Any]:
    payload = dict(state)
    payload["signature"] = sign(payload, fingerprint)
    return payload


def _read_json_file(path: Path) -> tuple[bool, dict[str, Any] | None]:
    if not path.exists():
        return False, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return True, payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return True, None


def _write_json_file(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temp.replace(path)


def _read_registry() -> tuple[bool, dict[str, Any] | None]:
    if winreg is None:
        return False, None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY) as key:
            raw, _ = winreg.QueryValueEx(key, REGISTRY_VALUE)
    except FileNotFoundError:
        return False, None
    except OSError:
        return True, None
    try:
        payload = json.loads(str(raw))
        return True, payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        return True, None


def _write_registry(state: dict[str, Any]) -> None:
    if winreg is None:
        raise OSError("Windows registry is unavailable")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY_KEY, 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, REGISTRY_VALUE, 0, winreg.REG_SZ, json.dumps(state, ensure_ascii=False, separators=(",", ":")))


def _valid_state(payload: dict[str, Any] | None, fingerprint: str) -> bool:
    if not isinstance(payload, dict):
        return False
    required = {
        "state_version",
        "machine_fingerprint",
        "trial_started_at",
        "expires_at",
        "last_seen_at",
        "expired_latched",
        "generation",
        "renewal_count",
        "signature",
    }
    if not required.issubset(payload):
        return False
    if payload.get("state_version") != STATE_VERSION or payload.get("machine_fingerprint") != fingerprint:
        return False
    if not isinstance(payload["expired_latched"], bool):
        return False
    try:
        trial_started = parse_iso(str(payload["trial_started_at"]))
        expires = parse_iso(str(payload["expires_at"]))
        last_seen = parse_iso(str(payload["last_seen_at"]))
        if expires < trial_started or last_seen < trial_started:
            return False
        if int(payload["generation"]) < 1 or int(payload["renewal_count"]) < 0:
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(str(payload["signature"]), sign(payload, fingerprint))


def _state_sources() -> list[tuple[str, bool, dict[str, Any] | None]]:
    return [("localappdata", *_read_json_file(default_state_path())), ("registry", *_read_registry())]


def _explicit_state_file(args: argparse.Namespace) -> Path | None:
    raw = str(getattr(args, "state_file", "") or "").strip()
    return Path(raw) if raw else None


def _select_state(sources: list[tuple[str, bool, dict[str, Any] | None]], fingerprint: str) -> tuple[str, dict[str, Any] | None, str | None]:
    existing = [(name, payload) for name, exists, payload in sources if exists]
    if not existing:
        return "NEW", None, None

    valid = [(name, payload) for name, payload in existing if _valid_state(payload, fingerprint)]
    if len(valid) != len(existing):
        return "INVALID", None, None
    if len(valid) == 2 and valid[0][1] != valid[1][1]:
        return "CONFLICT", None, None
    name, state = valid[0]
    return "VALID", dict(state), name


def _readback_matches(payload: dict[str, Any], fingerprint: str, state_file: Path | None = None) -> tuple[bool, list[str]]:
    failures: list[str] = []
    if state_file is not None:
        exists, state = _read_json_file(state_file)
        if not exists or not _valid_state(state, fingerprint) or state != payload:
            failures.append("state-file:readback mismatch")
        return not failures, failures
    local_exists, local = _read_json_file(default_state_path())
    if not local_exists or not _valid_state(local, fingerprint) or local != payload:
        failures.append("localappdata:readback mismatch")
    registry_exists, registry = _read_registry()
    if not registry_exists or not _valid_state(registry, fingerprint) or registry != payload:
        failures.append("registry:readback mismatch")
    return not failures, failures


def _persist_state(state: dict[str, Any], fingerprint: str, state_file: Path | None = None) -> tuple[bool, list[str]]:
    payload = signed_state(state, fingerprint)
    failures: list[str] = []
    if state_file is not None:
        try:
            _write_json_file(state_file, payload)
        except OSError as exc:
            failures.append(f"state-file:{exc}")
    else:
        try:
            _write_json_file(default_state_path(), payload)
        except OSError as exc:
            failures.append(f"localappdata:{exc}")
        try:
            _write_registry(payload)
        except OSError as exc:
            failures.append(f"registry:{exc}")
    readback_ok, readback_failures = _readback_matches(payload, fingerprint, state_file=state_file)
    if not readback_ok:
        failures.extend(readback_failures)
    return not failures, failures


def _repair_missing_copy(state: dict[str, Any], fingerprint: str, state_file: Path | None = None) -> tuple[bool, list[str]]:
    # A missing copy is repairable; an existing invalid or conflicting copy is not.
    return _persist_state(state, fingerprint, state_file=state_file)


def emit(status: str, message: str, **extra: object) -> int:
    print(json.dumps({"status": status, "message": message, **extra}, ensure_ascii=False))
    return 0 if status == "AUTHORIZED" else 2


def check(args: argparse.Namespace) -> int:
    fingerprint = machine_fingerprint()
    if not fingerprint:
        return emit("MACHINE_ID_UNAVAILABLE", "无法读取稳定的本机系统标识，未执行转换。")

    state_file = _explicit_state_file(args)
    sources = [("state-file", *_read_json_file(state_file))] if state_file is not None else _state_sources()
    state_status, state, source_name = _select_state(sources, fingerprint)
    current = now_utc()

    if state_status == "NEW":
        state = {
            "state_version": STATE_VERSION,
            "machine_fingerprint": fingerprint,
            "trial_started_at": iso(current),
            "expires_at": iso(current + timedelta(days=VALID_DAYS)),
            "last_seen_at": iso(current),
            "expired_latched": False,
            "generation": 1,
            "renewal_count": 0,
        }
        persisted, failures = _persist_state(state, fingerprint, state_file=state_file)
        if not persisted:
            return emit("STATE_PERSIST_FAILED", "无法持久保存本机试用状态，未执行转换。", failures=failures)
        return emit("AUTHORIZED", "本机首次使用已自动开始30天试用。", license_mode="trial", expires_at=state["expires_at"], remaining_seconds=VALID_DAYS * 24 * 60 * 60)

    if state_status in {"INVALID", "CONFLICT"}:
        return emit("STATE_INVALID", "本机授权状态损坏、冲突或与机器不匹配，未执行转换。")

    assert state is not None
    if source_name is not None and any(not exists for name, exists, _payload in sources if name != source_name):
        repaired, failures = _repair_missing_copy(state, fingerprint, state_file=state_file)
        if not repaired:
            return emit("STATE_PERSIST_FAILED", "缺失的本机授权副本无法修复，未执行转换。", failures=failures)

    expires = parse_iso(str(state["expires_at"]))
    last_seen = parse_iso(str(state["last_seen_at"]))
    if current + CLOCK_SKEW < last_seen:
        return emit("CLOCK_ROLLBACK", "检测到系统时间回拨，未执行转换。")

    if state["expired_latched"] or current >= expires:
        if not state["expired_latched"]:
            state["expired_latched"] = True
            state["last_seen_at"] = iso(max(current, last_seen))
            state["generation"] = int(state["generation"]) + 1
            latched, failures = _persist_state(state, fingerprint, state_file=state_file)
            if not latched:
                return emit("STATE_PERSIST_FAILED", "到期锁定状态未能持久保存，未执行转换。", failures=failures)
        password = ""
        if bool(getattr(args, "password_stdin", False)):
            password = sys.stdin.readline().rstrip("\r\n")
        # The old --password field remains callable for in-process compatibility only.
        legacy_password = getattr(args, "password", None)
        if legacy_password and not password:
            password = str(legacy_password)
        if not password:
            return emit("EXPIRED", "本机30天使用期已到，需要通过标准输入提供密码续期后才能继续使用。", password_required=True, expires_at=state["expires_at"], expired_latched=True)
        if not password_ok(password):
            return emit("DENIED", "密码错误，未执行转换。", password_required=True, expired_latched=True)
        renewal_base = max(current, expires, last_seen)
        state["expires_at"] = iso(renewal_base + timedelta(days=VALID_DAYS))
        state["last_seen_at"] = iso(renewal_base)
        state["expired_latched"] = False
        state["generation"] = int(state["generation"]) + 1
        state["renewal_count"] = int(state["renewal_count"]) + 1
        persisted, failures = _persist_state(state, fingerprint, state_file=state_file)
        if not persisted:
            return emit("STATE_PERSIST_FAILED", "续期状态未能持久保存，未执行转换。", failures=failures)
        return emit("AUTHORIZED", "密码验证通过，本机使用期已续期30天。", license_mode="renewed", expires_at=state["expires_at"], remaining_seconds=VALID_DAYS * 24 * 60 * 60, renewal_count=state["renewal_count"])

    state["last_seen_at"] = iso(max(current, last_seen))
    persisted, failures = _persist_state(state, fingerprint, state_file=state_file)
    if not persisted:
        return emit("STATE_PERSIST_FAILED", "授权状态未能持久保存，未执行转换。", failures=failures)
    remaining = max(0, int((expires - current).total_seconds()))
    return emit("AUTHORIZED", "本机使用期有效，可以执行转换。", license_mode="trial" if int(state["renewal_count"]) == 0 else "renewed", trial_started_at=state["trial_started_at"], expires_at=state["expires_at"], remaining_seconds=remaining, renewal_count=state["renewal_count"])


def main() -> int:
    parser = argparse.ArgumentParser(description="固定模板转换 skill 本机试用与续期门槛")
    sub = parser.add_subparsers(dest="command", required=True)
    check_parser = sub.add_parser("check")
    check_parser.add_argument("--password-stdin", action="store_true", help="仅在授权到期时从标准输入读取一行续期密码")
    check_parser.add_argument("--activate-if-needed", action="store_true", help=argparse.SUPPRESS)
    check_parser.add_argument("--state-file", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    return check(args)


if __name__ == "__main__":
    raise SystemExit(main())
