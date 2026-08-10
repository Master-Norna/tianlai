from __future__ import annotations

import copy
import json
import multiprocessing
import os
from pathlib import Path
import pickle

import pytest

import tianlai.authoring_project as project_module
import tianlai.authoring_core as core_module
from tianlai.authoring_core import build_authoring_snapshot
from tianlai.authoring_project import (
    AuthoringProjectError,
    AuthoringProjectState,
    PROJECT_MANIFEST_NAME,
    create_authoring_project,
    open_authoring_project,
    save_authoring_project,
)
from tianlai.utc_timestamp import validate_canonical_utc_timestamp


def _hold_cross_process_project_lock(
    project_root: str,
    ready,
    release,
) -> None:
    from pathlib import Path

    import tianlai.authoring_project as child_project_module

    with child_project_module._exclusive_project_write_lock(
        Path(project_root)
    ):
        ready.set()
        release.wait(10)


def _code(call) -> str:
    with pytest.raises(AuthoringProjectError) as captured:
        call()
    return captured.value.code


def _project(tmp_path: Path, title: str = "天籁 工程"):
    root = tmp_path / "创作 空间" / title
    root.parent.mkdir()
    return root, create_authoring_project(root, title=title)


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 paths are required")
def test_authoring_round_trip_accepts_a_short_name_parent(
    tmp_path: Path,
) -> None:
    import ctypes

    parent = tmp_path / "Tianlai authoring parent with spaces"
    parent.mkdir()
    buffer = ctypes.create_unicode_buffer(32_768)
    length = ctypes.windll.kernel32.GetShortPathNameW(
        str(parent),
        buffer,
        len(buffer),
    )
    if not length or length >= len(buffer):
        pytest.skip("GetShortPathNameW did not return an alias")
    short_parent = Path(buffer.value)
    if short_parent == parent:
        pytest.skip("8.3 short-name generation is disabled on this volume")

    requested_root = short_parent / "project"
    created = create_authoring_project(requested_root, title="Short path")
    opened = open_authoring_project(requested_root)

    assert opened == created
    assert (parent / "project").is_dir()


def test_failed_entry_cleanup_preserves_a_last_moment_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "revisions"
    parent.mkdir()
    stage = parent / ".revision-stage-test"
    stage.mkdir()
    (stage / "original.txt").write_text("original", encoding="utf-8")
    parked = parent / "parked-original"
    real_rename = os.rename
    swapped = False

    def swap_then_rename(source, destination):
        nonlocal swapped
        if not swapped and Path(source) == stage:
            swapped = True
            real_rename(stage, parked)
            stage.mkdir()
            (stage / "valuable.txt").write_text("valuable", encoding="utf-8")
        return real_rename(source, destination)

    monkeypatch.setattr(project_module.os, "rename", swap_then_rename)
    project_module._preserve_failed_entry(
        stage,
        parent=parent,
        prefix=".revision-stage-",
    )

    preserved = list(parent.glob("*.cleanup-preserved-*"))
    assert swapped
    assert (parked / "original.txt").read_text(encoding="utf-8") == "original"
    assert len(preserved) == 1
    assert (preserved[0] / "valuable.txt").read_text(encoding="utf-8") == "valuable"
    assert not stage.exists()


def test_writability_probe_uses_handle_cleanup_not_path_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "renders"
    output.mkdir()
    before = {entry.name for entry in output.iterdir()}

    def reject_path_unlink(*_args, **_kwargs):
        raise AssertionError("writability probes must not unlink a pathname")

    monkeypatch.setattr(Path, "unlink", reject_path_unlink)
    assert core_module._probe_plain_directory_writable(output) is True
    assert {entry.name for entry in output.iterdir()} == before


def test_failed_project_creation_is_moved_to_recoverable_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "projects"
    parent.mkdir()
    root = parent / "unfinished"
    sibling = parent / "unrelated"
    sibling.mkdir()
    (sibling / "sentinel.txt").write_text("keep", encoding="utf-8")

    def fail_pointer(_root: Path, _manifest: dict[str, object]) -> None:
        raise AuthoringProjectError("injected_pointer_failure")

    monkeypatch.setattr(project_module, "_replace_manifest_pointer", fail_pointer)
    assert _code(
        lambda: create_authoring_project(root, title="unfinished")
    ) == "injected_pointer_failure"
    preserved = list(parent.glob("unfinished.cleanup-preserved-*"))
    assert not root.exists()
    assert len(preserved) == 1
    assert (preserved[0] / ".tianlai" / "revisions").is_dir()
    assert (sibling / "sentinel.txt").read_text(encoding="utf-8") == "keep"


def test_failed_revision_stage_is_preserved_outside_active_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state = _project(tmp_path)
    documents = state.detached_documents()
    documents["score"]["title"] = "will fail validation"
    real_validate = project_module._validate_revision_directory

    def fail_stage(directory: Path, **kwargs):
        if directory.name.startswith(".revision-stage-"):
            raise AuthoringProjectError("injected_stage_validation_failure")
        return real_validate(directory, **kwargs)

    monkeypatch.setattr(
        project_module,
        "_validate_revision_directory",
        fail_stage,
    )
    assert _code(
        lambda: save_authoring_project(
            root,
            expected_revision=state.revision,
            documents=documents,
        )
    ) == "injected_stage_validation_failure"
    revisions = root / ".tianlai" / "revisions"
    assert [
        entry
        for entry in revisions.glob(".revision-stage-*")
        if ".cleanup-preserved-" not in entry.name
    ] == []
    preserved = list(
        revisions.glob(".revision-stage-*.cleanup-preserved-*")
    )
    assert len(preserved) == 1
    assert (preserved[0] / "score.json").is_file()

    monkeypatch.setattr(
        project_module,
        "_validate_revision_directory",
        real_validate,
    )
    assert open_authoring_project(root).revision == state.revision


def test_create_unicode_project_is_instrument_neutral_and_reopens(tmp_path: Path) -> None:
    root, state = _project(tmp_path)
    assert len(state.project_id) == 32
    assert len(state.revision) == 64
    assert state.documents["score"] == {
        "schema_version": 1,
        "title": "天籁 工程",
        "sample_rate": 48_000,
        "tail_seconds": 2.0,
        "tuning": {"temperament": "equal", "a4_hz": 440.0},
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1.0,
                "bpm": 120.0,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "part-1",
                "name": "声部 1",
                "default_dynamic": "mf",
                "notes": [],
            }
        ],
    }
    assert state.documents["authoring_roster"]["assignments"] == [
        {"part": "part-1", "instrument": None}
    ]
    assert state.documents["render_profile"] == {
        "kind": "tianlai.render_profile",
        "schema_version": 1,
        "name": "preview-v1",
        "expression": "ensemble",
        "range_mode": "compatibility",
        "seed": 0,
        "master_gain_db": 0.0,
        "normalize_peak_db": -1.0,
        "space": {
            "enabled": True,
            "config": {
                "name": "小厅堂",
                "wet_db": -15.0,
                "room_size": 0.5,
                "predelay_ms": 18.0,
                "damping_hz": 6500.0,
                "highpass_hz": 150.0,
                "reference_distance_m": 3.0,
                "distance_exponent": 0.5,
                "min_send": 0.5,
                "max_send": 1.8,
            },
        },
        "collaboration_mode": None,
        "write_stems": True,
        "use_stem_cache": True,
        "refresh_stem_cache": False,
    }
    assert open_authoring_project(root) == state
    assert {item.name for item in root.iterdir()} == {
        PROJECT_MANIFEST_NAME,
        ".tianlai",
        "renders",
    }
    assert validate_canonical_utc_timestamp(state.created_at_utc) == (
        state.created_at_utc
    )
    assert validate_canonical_utc_timestamp(state.updated_at_utc) == (
        state.updated_at_utc
    )


@pytest.mark.parametrize(
    ("metadata", "timestamp", "expected_code"),
    [
        ("project", "2026-08-09T12:34:56+00:00", "invalid_project_manifest"),
        ("project", "2026-02-31T12:34:56.000Z", "invalid_project_manifest"),
        ("revision", "2026-08-09T12:34:56.000Z\n", "invalid_revision_manifest"),
        ("revision", "2026-08-09T12:34:56Z", "invalid_revision_manifest"),
    ],
)
def test_durable_metadata_rejects_noncanonical_or_controlled_timestamps(
    tmp_path: Path,
    metadata: str,
    timestamp: str,
    expected_code: str,
) -> None:
    root = tmp_path / f"timestamp-{metadata}-{len(timestamp)}"
    state = create_authoring_project(root, title="timestamp contract")
    path = (
        root / PROJECT_MANIFEST_NAME
        if metadata == "project"
        else root / ".tianlai" / "revisions" / state.revision / "revision.json"
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    document["created_at_utc"] = timestamp
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    assert _code(lambda: open_authoring_project(root)) == expected_code


def test_blank_project_is_saveable_but_not_renderable(tmp_path: Path) -> None:
    root, state = _project(tmp_path)
    saved = save_authoring_project(
        root,
        expected_revision=state.revision,
        documents=state.documents,
    )
    assert saved.revision == state.revision
    readiness = build_authoring_snapshot(saved, project_root=root)["readiness"]
    assert readiness["status"] == "blocked"
    assert readiness["render_allowed"] is False
    assert readiness["summary"] == {
        "part_count": 1,
        "note_count": 0,
        "assigned_part_count": 0,
        "duration_seconds": 0.0,
        "sample_rate": 48_000,
    }
    assert [issue["code"] for issue in readiness["issues"]] == [
        "authoring_roster.unassigned_part"
    ]


def test_save_is_three_document_cas_and_never_overwrites_stale_revision(
    tmp_path: Path,
) -> None:
    root, original = _project(tmp_path)
    documents = original.detached_documents()
    documents["score"]["title"] = "新标题"
    saved = save_authoring_project(
        root,
        expected_revision=original.revision,
        documents=documents,
    )
    assert saved.revision != original.revision
    assert saved.documents["score"]["title"] == "新标题"
    stale = original.detached_documents()
    stale["score"]["title"] = "不应覆盖"
    assert _code(
        lambda: save_authoring_project(
            root,
            expected_revision=original.revision,
            documents=stale,
        )
    ) == "revision_conflict"
    assert open_authoring_project(root) == saved


def test_crash_after_revision_publish_before_pointer_keeps_old_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, original = _project(tmp_path)
    documents = original.detached_documents()
    documents["score"]["title"] = "孤儿 revision"

    def fail_pointer(_root: Path, _manifest: dict[str, object]) -> None:
        raise AuthoringProjectError("injected_pointer_crash")

    monkeypatch.setattr(project_module, "_replace_manifest_pointer", fail_pointer)
    assert _code(
        lambda: save_authoring_project(
            root,
            expected_revision=original.revision,
            documents=documents,
        )
    ) == "injected_pointer_crash"
    assert open_authoring_project(root).revision == original.revision
    revisions = root / ".tianlai" / "revisions"
    assert len([entry for entry in revisions.iterdir() if len(entry.name) == 64]) == 2


def test_current_revision_tamper_is_rejected_without_leaking_a_path(
    tmp_path: Path,
) -> None:
    root, state = _project(tmp_path)
    score_path = root / ".tianlai" / "revisions" / state.revision / "score.json"
    document = json.loads(score_path.read_text(encoding="utf-8"))
    document["title"] = "篡改"
    score_path.write_text(json.dumps(document), encoding="utf-8")
    failure = None
    try:
        open_authoring_project(root)
    except AuthoringProjectError as exc:
        failure = exc
    assert failure is not None
    assert failure.code == "revision_tampered"
    assert str(root) not in str(failure)
    assert str(root) not in repr(failure.to_issue())


@pytest.mark.parametrize("target_outside_project", (False, True))
def test_symlink_or_reparse_managed_directory_is_rejected(
    tmp_path: Path,
    target_outside_project: bool,
) -> None:
    root, _state = _project(tmp_path)
    renders = root / "renders"
    renders.rmdir()
    target = (
        tmp_path / "outside"
        if target_outside_project
        else root / "unmanaged-render-target"
    )
    target.mkdir()
    try:
        os.symlink(target, renders, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable for this test account")
    assert _code(lambda: open_authoring_project(root)) == "unsafe_renders_directory"


def test_project_root_and_missing_root_parent_must_not_be_symlinks(
    tmp_path: Path,
) -> None:
    root, _state = _project(tmp_path)
    root_link = tmp_path / "project-link"
    parent_link = tmp_path / "parent-link"
    try:
        os.symlink(root, root_link, target_is_directory=True)
        os.symlink(root.parent, parent_link, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable for this test account")
    assert _code(lambda: open_authoring_project(root_link)) == "invalid_project_root"
    assert _code(
        lambda: create_authoring_project(
            parent_link / "new-project",
            title="must fail closed",
        )
    ) == "invalid_project_root"


def test_revision_directory_shape_is_exact(tmp_path: Path) -> None:
    root, state = _project(tmp_path)
    revision = root / ".tianlai" / "revisions" / state.revision
    (revision / "unexpected.txt").write_text("x", encoding="utf-8")
    assert _code(lambda: open_authoring_project(root)) == "invalid_revision_shape"


def test_invalid_document_set_and_stale_expected_revision_are_safe(tmp_path: Path) -> None:
    root, state = _project(tmp_path)
    assert _code(
        lambda: save_authoring_project(
            root,
            expected_revision="0" * 64,
            documents=state.documents,
        )
    ) == "revision_conflict"
    malformed = state.detached_documents()
    malformed.pop("render_profile")
    assert _code(
        lambda: save_authoring_project(
            root,
            expected_revision=state.revision,
            documents=malformed,
        )
    ) == "invalid_documents_shape"


def test_authoring_note_limit_is_fixed_at_fifty_thousand(tmp_path: Path) -> None:
    root, state = _project(tmp_path)
    documents = state.detached_documents()
    note = {
        "bar": 1,
        "beat": 1.0,
        "duration_beats": 0.25,
        "pitch": 60,
    }
    documents["score"]["parts"][0]["notes"] = [
        {**note, "event_id": f"event-{index}"}
        for index in range(50_001)
    ]
    assert _code(
        lambda: save_authoring_project(
            root,
            expected_revision=state.revision,
            documents=documents,
        )
    ) == "invalid_score"
    assert open_authoring_project(root).revision == state.revision


def test_authoring_score_title_has_a_bounded_utf8_product_contract(
    tmp_path: Path,
) -> None:
    root, state = _project(tmp_path)
    documents = state.detached_documents()
    documents["score"]["title"] = "乐" * 342
    assert _code(
        lambda: save_authoring_project(
            root,
            expected_revision=state.revision,
            documents=documents,
        )
    ) == "invalid_score"
    assert open_authoring_project(root).revision == state.revision


def test_state_returns_detached_documents(tmp_path: Path) -> None:
    root, state = _project(tmp_path)
    detached = state.detached_documents()
    detached["score"]["title"] = "mutated"
    assert open_authoring_project(root).documents["score"]["title"] == "天籁 工程"


def test_state_documents_and_hashes_are_deeply_immutable(tmp_path: Path) -> None:
    root, state = _project(tmp_path)

    with pytest.raises(TypeError):
        state.documents["score"]["title"] = "forged"
    with pytest.raises(TypeError):
        state.documents["score"]["parts"].append({})
    with pytest.raises(TypeError):
        state.document_revisions["score"] = "0" * 64

    detached = state.detached_documents()
    detached["score"]["title"] = "editable detached copy"
    assert state.documents["score"]["title"] != detached["score"]["title"]
    snapshot = build_authoring_snapshot(state, project_root=root)
    assert snapshot["project"]["revision"] == state.revision
    assert snapshot["documents"] == state.detached_documents()


def test_immutable_state_survives_a_pickle_round_trip(tmp_path: Path) -> None:
    root, state = _project(tmp_path)

    restored = pickle.loads(pickle.dumps(state))

    assert restored == state
    assert copy.copy(restored.documents) is restored.documents
    assert copy.copy(restored.documents["score"]["parts"]) is restored.documents[
        "score"
    ]["parts"]
    with pytest.raises(TypeError):
        restored.documents["score"]["parts"][0]["name"] = "forged"
    with pytest.raises(TypeError):
        restored.documents["score"]["parts"].append({})
    with pytest.raises(TypeError):
        restored.document_revisions["score"] = "0" * 64
    editable = restored.detached_documents()
    editable["score"]["title"] = "editable"
    assert editable["score"]["title"] != restored.documents["score"]["title"]
    assert build_authoring_snapshot(restored, project_root=root)["project"][
        "revision"
    ] == state.revision


@pytest.mark.parametrize("mismatch", ["document_hash", "revision"])
def test_snapshot_rejects_an_internally_inconsistent_state(
    tmp_path: Path,
    mismatch: str,
) -> None:
    root, state = _project(tmp_path)
    documents = state.detached_documents()
    hashes = dict(state.document_revisions)
    revision = state.revision
    if mismatch == "document_hash":
        documents["score"]["title"] = "changed without a new hash"
    else:
        revision = "0" * 64
    forged = AuthoringProjectState(
        project_id=state.project_id,
        title=state.title,
        created_at_utc=state.created_at_utc,
        updated_at_utc=state.updated_at_utc,
        revision=revision,
        documents=documents,
        document_revisions=hashes,
    )

    with pytest.raises(AuthoringProjectError):
        build_authoring_snapshot(forged, project_root=root)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", 7),
        ("title", "x" * 1025),
        ("created_at_utc", "2026-08-09T12:34:56+00:00"),
        ("updated_at_utc", "not-a-timestamp"),
        ("updated_at_utc", "2000-01-01T00:00:00.000Z"),
    ],
)
def test_snapshot_rejects_invalid_in_memory_project_metadata(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    root, state = _project(tmp_path)
    values: dict[str, object] = {
        "project_id": state.project_id,
        "title": state.title,
        "created_at_utc": state.created_at_utc,
        "updated_at_utc": state.updated_at_utc,
        "revision": state.revision,
        "documents": state.detached_documents(),
        "document_revisions": dict(state.document_revisions),
    }
    values[field] = value
    forged = AuthoringProjectState(**values)  # type: ignore[arg-type]

    with pytest.raises(AuthoringProjectError) as captured:
        build_authoring_snapshot(forged, project_root=root)
    assert captured.value.code == "invalid_project_state_metadata"


@pytest.mark.parametrize(
    "invalid_case",
    [
        "numeric_part_id",
        "missing_note_position",
        "string_tuning_frequency",
        "null_phrases",
        "string_master_gain",
        "null_space",
        "numeric_authoring_name",
    ],
)
def test_save_rejects_raw_documents_outside_the_public_schemas(
    tmp_path: Path,
    invalid_case: str,
) -> None:
    root, state = _project(tmp_path)
    documents = state.detached_documents()
    expected_code = "invalid_score"
    if invalid_case == "numeric_part_id":
        documents["score"]["parts"][0]["id"] = 1
        documents["authoring_roster"]["assignments"][0]["part"] = "1"
    elif invalid_case == "missing_note_position":
        documents["score"]["parts"][0]["notes"] = [
            {
                "duration_beats": 1.0,
                "pitch": 60,
                "event_id": "event-1",
            }
        ]
    elif invalid_case == "string_tuning_frequency":
        documents["score"]["tuning"]["a4_hz"] = "440"
    elif invalid_case == "null_phrases":
        documents["score"]["parts"][0]["phrases"] = None
    elif invalid_case == "string_master_gain":
        documents["render_profile"]["master_gain_db"] = "0"
        expected_code = "invalid_render_profile"
    elif invalid_case == "null_space":
        documents["render_profile"]["space"] = None
        expected_code = "invalid_render_profile"
    else:
        documents["authoring_roster"]["name"] = 1
        expected_code = "invalid_authoring_roster"

    revisions = root / ".tianlai" / "revisions"
    before = {entry.name for entry in revisions.iterdir()}
    assert _code(
        lambda: save_authoring_project(
            root,
            expected_revision=state.revision,
            documents=documents,
        )
    ) == expected_code
    assert {entry.name for entry in revisions.iterdir()} == before
    assert open_authoring_project(root) == state


def test_save_clamps_a_rollback_clock_before_revision_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future = "2099-01-01T00:00:00.000Z"
    monkeypatch.setattr(project_module, "_utc_now", lambda: future)
    root, state = _project(tmp_path)
    documents = state.detached_documents()
    documents["score"]["title"] = "saved after clock rollback"

    monkeypatch.setattr(
        project_module,
        "_utc_now",
        lambda: "2000-01-01T00:00:00.000Z",
    )
    saved = save_authoring_project(
        root,
        expected_revision=state.revision,
        documents=documents,
    )

    assert saved.revision != state.revision
    assert saved.updated_at_utc == future
    assert open_authoring_project(root) == saved
    revisions = root / ".tianlai" / "revisions"
    assert {entry.name for entry in revisions.iterdir()} == {
        state.revision,
        saved.revision,
    }


def test_project_lock_shape_is_verified(tmp_path: Path) -> None:
    root, _state = _project(tmp_path)
    lock = root / ".tianlai" / "project.lock"
    lock.write_bytes(b"invalid")
    assert _code(lambda: open_authoring_project(root)) == "unsafe_project_lock"


def test_cas_save_is_serialized_across_processes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state = _project(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_cross_process_project_lock,
        args=(str(root), ready, release),
    )
    process.start()
    try:
        assert ready.wait(10), "child did not acquire the project lock"
        monkeypatch.setattr(
            project_module, "_PROJECT_LOCK_TIMEOUT_SECONDS", 0.1
        )
        assert _code(
            lambda: save_authoring_project(
                root,
                expected_revision=state.revision,
                documents=state.documents,
            )
        ) == "project_busy"
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
    assert open_authoring_project(root).revision == state.revision
