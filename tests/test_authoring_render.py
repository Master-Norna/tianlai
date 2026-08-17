from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

import tianlai.authoring_render as authoring_render_module
import tianlai.candidate_integrity as candidate_integrity_module
import tianlai.creative_workflow as creative_workflow_module
from tianlai.authoring_project import (
    create_authoring_project,
    save_authoring_project,
)
from tianlai.authoring_render import (
    AuthoringRenderCancelled,
    AuthoringRenderError,
    RenderCheckpoint,
    render_project_candidate,
)
from tianlai.candidate import (
    AUTHORING_ROSTER_CANDIDATE_NAME,
    CANDIDATE_MANIFEST_NAME,
    build_candidate_playback_map,
    canonical_json_sha256,
    load_candidate,
    sha256_file,
)
from tianlai.candidate_integrity import (
    CandidateIntegrityError,
    verify_candidate_integrity,
)
from tianlai.creative_workflow import (
    CreativeWorkflowError,
    activate_creative_workflow,
    attach_existing_candidate_for_audit,
    cancel_workflow_render,
    create_creative_workflow,
    decide_workflow_iteration,
    inspect_workflow_candidate_status,
    record_verified_workflow_hard_failure,
    record_workflow_authoring_revision,
    record_workflow_candidate,
    record_workflow_evidence,
    record_workflow_review,
    rollback_workflow,
    request_workflow_render,
    verify_active_render_reservation,
    verify_creative_workflow_history,
    verify_render_reservation_history,
    workflow_render_authorization,
)


MUSIC_BOX = "\u952e\u76d8\u4e50\u5668/\u97f3\u4e50\u76d2"


def _renderable_project(tmp_path: Path):
    root = tmp_path / "Unicode 作者 工程"
    state = create_authoring_project(root, title="天籁 候选作品")
    documents = state.detached_documents()
    score = documents["score"]
    score["tail_seconds"] = 0.05
    score["tempo_map"][0]["bpm"] = 600.0
    score["parts"][0]["notes"] = [
        {
            "event_id": "event-1",
            "bar": 1,
            "beat": 1.0,
            "duration_beats": 0.1,
            "pitch": 84,
            "dynamic": "mf",
        }
    ]
    documents["authoring_roster"] = {
        "kind": "tianlai.authoring_roster",
        "schema_version": 1,
        "name": "音乐盒预览",
        "assignments": [
            {"part": "part-1", "instrument": MUSIC_BOX}
        ],
    }
    profile = documents["render_profile"]
    profile["name"] = "test-dry"
    profile["expression"] = "strict"
    profile["normalize_peak_db"] = None
    profile["space"] = {"enabled": False}
    profile["write_stems"] = False
    profile["use_stem_cache"] = False
    saved = save_authoring_project(
        root,
        expected_revision=state.revision,
        documents=documents,
    )
    return root, saved


def _candidate_directory(root: Path, result: dict[str, object]) -> Path:
    candidate = result["candidate"]
    assert isinstance(candidate, dict)
    return root / "renders" / candidate["work_id"] / candidate["candidate_id"]


def test_candidate_integrity_normalizes_authoring_semantic_type_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state = _renderable_project(tmp_path)
    rendered = render_project_candidate(
        root,
        expected_revision=state.revision,
    )
    candidate = _candidate_directory(root, rendered)

    def fail_semantic_shape(*_args: object, **_kwargs: object) -> None:
        raise TypeError("malformed authoring seat")

    monkeypatch.setattr(
        candidate_integrity_module,
        "_verify_formal_roster_plan",
        fail_semantic_shape,
    )
    with pytest.raises(CandidateIntegrityError) as caught:
        verify_candidate_integrity(candidate)

    assert caught.value.code == "identity_mismatch"


def _work_charter() -> dict[str, object]:
    return {
        "title": "Tiny managed render",
        "one_sentence_promise": "Let one small gesture remain traceable.",
        "target_listener_and_scene": "A focused listener in a quiet test room.",
        "primary_sovereignty": ["M"],
        "identity_kernel": {
            "invariants": ["the single-note gesture"],
            "transformable_parts": ["duration", "register"],
        },
        "ending_contract": "End without hiding the gesture under production.",
    }


def _record_review(root: Path, snapshot, phase: str):
    return record_workflow_review(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        phase=phase,
        reviewer="agent",
        perception_basis="report_only",
        summary=f"Reviewed {phase} for the bounded test work.",
    )


def _reserve_real_workflow(root: Path, state):
    snapshot = create_creative_workflow(
        root,
        mode="iterate",
        final_authority="agent",
        base_authoring_revision=state.revision,
    )
    snapshot = activate_creative_workflow(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        work_charter=_work_charter(),
    )
    for phase in ("intent", "symbolic_structure", "orchestration_performance"):
        snapshot = _record_review(root, snapshot, phase)
    snapshot = request_workflow_render(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
    )
    return snapshot, workflow_render_authorization(snapshot)


def _open_revision_after_managed_candidate(root: Path, state):
    pending, authorization = _reserve_real_workflow(root, state)
    rendered = render_project_candidate(
        root,
        expected_revision=state.revision,
        workflow_authorization=authorization,
    )
    candidate_path = _candidate_directory(root, rendered)
    snapshot = record_workflow_candidate(
        root,
        workflow_id=pending.workflow_id,
        expected_revision=pending.revision,
        candidate_path=candidate_path,
    )
    first_candidate = snapshot.detached_state()["iterations"][0]["anchor"][
        "candidate"
    ]
    snapshot = _record_review(root, snapshot, "render_report")
    snapshot = record_workflow_evidence(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        category="aesthetic_risk",
        code="structure.bounded_revision",
        basis_kind="diagnostic_hypothesis",
        basis_reference="rollback setup",
        reporter="agent",
        perception_basis="report_only",
        summary="Test one reversible authoring revision.",
        observation="The first managed candidate is a verified anchor.",
        interpretation="A child revision can be abandoned without erasing it.",
        confidence="medium",
    )
    evidence_id = snapshot.detached_state()["iterations"][0]["evidence"][-1][
        "evidence_id"
    ]
    snapshot = decide_workflow_iteration(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        disposition="revise",
        summary="Open one reversible child revision.",
        rationale="The rollback regression needs a later iteration.",
        final_authority="agent",
        perception_basis="report_only",
        evidence_ids=[evidence_id],
        expected_audible_change="The rendered tail becomes slightly longer.",
    )
    documents = state.detached_documents()
    documents["score"]["tail_seconds"] = 0.06
    child = save_authoring_project(
        root,
        expected_revision=state.revision,
        documents=documents,
    )
    snapshot = record_workflow_authoring_revision(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        authoring_revision=child.revision,
    )
    return snapshot, first_candidate


def test_first_cache_initialization_is_safe_under_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "concurrent-cache-project"
    create_authoring_project(root, title="Concurrent cache")
    barrier = threading.Barrier(2)
    original = authoring_render_module.ensure_authorized_child_directory

    def synchronized_first_child(parent_identity, child_name):
        if child_name == "cache":
            barrier.wait(timeout=5.0)
        return original(parent_identity, child_name)

    monkeypatch.setattr(
        authoring_render_module,
        "ensure_authorized_child_directory",
        synchronized_first_child,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(authoring_render_module._safe_cache_directories, root)
            for _ in range(2)
        ]
        results = [future.result(timeout=10.0) for future in futures]

    assert results[0] == results[1]
    stems, analysis = results[0]
    assert stems.is_dir()
    assert analysis.is_dir()
    assert not stems.is_symlink()
    assert not analysis.is_symlink()


def test_cache_initialization_rejects_symlink_or_reparse_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "linked-cache-project"
    create_authoring_project(root, title="Linked cache")
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    cache = root / ".tianlai" / "cache"
    junction = False
    try:
        os.symlink(outside, cache, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        if os.name != "nt":
            pytest.skip(f"directory links are unavailable: {exc}")
        created = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(cache), str(outside)],
            check=False,
            capture_output=True,
            text=True,
        )
        if created.returncode != 0:
            pytest.skip(f"directory junctions are unavailable: {created.stderr}")
        junction = True

    try:
        with pytest.raises(AuthoringRenderError) as captured:
            authoring_render_module._safe_cache_directories(root)

        assert captured.value.code == "project.cache_directory_unsafe"
        assert list(outside.iterdir()) == []
    finally:
        if junction and os.path.lexists(cache):
            os.rmdir(cache)


def test_cache_initialization_rejects_child_identity_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "replaced-cache-project"
    create_authoring_project(root, title="Replaced cache")
    original = authoring_render_module.ensure_authorized_child_directory
    replaced = False

    def replace_stems_after_capture(parent_identity, child_name):
        nonlocal replaced
        identity = original(parent_identity, child_name)
        if child_name == "stems" and not replaced:
            replaced = True
            parked = identity.path.with_name("parked-stems")
            os.replace(identity.path, parked)
            identity.path.mkdir()
        return identity

    monkeypatch.setattr(
        authoring_render_module,
        "ensure_authorized_child_directory",
        replace_stems_after_capture,
    )

    with pytest.raises(AuthoringRenderError) as captured:
        authoring_render_module._safe_cache_directories(root)

    assert captured.value.code == "project.cache_directory_unsafe"


def test_real_minimal_render_publishes_verified_candidate_and_playback_map(
    tmp_path: Path,
) -> None:
    root, state = _renderable_project(tmp_path)
    checkpoints: list[RenderCheckpoint] = []

    result = render_project_candidate(
        root,
        expected_revision=state.revision,
        control_callback=lambda checkpoint: checkpoints.append(checkpoint) or True,
    )

    assert result["status"] == "completed"
    assert result["project_id"] == state.project_id
    assert result["revision"] == state.revision
    assert [checkpoint.stage for checkpoint in checkpoints] == [
        "validate",
        "validate",
        "plan",
        "plan",
        "render_parts",
        "render_parts",
        "mix",
        "mix",
        "post_check",
        "post_check",
        "publish",
        "publish",
    ]
    candidate_directory = _candidate_directory(root, result)
    _directory, manifest = load_candidate(candidate_directory, verify=True)
    assert manifest["candidate_id"] == result["candidate"]["candidate_id"]
    binding = manifest["authoring_project"]
    assert binding["project_id"] == state.project_id
    assert binding["revision"] == state.revision
    authoring_roster = json.loads(
        (candidate_directory / AUTHORING_ROSTER_CANDIDATE_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert binding["authoring_roster"]["canonical_sha256"] == (
        canonical_json_sha256(authoring_roster)
    )
    receipt = json.loads(
        (candidate_directory / "渲染回执.json").read_text(encoding="utf-8")
    )
    assert receipt["authoring_project"] == {
        "project_id": state.project_id,
        "revision": state.revision,
        "authoring_roster_canonical_sha256": binding["authoring_roster"][
            "canonical_sha256"
        ],
    }
    playback = build_candidate_playback_map(candidate_directory)
    assert playback["candidate"]["candidate_id"] == manifest["candidate_id"]
    assert playback["events"][0]["source_event_id"] == "event-1"
    assert not any(
        child.name.endswith(".staging")
        for child in candidate_directory.parent.iterdir()
    )


def test_candidate_authoring_identity_chain_rejects_each_tamper(
    tmp_path: Path,
) -> None:
    root, state = _renderable_project(tmp_path)
    result = render_project_candidate(root, expected_revision=state.revision)
    directory = _candidate_directory(root, result)
    manifest_path = directory / CANDIDATE_MANIFEST_NAME
    receipt_path = directory / "渲染回执.json"
    authoring_path = directory / AUTHORING_ROSTER_CANDIDATE_NAME
    original_manifest = manifest_path.read_bytes()
    original_receipt = receipt_path.read_bytes()
    original_authoring = authoring_path.read_bytes()

    authoring_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="authoring roster file hash mismatch"):
        load_candidate(directory, verify=True)
    authoring_path.write_bytes(original_authoring)

    manifest = json.loads(original_manifest.decode("utf-8"))
    manifest["authoring_project"]["revision"] = "0" * 64
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="revision binding"):
        load_candidate(directory, verify=True)

    receipt = json.loads(original_receipt.decode("utf-8"))
    manifest = json.loads(original_manifest.decode("utf-8"))
    forged_revision = "e" * 64
    receipt["authoring_project"]["revision"] = forged_revision
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest["authoring_project"]["revision"] = forged_revision
    manifest["render_receipt"]["sha256"] = sha256_file(receipt_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="revision binding"):
        load_candidate(directory, verify=True)
    manifest_path.write_bytes(original_manifest)

    receipt = json.loads(original_receipt.decode("utf-8"))
    receipt["authoring_project"]["revision"] = "f" * 64
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = json.loads(original_manifest.decode("utf-8"))
    manifest["render_receipt"]["sha256"] = sha256_file(receipt_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="disagree on authoring identity"):
        load_candidate(directory, verify=True)


def test_real_render_writes_the_validated_unicode_stem_filename(
    tmp_path: Path,
) -> None:
    root, state = _renderable_project(tmp_path)
    documents = state.detached_documents()
    executor_id = "音乐盒・预览"
    documents["authoring_roster"]["assignments"][0][
        "executor_id"
    ] = executor_id
    documents["render_profile"]["write_stems"] = True
    saved = save_authoring_project(
        root,
        expected_revision=state.revision,
        documents=documents,
    )

    result = render_project_candidate(root, expected_revision=saved.revision)
    directory = _candidate_directory(root, result)

    assert (directory / "分轨" / f"{executor_id}.wav").is_file()
    load_candidate(directory, verify=True)


def test_cancel_during_render_parts_removes_staging_and_publishes_nothing(
    tmp_path: Path,
) -> None:
    root, state = _renderable_project(tmp_path)

    def cancel(checkpoint: RenderCheckpoint) -> bool:
        return not (
            checkpoint.stage == "render_parts"
            and checkpoint.completed_units == 0
        )

    with pytest.raises(AuthoringRenderCancelled) as captured:
        render_project_candidate(
            root,
            expected_revision=state.revision,
            control_callback=cancel,
        )
    assert captured.value.stage == "render_parts"
    work_directories = list((root / "renders").iterdir())
    assert all(
        (
            not child.is_dir()
            or (
                child.name.startswith(".")
                and ".cleanup-preserved-" in child.name
            )
        )
        for work in work_directories
        for child in work.iterdir()
        if not child.name.endswith(".lock")
    )
    assert not any(
        child.name.endswith(".staging")
        for work in work_directories
        for child in work.iterdir()
    )


def test_render_reads_requested_immutable_revision_not_new_current(
    tmp_path: Path,
) -> None:
    root, renderable = _renderable_project(tmp_path)
    newer_documents = renderable.detached_documents()
    newer_documents["authoring_roster"]["assignments"][0]["instrument"] = None
    newer = save_authoring_project(
        root,
        expected_revision=renderable.revision,
        documents=newer_documents,
    )
    assert newer.revision != renderable.revision

    result = render_project_candidate(
        root,
        expected_revision=renderable.revision,
    )

    assert result["revision"] == renderable.revision
    assert _candidate_directory(root, result).is_dir()


def test_managed_render_binds_authorization_and_retry_reuses_exact_candidate(
    tmp_path: Path,
) -> None:
    root, state = _renderable_project(tmp_path)
    pending, authorization = _reserve_real_workflow(root, state)
    if os.name == "nt":
        private_state_path = (
            root
            / ".tianlai"
            / "workflows"
            / f"workflow-{pending.workflow_id}"
            / "revisions"
            / pending.revision
            / "workflow-state.json"
        )
        assert len(str(private_state_path)) > 260

    first = render_project_candidate(
        root,
        expected_revision=state.revision,
        workflow_authorization=authorization,
    )
    retried = render_project_candidate(
        root,
        expected_revision=state.revision,
        workflow_authorization=authorization,
    )

    assert first["workflow_managed"] is True
    assert first["reused_existing"] is False
    assert retried["workflow_managed"] is True
    assert retried["reused_existing"] is True
    assert retried["candidate"] == first["candidate"]
    directory = _candidate_directory(root, first)
    _verified, manifest = load_candidate(directory, verify=True)
    receipt = json.loads(
        (directory / "渲染回执.json").read_text(encoding="utf-8")
    )
    assert manifest["authoring_workflow"] == authorization
    assert receipt["authoring_workflow"] == authorization
    assert [
        child
        for child in directory.parent.iterdir()
        if child.is_dir() and not child.name.startswith(".")
    ] == [directory]


def test_managed_render_rejects_an_alternate_output_namespace(
    tmp_path: Path,
) -> None:
    root, state = _renderable_project(tmp_path)
    _pending, authorization = _reserve_real_workflow(root, state)
    alternate = tmp_path / "alternate-managed-output"
    alternate.mkdir()

    with pytest.raises(AuthoringRenderError) as captured:
        render_project_candidate(
            root,
            expected_revision=state.revision,
            output_root=alternate,
            workflow_authorization=authorization,
        )

    assert captured.value.code == "workflow.output_root_mismatch"
    assert captured.value.stage == "validate"
    assert list(alternate.iterdir()) == []


def test_candidate_identity_race_returns_stable_workflow_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state = _renderable_project(tmp_path)
    rendered = render_project_candidate(root, expected_revision=state.revision)
    directory = _candidate_directory(root, rendered).resolve()
    created = create_creative_workflow(
        root,
        mode="audit",
        final_authority="agent",
        base_authoring_revision=state.revision,
    )
    active = activate_creative_workflow(
        root,
        workflow_id=created.workflow_id,
        expected_revision=created.revision,
        work_charter=_work_charter(),
    )
    real_revalidate = creative_workflow_module.revalidate_plain_directory
    candidate_revalidations = 0

    def replace_candidate_after_verification(identity):
        nonlocal candidate_revalidations
        if identity.path == directory:
            candidate_revalidations += 1
            if candidate_revalidations == 2:
                raise OSError(f"candidate disappeared at {directory}")
        return real_revalidate(identity)

    monkeypatch.setattr(
        creative_workflow_module,
        "revalidate_plain_directory",
        replace_candidate_after_verification,
    )
    with pytest.raises(CreativeWorkflowError) as captured:
        attach_existing_candidate_for_audit(
            root,
            workflow_id=active.workflow_id,
            expected_revision=active.revision,
            candidate_path=directory,
        )

    assert candidate_revalidations == 2
    assert captured.value.code == "candidate_changed_during_verification"
    assert str(captured.value) == "candidate_changed_during_verification"


def test_candidate_inspection_identity_race_returns_stable_workflow_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state = _renderable_project(tmp_path)
    _pending, authorization = _reserve_real_workflow(root, state)
    rendered = render_project_candidate(
        root,
        expected_revision=state.revision,
        workflow_authorization=authorization,
    )
    directory = _candidate_directory(root, rendered).resolve()
    real_revalidate = creative_workflow_module.revalidate_plain_directory
    candidate_revalidations = 0

    def replace_candidate_after_inspection(identity):
        nonlocal candidate_revalidations
        if identity.path == directory:
            candidate_revalidations += 1
            if candidate_revalidations == 4:
                raise OSError(f"candidate disappeared at {directory}")
        return real_revalidate(identity)

    monkeypatch.setattr(
        creative_workflow_module,
        "revalidate_plain_directory",
        replace_candidate_after_inspection,
    )
    with pytest.raises(CreativeWorkflowError) as captured:
        inspect_workflow_candidate_status(
            root,
            candidate_path=directory,
        )

    assert candidate_revalidations == 4
    assert captured.value.code == "candidate_changed_during_verification"
    assert str(captured.value) == "candidate_changed_during_verification"


def test_default_manual_managed_candidate_can_be_accepted_without_mix_report(
    tmp_path: Path,
) -> None:
    root, state = _renderable_project(tmp_path)
    pending, authorization = _reserve_real_workflow(root, state)
    rendered = render_project_candidate(
        root,
        expected_revision=state.revision,
        workflow_authorization=authorization,
    )
    directory = _candidate_directory(root, rendered)
    receipt = json.loads(
        (directory / "渲染回执.json").read_text(encoding="utf-8")
    )
    assert receipt.get("mix_report") is None

    snapshot = record_workflow_candidate(
        root,
        workflow_id=pending.workflow_id,
        expected_revision=pending.revision,
        candidate_path=directory,
    )
    candidate = snapshot.detached_state()["iterations"][-1]["anchor"][
        "candidate"
    ]
    assert candidate["complete_review_artifacts"] is True
    assert candidate["mix_report_sha256"] is None
    snapshot = _record_review(root, snapshot, "render_report")
    accepted = decide_workflow_iteration(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        disposition="accept",
        summary="Accept the bounded default-profile candidate.",
        rationale=(
            "The mandatory receipt, plan, mix and post-render check are "
            "verified; no optional mix report is claimed."
        ),
        final_authority="agent",
        perception_basis="report_only",
        candidate_path=directory,
    )

    final_state = accepted.detached_state()
    assert final_state["status"] == "completed"
    selected = final_state["termination"]["selected_candidate"]
    assert selected["candidate_manifest_sha256"] == candidate[
        "candidate_manifest_sha256"
    ]
    assert selected["mix_report_sha256"] is None


def test_resolved_output_failure_remains_audited_but_does_not_block_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state = _renderable_project(tmp_path)
    snapshot = create_creative_workflow(
        root,
        mode="iterate",
        final_authority="agent",
        base_authoring_revision=state.revision,
    )
    snapshot = activate_creative_workflow(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        work_charter=_work_charter(),
    )
    blocked = {
        "status": "blocked",
        "render_allowed": False,
        "issues": [{"code": "output.not_writable", "decision": "block"}],
        "issues_truncated": False,
    }
    ready = {
        "status": "ready",
        "render_allowed": True,
        "issues": [],
        "issues_truncated": False,
    }
    monkeypatch.setattr(
        creative_workflow_module, "validate_project_readiness", lambda *_a, **_k: blocked
    )
    snapshot = record_verified_workflow_hard_failure(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        issue_code="output.not_writable",
    )
    for phase in ("intent", "symbolic_structure", "orchestration_performance"):
        snapshot = _record_review(root, snapshot, phase)
    monkeypatch.setattr(
        creative_workflow_module, "validate_project_readiness", lambda *_a, **_k: ready
    )
    pending = request_workflow_render(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
    )
    authorization = workflow_render_authorization(pending)
    rendered = render_project_candidate(
        root,
        expected_revision=state.revision,
        workflow_authorization=authorization,
    )
    directory = _candidate_directory(root, rendered)
    snapshot = record_workflow_candidate(
        root,
        workflow_id=pending.workflow_id,
        expected_revision=pending.revision,
        candidate_path=directory,
    )
    snapshot = _record_review(root, snapshot, "render_report")
    accepted = decide_workflow_iteration(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        disposition="accept",
        summary="Accept after the environment recovered.",
        rationale="The historical output failure no longer reproduces.",
        final_authority="agent",
        perception_basis="report_only",
        candidate_path=directory,
    )

    accepted_state = accepted.detached_state()
    assert accepted_state["status"] == "completed"
    historical = accepted_state["iterations"][0]["evidence"][0]
    assert historical["code"] == "output.not_writable"
    assert historical["blocking"] is True


def test_candidate_pending_rollback_cancels_reservation_and_keeps_history(
    tmp_path: Path,
) -> None:
    root, state = _renderable_project(tmp_path)
    snapshot, first_candidate = _open_revision_after_managed_candidate(
        root, state
    )
    snapshot = _record_review(root, snapshot, "symbolic_structure")
    snapshot = _record_review(root, snapshot, "orchestration_performance")
    pending = request_workflow_render(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
    )
    authorization = workflow_render_authorization(pending)
    assert "rollback" in pending.to_dict()["allowed_actions"]

    rolled_back = rollback_workflow(
        root,
        workflow_id=pending.workflow_id,
        expected_revision=pending.revision,
        target_iteration_number=1,
        summary="Abandon the pending child render.",
        rationale="The earlier verified candidate remains the selected anchor.",
        final_authority="agent",
        perception_basis="report_only",
    )

    workflow_state = rolled_back.detached_state()
    abandoned = workflow_state["iterations"][1]
    attempt = abandoned["render_attempts"][0]
    assert abandoned["outcome"] == "rolled_back"
    assert attempt["status"] == "cancelled"
    assert attempt["reservation_revision"] == authorization[
        "reservation_revision"
    ]
    assert attempt["finished_at_utc"] is not None
    assert workflow_state["iterations"][2]["anchor"]["candidate"][
        "candidate_manifest_sha256"
    ] == first_candidate["candidate_manifest_sha256"]
    with pytest.raises(CreativeWorkflowError) as captured:
        verify_active_render_reservation(root, authorization)
    assert captured.value.code == "render_reservation_not_active"
    historical = verify_render_reservation_history(root, authorization)
    assert historical.revision == authorization["reservation_revision"]
    history = verify_creative_workflow_history(
        root, workflow_id=rolled_back.workflow_id
    )
    assert history["complete"] is True
    assert history["current_sequence"] == workflow_state["sequence"]


def test_revision_pending_rejects_rollback_without_overwriting_revise_decision(
    tmp_path: Path,
) -> None:
    root, state = _renderable_project(tmp_path)
    snapshot, first_candidate = _open_revision_after_managed_candidate(
        root, state
    )
    snapshot = _record_review(root, snapshot, "intent")
    snapshot = record_workflow_evidence(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        category="aesthetic_risk",
        code="structure.second_revision_not_needed",
        basis_kind="diagnostic_hypothesis",
        basis_reference="revision pending rollback",
        reporter="agent",
        perception_basis="report_only",
        summary="A second child revision may not be worth opening.",
        observation="The current child has no rendered candidate.",
        interpretation="Rollback should close the pending hypothesis cleanly.",
        confidence="medium",
    )
    evidence_id = snapshot.detached_state()["iterations"][1]["evidence"][-1][
        "evidence_id"
    ]
    revision_pending = decide_workflow_iteration(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        disposition="revise",
        summary="Propose another child revision.",
        rationale="This pending decision will be explicitly rolled back.",
        final_authority="agent",
        perception_basis="report_only",
        evidence_ids=[evidence_id],
        expected_audible_change="Test a shorter decay.",
    )
    assert first_candidate is not None
    assert "rollback" not in revision_pending.to_dict()["allowed_actions"]
    before = revision_pending.detached_state()["iterations"][1]["decision"]
    with pytest.raises(CreativeWorkflowError) as captured:
        rollback_workflow(
            root,
            workflow_id=revision_pending.workflow_id,
            expected_revision=revision_pending.revision,
            target_iteration_number=1,
            summary="This must not overwrite the revise decision.",
            rationale="Revision cancellation needs a future explicit record type.",
            final_authority="agent",
            perception_basis="report_only",
        )
    assert captured.value.code == "illegal_workflow_transition"

    unchanged = creative_workflow_module.open_creative_workflow(
        root, workflow_id=revision_pending.workflow_id
    )
    workflow_state = unchanged.detached_state()
    assert unchanged.revision == revision_pending.revision
    assert workflow_state["status"] == "revision_pending"
    assert workflow_state["iterations"][1]["decision"] == before
    assert workflow_state["iterations"][1]["decision"]["disposition"] == "revise"
    history = verify_creative_workflow_history(
        root, workflow_id=revision_pending.workflow_id
    )
    assert history["verified_revision_count"] == workflow_state["sequence"]


def test_cancel_after_render_authorization_leaves_only_an_unclaimable_orphan(
    tmp_path: Path,
) -> None:
    root, state = _renderable_project(tmp_path)
    pending, authorization = _reserve_real_workflow(root, state)
    render_started = threading.Event()
    allow_render = threading.Event()

    def pause_after_authorization(checkpoint: RenderCheckpoint) -> bool:
        if checkpoint.stage == "plan" and checkpoint.completed_units == 0:
            render_started.set()
            assert allow_render.wait(timeout=10.0)
        return True

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            render_project_candidate,
            root,
            expected_revision=state.revision,
            workflow_authorization=authorization,
            control_callback=pause_after_authorization,
        )
        assert render_started.wait(timeout=10.0)
        cancelled = cancel_workflow_render(
            root,
            workflow_id=pending.workflow_id,
            expected_revision=pending.revision,
        )
        allow_render.set()
        rendered = future.result(timeout=30.0)

    orphan = _candidate_directory(root, rendered)
    assert orphan.is_dir()
    assert cancelled.detached_state()["status"] == "reviewing"
    assert cancelled.detached_state()["iterations"][-1]["anchor"]["candidate"] is None
    with pytest.raises(CreativeWorkflowError) as captured:
        record_workflow_candidate(
            root,
            workflow_id=pending.workflow_id,
            expected_revision=pending.revision,
            candidate_path=orphan,
        )
    assert captured.value.code == "workflow_revision_conflict"


def test_concurrent_retry_of_one_reservation_renders_once_and_reuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, state = _renderable_project(tmp_path)
    _pending, authorization = _reserve_real_workflow(root, state)
    both_verified = threading.Barrier(2)
    real_verify = creative_workflow_module.verify_active_render_reservation
    real_render_plan = authoring_render_module.render_plan
    render_count = 0
    count_lock = threading.Lock()

    def synchronized_verify(project_root, workflow_authorization):
        snapshot = real_verify(project_root, workflow_authorization)
        both_verified.wait(timeout=10.0)
        return snapshot

    def counted_render(*args, **kwargs):
        nonlocal render_count
        with count_lock:
            render_count += 1
        return real_render_plan(*args, **kwargs)

    monkeypatch.setattr(
        creative_workflow_module,
        "verify_active_render_reservation",
        synchronized_verify,
    )
    monkeypatch.setattr(authoring_render_module, "render_plan", counted_render)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                render_project_candidate,
                root,
                expected_revision=state.revision,
                workflow_authorization=authorization,
            )
            for _ in range(2)
        ]
        results = [future.result(timeout=30.0) for future in futures]

    assert render_count == 1
    assert sorted(result["reused_existing"] for result in results) == [False, True]
    assert results[0]["candidate"] == results[1]["candidate"]


def test_managed_render_builds_verified_parent_chain(
    tmp_path: Path,
) -> None:
    root, state = _renderable_project(tmp_path)
    pending, first_authorization = _reserve_real_workflow(root, state)
    first = render_project_candidate(
        root,
        expected_revision=state.revision,
        workflow_authorization=first_authorization,
    )
    first_id = first["candidate"]["candidate_id"]
    first_directory = _candidate_directory(root, first)
    snapshot = record_workflow_candidate(
        root,
        workflow_id=pending.workflow_id,
        expected_revision=pending.revision,
        candidate_path=first_directory,
    )
    snapshot = _record_review(root, snapshot, "render_report")
    snapshot = record_workflow_evidence(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        category="aesthetic_risk",
        code="structure.test_variation",
        basis_kind="diagnostic_hypothesis",
        basis_reference="bounded test hypothesis",
        reporter="agent",
        perception_basis="report_only",
        summary="Test one small duration change.",
        observation="The first candidate establishes a stable parent.",
        interpretation="A second revision can verify the parent locator.",
        confidence="medium",
    )
    evidence_id = snapshot.detached_state()["iterations"][-1]["evidence"][-1][
        "evidence_id"
    ]
    snapshot = decide_workflow_iteration(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        disposition="revise",
        summary="Create one child candidate.",
        rationale="The bounded hypothesis requires exactly one new revision.",
        final_authority="agent",
        perception_basis="report_only",
        evidence_ids=[evidence_id],
        expected_audible_change="The note and tail become slightly longer.",
    )
    documents = state.detached_documents()
    documents["score"]["tail_seconds"] = 0.06
    child_state = save_authoring_project(
        root,
        expected_revision=state.revision,
        documents=documents,
    )
    snapshot = record_workflow_authoring_revision(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
        authoring_revision=child_state.revision,
    )
    for phase in ("intent", "symbolic_structure", "orchestration_performance"):
        snapshot = _record_review(root, snapshot, phase)
    pending = request_workflow_render(
        root,
        workflow_id=snapshot.workflow_id,
        expected_revision=snapshot.revision,
    )
    second_authorization = workflow_render_authorization(pending)

    second = render_project_candidate(
        root,
        expected_revision=child_state.revision,
        workflow_authorization=second_authorization,
    )

    _directory, manifest = load_candidate(
        _candidate_directory(root, second), verify=True
    )
    assert manifest["parent_candidate_id"] == first_id
    assert manifest["authoring_workflow"] == second_authorization


def test_managed_render_rejects_forged_candidate_or_foreign_parent(
    tmp_path: Path,
) -> None:
    root, state = _renderable_project(tmp_path)
    _pending, authorization = _reserve_real_workflow(root, state)
    mismatched = dict(authorization)
    mismatched["candidate_id"] = "forged-candidate"
    with pytest.raises(AuthoringRenderError) as captured:
        render_project_candidate(
            root,
            expected_revision=state.revision,
            workflow_authorization=mismatched,
        )
    assert captured.value.code == "workflow.reservation_inactive"

    foreign_parent = dict(authorization)
    foreign_parent["parent_work_id"] = authorization["candidate_work_id"]
    foreign_parent["parent_candidate_id"] = "missing-parent"
    foreign_parent["parent_manifest_sha256"] = "b" * 64
    with pytest.raises(AuthoringRenderError) as captured:
        render_project_candidate(
            root,
            expected_revision=state.revision,
            workflow_authorization=foreign_parent,
        )
    assert captured.value.code == "workflow.reservation_inactive"


def test_managed_candidate_rejects_workflow_authorization_tamper(
    tmp_path: Path,
) -> None:
    root, state = _renderable_project(tmp_path)
    _pending, authorization = _reserve_real_workflow(root, state)
    result = render_project_candidate(
        root,
        expected_revision=state.revision,
        workflow_authorization=authorization,
    )
    directory = _candidate_directory(root, result)
    manifest_path = directory / CANDIDATE_MANIFEST_NAME
    receipt_path = directory / "渲染回执.json"
    original_manifest = manifest_path.read_bytes()
    original_receipt = receipt_path.read_bytes()

    manifest = json.loads(original_manifest.decode("utf-8"))
    manifest["authoring_workflow"]["operation_id"] = "9" * 32
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="workflow authorization"):
        load_candidate(directory, verify=True)

    manifest_path.write_bytes(original_manifest)
    receipt = json.loads(original_receipt.decode("utf-8"))
    receipt["authoring_workflow"]["operation_id"] = "a" * 32
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = json.loads(original_manifest.decode("utf-8"))
    manifest["render_receipt"]["sha256"] = sha256_file(receipt_path)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="workflow authorization"):
        load_candidate(directory, verify=True)
