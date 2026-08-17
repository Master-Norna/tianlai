from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import wave

import pytest

from tianlai.score_v2_candidate import SCORE_V2_MIX_NAME
from tianlai.score_v2_formal_render import (
    SCORE_V2_FORMAL_RENDER_CONTRACT,
    ScoreV2FormalRenderError,
    ScoreV2FormalRenderGeneration,
    render_score_v2_formal_pcm24_generation,
)
import tianlai.score_v2_formal_render as formal_module
from tianlai.resource_limits import ProjectLimits, ResourceLimitError
from tianlai.score_v2_runtime_authority import (
    ScoreV2OscillatorRuntimeAuthority,
    open_score_v2_oscillator_runtime_authority,
)


def _load_authority_fixture_module():
    path = Path(__file__).with_name("test_score_v2_runtime_authority.py")
    name = "_tianlai_score_v2_formal_authority_fixture"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Score-v2 runtime-authority fixtures")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_AUTHORITY_FIXTURE = _load_authority_fixture_module()


def _bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    notes=None,
):
    if notes is None:
        return _AUTHORITY_FIXTURE._bundle(tmp_path, monkeypatch)
    _AUTHORITY_FIXTURE._copy_runtime_sources(tmp_path)
    monkeypatch.setattr(
        _AUTHORITY_FIXTURE._RENDERER_FIXTURE,
        "_fingerprint",
        _AUTHORITY_FIXTURE._fingerprint,
    )
    return _AUTHORITY_FIXTURE._RENDERER_FIXTURE._bundle(
        tmp_path,
        monkeypatch,
        notes=notes,
    )


def test_formal_pcm24_generation_is_sealed_bound_and_self_consistent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(
        tmp_path,
        monkeypatch,
        notes=[
            _AUTHORITY_FIXTURE._RENDERER_FIXTURE._note("whole", 0, 4)
        ],
    )
    staging = tmp_path / "candidate-stage"
    staging.mkdir()

    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        result = render_score_v2_formal_pcm24_generation(
            bundle,
            authority,
            output_directory=staging,
            maximum_block_frames=997,
        )
        result.revalidate_generation()

    assert type(result) is ScoreV2FormalRenderGeneration
    assert result.contract == SCORE_V2_FORMAL_RENDER_CONTRACT
    assert Path(result.mix_path) == (staging / SCORE_V2_MIX_NAME).resolve()
    assert result.mix_size_bytes == 44 + 6 * result.frame_count
    assert Path(result.mix_path).stat().st_size == result.mix_size_bytes
    assert hashlib.sha256(Path(result.mix_path).read_bytes()).hexdigest() == (
        result.mix_sha256
    )
    with wave.open(result.mix_path, "rb") as source:
        assert source.getnchannels() == 2
        assert source.getsampwidth() == 3
        assert source.getframerate() == result.sample_rate
        assert source.getnframes() == result.frame_count
    assert result.event_count == bundle.event_count
    assert result.endpoint_event_count == 1
    assert result.block_count > 1
    assert result.peak > 0.0
    assert result.active_sample_count > 0
    assert result.runtime_authority()["lifecycle"][
        "execution_retired_before_receipt"
    ] is True
    assert result.post_render_check()["summary"]["can_proceed"] is True
    with pytest.raises(ScoreV2FormalRenderError) as inactive:
        result.revalidate_generation()
    assert inactive.value.code == "render.runtime_authority_inactive"

    with pytest.raises(TypeError):
        ScoreV2FormalRenderGeneration()
    forged = object.__new__(ScoreV2FormalRenderGeneration)
    with pytest.raises(ScoreV2FormalRenderError) as caught:
        forged.revalidate_generation()
    assert caught.value.code == "render.evidence_integrity_mismatch"
    with pytest.raises(AttributeError):
        object.__setattr__(result, "mix_sha256", "0" * 64)


def test_formal_renderer_does_not_replace_an_existing_mix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "candidate-stage"
    staging.mkdir()
    mix = staging / SCORE_V2_MIX_NAME
    sentinel = b"existing candidate mix"
    mix.write_bytes(sentinel)

    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        with pytest.raises(ScoreV2FormalRenderError) as caught:
            render_score_v2_formal_pcm24_generation(
                bundle,
                authority,
                output_directory=staging,
            )

    assert caught.value.code == "render.failed"
    assert mix.read_bytes() == sentinel
    assert list(staging.iterdir()) == [mix]


def test_failed_post_check_never_installs_the_fixed_mix_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "candidate-stage"
    staging.mkdir()
    real_post_check = formal_module._post_render_check_document

    def reject_post_check(**kwargs):
        document = real_post_check(**kwargs)
        document["status"] = "fail"
        document["summary"]["can_proceed"] = False
        return document

    monkeypatch.setattr(
        formal_module,
        "_post_render_check_document",
        reject_post_check,
    )
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        with pytest.raises(ScoreV2FormalRenderError) as caught:
            render_score_v2_formal_pcm24_generation(
                bundle,
                authority,
                output_directory=staging,
            )

    assert caught.value.code == "render.post_check_failed"
    assert not (staging / SCORE_V2_MIX_NAME).exists()
    assert list(staging.iterdir()) == []


def test_generation_detects_path_replacement_even_with_identical_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "candidate-stage"
    staging.mkdir()
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        result = render_score_v2_formal_pcm24_generation(
            bundle,
            authority,
            output_directory=staging,
        )

    path = Path(result.mix_path)
    payload = path.read_bytes()
    replacement = staging / "replacement.wav"
    replacement.write_bytes(payload)
    os.replace(replacement, path)

    with pytest.raises(ScoreV2FormalRenderError) as caught:
        result.revalidate_mix()
    assert caught.value.code == "render.mix_generation_changed"


def test_registered_evidence_bytes_cannot_be_consistently_resealed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "candidate-stage"
    staging.mkdir()
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        result = render_score_v2_formal_pcm24_generation(
            bundle,
            authority,
            output_directory=staging,
        )

    document = json.loads(result.runtime_authority_canonical_bytes)
    document["bindings"]["effective_manifest_sha256"] = "0" * 64
    forged = json.dumps(document, sort_keys=True).encode("utf-8")
    with pytest.raises(AttributeError):
        object.__setattr__(
            result,
            "runtime_authority_canonical_bytes",
            forged,
        )
    assert result.runtime_authority()["bindings"][
        "effective_manifest_sha256"
    ] == result.effective_manifest_sha256


def test_hash_consistent_cross_generation_evidence_splice_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "candidate-stage"
    staging.mkdir()
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        result = render_score_v2_formal_pcm24_generation(
            bundle,
            authority,
            output_directory=staging,
        )
        registered = formal_module._FORMAL_GENERATIONS[id(result)]
        original = registered[1]
        spliced = replace(
            original,
            runtime_authority_canonical_bytes=(
                original.runtime_authority_acquisition_canonical_bytes
            ),
            runtime_authority_sha256=(
                original.runtime_authority_acquisition_sha256
            ),
        )
        formal_module._FORMAL_GENERATIONS[id(result)] = (
            registered[0],
            spliced,
        )
        try:
            with pytest.raises(ScoreV2FormalRenderError) as caught:
                result.runtime_authority()
            assert caught.value.code == "render.evidence_integrity_mismatch"
        finally:
            formal_module._FORMAL_GENERATIONS[id(result)] = registered


def test_output_budget_fails_before_transport_decode_or_file_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "candidate-stage"
    staging.mkdir()

    def should_not_decode(*_args, **_kwargs):
        raise AssertionError("transport decode crossed the resource gate")

    monkeypatch.setattr(
        formal_module,
        "_decoded_execution_documents",
        should_not_decode,
    )
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        with pytest.raises(ResourceLimitError) as caught:
            render_score_v2_formal_pcm24_generation(
                bundle,
                authority,
                output_directory=staging,
                limits=ProjectLimits(max_primary_output_bytes=43),
            )

    assert caught.value.code == "render.output_budget_exceeded"
    assert list(staging.iterdir()) == []


def test_failure_after_fixed_name_install_retires_the_formal_wav(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "candidate-stage"
    staging.mkdir()

    def fail_registration(*_args, **_kwargs):
        raise RuntimeError("injected post-install registration failure")

    monkeypatch.setattr(
        formal_module,
        "_register_formal_generation",
        fail_registration,
    )
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        with pytest.raises(ScoreV2FormalRenderError) as caught:
            render_score_v2_formal_pcm24_generation(
                bundle,
                authority,
                output_directory=staging,
            )

    assert caught.value.code == "render.failed"
    assert not (staging / SCORE_V2_MIX_NAME).exists()
    assert list(staging.iterdir()) == []


def test_mutable_audio_block_is_rejected_before_pcm_encoding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "candidate-stage"
    staging.mkdir()
    original = ScoreV2OscillatorRuntimeAuthority.render_block

    def mutable_block(self, frame_count):
        return original(self, frame_count).copy()

    monkeypatch.setattr(
        ScoreV2OscillatorRuntimeAuthority,
        "render_block",
        mutable_block,
    )
    with open_score_v2_oscillator_runtime_authority(
        bundle,
        executor_id,
    ) as authority:
        with pytest.raises(ScoreV2FormalRenderError) as caught:
            render_score_v2_formal_pcm24_generation(
                bundle,
                authority,
                output_directory=staging,
            )

    assert caught.value.code == "render.audio_block_invalid"
    assert list(staging.iterdir()) == []
