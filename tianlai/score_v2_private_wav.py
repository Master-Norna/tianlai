"""Identity-bound private PCM24 staging for the narrow Score-v2 renderer.

The context manager in this module never installs a public pathname and never
creates candidate or render authority.  It exists so a caller can inspect or
play one fully written WAV while its random private inode remains bound to the
same local-execution evidence.  Leaving the context retires that inode.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import wave
import weakref
from typing import Any, Iterator

from .atomic_publish import (
    _PrivateFileClaim,
    _SealedPrivateFileClaim,
    _open_private_file_claim,
    _reserve_private_file,
    _retire_private_file,
    _retire_sealed_private_file,
    _seal_private_file_claim,
    _same_file,
)
from .audio import _SequentialDigestWriter, _write_numpy_pcm24
from .canonical_json import canonical_json_bytes
from .plain_file import sha256_plain_file
from .resource_limits import ProjectLimits, ResourceLimitError
from .score_v2_performance import ScoreV2PerformanceBundle
from .score_v2_renderer import (
    ScoreV2LocalExecutionReceipt,
    ScoreV2RendererError,
    _new_audio_stream_hasher,
    render_score_v2_executor_to_private_block_sink,
)


SCORE_V2_PRIVATE_WAV_STAGE_KIND = "tianlai.score_v2_private_wav_stage"
SCORE_V2_PRIVATE_WAV_STAGE_SCHEMA_VERSION = 1
SCORE_V2_PRIVATE_WAV_STAGE_CONTRACT = (
    "score-v2-private-wav-stage-v1-not-render-or-publish-authority"
)
PRIVATE_WAV_STAGE_STATUS = "ready_private_identity_bound_pcm24_wav"

_PCM24_WAV_HEADER_BYTES = 44
_PCM24_STEREO_BYTES_PER_FRAME = 6
_STREAMING_WORKING_BYTES_PER_FRAME = 64
_RIFF_MAX_CHUNK_SIZE = (1 << 32) - 1
_MAXIMUM_BLOCK_FRAMES = 65_536
_HEX = frozenset("0123456789abcdef")
_LIMITATIONS = {
    "render_authority": "not_granted",
    "publish_authority": "not_granted",
    "candidate_authority": "not_granted",
    "public_path_installation": "not_performed",
    "runtime_generation_atomicity": "not_atomic",
    "runtime_generation_aba_resistance": "not_claimed",
    "loaded_python_object_generation": "not_bound_to_disk_closure",
    "lazy_asset_descriptor_generation": "not_available",
    "release_tail": "transport_frame_count_only_no_implicit_tail",
    "memory_budget": "bounded_streaming_blocks_not_process_rss",
    "pathname_retirement": (
        "missing_or_relocated_claimed_path_is_cleanup_incomplete"
    ),
}


class ScoreV2PrivateWavError(ValueError):
    """Stable failure at the private Score-v2 WAV staging boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.message_key = f"scoreV2PrivateWav.{code.replace('.', '_')}"
        super().__init__(code)


@dataclass(slots=True)
class _StageLifecycle:
    active: bool = True


@dataclass(frozen=True, slots=True)
class _StageGeneration:
    path: Path
    sealed: _SealedPrivateFileClaim
    canonical_bytes: bytes
    artifact_sha256: str
    local_receipt: ScoreV2LocalExecutionReceipt
    lifecycle: _StageLifecycle


_STAGE_GENERATIONS: dict[
    int,
    tuple[weakref.ReferenceType[object], _StageGeneration],
] = {}


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _add_note_safely(error: BaseException, note: str) -> None:
    try:
        error.add_note(note)
    except BaseException:
        pass


def _stage_document(
    *,
    bundle_sha256: str,
    runtime_source_sha256: str,
    local_receipt_sha256: str,
    float_stream_sha256: str,
    wav_sha256: str,
    wav_size_bytes: int,
    sample_rate: int,
    frame_count: int,
) -> dict[str, object]:
    return {
        "kind": SCORE_V2_PRIVATE_WAV_STAGE_KIND,
        "schema_version": SCORE_V2_PRIVATE_WAV_STAGE_SCHEMA_VERSION,
        "contract": SCORE_V2_PRIVATE_WAV_STAGE_CONTRACT,
        "status": PRIVATE_WAV_STAGE_STATUS,
        "render_authority": False,
        "publish_authority": False,
        "candidate_authority": False,
        "bindings": {
            "performance_bundle_sha256": bundle_sha256,
            "runtime_source_sha256": runtime_source_sha256,
            "local_execution_receipt_sha256": local_receipt_sha256,
        },
        "float_stream_sha256": float_stream_sha256,
        "wav": {
            "encoding": "pcm_s24le",
            "channels": 2,
            "sample_rate": sample_rate,
            "frame_count": frame_count,
            "size_bytes": wav_size_bytes,
            "sha256": wav_sha256,
        },
        "limitations": dict(_LIMITATIONS),
    }


@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class ScoreV2PrivateWavStage:
    """Short-lived handle for one sealed private WAV generation."""

    path: Path
    wav_sha256: str
    wav_size_bytes: int
    sample_rate: int
    frame_count: int
    local_execution_receipt: ScoreV2LocalExecutionReceipt
    _canonical_bytes: bytes = field(repr=False, compare=False)
    _artifact_sha256: str = field(repr=False, compare=False)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ScoreV2PrivateWavStage cannot be subclassed")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "ScoreV2PrivateWavStage must be created by its staging context"
        )

    def _trusted_generation(self) -> _StageGeneration:
        try:
            registered = _STAGE_GENERATIONS.get(id(self))
            if registered is None or registered[0]() is not self:
                raise ValueError
            generation = registered[1]
            payload = generation.canonical_bytes
            digest = generation.artifact_sha256
            document = json.loads(payload)
            wav = document.get("wav") if type(document) is dict else None
            bindings = (
                document.get("bindings") if type(document) is dict else None
            )
            if (
                self.path != generation.path
                or self.wav_sha256 != generation.sealed.sha256
                or self.wav_size_bytes != generation.sealed.identity.size
                or self.sample_rate != document.get("wav", {}).get("sample_rate")
                or self.frame_count != document.get("wav", {}).get("frame_count")
                or self.local_execution_receipt is not generation.local_receipt
                or self._canonical_bytes is not payload
                or self._artifact_sha256 != digest
                or type(payload) is not bytes
                or not _is_sha256(digest)
                or hashlib.sha256(payload).hexdigest() != digest
                or type(document) is not dict
                or set(document)
                != {
                    "kind",
                    "schema_version",
                    "contract",
                    "status",
                    "render_authority",
                    "publish_authority",
                    "candidate_authority",
                    "bindings",
                    "float_stream_sha256",
                    "wav",
                    "limitations",
                }
                or document.get("kind") != SCORE_V2_PRIVATE_WAV_STAGE_KIND
                or document.get("schema_version")
                != SCORE_V2_PRIVATE_WAV_STAGE_SCHEMA_VERSION
                or document.get("contract")
                != SCORE_V2_PRIVATE_WAV_STAGE_CONTRACT
                or document.get("status") != PRIVATE_WAV_STAGE_STATUS
                or document.get("render_authority") is not False
                or document.get("publish_authority") is not False
                or document.get("candidate_authority") is not False
                or type(bindings) is not dict
                or set(bindings)
                != {
                    "performance_bundle_sha256",
                    "runtime_source_sha256",
                    "local_execution_receipt_sha256",
                }
                or any(not _is_sha256(value) for value in bindings.values())
                or bindings.get("local_execution_receipt_sha256")
                != generation.local_receipt.artifact_sha256
                or type(wav) is not dict
                or set(wav)
                != {
                    "encoding",
                    "channels",
                    "sample_rate",
                    "frame_count",
                    "size_bytes",
                    "sha256",
                }
                or wav.get("encoding") != "pcm_s24le"
                or wav.get("channels") != 2
                or wav.get("sample_rate") != self.sample_rate
                or wav.get("frame_count") != self.frame_count
                or wav.get("size_bytes") != self.wav_size_bytes
                or wav.get("sha256") != self.wav_sha256
                or not _is_sha256(document.get("float_stream_sha256"))
                or document.get("limitations") != _LIMITATIONS
                or canonical_json_bytes(document) != payload
            ):
                raise ValueError
            return generation
        except (
            AttributeError,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise ScoreV2PrivateWavError(
                "stage.evidence_integrity_mismatch"
            ) from exc

    @property
    def canonical_bytes(self) -> bytes:
        return self._trusted_generation().canonical_bytes

    @property
    def artifact_sha256(self) -> str:
        return self._trusted_generation().artifact_sha256

    @property
    def active(self) -> bool:
        return self._trusted_generation().lifecycle.active

    def to_dict(self) -> dict[str, object]:
        document = json.loads(self._trusted_generation().canonical_bytes)
        if type(document) is not dict:
            raise ScoreV2PrivateWavError(
                "stage.evidence_integrity_mismatch"
            )
        return document

    def revalidate_private_wav(self) -> None:
        generation = self._trusted_generation()
        if not generation.lifecycle.active:
            raise ScoreV2PrivateWavError("stage.retired")
        try:
            identity, digest = sha256_plain_file(generation.path)
            if not _same_file(
                generation.sealed.identity,
                identity,
                left_sha256=generation.sealed.sha256,
                right_sha256=digest,
            ):
                raise ValueError
        except (OSError, TypeError, ValueError) as exc:
            raise ScoreV2PrivateWavError(
                "stage.private_wav_generation_changed"
            ) from exc


def _register_stage(
    stage: ScoreV2PrivateWavStage,
    generation: _StageGeneration,
) -> None:
    stage_id = id(stage)

    def retire_registration(
        reference: weakref.ReferenceType[object],
        *,
        expected_id: int = stage_id,
    ) -> None:
        current = _STAGE_GENERATIONS.get(expected_id)
        if current is not None and current[0] is reference:
            _STAGE_GENERATIONS.pop(expected_id, None)

    reference = weakref.ref(stage, retire_registration)
    _STAGE_GENERATIONS[stage_id] = (reference, generation)


def _retire_claim(
    claim: _PrivateFileClaim,
    *,
    primary: BaseException | None,
) -> None:
    try:
        retained = _retire_private_file(claim, require_present=True)
        if retained is not None:
            raise OSError(f"private stage cleanup retained {retained}")
    except BaseException as cleanup_error:
        if primary is not None:
            _add_note_safely(
                primary,
                "private WAV stage cleanup was not completed: "
                f"{cleanup_error}",
            )
            return
        if not isinstance(cleanup_error, Exception):
            raise
        raise ScoreV2PrivateWavError("stage.cleanup_failed") from cleanup_error


def _retire_sealed_stage(
    sealed: _SealedPrivateFileClaim,
    *,
    primary: BaseException | None,
) -> None:
    try:
        _retire_sealed_private_file(sealed, require_present=True)
    except BaseException as cleanup_error:
        if primary is not None:
            _add_note_safely(
                primary,
                "private WAV stage cleanup was not completed: "
                f"{cleanup_error}",
            )
            return
        if not isinstance(cleanup_error, Exception):
            raise
        raise ScoreV2PrivateWavError("stage.cleanup_failed") from cleanup_error


def _bundle_generation(
    bundle: ScoreV2PerformanceBundle,
) -> tuple[bytes, str, str, int, int, int]:
    try:
        values = (
            bundle.canonical_bytes,
            bundle.artifact_sha256,
            bundle.runtime_source_sha256,
            bundle.sample_rate,
            bundle.frame_count,
            bundle.executor_count,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2PrivateWavError("stage.bundle_invalid") from exc
    if (
        type(values[0]) is not bytes
        or not _is_sha256(values[1])
        or hashlib.sha256(values[0]).hexdigest() != values[1]
        or not _is_sha256(values[2])
        or type(values[3]) is not int
        or not 8_000 <= values[3] <= 384_000
        or type(values[4]) is not int
        or values[4] < 1
        or type(values[5]) is not int
        or values[5] != 1
    ):
        raise ScoreV2PrivateWavError("stage.bundle_invalid")
    return values


def _require_bundle_generation(
    bundle: ScoreV2PerformanceBundle,
    expected: tuple[bytes, str, str, int, int, int],
) -> None:
    try:
        bundle.revalidate_runtime_sources()
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ScoreV2PrivateWavError(
            "stage.runtime_generation_changed"
        ) from exc
    if _bundle_generation(bundle) != expected:
        raise ScoreV2PrivateWavError("stage.bundle_generation_changed")


def _prepare_private_stage(
    bundle: ScoreV2PerformanceBundle,
    executor_id: str,
    *,
    staging_directory: Path,
    expected: tuple[bytes, str, str, int, int, int],
    maximum_block_frames: int,
) -> tuple[ScoreV2PrivateWavStage, _SealedPrivateFileClaim]:
    bundle_bytes, bundle_hash, runtime_hash, sample_rate, frame_count, _ = expected
    del bundle_bytes
    claim = _reserve_private_file(
        staging_directory,
        prefix=".tianlai-score-v2-private-",
        suffix=".pcm24.wav",
    )
    active_claim = claim
    active_sealed: _SealedPrivateFileClaim | None = None
    try:
        written_frames = 0
        float_hash = _new_audio_stream_hasher(
            sample_rate=sample_rate,
            frame_count=frame_count,
        )
        receipt: ScoreV2LocalExecutionReceipt | None = None
        with _open_private_file_claim(claim, truncate=True) as raw:
            tracked = _SequentialDigestWriter(raw)
            output = wave.open(tracked, "wb")
            primary: BaseException | None = None
            try:
                output.setnchannels(2)
                output.setsampwidth(3)
                output.setframerate(sample_rate)
                output.setnframes(frame_count)

                def sink(block: Any, offset: int) -> None:
                    nonlocal written_frames
                    import numpy as np

                    block_frames = (
                        int(block.shape[0])
                        if type(block) is np.ndarray and block.ndim == 2
                        else -1
                    )
                    if (
                        type(offset) is not int
                        or offset != written_frames
                        or type(block) is not np.ndarray
                        or block.shape != (block_frames, 2)
                        or block_frames < 1
                        or block_frames > maximum_block_frames
                        or written_frames + block_frames > frame_count
                        or block.dtype != np.dtype("<f8")
                        or not block.flags.c_contiguous
                        or not bool(np.isfinite(block).all())
                        or float(np.max(np.abs(block), initial=0.0)) > 1.0
                    ):
                        raise ScoreV2PrivateWavError(
                            "stage.audio_block_invalid"
                        )
                    float_hash.update(block.tobytes(order="C"))
                    try:
                        written = _write_numpy_pcm24(
                            output,
                            block,
                            reject_out_of_range=True,
                            frame_offset=written_frames,
                        )
                    except (OverflowError, TypeError, ValueError) as exc:
                        raise ScoreV2PrivateWavError(
                            "stage.pcm24_encode_failed"
                        ) from exc
                    if written is None or written != block_frames:
                        raise ScoreV2PrivateWavError(
                            "stage.pcm24_encode_failed"
                        )
                    written_frames += written

                receipt = render_score_v2_executor_to_private_block_sink(
                    bundle,
                    executor_id,
                    sink,
                    maximum_block_frames=maximum_block_frames,
                )
                if written_frames != frame_count:
                    raise ScoreV2PrivateWavError(
                        "stage.frame_count_mismatch"
                    )
            except BaseException as exc:
                primary = exc
                raise
            finally:
                try:
                    output.close()
                except BaseException as close_error:
                    if primary is not None:
                        _add_note_safely(
                            primary,
                            "private WAV writer close also failed"
                        )
                    else:
                        if not isinstance(close_error, Exception):
                            raise
                        raise ScoreV2PrivateWavError(
                            "stage.wav_close_failed"
                        ) from close_error
            expected_size = (
                _PCM24_WAV_HEADER_BYTES
                + _PCM24_STEREO_BYTES_PER_FRAME * frame_count
            )
            if (
                not tracked.sequential
                or tracked.byte_count != expected_size
                or raw.tell() != expected_size
            ):
                raise ScoreV2PrivateWavError(
                    "stage.wav_stream_mismatch"
                )
            wav_hash = tracked.digest.hexdigest()
        sealed = _seal_private_file_claim(
            claim,
            expected_sha256=wav_hash,
        )
        active_sealed = sealed
        active_claim = sealed.claim
        _require_bundle_generation(bundle, expected)
        if receipt is None:
            raise ScoreV2PrivateWavError("stage.local_receipt_missing")
        receipt_document = receipt.to_dict()
        receipt_bindings = receipt_document.get("bindings")
        if (
            receipt.sample_rate != sample_rate
            or receipt.frame_count != frame_count
            or receipt_document.get("audio_stream_sha256")
            != float_hash.hexdigest()
            or type(receipt_bindings) is not dict
            or receipt_bindings.get("performance_bundle_sha256")
            != bundle_hash
            or receipt_bindings.get("runtime_source_sha256") != runtime_hash
            or sealed.identity.size != expected_size
        ):
            raise ScoreV2PrivateWavError(
                "stage.execution_evidence_mismatch"
            )
        document = _stage_document(
            bundle_sha256=bundle_hash,
            runtime_source_sha256=runtime_hash,
            local_receipt_sha256=receipt.artifact_sha256,
            float_stream_sha256=float_hash.hexdigest(),
            wav_sha256=wav_hash,
            wav_size_bytes=expected_size,
            sample_rate=sample_rate,
            frame_count=frame_count,
        )
        payload = canonical_json_bytes(document)
        digest = hashlib.sha256(payload).hexdigest()
        stage = object.__new__(ScoreV2PrivateWavStage)
        for name, value in (
            ("path", sealed.claim.path),
            ("wav_sha256", wav_hash),
            ("wav_size_bytes", expected_size),
            ("sample_rate", sample_rate),
            ("frame_count", frame_count),
            ("local_execution_receipt", receipt),
            ("_canonical_bytes", payload),
            ("_artifact_sha256", digest),
        ):
            object.__setattr__(stage, name, value)
        lifecycle = _StageLifecycle()
        generation = _StageGeneration(
            path=sealed.claim.path,
            sealed=sealed,
            canonical_bytes=payload,
            artifact_sha256=digest,
            local_receipt=receipt,
            lifecycle=lifecycle,
        )
        _register_stage(stage, generation)
        stage.revalidate_private_wav()
        return stage, sealed
    except BaseException as exc:
        if active_sealed is None:
            _retire_claim(active_claim, primary=exc)
        else:
            _retire_sealed_stage(active_sealed, primary=exc)
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, ScoreV2PrivateWavError):
            raise
        if isinstance(exc, ScoreV2RendererError):
            raise ScoreV2PrivateWavError(
                "stage.local_execution_failed"
            ) from exc
        raise ScoreV2PrivateWavError("stage.creation_failed") from exc


@contextmanager
def stage_score_v2_executor_pcm24_wav(
    bundle: ScoreV2PerformanceBundle,
    executor_id: str,
    *,
    staging_directory: str | os.PathLike[str],
    limits: ProjectLimits | None = None,
    maximum_block_frames: int = _MAXIMUM_BLOCK_FRAMES,
) -> Iterator[ScoreV2PrivateWavStage]:
    """Yield one auto-retired private PCM24 WAV for local inspection."""

    if type(bundle) is not ScoreV2PerformanceBundle:
        raise TypeError("bundle must be ScoreV2PerformanceBundle")
    if type(executor_id) is not str or not executor_id:
        raise ValueError("executor_id must be a non-empty string")
    if (
        type(maximum_block_frames) is not int
        or not 1 <= maximum_block_frames <= _MAXIMUM_BLOCK_FRAMES
    ):
        raise ValueError(
            "maximum_block_frames must be an integer between 1 and 65536"
        )
    if limits is None:
        active_limits = ProjectLimits.from_environment()
    elif type(limits) is ProjectLimits:
        active_limits = limits
    else:
        raise TypeError("limits must be ProjectLimits")
    maximum_output = active_limits.max_primary_output_bytes
    maximum_seconds = active_limits.max_plan_seconds
    maximum_memory = active_limits.max_audio_memory_bytes
    if any(
        type(value) is not int or value < 1
        for value in (maximum_output, maximum_seconds, maximum_memory)
    ):
        raise ValueError(
            "relevant ProjectLimits fields must be positive integers"
        )
    expected = _bundle_generation(bundle)
    sample_rate = expected[3]
    frame_count = expected[4]
    expected_size = (
        _PCM24_WAV_HEADER_BYTES
        + _PCM24_STEREO_BYTES_PER_FRAME * frame_count
    )
    if _PCM24_STEREO_BYTES_PER_FRAME * frame_count + 36 > _RIFF_MAX_CHUNK_SIZE:
        raise ScoreV2PrivateWavError("stage.riff_size_limit_exceeded")
    if expected_size > maximum_output:
        raise ResourceLimitError(
            "render.output_budget_exceeded",
            "private Score-v2 WAV bytes exceed max_primary_output_bytes",
            actual=expected_size,
            limit=maximum_output,
        )
    if frame_count > sample_rate * maximum_seconds:
        raise ResourceLimitError(
            "render.duration_too_long",
            "private Score-v2 WAV duration exceeds max_plan_seconds",
            actual=frame_count / sample_rate,
            limit=maximum_seconds,
        )
    affordable_block_frames = maximum_memory // (
        _STREAMING_WORKING_BYTES_PER_FRAME
    )
    if affordable_block_frames < 1:
        raise ResourceLimitError(
            "render.memory_budget_exceeded",
            "private Score-v2 streaming block exceeds max_audio_memory_bytes",
            actual=_STREAMING_WORKING_BYTES_PER_FRAME,
            limit=maximum_memory,
        )
    effective_block_frames = min(
        maximum_block_frames,
        affordable_block_frames,
        frame_count,
    )
    directory = Path(os.path.abspath(os.fspath(staging_directory)))
    stage, sealed = _prepare_private_stage(
        bundle,
        executor_id,
        staging_directory=directory,
        expected=expected,
        maximum_block_frames=effective_block_frames,
    )
    primary: BaseException | None = None
    generation: _StageGeneration | None = None
    try:
        generation = stage._trusted_generation()
        yield stage
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if generation is not None:
            generation.lifecycle.active = False
        else:
            registered = _STAGE_GENERATIONS.get(id(stage))
            if registered is not None:
                registered[1].lifecycle.active = False
        _retire_sealed_stage(sealed, primary=primary)


__all__ = [
    "PRIVATE_WAV_STAGE_STATUS",
    "SCORE_V2_PRIVATE_WAV_STAGE_CONTRACT",
    "SCORE_V2_PRIVATE_WAV_STAGE_KIND",
    "SCORE_V2_PRIVATE_WAV_STAGE_SCHEMA_VERSION",
    "ScoreV2PrivateWavError",
    "ScoreV2PrivateWavStage",
    "stage_score_v2_executor_pcm24_wav",
]
