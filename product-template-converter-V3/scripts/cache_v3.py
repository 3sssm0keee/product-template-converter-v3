from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from pipeline_common import sha256_file, utc_now
from schema_validation_v3 import load_schema, validate_instance
from v3_common import canonical_json_sha256


CACHE_SCHEMA_VERSION = "content-addressed-cache-v3"
PASS_BUNDLE_CACHE_SCHEMA_VERSION = "pass-artifact-cache-bundle-v3"
PASS_BUNDLE_MANIFEST_SCHEMA_VERSION = "pass-artifact-bundle-manifest-v3"
PASS_BUNDLE_MANIFEST_MEMBER = "bundle-manifest.json"
PASS_BUNDLE_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "references"
    / "schemas"
    / "pass-cache-bundle-manifest-v3.schema.json"
)
MAX_BUNDLE_MEMBERS = 256
MAX_BUNDLE_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_SHA256_RE = re.compile(r"^[A-F0-9]{64}$")
_STAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _promote_cache_entry(
    temporary: Path,
    entry: Path,
    members: tuple[str, ...],
) -> None:
    """Publish cache files without replacing a populated directory.

    Windows file providers such as OneDrive may reject ``os.replace`` for a
    non-empty directory even when source and destination share a parent.
    Content-addressed writers for the same key produce the same payload, so we
    can create the exact entry directory once and atomically replace its files,
    publishing metadata last.  An interrupted write remains a MISS/CORRUPT and
    can be recomputed; it is never treated as a deliverable cache hit.
    """

    if entry.exists() and (not entry.is_dir() or entry.is_symlink()):
        raise ValueError(f"cache entry is not a regular directory: {entry}")
    entry.mkdir(parents=True, exist_ok=True)
    for name in members:
        source = temporary / name
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"cache promotion member is invalid: {source}")
        source.replace(entry / name)


def default_cache_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path(tempfile.gettempdir())
    return base / "ZXTY" / "product-template-converter-v3" / "cache"


def cache_key(stage: str, **inputs: Any) -> str:
    return canonical_json_sha256({"stage": stage, "inputs": inputs})


@dataclass(frozen=True)
class CacheLookup:
    status: str
    key: str
    payload: dict[str, Any] | None = None
    artifact: Path | None = None
    reason: str = ""


@dataclass(frozen=True)
class BundleMaterialization:
    status: str
    key: str
    destination: Path | None = None
    files: dict[str, Path] | None = None
    final_artifact: Path | None = None
    manifest: dict[str, Any] | None = None
    reason: str = ""


def _validate_stage_and_key(stage: str, key: str) -> tuple[str, str]:
    normalized_stage = str(stage).strip()
    normalized_key = str(key).strip().upper()
    if not _STAGE_RE.fullmatch(normalized_stage):
        raise ValueError("cache stage must match [A-Za-z0-9][A-Za-z0-9_-]{0,63}")
    if not _SHA256_RE.fullmatch(normalized_key):
        raise ValueError("cache key must be a 64-character hexadecimal SHA-256")
    return normalized_stage, normalized_key


def _validate_member_path(value: str) -> str:
    name = unicodedata.normalize("NFC", str(value))
    if not name or "\\" in name or "\x00" in name or ":" in name:
        raise ValueError(f"unsafe bundle member path: {value!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or path.as_posix() != name:
        raise ValueError(f"bundle members must use canonical relative POSIX paths: {value!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"bundle member traversal is forbidden: {value!r}")
    for part in path.parts:
        if part.endswith((" ", ".")) or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise ValueError(f"bundle member is unsafe on Windows: {value!r}")
    return name


def _member_collision_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _sha256_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest().upper(), size


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _zip_member_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0o170000
    return mode == stat.S_IFLNK


class ContentAddressedCache:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_cache_root()).resolve()

    def _entry_dir(self, stage: str, key: str) -> Path:
        safe_stage = "".join(character if character.isalnum() or character in "-_" else "-" for character in stage)
        return self.root / safe_stage / key.upper()

    def lookup_file(self, stage: str, key: str) -> CacheLookup:
        entry = self._entry_dir(stage, key)
        metadata_path = entry / "metadata.json"
        artifact = entry / "artifact.bin"
        if not metadata_path.is_file() or not artifact.is_file():
            return CacheLookup("MISS", key, reason="entry missing")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("schema_version") != CACHE_SCHEMA_VERSION:
                return CacheLookup("MISS", key, reason="schema version changed")
            if metadata.get("key") != key.upper():
                return CacheLookup("CORRUPT", key, reason="key mismatch")
            actual = sha256_file(artifact)
            if actual != str(metadata.get("artifact_sha256", "")).upper():
                return CacheLookup("CORRUPT", key, reason="artifact hash mismatch")
            return CacheLookup("HIT", key, metadata, artifact)
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            return CacheLookup("CORRUPT", key, reason=str(exc))

    def materialize_file(self, lookup: CacheLookup, destination: Path) -> bool:
        if lookup.status != "HIT" or lookup.artifact is None:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lookup.artifact, destination)
        expected = str((lookup.payload or {}).get("artifact_sha256", "")).upper()
        return bool(expected) and sha256_file(destination) == expected

    def store_file(
        self,
        stage: str,
        key: str,
        source: Path,
        *,
        inputs: dict[str, Any],
        facts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not source.is_file():
            raise ValueError(f"cache source missing: {source}")
        entry = self._entry_dir(stage, key)
        entry.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f"{key[:12]}-", dir=str(entry.parent)))
        try:
            artifact = temporary / "artifact.bin"
            shutil.copy2(source, artifact)
            metadata = {
                "schema_version": CACHE_SCHEMA_VERSION,
                "stage": stage,
                "key": key.upper(),
                "created_at": utc_now(),
                "artifact_sha256": sha256_file(artifact),
                "artifact_bytes": artifact.stat().st_size,
                "inputs": inputs,
                "facts": facts or {},
            }
            (temporary / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _promote_cache_entry(temporary, entry, ("artifact.bin", "metadata.json"))
            return metadata
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def lookup_json(self, stage: str, key: str) -> CacheLookup:
        lookup = self.lookup_file(stage, key)
        if lookup.status != "HIT" or lookup.artifact is None:
            return lookup
        try:
            json.loads(lookup.artifact.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeError) as exc:
            return CacheLookup("CORRUPT", key, lookup.payload, lookup.artifact, str(exc))
        return lookup

    def store_pass_bundle(
        self,
        stage: str,
        key: str,
        files: dict[str, Path],
        *,
        final_artifact_member: str,
        inputs: dict[str, Any],
        facts: dict[str, Any],
    ) -> dict[str, Any]:
        """Store one PASS deliverable together with immutable validation evidence."""

        stage, key = _validate_stage_and_key(stage, key)
        if facts.get("status") != "PASS" or facts.get("deliverable") is not True:
            raise ValueError("only status=PASS and deliverable=true may enter the final artifact cache")
        if not files or len(files) > MAX_BUNDLE_MEMBERS:
            raise ValueError("pass bundle member count is invalid")
        normalized: dict[str, Path] = {}
        collision_keys: set[str] = set()
        for raw_name, raw_path in files.items():
            name = _validate_member_path(raw_name)
            collision = _member_collision_key(name)
            if name == PASS_BUNDLE_MANIFEST_MEMBER or collision in collision_keys:
                raise ValueError(f"duplicate or reserved bundle member: {name}")
            path = Path(raw_path).resolve()
            if not path.is_file() or path.is_symlink():
                raise ValueError(f"bundle source must be a regular file: {path}")
            normalized[name] = path
            collision_keys.add(collision)
        final_member = _validate_member_path(final_artifact_member)
        if final_member not in normalized:
            raise ValueError("final_artifact_member must name one supplied file")

        members: list[dict[str, Any]] = []
        total_bytes = 0
        for name, path in sorted(normalized.items()):
            size = path.stat().st_size
            total_bytes += size
            if total_bytes > MAX_BUNDLE_UNCOMPRESSED_BYTES:
                raise ValueError("pass bundle exceeds the uncompressed size limit")
            members.append({
                "path": name,
                "sha256": sha256_file(path),
                "bytes": size,
                "role": "final_artifact" if name == final_member else "validation_evidence",
            })
        manifest = {
            "schema_version": PASS_BUNDLE_MANIFEST_SCHEMA_VERSION,
            "stage": stage,
            "key": key,
            "created_at": utc_now(),
            "inputs_sha256": canonical_json_sha256(inputs),
            "facts": facts,
            "final_artifact": final_member,
            "members": members,
        }
        errors = validate_instance(manifest, load_schema(PASS_BUNDLE_SCHEMA_PATH))
        if errors:
            raise ValueError(f"pass bundle manifest is invalid: {errors[:3]}")
        manifest_bytes = _canonical_json_bytes(manifest)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest().upper()

        entry = self._entry_dir(stage, key)
        entry.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f"{key[:12]}-", dir=str(entry.parent)))
        try:
            bundle_path = temporary / "bundle.zip"
            with zipfile.ZipFile(bundle_path, "w", allowZip64=True) as archive:
                archive.writestr(_zip_info(PASS_BUNDLE_MANIFEST_MEMBER), manifest_bytes)
                for name, path in sorted(normalized.items()):
                    with path.open("rb") as source_stream, archive.open(_zip_info(name), "w", force_zip64=True) as target_stream:
                        shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
            metadata = {
                "schema_version": PASS_BUNDLE_CACHE_SCHEMA_VERSION,
                "stage": stage,
                "key": key,
                "created_at": utc_now(),
                "bundle_sha256": sha256_file(bundle_path),
                "bundle_bytes": bundle_path.stat().st_size,
                "manifest_sha256": manifest_sha,
                "inputs": inputs,
                "facts": facts,
            }
            (temporary / "metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _promote_cache_entry(temporary, entry, ("bundle.zip", "metadata.json"))
            return {**metadata, "manifest": manifest}
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def lookup_pass_bundle(
        self,
        stage: str,
        key: str,
        *,
        expected_facts: dict[str, Any] | None = None,
    ) -> CacheLookup:
        try:
            stage, key = _validate_stage_and_key(stage, key)
        except ValueError as exc:
            return CacheLookup("CORRUPT", str(key), reason=str(exc))
        entry = self._entry_dir(stage, key)
        metadata_path = entry / "metadata.json"
        bundle_path = entry / "bundle.zip"
        if not metadata_path.is_file() or not bundle_path.is_file():
            return CacheLookup("MISS", key, reason="pass bundle entry missing")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("schema_version") != PASS_BUNDLE_CACHE_SCHEMA_VERSION:
                return CacheLookup("MISS", key, reason="pass bundle cache schema changed")
            if metadata.get("stage") != stage or metadata.get("key") != key:
                raise ValueError("pass bundle stage/key mismatch")
            if sha256_file(bundle_path) != str(metadata.get("bundle_sha256") or "").upper():
                raise ValueError("pass bundle ZIP SHA-256 mismatch")
            stored_facts = metadata.get("facts")
            if not isinstance(stored_facts, dict) or stored_facts.get("status") != "PASS" or stored_facts.get("deliverable") is not True:
                raise ValueError("pass bundle facts do not prove a deliverable PASS")
            if expected_facts is not None and any(stored_facts.get(name) != value for name, value in expected_facts.items()):
                return CacheLookup("MISS", key, reason="current engine, approval, manifest or validation facts changed")

            with zipfile.ZipFile(bundle_path) as archive:
                infos = archive.infolist()
                if not infos or len(infos) > MAX_BUNDLE_MEMBERS + 1:
                    raise ValueError("pass bundle ZIP member count is invalid")
                names: list[str] = []
                collision_keys: set[str] = set()
                total = 0
                for info in infos:
                    name = _validate_member_path(info.filename)
                    collision = _member_collision_key(name)
                    if collision in collision_keys or info.is_dir() or _zip_member_is_symlink(info):
                        raise ValueError(f"unsafe or duplicate ZIP member: {name}")
                    collision_keys.add(collision)
                    names.append(name)
                    total += int(info.file_size)
                    if total > MAX_BUNDLE_UNCOMPRESSED_BYTES:
                        raise ValueError("pass bundle ZIP exceeds uncompressed limit")
                if names.count(PASS_BUNDLE_MANIFEST_MEMBER) != 1:
                    raise ValueError("pass bundle manifest member is missing or duplicated")
                manifest_bytes = archive.read(PASS_BUNDLE_MANIFEST_MEMBER)
                if hashlib.sha256(manifest_bytes).hexdigest().upper() != str(metadata.get("manifest_sha256") or "").upper():
                    raise ValueError("pass bundle manifest SHA-256 mismatch")
                manifest = json.loads(manifest_bytes.decode("utf-8"))
                errors = validate_instance(manifest, load_schema(PASS_BUNDLE_SCHEMA_PATH))
                if errors:
                    raise ValueError(f"pass bundle manifest schema invalid: {errors[:3]}")
                if manifest.get("stage") != stage or manifest.get("key") != key:
                    raise ValueError("pass bundle manifest stage/key mismatch")
                if manifest.get("facts") != stored_facts:
                    raise ValueError("pass bundle manifest facts are not bound to metadata")
                if manifest.get("inputs_sha256") != canonical_json_sha256(metadata.get("inputs", {})):
                    raise ValueError("pass bundle manifest inputs are not bound to metadata")
                declared = {member["path"]: member for member in manifest["members"]}
                if set(names) != {PASS_BUNDLE_MANIFEST_MEMBER, *declared}:
                    raise ValueError("pass bundle ZIP and manifest members differ")
                for name, member in declared.items():
                    with archive.open(name) as stream:
                        digest, size = _sha256_stream(stream)
                    if digest != member["sha256"] or size != member["bytes"]:
                        raise ValueError(f"pass bundle member hash/size mismatch: {name}")
                if manifest["final_artifact"] not in declared or declared[manifest["final_artifact"]]["role"] != "final_artifact":
                    raise ValueError("pass bundle final artifact declaration is invalid")
            return CacheLookup("HIT", key, {**metadata, "manifest": manifest}, bundle_path)
        except (
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            UnicodeError,
            json.JSONDecodeError,
            zipfile.BadZipFile,
            KeyError,
        ) as exc:
            return CacheLookup("CORRUPT", key, reason=str(exc))

    def materialize_pass_bundle(self, lookup: CacheLookup, destination: Path) -> BundleMaterialization:
        if lookup.status != "HIT" or lookup.artifact is None or not isinstance(lookup.payload, dict):
            return BundleMaterialization(lookup.status, lookup.key, reason=lookup.reason)
        stage = str(lookup.payload.get("stage") or "")
        fresh = self.lookup_pass_bundle(stage, lookup.key)
        if fresh.status != "HIT" or fresh.artifact is None or not isinstance(fresh.payload, dict):
            return BundleMaterialization(fresh.status, lookup.key, reason=fresh.reason)
        manifest = fresh.payload["manifest"]
        destination = destination.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="v3-cache-materialize-", dir=str(destination.parent)))
        extracted: dict[str, Path] = {}
        try:
            with zipfile.ZipFile(fresh.artifact) as archive:
                for member in manifest["members"]:
                    name = _validate_member_path(member["path"])
                    target = temporary.joinpath(*PurePosixPath(name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(name) as source_stream, target.open("wb") as target_stream:
                        shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                    if sha256_file(target) != member["sha256"]:
                        raise ValueError(f"materialized pass bundle member hash mismatch: {name}")
            destination.mkdir(parents=True, exist_ok=True)
            for member in manifest["members"]:
                name = member["path"]
                source_path = temporary.joinpath(*PurePosixPath(name).parts)
                target_path = destination.joinpath(*PurePosixPath(name).parts)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target_path)
                extracted[name] = target_path
            final_path = extracted[manifest["final_artifact"]]
            return BundleMaterialization(
                "HIT",
                lookup.key,
                destination=destination,
                files=extracted,
                final_artifact=final_path,
                manifest=manifest,
            )
        except (OSError, ValueError, zipfile.BadZipFile, KeyError) as exc:
            return BundleMaterialization("CORRUPT", lookup.key, reason=str(exc))
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
