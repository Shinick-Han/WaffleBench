"""Defensive ZIP extraction.

Every entry is validated before anything is written. Extraction goes to a
sibling staging directory that is renamed into place only after all entries
were written and their streamed sizes matched the declared sizes.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import pathlib
import shutil
import stat
import zipfile

DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 50_000
EXTRACTION_RECORD = ".sem_data_extraction.json"

_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_CHUNK = 1024 * 1024


class UnsafeArchiveError(ValueError):
    """Raised when an archive entry or the archive as a whole is rejected."""


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_member_parts(name: str) -> tuple[str, ...]:
    """Return the validated relative path parts of an archive member name."""
    if not name or "\x00" in name:
        raise UnsafeArchiveError(f"empty or NUL-containing entry name: {name!r}")
    normalized = name.replace("\\", "/")
    if normalized.startswith("/"):
        raise UnsafeArchiveError(f"absolute entry path: {name!r}")
    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if not parts:
        raise UnsafeArchiveError(f"entry resolves to archive root: {name!r}")
    for part in parts:
        if part == "..":
            raise UnsafeArchiveError(f"path traversal entry: {name!r}")
        if ":" in part:
            raise UnsafeArchiveError(f"drive or stream specifier in entry: {name!r}")
        if part != part.rstrip(" ."):
            raise UnsafeArchiveError(f"trailing dot or space in entry: {name!r}")
        if part.split(".")[0].lower() in _WINDOWS_RESERVED:
            raise UnsafeArchiveError(f"reserved device name in entry: {name!r}")
        if any(ord(ch) < 32 for ch in part):
            raise UnsafeArchiveError(f"control character in entry: {name!r}")
    return parts


def _entry_kind(info: zipfile.ZipInfo) -> str:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type == stat.S_IFLNK:
        raise UnsafeArchiveError(f"symbolic link entry: {info.filename!r}")
    if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise UnsafeArchiveError(f"special file entry: {info.filename!r}")
    if info.is_dir() or file_type == stat.S_IFDIR:
        return "dir"
    return "file"


def plan_extraction(
    archive: zipfile.ZipFile,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> list[tuple[zipfile.ZipInfo, tuple[str, ...], str]]:
    """Validate every entry and return (info, parts, kind) in archive order."""
    infos = archive.infolist()
    if len(infos) > max_entries:
        raise UnsafeArchiveError(f"archive has {len(infos)} entries, limit {max_entries}")
    plan = []
    seen: dict[str, str] = {}
    total = 0
    for info in infos:
        if info.flag_bits & 0x1:
            raise UnsafeArchiveError(f"encrypted entry: {info.filename!r}")
        parts = safe_member_parts(info.filename)
        kind = _entry_kind(info)
        key = "/".join(parts).casefold()
        previous = seen.get(key)
        if previous is not None and not (previous == kind == "dir"):
            raise UnsafeArchiveError(f"colliding entry path: {info.filename!r}")
        seen[key] = kind
        if kind == "file":
            if info.file_size > max_file_bytes:
                raise UnsafeArchiveError(
                    f"entry {info.filename!r} is {info.file_size} bytes, limit {max_file_bytes}"
                )
            total += info.file_size
            if total > max_total_bytes:
                raise UnsafeArchiveError(f"aggregate size exceeds limit {max_total_bytes}")
        plan.append((info, parts, kind))
    # A file may not also be used as a parent directory of another entry.
    for key, kind in seen.items():
        segments = key.split("/")
        for depth in range(1, len(segments)):
            if seen.get("/".join(segments[:depth])) == "file":
                raise UnsafeArchiveError(f"file/directory collision at {key!r}")
    return plan


def extract_archive(
    archive_path: str | os.PathLike,
    root: str | os.PathLike,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> dict:
    """Extract ``archive_path`` into the absent or empty directory ``root``."""
    archive_path = pathlib.Path(archive_path).resolve()
    root = pathlib.Path(root).resolve()
    if not archive_path.is_file():
        raise FileNotFoundError(f"archive not found: {archive_path}")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise FileExistsError(f"extraction root exists and is not an empty directory: {root}")
    staging = root.parent / f".{root.name}.extracting"
    if staging.exists():
        raise FileExistsError(f"stale staging directory present: {staging}")

    with zipfile.ZipFile(archive_path) as archive:
        plan = plan_extraction(
            archive,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
            max_entries=max_entries,
        )
        if any("/".join(parts).casefold() == EXTRACTION_RECORD for _, parts, _ in plan):
            raise UnsafeArchiveError(f"archive entry collides with {EXTRACTION_RECORD}")
        staging.mkdir(parents=True)
        staging_resolved = staging.resolve()
        files = 0
        total = 0
        try:
            for info, parts, kind in plan:
                target = staging.joinpath(*parts)
                resolved = target.resolve()
                if staging_resolved not in resolved.parents:
                    raise UnsafeArchiveError(f"entry escapes extraction root: {info.filename!r}")
                if kind == "dir":
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with archive.open(info) as source, open(target, "xb") as sink:
                    while True:
                        chunk = source.read(_CHUNK)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size or written > max_file_bytes:
                            raise UnsafeArchiveError(
                                f"entry {info.filename!r} streamed more bytes than declared"
                            )
                        sink.write(chunk)
                if written != info.file_size:
                    raise UnsafeArchiveError(f"entry {info.filename!r} size mismatch")
                files += 1
                total += written
                if total > max_total_bytes:
                    raise UnsafeArchiveError(f"aggregate size exceeds limit {max_total_bytes}")
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    record = {
        "archive": str(archive_path),
        "archive_name": archive_path.name,
        "archive_bytes": archive_path.stat().st_size,
        "archive_sha256": _sha256_file(archive_path),
        "extracted_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "files": files,
        "uncompressed_bytes": total,
        "limits": {
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
            "max_entries": max_entries,
        },
    }
    (staging / EXTRACTION_RECORD).write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if root.exists():
        root.rmdir()
    os.replace(staging, root)
    record["root"] = str(root)
    return record
