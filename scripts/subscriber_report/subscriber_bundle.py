from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping
from zipfile import ZIP_DEFLATED, ZipFile

from scripts.advisor.immutable_run import sha256_file


@dataclass(frozen=True)
class PublicBundleResult:
    output_path: Path
    members: tuple[str, ...]
    source_sha256: dict[str, str]
    archive_sha256: str
    crc_status: str


def _public_text(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    if path.suffix.lower() in {".html", ".htm", ".csv", ".md", ".txt", ".json", ".svg"}:
        return path.read_text(encoding="utf-8", errors="strict")
    return ""


def build_public_distribution_bundle(
    *,
    output_path: str | Path,
    public_files: Mapping[str, str | Path] | Iterable[str | Path],
    allowed_suffixes: Iterable[str],
    forbidden_patterns: Iterable[str],
) -> PublicBundleResult:
    """Build and verify a caller-allowlisted public-only ZIP archive."""

    output = Path(output_path)
    if output.exists():
        raise FileExistsError(output)
    suffixes = {suffix.lower() if str(suffix).startswith(".") else f".{str(suffix).lower()}" for suffix in allowed_suffixes}
    if isinstance(public_files, Mapping):
        requested = [(str(name), Path(path)) for name, path in public_files.items()]
    else:
        requested = [(Path(path).name, Path(path)) for path in public_files]
    if not requested:
        raise ValueError("public_files allowlist is empty")

    compiled = [re.compile(pattern, flags=re.IGNORECASE) for pattern in forbidden_patterns]
    universal_forbidden = [
        re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"),
        re.compile(r"\b(?:19|20)\d{12}\b"),
        re.compile(r"\b[a-fA-F0-9]{64}\b"),
        re.compile(r"\b(?:account_id|account_number|broker_name|source_receipt)\b", re.IGNORECASE),
    ]
    members: list[tuple[str, Path]] = []
    source_hashes: dict[str, str] = {}
    seen: set[str] = set()
    for arcname, source in requested:
        archive_path = Path(arcname)
        if archive_path.is_absolute() or ".." in archive_path.parts or archive_path.as_posix().startswith("/"):
            raise ValueError(f"unsafe public archive name: {arcname}")
        normalized = archive_path.as_posix()
        if normalized in seen:
            raise ValueError(f"duplicate public archive name: {normalized}")
        seen.add(normalized)
        if not source.is_file():
            raise FileNotFoundError(source)
        lower_name = source.name.lower()
        if source.suffix.lower() not in suffixes:
            raise ValueError(f"public file suffix is not allowed: {source}")
        if source.suffix.lower() in {".xls", ".xlsx", ".xlsm"} or "xbrl" in lower_name:
            raise ValueError(f"private source artifact is forbidden: {source.name}")
        text = _public_text(source)
        scan_text = normalized + "\n" + text
        hits = [pattern.pattern for pattern in (*compiled, *universal_forbidden) if pattern.search(scan_text)]
        if hits:
            raise ValueError(f"forbidden public content in {normalized}: {hits}")
        members.append((normalized, source))
        source_hashes[normalized] = sha256_file(source)

    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for arcname, source in members:
            archive.write(source, arcname=arcname)
    with ZipFile(output, "r") as archive:
        names = tuple(archive.namelist())
        if names != tuple(name for name, _ in members):
            raise RuntimeError("public ZIP member count/order mismatch")
        bad = archive.testzip()
        if bad is not None:
            raise RuntimeError(f"public ZIP CRC failed: {bad}")
        for name in names:
            digest = hashlib.sha256(archive.read(name)).hexdigest()
            if digest != source_hashes[name]:
                raise RuntimeError(f"public ZIP content hash mismatch: {name}")
    return PublicBundleResult(
        output_path=output,
        members=tuple(name for name, _ in members),
        source_sha256=source_hashes,
        archive_sha256=sha256_file(output),
        crc_status="PASS",
    )
