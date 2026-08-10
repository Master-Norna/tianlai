from __future__ import annotations

import os
from pathlib import Path

import pytest

from tianlai.plain_file import (
    read_plain_file_bytes,
    revalidate_plain_file,
    sha256_plain_file,
)


def test_descriptor_bound_read_and_hash_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "evidence.json"
    target.write_bytes(b'{"ok":true}\n')

    identity, payload = read_plain_file_bytes(target, maximum_bytes=1024)
    hash_identity, digest = sha256_plain_file(target, maximum_bytes=1024)

    assert payload == b'{"ok":true}\n'
    assert digest == "e5f1eb4d806641698a35efe20e098efd20d7d57a9b90ee69079d5bb650920726"
    assert identity == hash_identity
    assert revalidate_plain_file(identity) == target.absolute()


def test_plain_file_read_rejects_links_hardlinks_and_oversize(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(b"{}")
    hardlink = tmp_path / "hardlink.json"
    os.link(target, hardlink)
    with pytest.raises(OSError):
        read_plain_file_bytes(target, maximum_bytes=16)
    hardlink.unlink()

    with pytest.raises(OSError):
        read_plain_file_bytes(target, maximum_bytes=1)

    symlink = tmp_path / "symlink.json"
    try:
        symlink.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")
    with pytest.raises(OSError):
        read_plain_file_bytes(symlink, maximum_bytes=16)


def test_revalidation_rejects_same_name_replacement(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b'{"version":1}')
    identity, _ = read_plain_file_bytes(target, maximum_bytes=1024)
    old = tmp_path / "old.json"
    target.replace(old)
    target.write_bytes(b'{"version":2}')

    with pytest.raises(OSError):
        revalidate_plain_file(identity)


def test_open_race_rejects_replaced_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b'{"version":1}')
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(b'{"version":2}')
    original_open = os.open
    changed = False

    def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal changed
        if not changed and Path(path) == target:
            changed = True
            target.unlink()
            replacement.replace(target)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(OSError, match="identity changed"):
        read_plain_file_bytes(target, maximum_bytes=1024)
