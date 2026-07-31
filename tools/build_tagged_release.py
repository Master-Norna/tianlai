#!/usr/bin/env python3
"""Build the two files published for one already-created Tianlai tag."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Final, Sequence


_SOURCE_BUILDER_PATH: Final = (
    Path(__file__).resolve().with_name("build_source_release.py")
)
_SOURCE_BUILDER_SPEC = importlib.util.spec_from_file_location(
    "tianlai_tagged_release_source_builder",
    _SOURCE_BUILDER_PATH,
)
if (
    _SOURCE_BUILDER_SPEC is None
    or _SOURCE_BUILDER_SPEC.loader is None
):  # pragma: no cover - import machinery failure
    raise RuntimeError("could not load the Tianlai source-release builder")
source_release = importlib.util.module_from_spec(_SOURCE_BUILDER_SPEC)
sys.modules[_SOURCE_BUILDER_SPEC.name] = source_release
_SOURCE_BUILDER_SPEC.loader.exec_module(source_release)


_TAG_RE: Final = re.compile(
    r"^v(?P<version>[0-9][0-9A-Za-z]*(?:[._+-][0-9A-Za-z]+)*)$"
)
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")


class TaggedReleaseError(RuntimeError):
    """Raised when a tag cannot produce a publishable artifact pair."""


def version_from_tag(tag: str) -> str:
    """Return the exact package version encoded by a safe ``v<version>`` tag."""

    if not isinstance(tag, str):
        raise TypeError("tag must be a string")
    matched = _TAG_RE.fullmatch(tag)
    if matched is None:
        raise TaggedReleaseError(
            "release tag must be `v<package-version>` and contain only "
            "portable version characters"
        )
    return matched.group("version")


def resolve_tagged_head(
    repo: str | Path,
    *,
    tag: str,
) -> tuple[Path, str]:
    """Resolve one existing lightweight/annotated tag and require current HEAD."""

    # Validate before embedding the name in a Git revision argument. The Git
    # process still receives an argv list (never shell text), and the fully
    # qualified ref prevents similarly named branches from satisfying it.
    version_from_tag(tag)
    root = source_release._repository_root(repo)
    head_raw = source_release._run_git(
        root,
        [
            "rev-parse",
            "--verify",
            "--end-of-options",
            "HEAD^{commit}",
        ],
    ).strip()
    try:
        tag_raw = source_release._run_git(
            root,
            [
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"refs/tags/{tag}^{{commit}}",
            ],
        ).strip()
    except source_release.ReleaseBuildError as exc:
        raise TaggedReleaseError(
            f"release tag does not exist or does not resolve to a commit: {tag}"
        ) from exc

    try:
        head = head_raw.decode("ascii")
        tagged_commit = tag_raw.decode("ascii")
    except UnicodeDecodeError as exc:  # pragma: no cover - corrupt Git output
        raise TaggedReleaseError(
            "Git returned a non-ASCII release commit identifier"
        ) from exc
    if (
        source_release._COMMIT_RE.fullmatch(head) is None
        or source_release._COMMIT_RE.fullmatch(tagged_commit) is None
    ):
        raise TaggedReleaseError(
            "Git returned an invalid release commit identifier"
        )
    if tagged_commit != head:
        raise TaggedReleaseError(
            f"release tag {tag!r} does not point at current HEAD "
            f"({tagged_commit} != {head})"
        )
    return root, head


def _write_checksum(
    target: Path,
    *,
    archive_name: str,
    archive_sha256: str,
) -> None:
    if _SHA256_RE.fullmatch(archive_sha256) is None:
        raise TaggedReleaseError(
            "source builder returned an invalid lowercase SHA-256"
        )
    if target.exists():
        raise FileExistsError(f"release checksum already exists: {target}")

    payload = f"{archive_sha256}  {archive_name}\n".encode("ascii")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=".tianlai-release-checksum.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    published = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
        published = True
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                if not published and sys.exc_info()[0] is None:
                    raise


def build_tagged_release(
    repo: str | Path,
    output_dir: str | Path,
    *,
    tag: str,
) -> dict[str, object]:
    """Build a clean source ZIP and adjacent SHA-256 for an existing tag.

    This function deliberately has no overwrite or dirty-tree mode. A release
    candidate must be bound to committed metadata and must never silently
    replace a previously shared artifact.
    """

    version = version_from_tag(tag)
    root, tagged_commit = resolve_tagged_head(repo, tag=tag)
    destination = Path(output_dir).expanduser().absolute()
    if destination.exists() and not destination.is_dir():
        raise TaggedReleaseError(
            f"release output directory is not a directory: {destination}"
        )
    destination.mkdir(parents=True, exist_ok=True)

    archive = destination / f"tianlai-{version}-source.zip"
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    if checksum.exists():
        raise FileExistsError(f"release checksum already exists: {checksum}")

    result = source_release.build_source_release(
        root,
        archive,
        expected_version=version,
    )
    checksum_published = False
    try:
        project_version = result.get("project_version")
        if project_version != version:
            raise TaggedReleaseError(
                "source builder returned a project version that does not "
                f"match tag {tag!r}: {project_version!r}"
            )
        if result.get("dirty") is not False:
            raise TaggedReleaseError(
                "tagged release builder accepted a dirty source snapshot"
            )
        if result.get("commit") != tagged_commit:
            raise TaggedReleaseError(
                "source archive commit does not match the release tag "
                f"({result.get('commit')!r} != {tagged_commit!r})"
            )
        _, final_tagged_commit = resolve_tagged_head(root, tag=tag)
        if final_tagged_commit != tagged_commit:
            raise TaggedReleaseError(
                f"release tag moved during the build: {tag}"
            )
        archive_sha256 = result.get("archive_sha256")
        if not isinstance(archive_sha256, str):
            raise TaggedReleaseError(
                "source builder did not return an archive SHA-256"
            )
        actual_sha256 = hashlib.sha256(archive.read_bytes()).hexdigest()
        if archive_sha256 != actual_sha256:
            raise TaggedReleaseError(
                "published archive does not match the source builder SHA-256"
            )
        _write_checksum(
            checksum,
            archive_name=archive.name,
            archive_sha256=archive_sha256,
        )
        checksum_published = True
    except BaseException:
        # The archive was created by this invocation. Without its checksum the
        # promised release pair is incomplete, so leave neither file visible.
        try:
            archive.unlink(missing_ok=True)
        finally:
            if checksum_published:
                checksum.unlink(missing_ok=True)
        raise

    return {
        "tag": tag,
        "project_version": version,
        "commit": result["commit"],
        "archive": str(archive),
        "archive_sha256": archive_sha256,
        "checksum": str(checksum),
        "output_dir": str(destination),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a clean Tianlai source ZIP and SHA-256 for an existing "
            "v<package-version> tag."
        )
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Git worktree root",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="new artifact directory",
    )
    parser.add_argument(
        "--tag",
        required=True,
        help="existing Git tag, exactly v<package-version>",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        result = build_tagged_release(
            arguments.repo,
            arguments.output_dir,
            tag=arguments.tag,
        )
    except (
        FileExistsError,
        TaggedReleaseError,
        source_release.ReleaseBuildError,
        OSError,
    ) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
