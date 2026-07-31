"""Migrate tracked audition reports from source-byte to canonical JSON hashes.

The migration is deliberately fail-closed: a legacy hash must still match the
current workspace bytes before it is archived. This proves that the canonical
identity was derived from the exact manifest and event document to which the
old report referred, rather than silently rebinding stale listening evidence.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.canonical_json import (  # noqa: E402
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
)
from tianlai.candidate import sha256_file  # noqa: E402


EXPECTED_REPORT_COUNT = 103
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AuditionEvidenceMigrationError(RuntimeError):
    """The old evidence cannot be safely rebound to canonical JSON."""


def _project_path(label: object, *, root: Path) -> Path:
    if not isinstance(label, str) or not label.strip():
        raise AuditionEvidenceMigrationError("试听报告缺少 events 路径")
    candidate = (root / Path(label)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as error:
        raise AuditionEvidenceMigrationError(
            f"events 路径越出项目目录: {label}"
        ) from error
    if not candidate.is_file():
        raise AuditionEvidenceMigrationError(f"events 不存在: {label}")
    return candidate


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as out:
            json.dump(document, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def migrate_report(
    report_path: str | Path,
    *,
    root: str | Path = ROOT,
    write: bool = False,
) -> bool:
    """Validate and optionally migrate one report.

    Returns ``True`` only when a legacy report needs migration.
    """

    root = Path(root).resolve()
    report_path = Path(report_path).resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise AuditionEvidenceMigrationError(
            f"试听报告不是 JSON object: {report_path}"
        )
    manifest_path = report_path.parent / "乐器.json"
    if not manifest_path.is_file():
        raise AuditionEvidenceMigrationError(f"乐器清单不存在: {manifest_path}")
    events_path = _project_path(report.get("events"), root=root)
    manifest_canonical = canonical_json_file_sha256(manifest_path)
    events_canonical = canonical_json_file_sha256(events_path)

    already_canonical = (
        report.get("hash_algorithm") == HASH_ALGORITHM
        and report.get("canonicalization") == CANONICALIZATION
        and report.get("manifest_canonical_sha256") == manifest_canonical
        and report.get("events_canonical_sha256") == events_canonical
    )
    if already_canonical:
        if "manifest_sha256" in report or "events_sha256" in report:
            raise AuditionEvidenceMigrationError(
                f"规范化报告仍含歧义旧顶层字段: {report_path}"
            )
        return False

    legacy_manifest = report.get("manifest_sha256")
    legacy_events = report.get("events_sha256")
    if not (
        isinstance(legacy_manifest, str)
        and _SHA256.fullmatch(legacy_manifest)
        and isinstance(legacy_events, str)
        and _SHA256.fullmatch(legacy_events)
    ):
        raise AuditionEvidenceMigrationError(
            f"报告既不是有效 canonical 证据，也没有完整旧字节证据: {report_path}"
        )
    if sha256_file(manifest_path) != legacy_manifest:
        raise AuditionEvidenceMigrationError(
            f"旧 manifest 字节 Hash 已过期，拒绝迁移: {report_path}"
        )
    if sha256_file(events_path) != legacy_events:
        raise AuditionEvidenceMigrationError(
            f"旧 events 字节 Hash 已过期，拒绝迁移: {report_path}"
        )

    migrated: dict[str, Any] = {}
    for key, value in report.items():
        if key == "manifest_sha256":
            migrated["hash_algorithm"] = HASH_ALGORITHM
            migrated["canonicalization"] = CANONICALIZATION
            migrated["manifest_canonical_sha256"] = manifest_canonical
            continue
        if key == "events_sha256":
            migrated["events_canonical_sha256"] = events_canonical
            migrated["identity_migration"] = {
                "status": "superseded_by_canonical_json_v1",
                "hash_algorithm": HASH_ALGORITHM,
                "hash_semantics": "source-file-bytes",
                "manifest_sha256": legacy_manifest,
                "events_sha256": legacy_events,
            }
            continue
        migrated[key] = value

    if write:
        _write_json_atomic(report_path, migrated)
    return True


def migrate_all(*, root: str | Path = ROOT, write: bool = False) -> tuple[int, int]:
    root = Path(root).resolve()
    reports = sorted((root / "乐器").rglob("试听核验.json"))
    if len(reports) != EXPECTED_REPORT_COUNT:
        raise AuditionEvidenceMigrationError(
            f"应有 {EXPECTED_REPORT_COUNT} 份试听报告，实际 {len(reports)}"
        )
    changed = sum(
        migrate_report(path, root=root, write=write) for path in reports
    )
    return len(reports), changed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="检查或迁移 103 份试听报告的规范化 JSON 证据 Hash"
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="通过全部旧字节绑定检查后原子写回报告",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    total, changed = migrate_all(write=args.write)
    verb = "已迁移" if args.write else "待迁移"
    print(f"试听报告 {total} 份；{verb} {changed} 份。")


if __name__ == "__main__":
    main()
