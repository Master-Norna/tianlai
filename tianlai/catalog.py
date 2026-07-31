from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    name: str
    category: str
    implementation_type: str
    manifest_path: str
    note_min: float | None = None
    note_max: float | None = None
    bank: int | None = None
    program: int | None = None
    quality_tier: str | None = None
    collaboration_review_status: str | None = None
    upgrade_status: str | None = None
    license_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def discover_instruments(root: str | Path) -> list[CatalogEntry]:
    base = Path(root).resolve()
    if not base.is_dir():
        raise ValueError(f"instrument catalog does not exist: {base}")
    paths = sorted(
        base.rglob("乐器.json"),
        key=lambda path: path.relative_to(base).as_posix().casefold(),
    )
    entries: list[CatalogEntry] = []
    for path in paths:
        with path.open("r", encoding="utf-8") as source:
            manifest = json.load(source)
        if not isinstance(manifest, dict):
            raise ValueError(f"instrument manifest root must be an object: {path}")
        relative = path.parent.relative_to(base)
        category = " / ".join(relative.parts[:-1]) or "未分类"
        entries.append(
            CatalogEntry(
                name=relative.parts[-1],
                category=category,
                implementation_type=str(manifest.get("type", "")),
                manifest_path=str(path),
                note_min=float(manifest["note_min"]) if "note_min" in manifest else None,
                note_max=float(manifest["note_max"]) if "note_max" in manifest else None,
                bank=int(manifest["bank"]) if "bank" in manifest else None,
                program=int(manifest["program"]) if "program" in manifest else None,
                quality_tier=(
                    str(manifest["quality_tier"]) if "quality_tier" in manifest else None
                ),
                collaboration_review_status=(
                    str(manifest["collaboration_review_status"])
                    if "collaboration_review_status" in manifest
                    else None
                ),
                upgrade_status=(
                    str(manifest["upgrade_status"]) if "upgrade_status" in manifest else None
                ),
                license_status=(
                    str(manifest["license_status"])
                    if "license_status" in manifest
                    else None
                ),
            )
        )
    return entries
