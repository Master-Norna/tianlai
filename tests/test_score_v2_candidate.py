from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator
import pytest

from test_score_v2_project_render import _capture, _project_files
from test_score_v2_runtime_authority import _SOURCE_MODULES, _fingerprint
from tianlai.candidate import (
    CANDIDATE_MANIFEST_NAME,
    candidate_publication,
    compare_candidates,
    load_candidate,
    locate_candidate,
    prepare_candidate_target,
)
from tianlai.candidate_integrity import (
    CandidateIntegrityError,
    verify_candidate_integrity,
)
from tianlai.onset_evidence import OnsetEvidenceError
from tianlai.score_v2_candidate import (
    SCORE_V2_CAPABILITY_PLAN_NAME,
    SCORE_V2_MIX_NAME,
    SCORE_V2_PERFORMANCE_BUNDLE_NAME,
    SCORE_V2_PLAN_NAME,
    SCORE_V2_POST_RENDER_CHECK_NAME,
    SCORE_V2_RENDER_RECEIPT_NAME,
    SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_NAME,
    SCORE_V2_RUNTIME_AUTHORITY_NAME,
    SCORE_V2_RUNTIME_MANIFEST_NAME,
    SCORE_V2_RUNTIME_SOURCE_NAME,
    SCORE_V2_ROSTER_NAME,
    ScoreV2CandidateError,
    preflight_score_v2_candidate_compilation,
    publish_score_v2_candidate_metadata,
)
import tianlai.candidate as candidate_module
import tianlai.atomic_publish as atomic_publish_module
from tianlai.canonical_json import canonical_json_bytes, canonical_json_sha256
from tianlai.score_v2_formal_render import (
    ScoreV2FormalRenderError,
    ScoreV2FormalRenderGeneration,
    render_score_v2_formal_pcm24_generation,
)
from tianlai.score_v2_project_render import (
    ScoreV2ProjectRenderCompilation,
    compile_score_v2_project_render,
)
from tianlai.score_v2_runtime_authority import (
    open_score_v2_oscillator_runtime_authority,
)
import tianlai.score_v2_runtime_source as runtime_source_module


def _read_document(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert type(value) is dict
    return value


def _write_canonical(path: Path, document: dict[str, object]) -> str:
    payload = canonical_json_bytes(document)
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _rebind(
    receipt: dict[str, object],
    role: str,
    path: Path,
) -> str:
    document = _read_document(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    bindings = receipt["bindings"]
    assert type(bindings) is dict
    bindings[role] = {
        "path": path.name,
        "canonical_sha256": canonical_json_sha256(document),
        "file_sha256": digest,
    }
    return canonical_json_sha256(document)


def _finish_reseal(
    directory: Path,
    receipt: dict[str, object],
) -> None:
    receipt_hash = _write_canonical(
        directory / SCORE_V2_RENDER_RECEIPT_NAME,
        receipt,
    )
    manifest = _read_document(directory / CANDIDATE_MANIFEST_NAME)
    binding = manifest["render_receipt"]
    assert type(binding) is dict
    binding["sha256"] = receipt_hash
    _write_canonical(directory / CANDIDATE_MANIFEST_NAME, manifest)


def _reseal_authority_tail(
    directory: Path,
    receipt: dict[str, object],
    *,
    performance_hash: str | None = None,
    runtime_hash: str | None = None,
) -> None:
    acquisition_hash = _rebind(
        receipt,
        "runtime_authority_acquisition",
        directory / SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_NAME,
    )
    authority = _read_document(directory / SCORE_V2_RUNTIME_AUTHORITY_NAME)
    authority_bindings = authority["bindings"]
    assert type(authority_bindings) is dict
    authority_bindings["acquisition_sha256"] = acquisition_hash
    if performance_hash is not None:
        authority_bindings["performance_bundle_sha256"] = performance_hash
    if runtime_hash is not None:
        authority_bindings["runtime_source_sha256"] = runtime_hash
    _write_canonical(directory / SCORE_V2_RUNTIME_AUTHORITY_NAME, authority)
    authority_hash = _rebind(
        receipt,
        "runtime_authority",
        directory / SCORE_V2_RUNTIME_AUTHORITY_NAME,
    )
    postcheck = _read_document(directory / SCORE_V2_POST_RENDER_CHECK_NAME)
    post_bindings = postcheck["bindings"]
    assert type(post_bindings) is dict
    post_bindings["runtime_authority_sha256"] = authority_hash
    if performance_hash is not None:
        post_bindings["performance_bundle_sha256"] = performance_hash
    post_hash = _write_canonical(
        directory / SCORE_V2_POST_RENDER_CHECK_NAME,
        postcheck,
    )
    post_binding = receipt["post_render_check"]
    assert type(post_binding) is dict
    post_binding["sha256"] = post_hash
    _finish_reseal(directory, receipt)
    if performance_hash is not None:
        manifest = _read_document(directory / CANDIDATE_MANIFEST_NAME)
        project = manifest["project"]
        assert type(project) is dict
        project["performance_bundle_sha256"] = performance_hash
        _write_canonical(directory / CANDIDATE_MANIFEST_NAME, manifest)


def _reseal_from_runtime_source(directory: Path) -> None:
    receipt = _read_document(directory / SCORE_V2_RENDER_RECEIPT_NAME)
    runtime_hash = _rebind(
        receipt,
        "runtime_source",
        directory / SCORE_V2_RUNTIME_SOURCE_NAME,
    )
    runtime = _read_document(directory / SCORE_V2_RUNTIME_SOURCE_NAME)
    runtime_executor = runtime["executors"][0]
    assert type(runtime_executor) is dict
    fingerprint = runtime_executor["legacy_runtime_fingerprint"]
    evidence = runtime_executor["runtime_evidence"]
    assert type(fingerprint) is dict and type(evidence) is dict

    performance = _read_document(directory / SCORE_V2_PERFORMANCE_BUNDLE_NAME)
    performance_bindings = performance["bindings"]
    performance_executor = performance["executors"][0]
    assert type(performance_bindings) is dict
    assert type(performance_executor) is dict
    performance_bindings["runtime_source_sha256"] = runtime_hash
    runtime_binding = performance_executor["runtime_binding"]
    assert type(runtime_binding) is dict
    runtime_binding["legacy_runtime_fingerprint_sha256"] = (
        runtime_executor["legacy_runtime_fingerprint_sha256"]
    )
    runtime_binding["runtime_asset_graph_sha256"] = fingerprint[
        "runtime_asset_graph"
    ]["sha256"]
    performance["executors_sha256"] = canonical_json_sha256(
        performance["executors"]
    )
    _write_canonical(directory / SCORE_V2_PERFORMANCE_BUNDLE_NAME, performance)
    performance_hash = _rebind(
        receipt,
        "performance_bundle",
        directory / SCORE_V2_PERFORMANCE_BUNDLE_NAME,
    )
    acquisition = _read_document(
        directory / SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_NAME
    )
    acquisition_bindings = acquisition["bindings"]
    assert type(acquisition_bindings) is dict
    acquisition_bindings["runtime_source_sha256"] = runtime_hash
    acquisition_bindings["performance_bundle_sha256"] = performance_hash
    _write_canonical(
        directory / SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_NAME,
        acquisition,
    )
    _reseal_authority_tail(
        directory,
        receipt,
        performance_hash=performance_hash,
        runtime_hash=runtime_hash,
    )


def _reseal_from_plan(directory: Path) -> None:
    """Re-seal the public hash spine after changing the score-v2 plan."""

    receipt = _read_document(directory / SCORE_V2_RENDER_RECEIPT_NAME)
    plan_hash = _rebind(
        receipt,
        "score_v2_plan",
        directory / SCORE_V2_PLAN_NAME,
    )
    capability = _read_document(directory / SCORE_V2_CAPABILITY_PLAN_NAME)
    capability_bindings = capability["bindings"]
    assert type(capability_bindings) is dict
    capability_bindings["score_v2_plan_sha256"] = plan_hash
    _write_canonical(directory / SCORE_V2_CAPABILITY_PLAN_NAME, capability)
    capability_hash = _rebind(
        receipt,
        "capability_plan",
        directory / SCORE_V2_CAPABILITY_PLAN_NAME,
    )

    runtime = _read_document(directory / SCORE_V2_RUNTIME_SOURCE_NAME)
    runtime_bindings = runtime["bindings"]
    assert type(runtime_bindings) is dict
    runtime_bindings["capability_plan_sha256"] = capability_hash
    _write_canonical(directory / SCORE_V2_RUNTIME_SOURCE_NAME, runtime)
    runtime_hash = _rebind(
        receipt,
        "runtime_source",
        directory / SCORE_V2_RUNTIME_SOURCE_NAME,
    )

    performance = _read_document(directory / SCORE_V2_PERFORMANCE_BUNDLE_NAME)
    performance_bindings = performance["bindings"]
    assert type(performance_bindings) is dict
    performance_bindings.update(
        {
            "score_v2_plan_sha256": plan_hash,
            "capability_plan_sha256": capability_hash,
            "runtime_source_sha256": runtime_hash,
        }
    )
    _write_canonical(directory / SCORE_V2_PERFORMANCE_BUNDLE_NAME, performance)
    performance_hash = _rebind(
        receipt,
        "performance_bundle",
        directory / SCORE_V2_PERFORMANCE_BUNDLE_NAME,
    )

    acquisition = _read_document(
        directory / SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_NAME
    )
    acquisition_bindings = acquisition["bindings"]
    assert type(acquisition_bindings) is dict
    acquisition_bindings.update(
        {
            "performance_bundle_sha256": performance_hash,
            "runtime_source_sha256": runtime_hash,
            "capability_plan_sha256": capability_hash,
        }
    )
    _write_canonical(
        directory / SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_NAME,
        acquisition,
    )
    acquisition_hash = _rebind(
        receipt,
        "runtime_authority_acquisition",
        directory / SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_NAME,
    )

    authority = _read_document(directory / SCORE_V2_RUNTIME_AUTHORITY_NAME)
    authority_bindings = authority["bindings"]
    assert type(authority_bindings) is dict
    authority_bindings.update(
        {
            "performance_bundle_sha256": performance_hash,
            "runtime_source_sha256": runtime_hash,
            "capability_plan_sha256": capability_hash,
            "acquisition_sha256": acquisition_hash,
        }
    )
    _write_canonical(directory / SCORE_V2_RUNTIME_AUTHORITY_NAME, authority)
    authority_hash = _rebind(
        receipt,
        "runtime_authority",
        directory / SCORE_V2_RUNTIME_AUTHORITY_NAME,
    )

    postcheck = _read_document(directory / SCORE_V2_POST_RENDER_CHECK_NAME)
    post_bindings = postcheck["bindings"]
    assert type(post_bindings) is dict
    post_bindings.update(
        {
            "performance_bundle_sha256": performance_hash,
            "runtime_authority_sha256": authority_hash,
        }
    )
    post_hash = _write_canonical(
        directory / SCORE_V2_POST_RENDER_CHECK_NAME,
        postcheck,
    )
    post_binding = receipt["post_render_check"]
    assert type(post_binding) is dict
    post_binding["sha256"] = post_hash
    project = _read_document(directory / CANDIDATE_MANIFEST_NAME)["project"]
    assert type(project) is dict
    # _finish_reseal opens the manifest again; update the roots afterwards.
    _finish_reseal(directory, receipt)
    manifest = _read_document(directory / CANDIDATE_MANIFEST_NAME)
    manifest_project = manifest["project"]
    assert type(manifest_project) is dict
    manifest_project["score_v2_plan_sha256"] = plan_hash
    manifest_project["performance_bundle_sha256"] = performance_hash
    _write_canonical(directory / CANDIDATE_MANIFEST_NAME, manifest)


def _install_real_runtime_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = "candidate-v3-real-runtime-fixture"

    def compute(
        project_root: Path,
        manifest_path: str,
        *,
        effective_manifest: dict[str, object],
        sample_rate_hz: int,
    ) -> dict[str, object]:
        del effective_manifest
        return _fingerprint(
            Path(project_root),
            Path(manifest_path),
            sample_rate=sample_rate_hz,
            generation=generation,
        )

    def validate(
        fingerprint: dict[str, object],
        *,
        project_root: Path | str,
        manifest_path: str,
        effective_manifest: dict[str, object],
        sample_rate_hz: int,
    ) -> dict[str, object]:
        del effective_manifest
        current = _fingerprint(
            Path(project_root),
            Path(manifest_path),
            sample_rate=sample_rate_hz,
            generation=generation,
        )
        if fingerprint != current:
            raise OnsetEvidenceError("candidate runtime fixture changed")
        return fingerprint

    monkeypatch.setattr(runtime_source_module, "compute_runtime_fingerprint", compute)
    monkeypatch.setattr(runtime_source_module, "validate_runtime_fingerprint", validate)


def _compilation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ScoreV2ProjectRenderCompilation:
    paths = _project_files(tmp_path / "project", monkeypatch)
    project = paths[0]
    import tianlai

    installed_package = Path(tianlai.__file__).resolve().parent
    fixture_package = project / "tianlai"
    for module_name in _SOURCE_MODULES:
        (fixture_package / f"{module_name}.py").write_bytes(
            (installed_package / f"{module_name}.py").read_bytes()
        )
    _install_real_runtime_fingerprint(monkeypatch)
    return compile_score_v2_project_render(_capture(paths))


def _publish(
    output_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    compilation = _compilation(tmp_path, monkeypatch)
    target = prepare_candidate_target(
        output_root,
        "Score-v2 formal candidate",
        output_id="score-v2-v3",
    )
    with candidate_publication(target) as staged:
        with open_score_v2_oscillator_runtime_authority(
            compilation.performance_bundle,
            compilation.executor_id,
        ) as authority:
            generation = render_score_v2_formal_pcm24_generation(
                compilation.performance_bundle,
                authority,
                output_directory=staged.directory,
                maximum_block_frames=997,
            )
            publish_score_v2_candidate_metadata(
                staged,
                title="Score-v2 formal candidate",
                compilation=compilation,
                generation=generation,
            )
    return target.directory


def test_candidate_v3_round_trip_and_schemas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _publish(tmp_path / "output", tmp_path, monkeypatch)

    loaded_directory, manifest = load_candidate(directory)
    report = verify_candidate_integrity(directory)
    schema_root = Path(__file__).parents[1] / "schemas"
    candidate_schema = json.loads(
        (schema_root / "candidate.schema.json").read_text(encoding="utf-8")
    )
    receipt_schema = json.loads(
        (schema_root / "score-v2-render-receipt.schema.json").read_text(
            encoding="utf-8"
        )
    )
    postcheck_schema = json.loads(
        (schema_root / "score-v2-post-render-check.schema.json").read_text(
            encoding="utf-8"
        )
    )
    receipt = json.loads(
        (directory / SCORE_V2_RENDER_RECEIPT_NAME).read_text(encoding="utf-8")
    )
    postcheck = json.loads(
        (directory / SCORE_V2_POST_RENDER_CHECK_NAME).read_text(encoding="utf-8")
    )

    assert loaded_directory == directory
    assert manifest["version"] == 3
    assert report["candidate"]["pipeline"] == "score_v2"
    assert report["integrity"]["runtime_authority_document_reusable"] is False
    Draft202012Validator(candidate_schema).validate(manifest)
    Draft202012Validator(receipt_schema).validate(receipt)
    Draft202012Validator(postcheck_schema).validate(postcheck)


def test_candidate_v3_rejects_unbound_and_tampered_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _publish(tmp_path / "output", tmp_path, monkeypatch)
    (directory / "unbound.txt").write_text("no\n", encoding="utf-8")
    with pytest.raises(CandidateIntegrityError):
        verify_candidate_integrity(directory)
    (directory / "unbound.txt").unlink()
    manifest = directory / SCORE_V2_RUNTIME_MANIFEST_NAME
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    with pytest.raises(CandidateIntegrityError):
        verify_candidate_integrity(directory)


def test_candidate_v3_rejects_consistently_resealed_protocol_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _publish(tmp_path / "output", tmp_path, monkeypatch)
    plan_path = directory / SCORE_V2_PLAN_NAME
    plan = _read_document(plan_path)
    plan["forged_unvalidated_field"] = True
    _write_canonical(plan_path, plan)
    _reseal_from_plan(directory)

    with pytest.raises(
        CandidateIntegrityError,
        match="score-v2 plan has an invalid exact shape",
    ):
        verify_candidate_integrity(directory)


def test_candidate_v3_rejects_resealed_plan_occurrence_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _publish(tmp_path / "output", tmp_path, monkeypatch)
    plan_path = directory / SCORE_V2_PLAN_NAME
    plan = _read_document(plan_path)
    occurrence = plan["occurrences"][0]
    assert type(occurrence) is dict and type(occurrence["start"]) is dict
    occurrence["start"]["resolved_sample"] += 1
    _write_canonical(plan_path, plan)
    _reseal_from_plan(directory)

    with pytest.raises(CandidateIntegrityError, match="capability occurrence"):
        verify_candidate_integrity(directory)


def test_candidate_v3_rejects_resealed_external_runtime_asset_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _publish(tmp_path / "output", tmp_path, monkeypatch)
    runtime_path = directory / SCORE_V2_RUNTIME_SOURCE_NAME
    runtime = _read_document(runtime_path)
    executor = runtime["executors"][0]
    assert type(executor) is dict
    fingerprint = executor["legacy_runtime_fingerprint"]
    evidence = executor["runtime_evidence"]
    assert type(fingerprint) is dict and type(evidence) is dict
    graph = dict(fingerprint["runtime_asset_graph"])
    graph.update(
        {
            "file_count": 1,
            "region_count": 1,
            "total_bytes": 1,
            "sha256": hashlib.sha256(b"x").hexdigest(),
        }
    )
    fingerprint["runtime_asset_graph"] = graph
    evidence["runtime_asset_graph"] = dict(graph)
    executor["legacy_runtime_fingerprint_sha256"] = canonical_json_sha256(
        fingerprint
    )
    executor["legacy_runtime_fingerprint_bytes_size"] = len(
        canonical_json_bytes(fingerprint)
    )
    _write_canonical(runtime_path, runtime)
    _reseal_from_runtime_source(directory)

    with pytest.raises(CandidateIntegrityError, match="asset-free oscillator"):
        verify_candidate_integrity(directory)


def test_candidate_v3_rejects_resealed_loaded_source_splice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _publish(tmp_path / "output", tmp_path, monkeypatch)
    acquisition_path = directory / SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_NAME
    acquisition = _read_document(acquisition_path)
    loaded = acquisition["loaded_python_generation"]
    assert type(loaded) is dict
    projection = loaded["projection"]
    assert type(projection) is dict
    root = projection["roots"][0]
    assert type(root) is dict and type(root["source"]) is dict
    root["source"]["sha256"] = "0" * 64
    loaded["projection_sha256"] = canonical_json_sha256(projection)
    _write_canonical(acquisition_path, acquisition)
    receipt = _read_document(directory / SCORE_V2_RENDER_RECEIPT_NAME)
    _reseal_authority_tail(directory, receipt)

    with pytest.raises(CandidateIntegrityError, match="loaded Python root"):
        verify_candidate_integrity(directory)


def test_candidate_v3_distinguishes_source_and_generated_canonical_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _publish(tmp_path / "output", tmp_path, monkeypatch)
    receipt = _read_document(directory / SCORE_V2_RENDER_RECEIPT_NAME)
    bindings = receipt["bindings"]
    assert type(bindings) is dict
    runtime_manifest = bindings["runtime_manifest"]
    assert type(runtime_manifest) is dict
    # The captured catalogue manifest is intentionally retained as raw source
    # bytes; its raw and canonical identities are both meaningful.
    assert runtime_manifest["file_sha256"] != runtime_manifest["canonical_sha256"]

    roster_path = directory / SCORE_V2_ROSTER_NAME
    roster = _read_document(roster_path)
    roster_path.write_text(
        json.dumps(roster, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    roster_hash = _rebind(receipt, "roster", roster_path)
    _finish_reseal(directory, receipt)
    manifest = _read_document(directory / CANDIDATE_MANIFEST_NAME)
    project = manifest["project"]
    assert type(project) is dict
    project_roster = project["roster"]
    assert type(project_roster) is dict
    receipt = _read_document(directory / SCORE_V2_RENDER_RECEIPT_NAME)
    receipt_bindings = receipt["bindings"]
    assert type(receipt_bindings) is dict
    project["roster"] = dict(receipt_bindings["roster"])
    _write_canonical(directory / CANDIDATE_MANIFEST_NAME, manifest)
    assert roster_hash == project["roster"]["canonical_sha256"]

    with pytest.raises(CandidateIntegrityError, match="roster projection"):
        verify_candidate_integrity(directory)


@pytest.mark.parametrize("mutation", ["riff", "size"])
def test_candidate_v3_rejects_resealed_invalid_wav_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    directory = _publish(tmp_path / "output", tmp_path, monkeypatch)
    mix_path = directory / "合奏.wav"
    payload = bytearray(mix_path.read_bytes())
    if mutation == "riff":
        payload[:4] = b"NOPE"
    else:
        payload.extend(b"\0" * 6)
    mix_path.write_bytes(payload)

    receipt = _read_document(directory / SCORE_V2_RENDER_RECEIPT_NAME)
    mix = receipt["mix"]
    assert type(mix) is dict
    mix_hash = hashlib.sha256(payload).hexdigest()
    mix["sha256"] = mix_hash
    mix["size_bytes"] = len(payload)
    postcheck = _read_document(directory / SCORE_V2_POST_RENDER_CHECK_NAME)
    artifact = postcheck["artifact"]
    assert type(artifact) is dict
    artifact.update({"sha256": mix_hash, "size_bytes": len(payload)})
    post_hash = _write_canonical(
        directory / SCORE_V2_POST_RENDER_CHECK_NAME,
        postcheck,
    )
    post_binding = receipt["post_render_check"]
    assert type(post_binding) is dict
    post_binding["sha256"] = post_hash
    _finish_reseal(directory, receipt)

    with pytest.raises(CandidateIntegrityError, match="PCM24 WAV"):
        verify_candidate_integrity(directory)


def test_candidate_v3_compilation_preflight_rejects_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = _compilation(tmp_path, monkeypatch)
    monkeypatch.setattr(candidate_module, "MAX_CANDIDATE_JSON_BYTES", 1)
    with pytest.raises(ScoreV2CandidateError) as caught:
        preflight_score_v2_candidate_compilation(compilation)
    assert caught.value.code == "resource_limit"
    assert not (tmp_path / "output").exists()


def test_candidate_v3_legacy_navigation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _publish(tmp_path / "output", tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Score-v2 Candidate v3"):
        locate_candidate(directory, at_seconds=0.0)
    with pytest.raises(ValueError, match="Score-v2 Candidate v3"):
        compare_candidates(directory, directory)


def test_candidate_v3_publisher_rejects_forged_or_inactive_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = _compilation(tmp_path, monkeypatch)
    target = prepare_candidate_target(
        tmp_path / "output",
        "Score-v2 formal candidate",
        output_id="score-v2-v3",
    )
    with pytest.raises(RuntimeError, match="abort incomplete"):
        with candidate_publication(target) as staged:
            forged = object.__new__(ScoreV2FormalRenderGeneration)
            with pytest.raises(ScoreV2FormalRenderError):
                publish_score_v2_candidate_metadata(
                    staged,
                    title="forged",
                    compilation=compilation,
                    generation=forged,
                )

            with open_score_v2_oscillator_runtime_authority(
                compilation.performance_bundle,
                compilation.executor_id,
            ) as authority:
                generation = render_score_v2_formal_pcm24_generation(
                    compilation.performance_bundle,
                    authority,
                    output_directory=staged.directory,
                )
            with pytest.raises(ScoreV2FormalRenderError) as caught:
                publish_score_v2_candidate_metadata(
                    staged,
                    title="inactive",
                    compilation=compilation,
                    generation=generation,
                )
            assert caught.value.code == "render.runtime_authority_inactive"
            raise RuntimeError("abort incomplete candidate stage")


def test_candidate_v3_publisher_failure_never_installs_manifest_or_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = _compilation(tmp_path, monkeypatch)
    target = prepare_candidate_target(
        tmp_path / "output",
        "Score-v2 formal candidate",
        output_id="publisher-failure",
    )
    real_publish = atomic_publish_module._publish_bytes_atomic
    observed: list[str] = []

    def fail_fourth(
        path: str | os.PathLike[str],
        payload: bytes,
        *,
        overwrite: bool,
    ) -> None:
        destination = Path(path)
        assert not (destination.parent / CANDIDATE_MANIFEST_NAME).exists()
        observed.append(destination.name)
        if len(observed) == 4:
            raise OSError("injected Candidate-v3 publication failure")
        real_publish(destination, payload, overwrite=overwrite)

    with pytest.raises(OSError, match="injected Candidate-v3"):
        with candidate_publication(target) as staged:
            with open_score_v2_oscillator_runtime_authority(
                compilation.performance_bundle,
                compilation.executor_id,
            ) as authority:
                generation = render_score_v2_formal_pcm24_generation(
                    compilation.performance_bundle,
                    authority,
                    output_directory=staged.directory,
                )
                with mock.patch.object(
                    atomic_publish_module,
                    "_publish_bytes_atomic",
                    side_effect=fail_fourth,
                ):
                    publish_score_v2_candidate_metadata(
                        staged,
                        title="must not publish",
                        compilation=compilation,
                        generation=generation,
                    )

    assert len(observed) == 4
    assert CANDIDATE_MANIFEST_NAME not in observed
    assert not target.directory.exists()
    assert not list(target.directory.parent.glob("*.staging"))
    assert not list(target.directory.parent.glob("*.previous"))


def test_candidate_v3_publisher_rejects_same_bytes_wav_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = _compilation(tmp_path, monkeypatch)
    target = prepare_candidate_target(
        tmp_path / "output",
        "Score-v2 formal candidate",
        output_id="wav-replacement",
    )
    with pytest.raises(ScoreV2FormalRenderError) as caught:
        with candidate_publication(target) as staged:
            with open_score_v2_oscillator_runtime_authority(
                compilation.performance_bundle,
                compilation.executor_id,
            ) as authority:
                generation = render_score_v2_formal_pcm24_generation(
                    compilation.performance_bundle,
                    authority,
                    output_directory=staged.directory,
                )
                mix = staged.directory / SCORE_V2_MIX_NAME
                replacement = staged.directory / "same-bytes-replacement.wav"
                replacement.write_bytes(mix.read_bytes())
                os.replace(replacement, mix)
                publish_score_v2_candidate_metadata(
                    staged,
                    title="must reject replacement",
                    compilation=compilation,
                    generation=generation,
                )
    assert caught.value.code == "render.mix_generation_changed"
    assert not target.directory.exists()
