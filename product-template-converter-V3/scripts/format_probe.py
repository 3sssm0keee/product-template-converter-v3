from __future__ import annotations

import argparse
import re
import struct
import zipfile
from pathlib import Path
from typing import Any, BinaryIO
from xml.etree import ElementTree

from pipeline_common import finish, result, sha256_file, write_json


SCHEMA_VERSION = "3.0.0"
PROBE_VERSION = "format-probe-v3.0.0"
CFB_SIGNATURE = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"
ZIP_SIGNATURES = {b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"}
FREESECT = 0xFFFFFFFF
ENDOFCHAIN = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
DIFSECT = 0xFFFFFFFC
MAXREGSECT = 0xFFFFFFFA
MAX_XML_BYTES = 4 * 1024 * 1024
MAX_MAIN_PART_BYTES = 128 * 1024 * 1024
MAX_CFB_CHAIN_SECTORS = 2_000_000

FORMAT_INFO = {
    "doc": ("word", ".doc", ".docx", True),
    "docx": ("word", ".docx", ".docx", False),
    "ppt": ("presentation", ".ppt", ".pptx", True),
    "pptx": ("presentation", ".pptx", ".pptx", False),
    "pdf": ("fixed_layout", ".pdf", ".pdf", False),
}

WORD_MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "wordprocessingml.document.main+xml"
)
PRESENTATION_MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument."
    "presentationml.presentation.main+xml"
)


class ProbeError(Exception):
    def __init__(self, code: str, message: str, **evidence: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.evidence = evidence


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _blocked_payload(
    source: Path,
    source_sha256: str,
    size_bytes: int,
    container: str,
    error: ProbeError,
) -> dict[str, Any]:
    return result(
        "BLOCKED",
        "format_probe",
        findings=[{
            "code": error.code,
            "severity": "ERROR",
            "message": error.message,
            **error.evidence,
        }],
        schema_version=SCHEMA_VERSION,
        artifact_type="FormatProbeResultV3",
        probe_version=PROBE_VERSION,
        source={
            "path": str(source),
            "sha256": source_sha256,
            "size_bytes": size_bytes,
            "extension": source.suffix.lower(),
        },
        detected_format=None,
        family=None,
        container=container,
        normalized_extension=None,
        requires_normalization=False,
        evidence={},
    )


def _extension_finding(source: Path, detected_format: str) -> list[dict[str, Any]]:
    expected_extension = FORMAT_INFO[detected_format][1]
    actual_extension = source.suffix.lower()
    if actual_extension == expected_extension:
        return []
    return [{
        "code": "EXTENSION_MISMATCH",
        "severity": "WARNING",
        "message": "文件后缀与实际容器格式不一致，已按实际格式处理",
        "actual_extension": actual_extension,
        "expected_extension": expected_extension,
        "detected_format": detected_format,
    }]


def _pass_payload(
    source: Path,
    source_sha256: str,
    size_bytes: int,
    detected_format: str,
    container: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    family, _source_extension, normalized_extension, requires_normalization = FORMAT_INFO[detected_format]
    return result(
        "PASS",
        "format_probe",
        findings=_extension_finding(source, detected_format),
        schema_version=SCHEMA_VERSION,
        artifact_type="FormatProbeResultV3",
        probe_version=PROBE_VERSION,
        source={
            "path": str(source),
            "sha256": source_sha256,
            "size_bytes": size_bytes,
            "extension": source.suffix.lower(),
        },
        detected_format=detected_format,
        family=family,
        container=container,
        normalized_extension=normalized_extension,
        requires_normalization=requires_normalization,
        evidence=evidence,
    )


def _read_zip_member(
    package: zipfile.ZipFile,
    name: str,
    *,
    maximum_bytes: int = MAX_XML_BYTES,
) -> bytes:
    try:
        info = package.getinfo(name)
    except KeyError as exc:
        raise ProbeError(
            "OOXML_REQUIRED_PART_MISSING",
            f"OOXML 缺少必要部件: {name}",
            part=name,
        ) from exc
    if info.flag_bits & 0x1:
        raise ProbeError(
            "ENCRYPTED_DOCUMENT",
            "OOXML 部件已加密，必须先由授权人员解密",
            part=name,
        )
    if info.file_size > maximum_bytes:
        raise ProbeError(
            "OOXML_METADATA_TOO_LARGE",
            f"OOXML 元数据部件异常大: {name}",
            part=name,
            size_bytes=info.file_size,
        )
    try:
        return package.read(info)
    except (RuntimeError, OSError, zipfile.BadZipFile) as exc:
        raise ProbeError(
            "OOXML_CONTAINER_CORRUPT",
            f"无法读取 OOXML 必要部件 {name}: {exc}",
            part=name,
        ) from exc


def _parse_xml(data: bytes, part: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise ProbeError(
            "OOXML_XML_INVALID",
            f"OOXML XML 无法解析: {part}",
            part=part,
            detail=str(exc),
        ) from exc


def _probe_ooxml(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        package = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ProbeError("OOXML_CONTAINER_CORRUPT", f"ZIP/OOXML 容器损坏: {exc}") from exc

    with package:
        infos = package.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ProbeError("OOXML_DUPLICATE_PART", "OOXML 包中存在重复部件名")
        if any(info.flag_bits & 0x1 for info in infos):
            raise ProbeError("ENCRYPTED_DOCUMENT", "OOXML 包含加密部件，必须先解密")
        if "[Content_Types].xml" not in names:
            raise ProbeError(
                "OOXML_CONTENT_TYPES_MISSING",
                "ZIP 容器缺少 [Content_Types].xml，不能视为可处理的 OOXML",
            )
        content_types_root = _parse_xml(
            _read_zip_member(package, "[Content_Types].xml"),
            "[Content_Types].xml",
        )
        overrides: dict[str, str] = {}
        for element in content_types_root.iter():
            if _local_name(element.tag) != "Override":
                continue
            part_name = str(element.attrib.get("PartName") or "")
            content_type = str(element.attrib.get("ContentType") or "")
            if part_name and content_type:
                overrides[part_name] = content_type

        candidates: list[tuple[str, str, str]] = []
        if overrides.get("/word/document.xml") == WORD_MAIN_CONTENT_TYPE:
            candidates.append(("docx", "word/document.xml", WORD_MAIN_CONTENT_TYPE))
        if overrides.get("/ppt/presentation.xml") == PRESENTATION_MAIN_CONTENT_TYPE:
            candidates.append(("pptx", "ppt/presentation.xml", PRESENTATION_MAIN_CONTENT_TYPE))
        if len(candidates) > 1:
            raise ProbeError(
                "OOXML_TYPE_AMBIGUOUS",
                "OOXML 同时声明 Word 与 PowerPoint 主部件",
                candidates=[candidate[0] for candidate in candidates],
            )
        if not candidates:
            known_main_parts = sorted(
                part for part in overrides if part in {"/word/document.xml", "/ppt/presentation.xml"}
            )
            raise ProbeError(
                "UNSUPPORTED_OOXML_TYPE",
                "OOXML 不是受支持的 DOCX 或 PPTX 正文包",
                declared_main_parts=known_main_parts,
            )

        detected_format, main_part, content_type = candidates[0]
        if main_part not in names:
            raise ProbeError(
                "OOXML_MAIN_PART_MISSING",
                f"OOXML 声明了主部件但实际不存在: {main_part}",
                part=main_part,
            )
        _read_zip_member(package, main_part, maximum_bytes=MAX_MAIN_PART_BYTES)

        relationships_root = _parse_xml(
            _read_zip_member(package, "_rels/.rels"),
            "_rels/.rels",
        )
        office_targets: list[str] = []
        for relationship in relationships_root.iter():
            if _local_name(relationship.tag) != "Relationship":
                continue
            relationship_type = str(relationship.attrib.get("Type") or "")
            target = str(relationship.attrib.get("Target") or "").lstrip("/")
            if relationship_type.endswith("/officeDocument"):
                office_targets.append(target.replace("\\", "/"))
        if office_targets != [main_part]:
            raise ProbeError(
                "OOXML_ROOT_RELATIONSHIP_INVALID",
                "OOXML 根关系未唯一绑定到已声明主部件",
                expected=main_part,
                actual=office_targets,
            )

        return detected_format, {
            "entry_count": len(infos),
            "main_part": main_part,
            "main_content_type": content_type,
            "root_relationship_target": office_targets[0],
        }


class _CfbReader:
    """读取识别 Office 旧格式所需的最小 CFB 结构，不依赖 olefile。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.size = path.stat().st_size
        self.stream: BinaryIO = path.open("rb")
        self.header = self._read_exact(0, 512)
        if self.header[:8] != CFB_SIGNATURE:
            raise ProbeError("OLE_SIGNATURE_INVALID", "OLE 复合文档签名无效")
        if struct.unpack_from("<H", self.header, 0x1C)[0] != 0xFFFE:
            raise ProbeError("OLE_BYTE_ORDER_INVALID", "OLE 字节序标记无效")
        self.major_version = struct.unpack_from("<H", self.header, 0x1A)[0]
        sector_shift = struct.unpack_from("<H", self.header, 0x1E)[0]
        mini_sector_shift = struct.unpack_from("<H", self.header, 0x20)[0]
        self.sector_size = 1 << sector_shift
        self.mini_sector_size = 1 << mini_sector_shift
        if self.major_version not in {3, 4}:
            raise ProbeError("OLE_VERSION_UNSUPPORTED", "OLE 主版本不是 3 或 4")
        expected_sector_size = 512 if self.major_version == 3 else 4096
        if self.sector_size != expected_sector_size or self.mini_sector_size != 64:
            raise ProbeError("OLE_SECTOR_GEOMETRY_INVALID", "OLE 扇区几何无效")
        # 部分真实旧版 Word 文件会在完整 CFB 扇区后附带非扇区对齐数据。
        # CFB 索引仍只允许指向完整扇区；尾随数据本身不作为损坏证据。
        if self.size < self.sector_size * 2:
            raise ProbeError("OLE_CONTAINER_CORRUPT", "OLE 文件不足以容纳有效扇区")
        self.sector_count = self.size // self.sector_size - 1
        self.first_directory_sector = struct.unpack_from("<I", self.header, 0x30)[0]
        self.mini_stream_cutoff = struct.unpack_from("<I", self.header, 0x38)[0]
        self.first_mini_fat_sector = struct.unpack_from("<I", self.header, 0x3C)[0]
        self.number_of_mini_fat_sectors = struct.unpack_from("<I", self.header, 0x40)[0]
        self.fat = self._read_fat()
        self.directory_entries = self._read_directory()

    def close(self) -> None:
        self.stream.close()

    def __enter__(self) -> "_CfbReader":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _read_exact(self, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0 or offset + size > self.size:
            raise ProbeError("OLE_CONTAINER_CORRUPT", "OLE 读取范围超出文件边界")
        self.stream.seek(offset)
        data = self.stream.read(size)
        if len(data) != size:
            raise ProbeError("OLE_CONTAINER_CORRUPT", "OLE 扇区读取不完整")
        return data

    def _read_sector(self, sector_id: int) -> bytes:
        if sector_id >= MAXREGSECT or sector_id >= self.sector_count:
            raise ProbeError(
                "OLE_CONTAINER_CORRUPT",
                "OLE 扇区编号越界",
                sector_id=sector_id,
            )
        return self._read_exact((sector_id + 1) * self.sector_size, self.sector_size)

    def _read_fat(self) -> list[int]:
        number_of_fat_sectors = struct.unpack_from("<I", self.header, 0x2C)[0]
        first_difat_sector = struct.unpack_from("<I", self.header, 0x44)[0]
        number_of_difat_sectors = struct.unpack_from("<I", self.header, 0x48)[0]
        fat_sector_ids = [
            sector_id
            for sector_id in struct.unpack_from("<109I", self.header, 0x4C)
            if sector_id < MAXREGSECT
        ]
        visited: set[int] = set()
        current = first_difat_sector
        entries_per_difat_sector = self.sector_size // 4 - 1
        for _ in range(number_of_difat_sectors):
            if current in visited or current >= MAXREGSECT:
                raise ProbeError("OLE_DIFAT_INVALID", "OLE DIFAT 链损坏")
            visited.add(current)
            sector = self._read_sector(current)
            values = struct.unpack(f"<{self.sector_size // 4}I", sector)
            fat_sector_ids.extend(
                sector_id for sector_id in values[:entries_per_difat_sector] if sector_id < MAXREGSECT
            )
            current = values[-1]
        if number_of_difat_sectors and current != ENDOFCHAIN:
            raise ProbeError("OLE_DIFAT_INVALID", "OLE DIFAT 链未正常终止")
        if len(fat_sector_ids) < number_of_fat_sectors:
            raise ProbeError("OLE_FAT_MISSING", "OLE FAT 扇区数量不足")
        fat: list[int] = []
        for sector_id in fat_sector_ids[:number_of_fat_sectors]:
            sector = self._read_sector(sector_id)
            fat.extend(struct.unpack(f"<{self.sector_size // 4}I", sector))
        if not fat:
            raise ProbeError("OLE_FAT_MISSING", "OLE 未包含 FAT")
        return fat

    def _read_chain(self, start_sector: int, maximum: int | None = None) -> bytes:
        if start_sector == ENDOFCHAIN:
            return b""
        current = start_sector
        visited: set[int] = set()
        chunks: list[bytes] = []
        while current != ENDOFCHAIN:
            if current >= MAXREGSECT or current >= len(self.fat) or current in visited:
                raise ProbeError("OLE_CHAIN_INVALID", "OLE FAT 链损坏", sector_id=current)
            visited.add(current)
            if len(visited) > MAX_CFB_CHAIN_SECTORS:
                raise ProbeError("OLE_CHAIN_TOO_LONG", "OLE FAT 链异常过长")
            chunks.append(self._read_sector(current))
            if maximum is not None and len(chunks) >= maximum:
                break
            current = self.fat[current]
            if current in {FREESECT, FATSECT, DIFSECT}:
                raise ProbeError("OLE_CHAIN_INVALID", "OLE FAT 链遇到非法终止标记")
        return b"".join(chunks)

    def _read_directory(self) -> list[dict[str, Any]]:
        directory_data = self._read_chain(self.first_directory_sector)
        if not directory_data or len(directory_data) % 128:
            raise ProbeError("OLE_DIRECTORY_INVALID", "OLE 目录流无效")
        entries: list[dict[str, Any]] = []
        for offset in range(0, len(directory_data), 128):
            entry = directory_data[offset:offset + 128]
            object_type = entry[66]
            if object_type == 0:
                continue
            name_length = struct.unpack_from("<H", entry, 64)[0]
            if name_length < 2 or name_length > 64 or name_length % 2:
                raise ProbeError("OLE_DIRECTORY_INVALID", "OLE 目录项名称长度无效")
            try:
                name = entry[:name_length - 2].decode("utf-16le")
            except UnicodeDecodeError as exc:
                raise ProbeError("OLE_DIRECTORY_INVALID", "OLE 目录项名称编码无效") from exc
            start_sector = struct.unpack_from("<I", entry, 116)[0]
            stream_size = struct.unpack_from("<Q", entry, 120)[0]
            if self.major_version == 3:
                stream_size &= 0xFFFFFFFF
            entries.append({
                "name": name,
                "object_type": object_type,
                "start_sector": start_sector,
                "stream_size": stream_size,
            })
        if not any(entry["object_type"] == 5 for entry in entries):
            raise ProbeError("OLE_ROOT_MISSING", "OLE 缺少 Root Entry")
        return entries

    def stream_names(self) -> list[str]:
        return [str(entry["name"]) for entry in self.directory_entries if entry["object_type"] == 2]

    def read_stream(self, stream_name: str) -> bytes:
        matches = [
            entry
            for entry in self.directory_entries
            if entry["object_type"] == 2 and str(entry["name"]).casefold() == stream_name.casefold()
        ]
        if len(matches) != 1:
            return b""
        entry = matches[0]
        size = int(entry["stream_size"])
        if size == 0:
            return b""
        start_sector = int(entry["start_sector"])
        if size >= self.mini_stream_cutoff:
            return self._read_chain(start_sector)[:size]

        root = next(item for item in self.directory_entries if item["object_type"] == 5)
        mini_stream = self._read_chain(int(root["start_sector"]))[:int(root["stream_size"])]
        if self.number_of_mini_fat_sectors == 0:
            raise ProbeError("OLE_MINIFAT_MISSING", "OLE 小流缺少 MiniFAT")
        mini_fat_data = self._read_chain(
            self.first_mini_fat_sector,
            maximum=self.number_of_mini_fat_sectors,
        )
        mini_fat = list(struct.unpack(f"<{len(mini_fat_data) // 4}I", mini_fat_data))
        current = start_sector
        visited: set[int] = set()
        chunks: list[bytes] = []
        collected_bytes = 0
        while current != ENDOFCHAIN and collected_bytes < size:
            if current >= len(mini_fat) or current in visited:
                raise ProbeError("OLE_MINICHAIN_INVALID", "OLE MiniFAT 链损坏")
            visited.add(current)
            offset = current * self.mini_sector_size
            end = offset + self.mini_sector_size
            if end > len(mini_stream):
                raise ProbeError("OLE_MINICHAIN_INVALID", "OLE 小流超出 Root mini stream")
            chunks.append(mini_stream[offset:end])
            collected_bytes += self.mini_sector_size
            current = mini_fat[current]
        return b"".join(chunks)[:size]


def _probe_ole(path: Path) -> tuple[str, dict[str, Any]]:
    try:
        with _CfbReader(path) as cfb:
            stream_names = cfb.stream_names()
            folded = {name.casefold() for name in stream_names}
            encryption_markers = sorted(name for name in stream_names if name.casefold() in {
                "encryptedpackage",
                "encryptioninfo",
                "encryptedsummary",
                "cryptsession10container",
                "drmcontent",
                "strongencryptiontransform",
            })
            if encryption_markers:
                raise ProbeError(
                    "ENCRYPTED_DOCUMENT",
                    "OLE Office 文件包含加密流，必须先由授权人员解密",
                    encryption_streams=encryption_markers,
                )
            candidates: list[str] = []
            if "worddocument" in folded:
                candidates.append("doc")
            if "powerpoint document" in folded:
                candidates.append("ppt")
            if len(candidates) > 1:
                raise ProbeError(
                    "OLE_TYPE_AMBIGUOUS",
                    "OLE 同时包含 Word 与 PowerPoint 主流",
                    candidates=candidates,
                )
            if not candidates:
                raise ProbeError(
                    "UNSUPPORTED_OLE_TYPE",
                    "OLE 复合文档不是受支持的 DOC 或 PPT",
                    stream_count=len(stream_names),
                )
            detected_format = candidates[0]
            if detected_format == "doc":
                word_document = cfb.read_stream("WordDocument")
                if len(word_document) >= 12:
                    fib_flags = struct.unpack_from("<H", word_document, 10)[0]
                    if fib_flags & 0x8100:
                        raise ProbeError(
                            "ENCRYPTED_DOCUMENT",
                            "DOC FIB 标记为已加密或混淆，必须先解密",
                        )
            return detected_format, {
                "cfb_major_version": cfb.major_version,
                "sector_size": cfb.sector_size,
                "stream_count": len(stream_names),
                "type_streams": sorted(
                    name for name in stream_names if name.casefold() in {"worddocument", "powerpoint document"}
                ),
            }
    except ProbeError:
        raise
    except (OSError, struct.error, ValueError) as exc:
        raise ProbeError("OLE_CONTAINER_CORRUPT", f"OLE 复合文档损坏: {exc}") from exc


def _contains_binary_token(path: Path, token: bytes) -> bool:
    overlap = max(len(token) - 1, 0)
    previous = b""
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return False
            data = previous + chunk
            if token in data:
                return True
            previous = data[-overlap:] if overlap else b""


def _probe_pdf(path: Path, head: bytes) -> tuple[str, dict[str, Any]]:
    signature_offset = head.find(b"%PDF-")
    if signature_offset < 0 or signature_offset > 1024:
        raise ProbeError("PDF_SIGNATURE_INVALID", "PDF 标识未出现在文件前 1024 字节")
    version_match = re.match(rb"%PDF-(\d\.\d)", head[signature_offset:signature_offset + 16])
    if not version_match:
        raise ProbeError("PDF_VERSION_INVALID", "PDF 版本标识无效")
    tail_size = min(path.stat().st_size, 64 * 1024)
    with path.open("rb") as stream:
        stream.seek(-tail_size, 2)
        tail = stream.read(tail_size)
    eof_index = tail.rfind(b"%%EOF")
    if eof_index < 0:
        raise ProbeError("PDF_CONTAINER_CORRUPT", "PDF 缺少 %%EOF 结束标记")
    before_eof = tail[:eof_index]
    startxref_matches = list(re.finditer(rb"startxref\s+(\d+)", before_eof))
    if not startxref_matches:
        raise ProbeError("PDF_CONTAINER_CORRUPT", "PDF 缺少 startxref")
    startxref = int(startxref_matches[-1].group(1))
    if startxref >= path.stat().st_size:
        raise ProbeError(
            "PDF_CONTAINER_CORRUPT",
            "PDF startxref 超出文件边界",
            startxref=startxref,
        )
    if _contains_binary_token(path, b"/Encrypt"):
        raise ProbeError("ENCRYPTED_DOCUMENT", "PDF 包含 Encrypt 字典，必须先解密")
    return "pdf", {
        "pdf_version": version_match.group(1).decode("ascii"),
        "signature_offset": signature_offset,
        "startxref": startxref,
        "eof_present": True,
    }


def probe_format(source: Path) -> dict[str, Any]:
    source = source.resolve()
    if not source.is_file():
        return result(
            "BLOCKED",
            "format_probe",
            findings=[{
                "code": "SOURCE_MISSING",
                "severity": "ERROR",
                "message": "源文件不存在",
            }],
            schema_version=SCHEMA_VERSION,
            artifact_type="FormatProbeResultV3",
            probe_version=PROBE_VERSION,
            source={
                "path": str(source),
                "sha256": "",
                "size_bytes": 0,
                "extension": source.suffix.lower(),
            },
            detected_format=None,
            family=None,
            container="unknown",
            normalized_extension=None,
            requires_normalization=False,
            evidence={},
        )

    try:
        source_sha256 = sha256_file(source)
        size_bytes = source.stat().st_size
    except OSError as exc:
        return _blocked_payload(
            source,
            "",
            0,
            "unknown",
            ProbeError("SOURCE_UNREADABLE", f"源文件无法读取: {exc}"),
        )
    head = b""
    try:
        with source.open("rb") as stream:
            head = stream.read(4096)
        if head[:8] == CFB_SIGNATURE:
            detected_format, evidence = _probe_ole(source)
            return _pass_payload(source, source_sha256, size_bytes, detected_format, "ole", evidence)
        if head.find(b"%PDF-") in range(0, 1025):
            detected_format, evidence = _probe_pdf(source, head)
            return _pass_payload(source, source_sha256, size_bytes, detected_format, "pdf", evidence)
        if head[:4] in ZIP_SIGNATURES:
            detected_format, evidence = _probe_ooxml(source)
            return _pass_payload(source, source_sha256, size_bytes, detected_format, "ooxml", evidence)
        raise ProbeError(
            "UNKNOWN_FORMAT",
            "无法从文件签名、OOXML/OLE 容器或 PDF 标识确定受支持格式",
        )
    except ProbeError as exc:
        container = (
            "ole" if head[:8] == CFB_SIGNATURE
            else "pdf" if b"%PDF-" in head[:1025]
            else "ooxml" if head[:4] in ZIP_SIGNATURES
            else "unknown"
        )
        return _blocked_payload(source, source_sha256, size_bytes, container, exc)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, struct.error) as exc:
        error = ProbeError("FORMAT_PROBE_FAILED", f"格式探测失败: {exc}")
        return _blocked_payload(source, source_sha256, size_bytes, "unknown", error)


def main() -> int:
    parser = argparse.ArgumentParser(description="按文件签名与容器结构识别 V3 源格式")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    payload = probe_format(args.source)
    write_json(args.output, payload)
    return finish(payload, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
