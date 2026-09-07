from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any, Mapping

from pipeline_common import finish, result, sha256_file, utc_now, write_json
from preflight_dependencies import current_user_wps_candidates, server_executable
from schema_validation_v3 import load_schema, validate_instance


SCHEMA_VERSION = "3.0.0"
ARTIFACT_TYPE = "DependencyBindingsV3"
DEPENDENCY_NAMES = (
    "python",
    "office_powershell",
    "winrt_ocr_powershell",
    "pdftoppm",
    "wps_writer",
    "microsoft_word",
    "tesseract",
)
OPTIONAL_DEPENDENCIES = {"tesseract"}
DEPENDENCY_NAME_SET = set(DEPENDENCY_NAMES)
REQUIRED_DEPENDENCIES = DEPENDENCY_NAME_SET - OPTIONAL_DEPENDENCIES
PATH_EXECUTABLES = {
    "python": ("python.exe", "python"),
    "office_powershell": ("pwsh.exe", "pwsh", "powershell.exe", "powershell"),
    "winrt_ocr_powershell": ("powershell.exe", "powershell", "pwsh.exe", "pwsh"),
    "pdftoppm": ("pdftoppm.exe", "pdftoppm"),
    "wps_writer": ("wps.exe", "wps"),
    "microsoft_word": ("WINWORD.EXE", "winword.exe", "winword"),
    "tesseract": ("tesseract.exe", "tesseract"),
}
CONTROLLED_ENV_VARS = {
    name: f"PTC_DEPENDENCY_{name.upper()}_PATH"
    for name in DEPENDENCY_NAMES
}
RESOLUTION_HINTS = {
    "python": "Use the bundled Codex Python runtime or provide PTC_DEPENDENCY_PYTHON_PATH.",
    "office_powershell": "Provide the Office default PowerShell host path with PTC_DEPENDENCY_OFFICE_POWERSHELL_PATH.",
    "winrt_ocr_powershell": "Provide the Windows OCR PowerShell host path with PTC_DEPENDENCY_WINRT_OCR_POWERSHELL_PATH.",
    "pdftoppm": "Install Poppler or provide PTC_DEPENDENCY_PDFTOPPM_PATH.",
    "wps_writer": "Install or repair WPS Writer and provide its executable path.",
    "microsoft_word": "Install or repair Microsoft Word and provide WINWORD.EXE.",
    "tesseract": "Optional OCR fallback; install Tesseract with chi_sim/eng when Windows OCR is unavailable.",
}
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "references" / "schemas" / "dependency-bindings-v3.schema.json"
REBOUND_RUNTIME_FIELDS = {
    "v2_command[0]": ("v2_command", 0),
    "v3_command[0]": ("v3_command", 0),
    "v2_command.--python": ("v2_command", "--python"),
    "v3_command.--python": ("v3_command", "--python"),
}


def _dependency_required(name: str) -> bool:
    return name not in OPTIONAL_DEPENDENCIES


REQUIRED_RUNTIME_DEPENDENCIES = tuple(name for name in DEPENDENCY_NAMES if _dependency_required(name))


def _empty_record(name: str) -> dict[str, Any]:
    return {
        "required": _dependency_required(name),
        "status": "MISSING",
        "path": None,
        "source": "unresolved",
        "version": None,
        "sha256": None,
        "checked_locations": [],
        "resolution_hint": RESOLUTION_HINTS[name],
    }


def _as_path_candidate(value: Any) -> tuple[str, str | None]:
    if isinstance(value, Mapping):
        return str(value.get("path") or "").strip(), (
            None if value.get("version") in ("", None) else str(value.get("version"))
        )
    return str(value or "").strip(), None


def _config_candidate(config: Mapping[str, Any], name: str) -> tuple[bool, str, str | None]:
    dependencies = config.get("dependencies")
    if isinstance(dependencies, Mapping) and name in dependencies:
        raw_path, version = _as_path_candidate(dependencies[name])
        return True, raw_path, version
    if name in config:
        raw_path, version = _as_path_candidate(config[name])
        return True, raw_path, version
    return False, "", None


def _sequence_from_config(config: Mapping[str, Any], section: str, name: str) -> list[Any]:
    raw_section = config.get(section)
    if isinstance(raw_section, Mapping):
        raw_value = raw_section.get(name)
        if isinstance(raw_value, list):
            return raw_value
        if raw_value not in (None, ""):
            return [raw_value]
    return []


def _versioned_localappdata_wps_locations(local_app_data: Path) -> list[Path]:
    # WPS-ABL-02 / NECESSARY：禁用此发现会令版本化安装用例变成 MISSING。
    # 消融证据：docs/handoffs/2026-09-05-wps-discovery-ablation-closeout.md；保留动态目录发现。
    root = local_app_data / "Kingsoft" / "WPS Office"
    try:
        versions = sorted(
            (path for path in root.iterdir() if path.is_dir() and path.name != "office6"),
            reverse=True,
        )
    except OSError:
        return []
    return [version / "office6" / "wps.exe" for version in versions]


def _default_common_locations(name: str, env: Mapping[str, str]) -> list[Path]:
    program_files = Path(env.get("ProgramFiles") or r"C:\Program Files")
    program_files_x86 = Path(env.get("ProgramFiles(x86)") or r"C:\Program Files (x86)")
    system_root = Path(env.get("SystemRoot") or r"C:\Windows")
    local_app_data = Path(env.get("LOCALAPPDATA") or "")
    locations = {
        "python": [Path(sys.executable)],
        "office_powershell": [
            program_files / "PowerShell" / "7" / "pwsh.exe",
            system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        ],
        "winrt_ocr_powershell": [
            system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
            program_files / "PowerShell" / "7" / "pwsh.exe",
        ],
        "pdftoppm": [
            program_files / "poppler" / "Library" / "bin" / "pdftoppm.exe",
            program_files / "poppler" / "bin" / "pdftoppm.exe",
        ],
        "wps_writer": [
            local_app_data / "Kingsoft" / "WPS Office" / "office6" / "wps.exe",
            *_versioned_localappdata_wps_locations(local_app_data),
            program_files / "Kingsoft" / "WPS Office" / "office6" / "wps.exe",
            program_files_x86 / "Kingsoft" / "WPS Office" / "office6" / "wps.exe",
        ],
        "microsoft_word": [
            program_files / "Microsoft Office" / "root" / "Office16" / "WINWORD.EXE",
            program_files_x86 / "Microsoft Office" / "root" / "Office16" / "WINWORD.EXE",
        ],
        "tesseract": [
            program_files / "Tesseract-OCR" / "tesseract.exe",
            program_files_x86 / "Tesseract-OCR" / "tesseract.exe",
        ],
    }
    return [path for path in locations[name] if str(path)]


def _bind_path(record: dict[str, Any], raw_path: str, source: str, version: str | None = None) -> bool:
    if not raw_path:
        return False
    path = Path(raw_path).expanduser()
    record["checked_locations"].append(str(path))
    if not path.is_file():
        return False
    resolved = path.resolve()
    record.update({
        "status": "PASS",
        "path": str(resolved),
        "source": source,
        "version": version,
        "sha256": sha256_file(resolved),
    })
    return True


def _as_registry_path_candidate(value: Any) -> tuple[str, str | None]:
    raw_path, version = _as_path_candidate(value)
    if raw_path:
        return raw_path, version
    if not isinstance(value, Mapping):
        return "", None
    executable = str(value.get("executable") or "").strip()
    if executable:
        return executable, version
    return server_executable(str(value.get("server") or "")), version


def _registry_com_candidates(name: str, config: Mapping[str, Any]) -> list[Any]:
    candidates = list(_sequence_from_config(config, "registry_com", name))
    if name == "wps_writer":
        # WPS-ABL-01 / NECESSARY：移除此接入会破坏 registry_com 优先于 PATH 的契约。
        # 复用 preflight 的动态 COM 识别；沙盒不可见不等于真实机器未安装 WPS。
        # 消融证据：docs/handoffs/2026-09-05-wps-discovery-ablation-closeout.md。
        candidates.extend(current_user_wps_candidates())
    return candidates


def _bind_from_registry_com(record: dict[str, Any], config: Mapping[str, Any], name: str) -> bool:
    for raw in _registry_com_candidates(name, config):
        if isinstance(raw, Mapping) and raw.get("valid") is False:
            continue
        raw_path, version = _as_registry_path_candidate(raw)
        if _bind_path(record, raw_path, "registry_com", version):
            return True
    return False


def _bind_from_path(record: dict[str, Any], env: Mapping[str, str], name: str) -> bool:
    search_path = env.get("PATH")
    if not search_path:
        return False
    for raw_directory in search_path.split(os.pathsep):
        if not raw_directory:
            continue
        directory = Path(raw_directory)
        for executable in PATH_EXECUTABLES[name]:
            if _bind_path(record, str(directory / executable), "path"):
                return True
    return False


def _bind_from_common_locations(record: dict[str, Any], config: Mapping[str, Any], env: Mapping[str, str], name: str) -> bool:
    configured = _sequence_from_config(config, "common_locations", name)
    if name in config.get("common_locations", {}):
        candidates = [Path(str(value)) for value in configured]
    else:
        candidates = _default_common_locations(name, env)
    for candidate in candidates:
        if _bind_path(record, str(candidate), "common_location"):
            return True
    return False


def _discover_one(
    name: str,
    explicit: Mapping[str, str],
    config: Mapping[str, Any],
    env: Mapping[str, str],
) -> dict[str, Any]:
    record = _empty_record(name)

    if name in explicit:
        raw_path, version = _as_path_candidate(explicit[name])
        _bind_path(record, raw_path, "explicit", version)
        return record

    has_config, config_path, config_version = _config_candidate(config, name)
    if has_config:
        _bind_path(record, config_path, "config", config_version)
        return record

    env_var = CONTROLLED_ENV_VARS[name]
    if env_var in env:
        _bind_path(record, env[env_var], "environment")
        return record

    if _bind_from_registry_com(record, config, name):
        return record
    if _bind_from_path(record, env, name):
        return record
    _bind_from_common_locations(record, config, env, name)
    return record


def _machine(env: Mapping[str, str]) -> dict[str, Any]:
    return {
        "hostname": env.get("COMPUTERNAME") or socket.gethostname(),
        "platform": sys.platform,
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
    }


def discover_dependency_bindings(
    explicit: Mapping[str, str] | None = None,
    config: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    explicit = dict(explicit or {})
    config = dict(config or {})
    env = dict(os.environ if env is None else env)

    dependencies = {
        name: _discover_one(name, explicit, config, env)
        for name in DEPENDENCY_NAMES
    }
    findings: list[dict[str, Any]] = []
    for name, record in dependencies.items():
        if record["required"] and record["status"] != "PASS":
            findings.append({
                "code": "DEPENDENCY_MISSING",
                "dependency": name,
                "checked_locations": list(record["checked_locations"]),
                "resolution_hint": record["resolution_hint"],
            })

    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "generated_at": utc_now(),
        "machine": _machine(env),
        "dependencies": dependencies,
        "findings": findings,
        "status": "BLOCKED" if findings else "PASS",
    }


def validate_dependency_bindings(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return validate_instance(payload, load_schema(SCHEMA_PATH))


def _schema_path() -> Path:
    return Path(__file__).resolve().parents[1] / "references" / "schemas" / "dependency-bindings-v3.schema.json"


def _validate_record_file(
    record: Mapping[str, Any],
    name: str,
    *,
    missing_error: type[Exception],
) -> Path:
    raw_path = record.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        raise missing_error(f"dependency is not bound: {name}")
    path = Path(raw_path)
    if not path.is_file():
        raise missing_error(f"dependency path is not a file: {name}: {path}")
    expected_sha = record.get("sha256")
    actual_sha = sha256_file(path)
    if actual_sha != expected_sha:
        raise ValueError(
            f"dependency sha256 drift: {name}: expected {expected_sha}, got {actual_sha}"
        )
    return path.resolve()


def _validate_binding_shape(
    name: str,
    record: Mapping[str, Any],
    *,
    file_error: type[Exception] = ValueError,
) -> None:
    expected_required = _dependency_required(name)
    if record.get("required") is not expected_required:
        raise ValueError(f"dependency required flag mismatch: {name}")

    status = record.get("status")
    source = record.get("source")
    path = record.get("path")
    version = record.get("version")
    sha256 = record.get("sha256")

    if status == "PASS":
        if not isinstance(path, str) or not path:
            raise ValueError(f"PASS dependency must have a path: {name}")
        if source == "unresolved":
            raise ValueError(f"PASS dependency cannot use unresolved source: {name}")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise ValueError(f"PASS dependency must have sha256: {name}")
        _validate_record_file(record, name, missing_error=file_error)
        return

    if status == "MISSING":
        if path is not None or version is not None or sha256 is not None or source != "unresolved":
            raise ValueError(f"MISSING dependency must be unresolved with null path/version/sha256: {name}")
        return

    raise ValueError(f"invalid dependency status: {name}: {status}")


def _validate_dependency_names(dependencies: Mapping[str, Any]) -> None:
    # REDUNDANCY-RETAIN [T1-ABL-C2]: 隔离删除后 18 项中 1 项失败；Schema
    # 拒绝非法键，但不保留 unknown/missing dependency 的诊断契约。
    # 证据：development-evidence/t1-redundancy-2026-09-05/README.md。
    names = set(dependencies)
    unknown = sorted(names - DEPENDENCY_NAME_SET)
    missing = sorted(DEPENDENCY_NAME_SET - names)
    if unknown:
        raise ValueError(f"unknown dependency binding(s): {', '.join(unknown)}")
    if missing:
        raise ValueError(f"missing dependency binding(s): {', '.join(missing)}")


def _validate_findings(payload: Mapping[str, Any]) -> None:
    dependencies = payload["dependencies"]
    findings = payload["findings"]
    if not isinstance(dependencies, Mapping) or not isinstance(findings, list):
        raise ValueError("DependencyBindingsV3 dependencies/findings must be structured")

    required_missing = {
        name
        for name, record in dependencies.items()
        if record.get("required") is True and record.get("status") == "MISSING"
    }
    finding_dependencies = [finding.get("dependency") for finding in findings if isinstance(finding, Mapping)]
    finding_set = set(finding_dependencies)

    if len(finding_dependencies) != len(finding_set):
        raise ValueError("duplicate dependency findings")
    if finding_set != required_missing:
        raise ValueError("findings do not exactly match required missing dependencies")

    for finding in findings:
        if not isinstance(finding, Mapping):
            raise ValueError("dependency finding must be an object")
        dependency = finding.get("dependency")
        if dependency not in REQUIRED_DEPENDENCIES:
            raise ValueError(f"finding is not allowed for optional dependency: {dependency}")
        record = dependencies[dependency]
        if finding.get("code") != "DEPENDENCY_MISSING":
            raise ValueError(f"invalid finding code for dependency: {dependency}")
        if finding.get("checked_locations") != record.get("checked_locations"):
            raise ValueError(f"finding checked_locations mismatch: {dependency}")
        if finding.get("resolution_hint") != record.get("resolution_hint"):
            raise ValueError(f"finding resolution_hint mismatch: {dependency}")

    status = payload.get("status")
    if status == "PASS":
        if findings or required_missing:
            raise ValueError("PASS bindings cannot have findings or required missing dependencies")
    elif status == "BLOCKED":
        if finding_set != required_missing or not required_missing:
            raise ValueError("BLOCKED bindings must have exact required missing findings")
    else:
        raise ValueError(f"invalid top-level dependency binding status: {status}")


def _validate_payload(
    payload: Mapping[str, Any],
    *,
    file_error: type[Exception] = ValueError,
) -> None:
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, Mapping):
        raise ValueError("DependencyBindingsV3.dependencies must be an object")
    _validate_dependency_names(dependencies)

    schema = load_schema(_schema_path())
    errors = validate_instance(payload, schema)
    if errors:
        first = errors[0]
        raise ValueError(f"DependencyBindingsV3 schema validation failed at {first['path']}: {first['message']}")

    for name in DEPENDENCY_NAMES:
        record = dependencies[name]
        if not isinstance(record, Mapping):
            raise ValueError(f"dependency binding must be an object: {name}")
        _validate_binding_shape(name, record, file_error=file_error)
    _validate_findings(payload)


def load_dependency_bindings(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"DependencyBindingsV3 must be a JSON object: {path}")
    _validate_payload(payload)
    return payload


def resolve_dependency(
    bindings: Mapping[str, Any],
    name: str,
    *,
    required: bool = True,
) -> Path | None:
    if name not in DEPENDENCY_NAME_SET:
        raise ValueError(f"unknown dependency: {name}")
    payload = dict(bindings)
    _validate_payload(payload, file_error=FileNotFoundError)
    dependencies = payload.get("dependencies")
    if required and dependencies[name]["status"] == "MISSING":
        raise FileNotFoundError(f"dependency is not bound: {name}")
    if payload.get("status") != "PASS":
        missing = sorted(
            dependency
            for dependency, record in dependencies.items()
            if isinstance(record, Mapping)
            and record.get("required") is True
            and record.get("status") == "MISSING"
        )
        raise ValueError(
            "DependencyBindingsV3 is BLOCKED; missing required dependencies: "
            + ", ".join(missing)
        )
    record = dependencies.get(name)
    if not isinstance(record, Mapping):
        if required:
            raise FileNotFoundError(f"dependency is not bound: {name}")
        return None
    if record.get("status") != "PASS":
        if required:
            raise FileNotFoundError(f"dependency is not bound: {name}")
        return None
    return _validate_record_file(record, name, missing_error=FileNotFoundError)


def dependency_bindings_reference(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
    }


def dependency_bindings_load_blocked_result(path: Path, stage: str, exc: BaseException, **facts: Any) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    return result(
        "BLOCKED",
        stage,
        findings=[{
            "code": "DEPENDENCY_MISSING",
            "dependency": "DependencyBindingsV3",
            "checked_locations": [str(resolved)],
            "resolution_hint": f"Regenerate or provide a readable DependencyBindingsV3 JSON file: {exc}",
        }],
        dependency_bindings={"path": str(resolved), "sha256": None},
        **facts,
    )


def _runtime_finding_for_record(name: str, record: Mapping[str, Any]) -> dict[str, Any] | None:
    checked_locations = [
        str(value)
        for value in (record.get("checked_locations") or [])
        if str(value)
    ]
    path_value = str(record.get("path") or "").strip()
    if path_value and path_value not in checked_locations:
        checked_locations.append(path_value)
    resolution_hint = str(record.get("resolution_hint") or RESOLUTION_HINTS[name])
    if record.get("status") != "PASS" or not path_value:
        return {
            "code": "DEPENDENCY_MISSING",
            "dependency": name,
            "checked_locations": checked_locations,
            "resolution_hint": resolution_hint,
        }
    path = Path(path_value)
    if not path.is_file():
        return {
            "code": "DEPENDENCY_MISSING",
            "dependency": name,
            "checked_locations": checked_locations,
            "resolution_hint": resolution_hint,
        }
    expected_sha = str(record.get("sha256") or "").upper()
    if expected_sha and sha256_file(path) != expected_sha:
        return {
            "code": "DEPENDENCY_MISSING",
            "dependency": name,
            "checked_locations": checked_locations,
            "resolution_hint": resolution_hint,
        }
    return None


def missing_dependency_findings(
    bindings: Mapping[str, Any],
    names: tuple[str, ...] | list[str] | set[str] = REQUIRED_RUNTIME_DEPENDENCIES,
) -> list[dict[str, Any]]:
    dependencies = bindings.get("dependencies")
    if not isinstance(dependencies, Mapping):
        return [{
            "code": "DEPENDENCY_MISSING",
            "dependency": "DependencyBindingsV3",
            "checked_locations": [],
            "resolution_hint": "Regenerate DependencyBindingsV3; dependencies must be a JSON object.",
        }]
    findings: list[dict[str, Any]] = []
    for name in names:
        if name not in DEPENDENCY_NAMES:
            raise ValueError(f"unknown dependency: {name}")
        record = dependencies.get(name)
        if not isinstance(record, Mapping):
            findings.append({
                "code": "DEPENDENCY_MISSING",
                "dependency": name,
                "checked_locations": [],
                "resolution_hint": RESOLUTION_HINTS[name],
            })
            continue
        finding = _runtime_finding_for_record(name, record)
        if finding is not None:
            findings.append(finding)
    return findings


def _replace_command_argument(command: list[Any], selector: int | str, replacement: str) -> None:
    if isinstance(selector, int):
        if len(command) <= selector:
            raise ValueError(f"runtime spec command is too short for index {selector}")
        command[selector] = replacement
        return
    try:
        index = command.index(selector)
    except ValueError:
        command.extend([selector, replacement])
        return
    if index + 1 >= len(command):
        command.append(replacement)
    else:
        command[index + 1] = replacement


def rebind_runtime_spec_payload(
    spec: Mapping[str, Any],
    output_path: Path,
    bindings: Mapping[str, Any],
    *,
    replacements: list[Mapping[str, Any]],
) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ValueError("runtime spec must be a JSON object")
    rebound = json.loads(json.dumps(spec, ensure_ascii=False))
    for replacement in replacements:
        field = str(replacement.get("field") or "")
        dependency = str(replacement.get("dependency") or "")
        if field not in REBOUND_RUNTIME_FIELDS:
            raise ValueError(f"runtime spec field is not rebindable: {field}")
        path = resolve_dependency(bindings, dependency)
        command_key, selector = REBOUND_RUNTIME_FIELDS[field]
        command = rebound.get(command_key)
        if not isinstance(command, list):
            raise ValueError(f"runtime spec field must be a command list: {command_key}")
        _replace_command_argument(command, selector, str(path))
    write_json(output_path, rebound)
    return rebound


def rebind_runtime_spec_copy(
    input_path: Path,
    output_path: Path,
    bindings: Mapping[str, Any],
    *,
    replacements: list[Mapping[str, Any]],
) -> dict[str, Any]:
    if input_path.resolve() == output_path.resolve() or (
        output_path.exists() and input_path.samefile(output_path)
    ):
        raise ValueError("runtime spec copy must use a different file from its source")
    spec = json.loads(input_path.read_text(encoding="utf-8-sig"))
    return rebind_runtime_spec_payload(spec, output_path, bindings, replacements=replacements)


def write_dependency_bindings(path: Path, bindings: Mapping[str, Any]) -> None:
    errors = validate_dependency_bindings(bindings)
    if errors:
        first = errors[0]
        raise ValueError(
            "DependencyBindingsV3 schema invalid: "
            f"{first.get('path')} {first.get('keyword')} {first.get('message')}"
        )
    write_json(path, bindings)


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover and persist DependencyBindingsV3")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bindings = discover_dependency_bindings()
    write_dependency_bindings(args.output, bindings)
    return finish(result(bindings["status"], "dependency_bindings_v3", dependency_bindings=dependency_bindings_reference(args.output), findings=bindings["findings"]))


if __name__ == "__main__":
    raise SystemExit(main())
