from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any
import unicodedata

from .upgrade_registry import HISTORICAL_UPGRADE_REGISTRY


_ROW = re.compile(
    r"^\|\s*(?P<upgrade_id>(?:VPO|ORP|SAM|SYN|SFX)-\d{2})\s*"
    r"\|\s*`(?P<relative_path>[^`]+)`\s*\|"
)
_QUALITY_LEVELS = frozenset(("fallback", "candidate", "formal"))
_COLLABORATION_LEVELS = frozenset(
    ("untested", "in_progress", "passed", "failed")
)
_UPGRADE_ID = re.compile(r"(?:VPO|ORP|SAM|SYN|SFX)-\d{2}")


@dataclass(frozen=True, slots=True)
class UpgradeEntry:
    upgrade_id: str
    relative_path: str
    manifest_path: str
    implementation_type: str
    quality_tier: str
    collaboration_review_status: str | None
    upgrade_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class UpgradeProgress:
    total: int
    counts: dict[str, int]
    collaboration_counts: dict[str, int]
    entries: tuple[UpgradeEntry, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "tianlai.historical_upgrade_ledger",
            "schema_version": 1,
            "scope": "historical_first_expansion_98",
            "scope_note": "历史 98 件升级账本；不是当前声音入口总数",
            "total": self.total,
            "counts": self.counts,
            "collaboration_counts": self.collaboration_counts,
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _validated_upgrade_rows(
    rows: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    if len(rows) != 98:
        raise ValueError(
            f"upgrade registry must contain exactly 98 rows, found {len(rows)}"
        )

    ids = [upgrade_id for upgrade_id, _ in rows]
    paths = [relative_path for _, relative_path in rows]
    duplicate_ids = sorted(
        item for item, count in Counter(ids).items() if count > 1
    )
    duplicate_paths = sorted(
        item for item, count in Counter(paths).items() if count > 1
    )
    if duplicate_ids:
        raise ValueError(f"duplicate upgrade IDs: {', '.join(duplicate_ids)}")
    if duplicate_paths:
        raise ValueError(
            f"duplicate upgrade paths: {', '.join(duplicate_paths)}"
        )

    portable_path_keys: dict[str, str] = {}
    for upgrade_id, relative_path in rows:
        if _UPGRADE_ID.fullmatch(upgrade_id) is None:
            raise ValueError(f"invalid upgrade ID: {upgrade_id!r}")
        normalized = unicodedata.normalize("NFC", relative_path)
        pure_path = PurePosixPath(relative_path)
        if (
            not relative_path
            or normalized != relative_path
            or "\\" in relative_path
            or pure_path.is_absolute()
            or pure_path.as_posix() != relative_path
            or ".." in pure_path.parts
        ):
            raise ValueError(
                "upgrade path must be an NFC portable POSIX relative path: "
                f"{relative_path!r}"
            )
        key = normalized.casefold()
        previous = portable_path_keys.get(key)
        if previous is not None:
            raise ValueError(
                "upgrade paths collide on a case-insensitive filesystem: "
                f"{previous!r}, {relative_path!r}"
            )
        portable_path_keys[key] = relative_path
    return rows


def load_upgrade_progress(
    instrument_root: str | Path,
    registry_path: str | Path | None = None,
) -> UpgradeProgress:
    """Load the 98-item registry and reconcile every entry with its manifest.

    The packaged registry is used by default.  A compatible Markdown registry
    may still be supplied explicitly.  A SoundFont entry without explicit
    quality metadata is deliberately inferred as ``fallback``; every dedicated
    implementation must state its quality tier and may never receive credit
    merely because it can produce non-zero PCM.
    """

    root = Path(instrument_root).resolve()
    if registry_path is None:
        rows = list(HISTORICAL_UPGRADE_REGISTRY)
    else:
        registry = Path(registry_path).resolve()
        rows = []
        for line in registry.read_text(encoding="utf-8").splitlines():
            match = _ROW.match(line)
            if match is not None:
                rows.append(
                    (match.group("upgrade_id"), match.group("relative_path"))
                )

    rows = _validated_upgrade_rows(rows)

    entries: list[UpgradeEntry] = []
    for upgrade_id, relative_path in rows:
        manifest_path = (root.joinpath(*relative_path.split("/")) / "乐器.json").resolve()
        try:
            manifest_path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"upgrade path escapes instrument root: {relative_path}") from exc
        if not manifest_path.is_file():
            raise ValueError(f"upgrade manifest does not exist: {manifest_path}")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"instrument manifest root must be an object: {manifest_path}")
        implementation_type = str(data.get("type", ""))
        raw_quality = data.get("quality_tier")
        if raw_quality is None and implementation_type == "soundfont":
            quality = "fallback"
        elif raw_quality is None:
            raise ValueError(
                f"dedicated upgrade must declare quality_tier: {manifest_path}"
            )
        else:
            quality = str(raw_quality)
        if quality not in _QUALITY_LEVELS:
            raise ValueError(f"invalid quality_tier {quality!r}: {manifest_path}")
        if quality != "fallback" and implementation_type == "soundfont":
            raise ValueError(
                f"generic SoundFont cannot be credited as {quality}: {manifest_path}"
            )
        collaboration_status = data.get("collaboration_review_status")
        if collaboration_status is not None:
            collaboration_status = str(collaboration_status)
            if collaboration_status not in _COLLABORATION_LEVELS:
                raise ValueError(
                    "invalid collaboration_review_status "
                    f"{collaboration_status!r}: {manifest_path}"
                )
        entries.append(
            UpgradeEntry(
                upgrade_id=upgrade_id,
                relative_path=relative_path,
                manifest_path=str(manifest_path),
                implementation_type=implementation_type,
                quality_tier=quality,
                collaboration_review_status=collaboration_status,
                upgrade_status=str(data.get("upgrade_status", "待升级")),
            )
        )

    counts = Counter(entry.quality_tier for entry in entries)
    collaboration_counts = Counter(
        entry.collaboration_review_status for entry in entries
    )
    return UpgradeProgress(
        total=len(entries),
        counts={level: counts.get(level, 0) for level in ("fallback", "candidate", "formal")},
        collaboration_counts={
            level: collaboration_counts.get(level, 0)
            for level in ("untested", "in_progress", "passed", "failed")
        },
        entries=tuple(entries),
    )
