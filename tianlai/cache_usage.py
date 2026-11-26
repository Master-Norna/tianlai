"""Read-only, best-effort accounting of known regenerable render cache files."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import Any

from .render_lock import capture_plain_directory, revalidate_plain_directory


_PREFIX = re.compile(r"[0-9a-f]{2}")
_KEY = re.compile(r"[0-9a-f]{64}")


def _plain_directory(path: Path) -> bool:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(status.st_mode) and not (
        getattr(status, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def cache_usage(root: Path) -> dict[str, Any]:
    """Count logical bytes, never read payloads, create paths or delete files.

    This is an observational snapshot, not verification of cache contents.
    Concurrent changes may make the counts stale immediately.
    """
    result: dict[str, Any] = {
        "root": str(root.absolute()),
        "snapshot_only": True,
        "stems": {"files": 0, "bytes": 0},
        "analysis": {"files": 0, "bytes": 0},
        "total_bytes": 0,
    }
    if not os.path.lexists(root):
        return result
    root_identity = capture_plain_directory(root)
    for name, suffixes in (
        ("stems", {".f32le", ".json"}),
        ("analysis", {".json"}),
    ):
        version = root_identity.path / name / "v1"
        revalidate_plain_directory(root_identity)
        if not _plain_directory(version.parent) or not _plain_directory(version):
            continue
        version_identity = capture_plain_directory(version)
        for prefix in version_identity.path.iterdir():
            if not _PREFIX.fullmatch(prefix.name) or not _plain_directory(prefix):
                continue
            prefix_identity = capture_plain_directory(prefix)
            revalidate_plain_directory(version_identity)
            for entry in prefix_identity.path.iterdir():
                if (
                    entry.suffix not in suffixes
                    or not _KEY.fullmatch(entry.stem)
                    or not entry.stem.startswith(prefix.name)
                ):
                    continue
                revalidate_plain_directory(prefix_identity)
                try:
                    status = entry.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(status.st_mode) or (
                    getattr(status, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                ):
                    continue
                result[name]["files"] += 1
                result[name]["bytes"] += status.st_size
    result["total_bytes"] = result["stems"]["bytes"] + result["analysis"]["bytes"]
    return result
