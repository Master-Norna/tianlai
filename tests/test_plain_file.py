from __future__ import annotations

import os
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest import mock

import pytest

import tianlai.plain_file as plain_file_module
from tianlai.plain_file import (
    read_plain_file_bytes,
    revalidate_plain_file,
    sha256_plain_file,
)


def test_windows_path_and_handle_ctime_may_differ_but_identity_may_not() -> None:
    path_status = SimpleNamespace(
        st_dev=7,
        st_ino=11,
        st_size=13,
        st_mtime_ns=17,
        st_ctime_ns=19,
        st_birthtime_ns=29,
    )
    handle_status = SimpleNamespace(
        st_dev=7,
        st_ino=11,
        st_size=13,
        st_mtime_ns=17,
        st_ctime_ns=23,
        st_birthtime_ns=29,
    )

    with mock.patch(
        "tianlai.plain_file._is_windows_runtime",
        return_value=True,
    ):
        assert plain_file_module._same_object(path_status, handle_status)
        assert not plain_file_module._same_handle_snapshot(
            path_status,
            handle_status,
        )
        for field in (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_birthtime_ns",
        ):
            changed = SimpleNamespace(**vars(handle_status))
            setattr(changed, field, getattr(changed, field) + 1)
            assert not plain_file_module._same_object(path_status, changed)


def test_plain_file_rejects_an_unavailable_stable_file_id() -> None:
    status = SimpleNamespace(
        st_mode=stat.S_IFREG,
        st_nlink=1,
        st_file_attributes=0,
        st_ino=0,
    )

    with pytest.raises(OSError, match="stable filesystem identity"):
        plain_file_module._require_plain_status(status)


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 paths are required")
def test_plain_file_canonicalises_a_verified_short_name(tmp_path: Path) -> None:
    import ctypes

    target = tmp_path / "Tianlai evidence document with spaces.json"
    target.write_bytes(b'{"ok":true}\n')
    buffer = ctypes.create_unicode_buffer(32_768)
    length = ctypes.windll.kernel32.GetShortPathNameW(
        str(target),
        buffer,
        len(buffer),
    )
    if not length or length >= len(buffer):
        pytest.skip("GetShortPathNameW did not return an alias")
    short_path = Path(buffer.value)
    if short_path == target:
        pytest.skip("8.3 short-name generation is disabled on this volume")

    identity, payload = read_plain_file_bytes(short_path, maximum_bytes=1024)

    assert payload == b'{"ok":true}\n'
    assert identity.path == target.resolve()
    assert revalidate_plain_file(identity) == target.resolve()


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
    target_status = target.stat()
    os.utime(
        replacement,
        ns=(target_status.st_atime_ns, target_status.st_mtime_ns),
    )
    replacement_status = replacement.stat()
    assert replacement_status.st_size == target_status.st_size
    assert replacement_status.st_mtime_ns == target_status.st_mtime_ns
    assert replacement_status.st_ino != target_status.st_ino
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
