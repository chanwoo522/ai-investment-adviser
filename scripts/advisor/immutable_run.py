from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable
from zipfile import ZIP_DEFLATED, ZipFile


PROTECTED_RECURSIVE_ROOTS = (
    "data/production",
    "data/development",
    "data/live",
    "data/portfolio/evidence",
    "data/archive/quarantine_20260330",
    "data/backtest",
    "data/diagnostics",
    "data/processed",
    "artifacts",
)
PROTECTED_SCOPE_V3 = {
    "recursive_roots": list(PROTECTED_RECURSIVE_ROOTS),
    "content_hash": "SHA256_FULL_FILE_CONTENT",
    "staging_exclusion": "data/development/**/.run_id=*.staging",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_advisor_staging_name(name: str) -> bool:
    return name.startswith(".run_id=") and name.endswith(".staging")


def _scan_protected_tree(
    root: Path,
    files: list[Path],
    *,
    access_markers: list[dict[str, Any]] | None = None,
    forced_access_markers: dict[Path, dict[str, Any]] | None = None,
    filename_prefixes: tuple[str, ...] | None = None,
    exclude_advisor_staging: bool = False,
) -> None:
    if not root.exists():
        return
    pending = [root]
    while pending:
        directory = pending.pop()
        forced = (forced_access_markers or {}).get(directory.resolve())
        if forced is not None:
            stat = directory.stat()
            if (
                int(stat.st_size) != int(forced["size"])
                or int(stat.st_mtime_ns) != int(forced["mtime_ns"])
            ):
                raise RuntimeError(
                    f"baseline-opaque protected directory metadata changed: {directory}"
                )
            payload = dict(forced)
            payload["path"] = directory.as_posix()
            if access_markers is None:
                raise RuntimeError("forced access marker requires an access-marker sink")
            access_markers.append(payload)
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        if not (
                            exclude_advisor_staging
                            and _is_advisor_staging_name(entry.name)
                        ):
                            pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=True) and (
                        filename_prefixes is None
                        or entry.name.startswith(filename_prefixes)
                    ):
                        # DirEntry uses directory-enumeration metadata on Windows,
                        # avoiding a separate Path.is_file() syscall per artifact.
                        files.append(Path(entry.path))
        except PermissionError as exc:
            if access_markers is None:
                raise
            stat = directory.stat()
            access_markers.append(
                {
                    "path": directory.as_posix(),
                    "status": "ACCESS_DENIED",
                    "size": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                    "winerror": int(getattr(exc, "winerror", 0) or 0),
                }
            )


def _protected_files(
    repo_root: Path,
    *,
    baseline_access_denied_directories: Iterable[dict[str, Any]] = (),
) -> tuple[list[Path], list[dict[str, Any]]]:
    """Enumerate the exact V3 production/model/immutable-run protection scope."""

    files: list[Path] = []
    access_markers: list[dict[str, Any]] = []
    forced_access_markers: dict[Path, dict[str, Any]] = {}
    for marker in baseline_access_denied_directories:
        relative = Path(str(marker["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe baseline access marker path: {relative}")
        absolute = (repo_root / relative).resolve()
        try:
            absolute.relative_to(repo_root)
        except ValueError as exc:
            raise ValueError(f"baseline access marker escaped repository: {relative}") from exc
        forced_access_markers[absolute] = dict(marker)
    for relative_root in PROTECTED_RECURSIVE_ROOTS:
        _scan_protected_tree(
            repo_root / relative_root,
            files,
            access_markers=access_markers,
            forced_access_markers=forced_access_markers,
            exclude_advisor_staging=relative_root == "data/development",
        )
    normalized_markers = []
    for marker in access_markers:
        payload = dict(marker)
        payload["path"] = Path(payload["path"]).relative_to(repo_root).as_posix()
        normalized_markers.append(payload)
    return (
        sorted(set(files), key=lambda item: item.as_posix().lower()),
        sorted(normalized_markers, key=lambda item: item["path"].lower()),
    )


def _stable_hash_record(repo_root: Path, path: Path) -> dict[str, Any]:
    before = path.stat()
    file_hash = sha256_file(path)
    after = path.stat()
    before_signature = (
        int(before.st_size),
        int(before.st_mtime_ns),
        int(getattr(before, "st_ino", 0)),
        int(getattr(before, "st_dev", 0)),
    )
    after_signature = (
        int(after.st_size),
        int(after.st_mtime_ns),
        int(getattr(after, "st_ino", 0)),
        int(getattr(after, "st_dev", 0)),
    )
    if before_signature != after_signature:
        raise RuntimeError(f"protected artifact changed while hashing: {path}")
    return {
        "path": path.relative_to(repo_root).as_posix(),
        "size": int(after.st_size),
        "sha256": file_hash,
    }


def _resolved_hash_workers(requested: int | None) -> int:
    if requested is None:
        configured = os.environ.get("ADVISOR_PROTECTED_HASH_WORKERS", "").strip()
        if configured:
            try:
                requested = int(configured)
            except ValueError as exc:
                raise ValueError("ADVISOR_PROTECTED_HASH_WORKERS must be an integer") from exc
        else:
            requested = 32
    if int(requested) <= 0:
        raise ValueError("max_workers must be positive")
    # The cap keeps simultaneous Windows file handles conservative even if an
    # accidental environment setting requests hundreds of workers.
    return min(int(requested), 32)


def protected_state(
    repo_root: Path,
    *,
    max_workers: int | None = None,
    baseline_access_denied_directories: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Full-content fingerprint the existing production/model/run state.

    V3 intentionally protects the paths that can contain production/latest,
    model outputs, all processed model artifacts, portfolio evidence,
    backtests/diagnostics, quarantine evidence, and immutable development runs.
    Large raw/source/reference archives are not
    claimed by this invariant because this advisor workflow never writes them.
    A dot-prefixed advisor staging directory currently being assembled under
    ``data/development`` is excluded; the entire ``artifacts`` tree remains covered.
    """

    repo_root = Path(repo_root).resolve()
    explicit_files, access_markers = _protected_files(
        repo_root,
        baseline_access_denied_directories=baseline_access_denied_directories,
    )
    workers = _resolved_hash_workers(max_workers)
    if workers == 1 or len(explicit_files) <= 1:
        records = [_stable_hash_record(repo_root, path) for path in explicit_files]
    else:
        records: list[dict[str, Any]] = []
        # Batching avoids retaining one Future per file for repositories with
        # hundreds of thousands of small archived artifacts. executor.map keeps
        # input order, so the Merkle input remains deterministic.
        batch_size = 8192
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="protected-sha256") as pool:
            for start in range(0, len(explicit_files), batch_size):
                batch = explicit_files[start : start + batch_size]
                records.extend(
                    pool.map(lambda path: _stable_hash_record(repo_root, path), batch)
                )
    return {
        "contract": "PROTECTED_EXISTING_PRODUCTION_MODEL_AND_IMMUTABLE_RUNS_V3",
        "scope": PROTECTED_SCOPE_V3,
        "file_count": len(records),
        "access_denied_directory_count": len(access_markers),
        "access_denied_directories": access_markers,
        "tree_sha256": _canonical_digest(
            {"scope": PROTECTED_SCOPE_V3, "files": records, "access_denied_directories": access_markers}
        ),
    }


def relative_artifact_manifest(run_root: Path, *, exclude: Iterable[str] = ()) -> list[dict[str, Any]]:
    run_root = Path(run_root)
    excluded = set(exclude)
    rows: list[dict[str, Any]] = []
    for path in sorted((item for item in run_root.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        relative = path.relative_to(run_root).as_posix()
        if relative in excluded:
            continue
        rows.append({"path": relative, "size": int(path.stat().st_size), "sha256": sha256_file(path)})
    return rows


def publish_staging(staging_root: Path, final_root: Path) -> None:
    staging_root = Path(staging_root)
    final_root = Path(final_root)
    if final_root.exists():
        raise FileExistsError(f"immutable advisor run already exists: {final_root}")
    if not staging_root.is_dir():
        raise FileNotFoundError(staging_root)
    os.replace(staging_root, final_root)


def build_private_audit_bundle(
    *,
    run_root: Path,
    output_path: Path,
    allowlist: Iterable[str],
    forbidden_source_sha256: str,
) -> None:
    """Create a whitelist-only ZIP; broker workbooks can never enter it."""

    run_root = Path(run_root)
    output_path = Path(output_path)
    if output_path.exists():
        raise FileExistsError(output_path)
    members: list[tuple[Path, str]] = []
    for member in allowlist:
        relative = Path(member)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe bundle member: {member}")
        source = run_root / relative
        if not source.is_file():
            raise FileNotFoundError(source)
        if source.suffix.lower() in {".xls", ".xlsx", ".xlsm"}:
            raise ValueError("broker/spreadsheet files are forbidden in the audit ZIP")
        if sha256_file(source) == forbidden_source_sha256:
            raise ValueError("raw broker source content is forbidden in the audit ZIP")
        members.append((source, relative.as_posix()))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output_path, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for source, arcname in members:
            archive.write(source, arcname=arcname)
