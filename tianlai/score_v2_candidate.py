"""Formal, closed-generation Candidate v3 support for the first Score-v2 slice.

Candidate v3 is intentionally a separate protocol branch.  It accepts only
one built-in oscillator executor, no external audio assets, no release tail,
and a formal receipt produced by consumption of a live runtime-authority
lease.  The older local-execution receipt and private WAV-stage documents are
not publication inputs.

This module contains the portable document validation shared by the ordinary
candidate loader and the descriptor-bound integrity verifier.  It validates
historical evidence; it never recreates or grants a live runtime authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .authoring_json import AuthoringJsonLimits, strict_json_loads
from .canonical_json import canonical_json_bytes, canonical_json_sha256
from .instrument import factory_manifest_sha256
from .score_v2 import parse_score_v2_document, score_render_projection_sha256
from .score_v2_capability_adapter import (
    SCORE_V2_CAPABILITY_PLAN_CONTRACT,
    SCORE_V2_CAPABILITY_PLAN_KIND,
    SCORE_V2_CAPABILITY_PLAN_SCHEMA_VERSION,
)
from .score_v2_capability_source import (
    SCORE_V2_CAPABILITY_SOURCE_CONTRACT,
    SCORE_V2_CAPABILITY_SOURCE_KIND,
    SCORE_V2_CAPABILITY_SOURCE_SCHEMA_VERSION,
)
from .score_v2_execution_profile import (
    SCORE_V2_EXECUTION_PROFILE_KIND,
    SCORE_V2_EXECUTION_PROFILE_SCHEMA_VERSION,
    parse_score_v2_execution_profile,
)
from .score_v2_performance import (
    ENDPOINT_DISPATCH_STATUS,
    SCORE_V2_PERFORMANCE_CONTRACT,
    SCORE_V2_PERFORMANCE_KIND,
    SCORE_V2_PERFORMANCE_SCHEMA_VERSION,
)
from .score_v2_plan import (
    SCORE_V2_PLAN_CONTRACT,
    SCORE_V2_PLAN_KIND,
    SCORE_V2_PLAN_SCHEMA_VERSION,
)
from .score_v2_runtime_source import (
    NO_EXTERNAL_ASSET_INVENTORY_STATUS,
    SCORE_V2_RUNTIME_SOURCE_CONTRACT,
    SCORE_V2_RUNTIME_SOURCE_KIND,
    SCORE_V2_RUNTIME_SOURCE_SCHEMA_VERSION,
)
from .score_v2_runtime_authority import (
    SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_CONTRACT,
    SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_KIND,
    SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_SCHEMA_VERSION,
    SCORE_V2_RUNTIME_AUTHORITY_CONTRACT,
    SCORE_V2_RUNTIME_AUTHORITY_KIND,
    SCORE_V2_RUNTIME_AUTHORITY_SCHEMA_VERSION,
)

if TYPE_CHECKING:
    from .score_v2_formal_render import ScoreV2FormalRenderGeneration
    from .score_v2_project_render import ScoreV2ProjectRenderCompilation


SCORE_V2_CANDIDATE_VERSION = 3
SCORE_V2_CANDIDATE_PIPELINE_KIND = "score_v2"
SCORE_V2_CANDIDATE_CONTRACT = (
    "score-v2-candidate-single-oscillator-asset-free-v1"
)

SCORE_V2_RENDER_RECEIPT_KIND = "tianlai.score_v2_render_receipt"
SCORE_V2_RENDER_RECEIPT_SCHEMA_VERSION = 1
SCORE_V2_RENDER_RECEIPT_CONTRACT = "score-v2-formal-render-receipt-v1"
SCORE_V2_RENDER_RECEIPT_STATUS = (
    "formal_render_complete_runtime_lease_consumed"
)
SCORE_V2_POST_RENDER_CHECK_KIND = "tianlai.score_v2_post_render_check"
SCORE_V2_POST_RENDER_CHECK_SCHEMA_VERSION = 1
SCORE_V2_POST_RENDER_CHECK_CONTRACT = (
    "score-v2-post-render-check-pcm24-stream-v1"
)

# These names are a protocol surface, not presentation defaults.  Keeping the
# first v3 generation flat and fixed makes closed-world verification and
# portable filename collision checks unambiguous.
SCORE_V2_SOURCE_NAME = "score-v2.json"
SCORE_V2_ROSTER_NAME = "roster.json"
SCORE_V2_EXECUTION_PROFILE_NAME = "execution-profile.json"
SCORE_V2_PLAN_NAME = "score-v2-plan.json"
SCORE_V2_CAPABILITY_SOURCE_NAME = "capability-source.json"
SCORE_V2_CAPABILITY_PLAN_NAME = "capability-plan.json"
SCORE_V2_RUNTIME_SOURCE_NAME = "runtime-source.json"
SCORE_V2_RUNTIME_AUTHORITY_NAME = "runtime-authority.json"
SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_NAME = (
    "runtime-authority-acquisition.json"
)
SCORE_V2_RUNTIME_MANIFEST_NAME = "runtime-manifest.json"
SCORE_V2_PERFORMANCE_BUNDLE_NAME = "performance-bundle.json"
SCORE_V2_MIX_NAME = "合奏.wav"
SCORE_V2_POST_RENDER_CHECK_NAME = "渲染后自检.json"
SCORE_V2_RENDER_RECEIPT_NAME = "渲染回执.json"

SCORE_V2_CANDIDATE_JSON_NAMES = (
    SCORE_V2_SOURCE_NAME,
    SCORE_V2_ROSTER_NAME,
    SCORE_V2_EXECUTION_PROFILE_NAME,
    SCORE_V2_PLAN_NAME,
    SCORE_V2_CAPABILITY_SOURCE_NAME,
    SCORE_V2_CAPABILITY_PLAN_NAME,
    SCORE_V2_RUNTIME_SOURCE_NAME,
    SCORE_V2_RUNTIME_AUTHORITY_NAME,
    SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_NAME,
    SCORE_V2_RUNTIME_MANIFEST_NAME,
    SCORE_V2_PERFORMANCE_BUNDLE_NAME,
    SCORE_V2_POST_RENDER_CHECK_NAME,
    SCORE_V2_RENDER_RECEIPT_NAME,
)

_RECEIPT_BINDING_NAMES = {
    "score": SCORE_V2_SOURCE_NAME,
    "roster": SCORE_V2_ROSTER_NAME,
    "execution_profile": SCORE_V2_EXECUTION_PROFILE_NAME,
    "score_v2_plan": SCORE_V2_PLAN_NAME,
    "capability_source": SCORE_V2_CAPABILITY_SOURCE_NAME,
    "capability_plan": SCORE_V2_CAPABILITY_PLAN_NAME,
    "runtime_source": SCORE_V2_RUNTIME_SOURCE_NAME,
    "runtime_authority": SCORE_V2_RUNTIME_AUTHORITY_NAME,
    "runtime_authority_acquisition": (
        SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_NAME
    ),
    "runtime_manifest": SCORE_V2_RUNTIME_MANIFEST_NAME,
    "performance_bundle": SCORE_V2_PERFORMANCE_BUNDLE_NAME,
}

_GENERATED_CONTRACTS = {
    "score_v2_plan": (
        SCORE_V2_PLAN_KIND,
        SCORE_V2_PLAN_SCHEMA_VERSION,
        SCORE_V2_PLAN_CONTRACT,
    ),
    "capability_source": (
        SCORE_V2_CAPABILITY_SOURCE_KIND,
        SCORE_V2_CAPABILITY_SOURCE_SCHEMA_VERSION,
        SCORE_V2_CAPABILITY_SOURCE_CONTRACT,
    ),
    "capability_plan": (
        SCORE_V2_CAPABILITY_PLAN_KIND,
        SCORE_V2_CAPABILITY_PLAN_SCHEMA_VERSION,
        SCORE_V2_CAPABILITY_PLAN_CONTRACT,
    ),
    "runtime_source": (
        SCORE_V2_RUNTIME_SOURCE_KIND,
        SCORE_V2_RUNTIME_SOURCE_SCHEMA_VERSION,
        SCORE_V2_RUNTIME_SOURCE_CONTRACT,
    ),
    "runtime_authority": (
        SCORE_V2_RUNTIME_AUTHORITY_KIND,
        SCORE_V2_RUNTIME_AUTHORITY_SCHEMA_VERSION,
        SCORE_V2_RUNTIME_AUTHORITY_CONTRACT,
    ),
    "runtime_authority_acquisition": (
        SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_KIND,
        SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_SCHEMA_VERSION,
        SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_CONTRACT,
    ),
    "performance_bundle": (
        SCORE_V2_PERFORMANCE_KIND,
        SCORE_V2_PERFORMANCE_SCHEMA_VERSION,
        SCORE_V2_PERFORMANCE_CONTRACT,
    ),
}

_HEX = frozenset("0123456789abcdef")
_JSON_LIMITS = AuthoringJsonLimits(
    max_document_bytes=32 * 1024 * 1024,
    max_depth=128,
    max_nodes=2_000_000,
    max_string_bytes=4 * 1024 * 1024,
    max_array_items=500_000,
    max_object_members=65_536,
)


class ScoreV2CandidateError(ValueError):
    """One stable failure at the formal Candidate-v3 boundary."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(
            f"score_v2_candidate.{code}"
            + ("" if message is None else f": {message}")
        )


@dataclass(frozen=True, slots=True)
class ScoreV2CandidateArtifact:
    """Captured artifact facts supplied by either candidate verifier."""

    sha256: str
    size_bytes: int
    payload: bytes | None = None
    prefix: bytes | None = None


def _fail(code: str, message: str) -> None:
    raise ScoreV2CandidateError(code, message)


def _exact(
    value: object,
    keys: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        _fail("invalid_document", f"{label} has an invalid exact shape")
    return value


def _sha256(value: object, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        _fail("invalid_binding", f"{label} must be lowercase SHA-256")
    return value


def _positive_integer(value: object, *, label: str, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        _fail("invalid_document", f"{label} must be an integer >= {minimum}")
    return value


def _finite_number(value: object, *, label: str) -> float:
    import math

    if type(value) not in {int, float}:
        _fail("invalid_document", f"{label} must be finite")
    number = float(value)
    if not math.isfinite(number):
        _fail("invalid_document", f"{label} must be finite")
    return number


def _binding(value: object, *, role: str) -> dict[str, Any]:
    binding = _exact(
        value,
        {"path", "canonical_sha256", "file_sha256"},
        label=f"receipt bindings.{role}",
    )
    if binding.get("path") != _RECEIPT_BINDING_NAMES[role]:
        _fail("invalid_binding", f"receipt {role} path is not fixed")
    _sha256(binding.get("canonical_sha256"), label=f"{role} canonical hash")
    _sha256(binding.get("file_sha256"), label=f"{role} file hash")
    return binding


def validate_score_v2_candidate_manifest(
    document: object,
    *,
    expected_work_id: str,
    expected_candidate_id: str,
) -> dict[str, Any]:
    """Validate the exact Candidate-v3 manifest shape, without opening files."""

    manifest = _exact(
        document,
        {
            "format",
            "version",
            "pipeline",
            "candidate_id",
            "work_id",
            "title",
            "created_at_utc",
            "parent_candidate_id",
            "project",
            "render_receipt",
        },
        label="Candidate-v3 manifest",
    )
    if manifest.get("format") != "tianlai.candidate" or manifest.get(
        "version"
    ) != SCORE_V2_CANDIDATE_VERSION:
        _fail("unsupported_candidate", "Candidate-v3 format/version is invalid")
    if manifest.get("work_id") != expected_work_id or manifest.get(
        "candidate_id"
    ) != expected_candidate_id:
        _fail("identity_mismatch", "candidate identity disagrees with its directory")
    if type(manifest.get("title")) is not str:
        _fail("invalid_document", "candidate title must be a string")
    parent = manifest.get("parent_candidate_id")
    if parent is not None and (type(parent) is not str or not parent):
        _fail("invalid_document", "parent_candidate_id must be non-empty or null")

    pipeline = _exact(
        manifest.get("pipeline"),
        {"kind", "contract", "score_schema_version", "executor_count"},
        label="Candidate-v3 pipeline",
    )
    if pipeline != {
        "kind": SCORE_V2_CANDIDATE_PIPELINE_KIND,
        "contract": SCORE_V2_CANDIDATE_CONTRACT,
        "score_schema_version": 2,
        "executor_count": 1,
    }:
        _fail("unsupported_candidate", "Candidate-v3 pipeline is unsupported")

    project = _exact(
        manifest.get("project"),
        {
            "score",
            "roster",
            "execution_profile",
            "score_v2_plan_sha256",
            "performance_bundle_sha256",
        },
        label="Candidate-v3 project",
    )
    for role in ("score", "roster", "execution_profile"):
        _binding(project.get(role), role=role)
    _sha256(project.get("score_v2_plan_sha256"), label="score-v2 plan")
    _sha256(project.get("performance_bundle_sha256"), label="performance bundle")

    receipt = _exact(
        manifest.get("render_receipt"),
        {"path", "sha256", "kind", "schema_version"},
        label="Candidate-v3 render receipt binding",
    )
    if (
        receipt.get("path") != SCORE_V2_RENDER_RECEIPT_NAME
        or receipt.get("kind") != SCORE_V2_RENDER_RECEIPT_KIND
        or receipt.get("schema_version")
        != SCORE_V2_RENDER_RECEIPT_SCHEMA_VERSION
    ):
        _fail("invalid_binding", "Candidate-v3 render receipt identity is invalid")
    _sha256(receipt.get("sha256"), label="render receipt")
    return manifest


def validate_score_v2_render_receipt(document: object) -> dict[str, Any]:
    """Validate the exact formal receipt envelope and fixed artifact paths."""

    receipt = _exact(
        document,
        {
            "kind",
            "schema_version",
            "contract",
            "status",
            "scope",
            "bindings",
            "executor",
            "audio_format",
            "mix",
            "post_render_check",
            "limitations",
        },
        label="Score-v2 formal render receipt",
    )
    if (
        receipt.get("kind") != SCORE_V2_RENDER_RECEIPT_KIND
        or receipt.get("schema_version")
        != SCORE_V2_RENDER_RECEIPT_SCHEMA_VERSION
        or receipt.get("contract") != SCORE_V2_RENDER_RECEIPT_CONTRACT
        or receipt.get("status") != SCORE_V2_RENDER_RECEIPT_STATUS
    ):
        _fail("unsupported_receipt", "formal render receipt identity is unsupported")

    scope = _exact(
        receipt.get("scope"),
        {
            "executor_count",
            "backend",
            "external_audio_assets",
            "tail_samples",
            "mix_policy",
            "normalization",
            "space",
            "stems",
        },
        label="formal receipt scope",
    )
    if scope != {
        "executor_count": 1,
        "backend": "builtin_oscillator",
        "external_audio_assets": "none",
        "tail_samples": 0,
        "mix_policy": "single_executor_identity_no_gain",
        "normalization": "disabled",
        "space": "disabled",
        "stems": "not_written",
    }:
        _fail("unsupported_scope", "formal receipt is outside the first v3 scope")

    bindings = _exact(
        receipt.get("bindings"),
        set(_RECEIPT_BINDING_NAMES),
        label="formal receipt bindings",
    )
    for role in _RECEIPT_BINDING_NAMES:
        _binding(bindings.get(role), role=role)

    executor = _exact(
        receipt.get("executor"),
        {
            "executor_id",
            "part_id",
            "authority_consumption_status",
            "backend_scope",
            "effective_manifest_sha256",
            "factory_generation_sha256",
            "performance_sha256",
            "event_sidecar_sha256",
            "sample_rate",
            "frame_count",
            "block_count",
            "event_count",
            "endpoint_event_count",
            "peak_active_voices",
            "float_stream_encoding",
            "float_stream_sha256",
        },
        label="formal receipt executor",
    )
    if (
        type(executor.get("executor_id")) is not str
        or not executor["executor_id"]
        or type(executor.get("part_id")) is not str
        or not executor["part_id"]
        or executor.get("authority_consumption_status")
        != "active_single_use_runtime_lease_consumed"
        or executor.get("backend_scope")
        != "builtin_oscillator_manifest_route_declared_no_external_audio_assets"
        or executor.get("float_stream_encoding")
        != "little_endian_float64_stereo_interleaved"
    ):
        _fail("invalid_receipt", "formal receipt executor identity is invalid")
    for key in (
        "effective_manifest_sha256",
        "factory_generation_sha256",
        "performance_sha256",
        "event_sidecar_sha256",
        "float_stream_sha256",
    ):
        _sha256(executor.get(key), label=f"executor {key}")
    _positive_integer(executor.get("sample_rate"), label="executor sample_rate")
    _positive_integer(executor.get("frame_count"), label="executor frame_count")
    for key in (
        "block_count",
        "event_count",
        "endpoint_event_count",
        "peak_active_voices",
    ):
        _positive_integer(executor.get(key), label=f"executor {key}", allow_zero=True)

    audio = _exact(
        receipt.get("audio_format"),
        {
            "container",
            "encoding",
            "bits_per_sample",
            "channels",
            "sample_rate",
            "pcm24_contract",
        },
        label="formal receipt audio_format",
    )
    if (
        audio.get("container") != "WAV"
        or audio.get("encoding") != "PCM"
        or audio.get("bits_per_sample") != 24
        or audio.get("channels") != 2
        or audio.get("sample_rate") != executor.get("sample_rate")
        or audio.get("pcm24_contract") != "tianlai-pcm24-stereo-le-v1"
    ):
        _fail("invalid_audio", "formal receipt audio contract is invalid")

    mix = _exact(
        receipt.get("mix"),
        {
            "path",
            "sha256",
            "size_bytes",
            "frame_count",
            "float_stream_sha256",
            "peak",
        },
        label="formal receipt mix",
    )
    if (
        mix.get("path") != SCORE_V2_MIX_NAME
        or mix.get("frame_count") != executor.get("frame_count")
        or mix.get("float_stream_sha256")
        != executor.get("float_stream_sha256")
    ):
        _fail("invalid_audio", "formal receipt mix binding is inconsistent")
    _sha256(mix.get("sha256"), label="mix")
    _positive_integer(mix.get("size_bytes"), label="mix size")
    peak = _finite_number(mix.get("peak"), label="mix peak")
    if peak < 0.0 or peak > 1.0:
        _fail("invalid_audio", "mix peak must be within [0, 1]")

    post = _exact(
        receipt.get("post_render_check"),
        {"path", "sha256", "format", "version"},
        label="formal receipt post-render check",
    )
    if (
        post.get("path") != SCORE_V2_POST_RENDER_CHECK_NAME
        or post.get("format") != SCORE_V2_POST_RENDER_CHECK_KIND
        or post.get("version") != SCORE_V2_POST_RENDER_CHECK_SCHEMA_VERSION
    ):
        _fail("invalid_postcheck", "post-render check identity is invalid")
    _sha256(post.get("sha256"), label="post-render check")

    limitations = _exact(
        receipt.get("limitations"),
        {
            "authorship_verified",
            "provenance_verified",
            "live_tree_immutable_after_return",
            "runtime_authority_reusable",
            "external_assets_supported",
            "executor_count_limit",
            "release_tail",
        },
        label="formal receipt limitations",
    )
    if limitations != {
        "authorship_verified": False,
        "provenance_verified": False,
        "live_tree_immutable_after_return": False,
        "runtime_authority_reusable": False,
        "external_assets_supported": False,
        "executor_count_limit": 1,
        "release_tail": "transport_frame_count_only_no_implicit_tail",
    }:
        _fail("invalid_receipt", "formal receipt limitations are invalid")
    return receipt


def score_v2_candidate_expected_files(
    manifest: dict[str, Any],
    receipt: dict[str, Any],
    *,
    candidate_manifest_name: str,
) -> dict[str, str]:
    """Return the exact flat Candidate-v3 entry set and semantic roles."""

    validate_score_v2_render_receipt(receipt)
    expected = {
        candidate_manifest_name: "candidate manifest",
        SCORE_V2_MIX_NAME: "score-v2 mix",
        SCORE_V2_POST_RENDER_CHECK_NAME: "score-v2 post-render check",
        SCORE_V2_RENDER_RECEIPT_NAME: "score-v2 render receipt",
    }
    for role, name in _RECEIPT_BINDING_NAMES.items():
        expected[name] = f"score-v2 {role}"
    if set(expected) != {
        candidate_manifest_name,
        *SCORE_V2_CANDIDATE_JSON_NAMES,
        SCORE_V2_MIX_NAME,
    }:
        raise RuntimeError("Candidate-v3 fixed file accounting mismatch")
    # The manifest root hashes must already select the same receipt bindings.
    bindings = receipt["bindings"]
    project = manifest["project"]
    if (
        project["score"] != bindings["score"]
        or project["roster"] != bindings["roster"]
        or project["execution_profile"] != bindings["execution_profile"]
        or project["score_v2_plan_sha256"]
        != bindings["score_v2_plan"]["canonical_sha256"]
        or project["performance_bundle_sha256"]
        != bindings["performance_bundle"]["canonical_sha256"]
    ):
        _fail("identity_mismatch", "manifest and receipt roots disagree")
    return expected


def _document(
    artifacts: Mapping[str, ScoreV2CandidateArtifact],
    name: str,
) -> dict[str, Any]:
    artifact = artifacts.get(name)
    if artifact is None or type(artifact.payload) is not bytes:
        _fail("missing_artifact", f"{name} was not captured as JSON")
    try:
        value = strict_json_loads(
            artifact.payload,
            limits=_JSON_LIMITS,
            require_object=True,
            require_js_safe_integers=False,
        )
    except ValueError as exc:
        raise ScoreV2CandidateError(
            "invalid_json", f"{name} is not strict bounded JSON"
        ) from exc
    if type(value) is not dict:
        _fail("invalid_json", f"{name} must be an object")
    return value


def _require_artifact_binding(
    artifacts: Mapping[str, ScoreV2CandidateArtifact],
    binding: dict[str, Any],
    *,
    label: str,
) -> ScoreV2CandidateArtifact:
    artifact = artifacts.get(binding["path"])
    if artifact is None:
        _fail("missing_artifact", f"{label} is missing")
    if artifact.sha256 != binding["file_sha256"]:
        _fail("hash_mismatch", f"{label} file SHA-256 does not match")
    document = _document(artifacts, binding["path"])
    if canonical_json_sha256(document) != binding["canonical_sha256"]:
        _fail("hash_mismatch", f"{label} canonical SHA-256 does not match")
    return artifact


def _require_canonical_generated_document(
    artifacts: Mapping[str, ScoreV2CandidateArtifact],
    receipt: dict[str, Any],
    role: str,
) -> dict[str, Any]:
    binding = receipt["bindings"][role]
    artifact = _require_artifact_binding(
        artifacts,
        binding,
        label=role,
    )
    document = _document(artifacts, binding["path"])
    expected_kind, expected_version, expected_contract = _GENERATED_CONTRACTS[role]
    if (
        document.get("kind") != expected_kind
        or document.get("schema_version") != expected_version
        or document.get("contract") != expected_contract
    ):
        _fail("unsupported_artifact", f"{role} identity is unsupported")
    if artifact.payload != canonical_json_bytes(document):
        _fail("noncanonical_artifact", f"{role} must use canonical JSON bytes")
    if binding["canonical_sha256"] != binding["file_sha256"]:
        _fail("hash_mismatch", f"{role} canonical and file hashes must agree")
    return document


def _one_document(value: object, *, label: str) -> dict[str, Any]:
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        _fail("invalid_document", f"{label} must contain exactly one object")
    return value[0]


def _validate_generated_semantic_core(
    *,
    score: Any,
    profile: Any,
    plan: dict[str, Any],
    capability_source: dict[str, Any],
    capability_plan: dict[str, Any],
    runtime_source: dict[str, Any],
    runtime_manifest: dict[str, Any],
    runtime_manifest_artifact: ScoreV2CandidateArtifact,
    acquisition: dict[str, Any],
    performance: dict[str, Any],
    receipt: dict[str, Any],
) -> None:
    """Validate the portable semantics that the first v3 scope can prove.

    Candidate v3 is unsigned, so a party can consistently re-seal a whole
    generation.  Such re-sealing must not, however, turn contradictory or
    protocol-extended transport documents into a claimed semantic closure.
    """

    receipt_executor = receipt["executor"]
    sample_rate = receipt_executor["sample_rate"]
    frame_count = receipt_executor["frame_count"]
    projection_hash = score_render_projection_sha256(score)

    _exact(
        plan,
        {
            "kind",
            "schema_version",
            "contract",
            "bindings",
            "sample_rate",
            "sample_time_policy",
            "sample_time_policy_scope",
            "sample_rounding_mode",
            "occurrence_order",
            "dynamic_profile",
            "time_index_canonical_json_bytes",
            "score_duration",
            "occurrence_count",
            "occurrences",
        },
        label="score-v2 plan",
    )
    plan_bindings = _exact(
        plan.get("bindings"),
        {
            "source_document_sha256",
            "score_render_projection_sha256",
            "time_index_sha256",
            "dynamic_profile_sha256",
        },
        label="score-v2 plan bindings",
    )
    for key in (
        "source_document_sha256",
        "score_render_projection_sha256",
        "time_index_sha256",
        "dynamic_profile_sha256",
    ):
        _sha256(plan_bindings.get(key), label=f"score-v2 plan {key}")
    expected_dynamic = {
        "kind": "tianlai.score_v2_dynamic_profile",
        "schema_version": 1,
        "velocities": {
            level.mark: {
                "numerator": level.value.numerator,
                "denominator": level.value.denominator,
            }
            for level in profile.dynamic_profile
        },
    }
    occurrences = plan.get("occurrences")
    expected_occurrences = sum(len(part.notes) for part in score.parts) - len(
        score.ties
    )
    duration = plan.get("score_duration")
    duration_sample = duration.get("sample") if type(duration) is dict else None
    if (
        plan_bindings.get("score_render_projection_sha256") != projection_hash
        or plan_bindings.get("dynamic_profile_sha256")
        != canonical_json_sha256(expected_dynamic)
        or plan.get("dynamic_profile") != expected_dynamic
        or plan.get("sample_rate") != sample_rate
        or plan.get("sample_time_policy") != profile.sample_time_policy
        or plan.get("sample_time_policy_scope") != "occurrence_endpoints"
        or plan.get("occurrence_order")
        != [
            "resolved_start_sample",
            "requested_start_seconds",
            "source_order",
            "occurrence_id",
        ]
        or type(occurrences) is not list
        or plan.get("occurrence_count") != len(occurrences)
        or len(occurrences) != expected_occurrences
        or type(duration_sample) is not dict
        or duration_sample.get("resolved_sample") != frame_count
    ):
        _fail("identity_mismatch", "score-v2 plan semantics do not close")
    plan_occurrences: dict[str, dict[str, Any]] = {}
    for occurrence in occurrences:
        occurrence = _exact(
            occurrence,
            {
                "occurrence_id",
                "part_id",
                "source_event_ids",
                "source_tie_ids",
                "source_order",
                "source_notes",
                "start",
                "end",
                "sounding_pitch",
                "dynamic",
                "velocity",
            },
            label="score-v2 plan occurrence",
        )
        occurrence_id = occurrence.get("occurrence_id")
        start = occurrence.get("start")
        end = occurrence.get("end")
        if (
            type(occurrence_id) is not str
            or not occurrence_id
            or occurrence_id in plan_occurrences
            or type(occurrence.get("part_id")) is not str
            or type(occurrence.get("source_event_ids")) is not list
            or not occurrence["source_event_ids"]
            or type(occurrence.get("source_tie_ids")) is not list
            or type(start) is not dict
            or type(end) is not dict
            or type(start.get("resolved_sample")) is not int
            or type(end.get("resolved_sample")) is not int
            or not 0 <= start["resolved_sample"] <= end["resolved_sample"] <= frame_count
        ):
            _fail("invalid_document", "score-v2 plan occurrence is invalid")
        plan_occurrences[occurrence_id] = occurrence

    _exact(
        capability_source,
        {
            "kind",
            "schema_version",
            "contract",
            "catalogue_root",
            "roster_projection_sha256",
            "roster_projection",
            "runtime_fingerprint_policy",
            "manifest_generations",
            "capability_projections",
            "executor_bindings",
        },
        label="capability source",
    )
    source_executor = _one_document(
        capability_source.get("executor_bindings"),
        label="capability source executor_bindings",
    )
    manifest_generation = _one_document(
        capability_source.get("manifest_generations"),
        label="capability source manifest_generations",
    )
    capability_projection = _one_document(
        capability_source.get("capability_projections"),
        label="capability source capability_projections",
    )
    _exact(
        source_executor,
        {
            "executor_order",
            "executor_id",
            "part_id",
            "instrument_relative_path",
            "manifest_source_sha256",
            "capability_projection_sha256",
            "overrides",
            "effective_manifest_canonical_sha256",
            "effective_manifest_sha256",
            "custom_implementation_blocked",
            "runtime_fingerprint_status",
            "runtime_fingerprint_sha256",
            "execution_eligibility",
        },
        label="capability source executor",
    )
    _exact(
        manifest_generation,
        {
            "source_sha256",
            "manifest_path",
            "file_identity",
            "raw_bytes_size",
            "raw_sha256",
            "manifest_canonical_sha256",
            "custom_implementation_blocked",
        },
        label="capability source manifest generation",
    )
    _exact(
        capability_projection,
        {
            "manifest_source_sha256",
            "instrument_relative_path",
            "canonical_sha256",
            "projection",
        },
        label="capability projection",
    )
    projection = capability_projection.get("projection")
    if (
        capability_source.get("runtime_fingerprint_policy") != "not_captured"
        or canonical_json_sha256(capability_source.get("roster_projection"))
        != capability_source.get("roster_projection_sha256")
        or type(projection) is not dict
        or canonical_json_sha256(projection)
        != capability_projection.get("canonical_sha256")
        or capability_projection.get("canonical_sha256")
        != source_executor.get("capability_projection_sha256")
        or capability_projection.get("manifest_source_sha256")
        != source_executor.get("manifest_source_sha256")
        or capability_projection.get("instrument_relative_path")
        != source_executor.get("instrument_relative_path")
        or manifest_generation.get("source_sha256")
        != source_executor.get("manifest_source_sha256")
        or manifest_generation.get("raw_sha256")
        != runtime_manifest_artifact.sha256
        or manifest_generation.get("raw_bytes_size")
        != runtime_manifest_artifact.size_bytes
        or manifest_generation.get("manifest_canonical_sha256")
        != canonical_json_sha256(runtime_manifest)
        or manifest_generation.get("custom_implementation_blocked") is not False
        or source_executor.get("overrides") != {}
        or source_executor.get("custom_implementation_blocked") is not False
        or source_executor.get("runtime_fingerprint_status") != "not_captured"
        or source_executor.get("runtime_fingerprint_sha256") is not None
        or source_executor.get("execution_eligibility")
        != "pending_runtime_fingerprint"
    ):
        _fail("identity_mismatch", "capability source semantics do not close")

    _exact(
        capability_plan,
        {
            "kind",
            "schema_version",
            "contract",
            "render_authority",
            "bindings",
            "sample_rate",
            "runtime_fingerprint_status",
            "tuning_resolution",
            "occurrence_count",
            "occurrences_sha256",
            "occurrences",
        },
        label="capability plan",
    )
    capability_occurrences = capability_plan.get("occurrences")
    if (
        capability_plan.get("render_authority") is not False
        or capability_plan.get("runtime_fingerprint_status") != "not_captured"
        or capability_plan.get("sample_rate") != sample_rate
        or type(capability_occurrences) is not list
        or capability_plan.get("occurrence_count") != len(capability_occurrences)
        or len(capability_occurrences) != len(plan_occurrences)
        or canonical_json_sha256(capability_occurrences)
        != capability_plan.get("occurrences_sha256")
    ):
        _fail("identity_mismatch", "capability plan semantics do not close")
    capability_by_id: dict[str, dict[str, Any]] = {}
    for occurrence in capability_occurrences:
        occurrence = _exact(
            occurrence,
            {
                "occurrence_id",
                "part_id",
                "executor_id",
                "source_event_ids",
                "source_tie_ids",
                "start_sample",
                "end_sample",
                "articulation",
                "range",
                "pitch",
                "velocity",
                "capability_binding",
            },
            label="capability plan occurrence",
        )
        occurrence_id = occurrence.get("occurrence_id")
        source = plan_occurrences.get(occurrence_id)
        binding = occurrence.get("capability_binding")
        if (
            source is None
            or occurrence_id in capability_by_id
            or occurrence.get("part_id") != source.get("part_id")
            or occurrence.get("executor_id") != source_executor.get("executor_id")
            or occurrence.get("source_event_ids") != source.get("source_event_ids")
            or occurrence.get("source_tie_ids") != source.get("source_tie_ids")
            or occurrence.get("start_sample")
            != source["start"].get("resolved_sample")
            or occurrence.get("end_sample") != source["end"].get("resolved_sample")
            or type(binding) is not dict
            or binding.get("manifest_source_sha256")
            != source_executor.get("manifest_source_sha256")
            or binding.get("capability_projection_sha256")
            != source_executor.get("capability_projection_sha256")
            or binding.get("effective_manifest_sha256")
            != source_executor.get("effective_manifest_sha256")
            or binding.get("runtime_fingerprint_status") != "not_captured"
        ):
            _fail("identity_mismatch", "capability occurrence disagrees with plan")
        capability_by_id[occurrence_id] = occurrence

    _exact(
        runtime_source,
        {
            "kind",
            "schema_version",
            "contract",
            "render_authority",
            "project_root",
            "limitations",
            "bindings",
            "executor_count",
            "executors",
        },
        label="runtime source",
    )
    runtime_executor = _one_document(
        runtime_source.get("executors"), label="runtime source executors"
    )
    _exact(
        runtime_executor,
        {
            "executor_order",
            "executor_id",
            "part_id",
            "capability_plan_sha256",
            "capability_source_sha256",
            "roster_projection_sha256",
            "manifest_source_sha256",
            "manifest_raw_sha256",
            "manifest_canonical_sha256",
            "capability_projection_sha256",
            "effective_manifest_canonical_sha256",
            "effective_manifest_sha256",
            "sample_rate",
            "runtime_fingerprint_status",
            "legacy_runtime_fingerprint_bytes_size",
            "legacy_runtime_fingerprint_sha256",
            "asset_inventory_status",
            "runtime_evidence",
            "legacy_runtime_fingerprint",
        },
        label="runtime source executor",
    )
    fingerprint = runtime_executor.get("legacy_runtime_fingerprint")
    evidence = runtime_executor.get("runtime_evidence")
    if type(fingerprint) is not dict or type(evidence) is not dict:
        _fail("invalid_document", "runtime fingerprint evidence is missing")
    _exact(
        fingerprint,
        {
            "algorithm",
            "manifest",
            "local_implementation",
            "resource_verification",
            "pitch_calibration",
            "render_python_closure",
            "runtime_dependencies",
            "runtime_asset_graph",
        },
        label="runtime fingerprint",
    )
    _exact(
        evidence,
        {
            "render_python_closure_sha256",
            "runtime_dependencies_sha256",
            "local_implementation",
            "resource_verification",
            "pitch_calibration",
            "runtime_asset_graph",
            "asset_inventory_status",
            "asset_descriptor_status",
        },
        label="runtime evidence",
    )
    graph = fingerprint.get("runtime_asset_graph")
    closure = fingerprint.get("render_python_closure")
    dependencies = fingerprint.get("runtime_dependencies")
    null_file = {"path": None, "sha256": None}
    pitch_calibration = fingerprint.get("pitch_calibration")
    resource_verification = fingerprint.get("resource_verification")
    absent_optional_files = (pitch_calibration, resource_verification)
    if (
        runtime_source.get("render_authority") is not False
        or runtime_source.get("executor_count") != 1
        or runtime_executor.get("executor_order") != 0
        or runtime_executor.get("executor_id") != receipt_executor["executor_id"]
        or runtime_executor.get("part_id") != receipt_executor["part_id"]
        or runtime_executor.get("sample_rate") != sample_rate
        or runtime_executor.get("manifest_source_sha256")
        != source_executor.get("manifest_source_sha256")
        or runtime_executor.get("manifest_raw_sha256")
        != runtime_manifest_artifact.sha256
        or runtime_executor.get("manifest_canonical_sha256")
        != canonical_json_sha256(runtime_manifest)
        or runtime_executor.get("capability_projection_sha256")
        != source_executor.get("capability_projection_sha256")
        or runtime_executor.get("effective_manifest_canonical_sha256")
        != source_executor.get("effective_manifest_canonical_sha256")
        or runtime_executor.get("effective_manifest_sha256")
        != receipt_executor["effective_manifest_sha256"]
        or runtime_executor.get("runtime_fingerprint_status")
        != "captured_legacy_runtime_fingerprint"
        or runtime_executor.get("asset_inventory_status")
        != NO_EXTERNAL_ASSET_INVENTORY_STATUS
        or runtime_executor.get("legacy_runtime_fingerprint_sha256")
        != canonical_json_sha256(fingerprint)
        or runtime_executor.get("legacy_runtime_fingerprint_bytes_size")
        != len(canonical_json_bytes(fingerprint))
        or fingerprint.get("local_implementation") != null_file
        or any(
            type(item) is not dict
            or set(item) != {"path", "sha256"}
            or item.get("sha256") is not None
            or (
                item.get("path") is not None
                and (type(item.get("path")) is not str or not item["path"])
            )
            for item in absent_optional_files
        )
        or type(graph) is not dict
        or graph.get("file_count") != 0
        or graph.get("region_count") != 0
        or graph.get("total_bytes") != 0
        or graph.get("sample_rate_hz") != sample_rate
        or not _sha256(graph.get("sha256"), label="runtime asset graph")
        or evidence.get("runtime_asset_graph") != graph
        or evidence.get("local_implementation") != null_file
        or evidence.get("resource_verification") != resource_verification
        or evidence.get("pitch_calibration") != pitch_calibration
        or evidence.get("asset_inventory_status")
        != NO_EXTERNAL_ASSET_INVENTORY_STATUS
        or type(closure) is not dict
        or evidence.get("render_python_closure_sha256")
        != canonical_json_sha256(closure)
        or type(dependencies) is not dict
        or evidence.get("runtime_dependencies_sha256")
        != canonical_json_sha256(dependencies)
    ):
        _fail("unsupported_scope", "runtime source is not asset-free oscillator evidence")

    _exact(
        performance,
        {
            "kind",
            "schema_version",
            "contract",
            "render_authority",
            "endpoint_dispatch_status",
            "bindings",
            "sample_rate",
            "frame_count",
            "duration_seconds",
            "occurrence_count",
            "executor_count",
            "event_count",
            "frame_count_endpoint_event_count",
            "executors_sha256",
            "executors",
        },
        label="performance bundle",
    )
    performance_executor = _one_document(
        performance.get("executors"), label="performance executors"
    )
    _exact(
        performance_executor,
        {
            "executor_order",
            "executor_id",
            "part_id",
            "runtime_binding",
            "performance_sha256",
            "performance_canonical_json_bytes",
            "event_count",
            "event_sidecar_sha256",
            "event_sidecar",
            "endpoint_dispatch_status",
            "performance",
        },
        label="performance executor",
    )
    performance_document = performance_executor.get("performance")
    sidecar = performance_executor.get("event_sidecar")
    events = (
        performance_document.get("events")
        if type(performance_document) is dict
        else None
    )
    runtime_binding = performance_executor.get("runtime_binding")
    if (
        performance.get("render_authority") is not False
        or performance.get("sample_rate") != sample_rate
        or performance.get("frame_count") != frame_count
        or performance.get("duration_seconds") != frame_count / sample_rate
        or performance.get("occurrence_count") != len(plan_occurrences)
        or performance.get("executor_count") != 1
        or performance.get("executors_sha256")
        != canonical_json_sha256(performance.get("executors"))
        or performance_executor.get("executor_order") != 0
        or performance_executor.get("executor_id") != receipt_executor["executor_id"]
        or performance_executor.get("part_id") != receipt_executor["part_id"]
        or type(performance_document) is not dict
        or performance_executor.get("performance_sha256")
        != canonical_json_sha256(performance_document)
        or performance_executor.get("performance_canonical_json_bytes")
        != len(canonical_json_bytes(performance_document))
        or type(sidecar) is not list
        or performance_executor.get("event_sidecar_sha256")
        != canonical_json_sha256(sidecar)
        or type(events) is not list
        or len(events) != len(sidecar)
        or performance_executor.get("event_count") != len(events)
        or performance.get("event_count") != len(events)
        or receipt_executor["event_count"] != len(events)
        or performance.get("frame_count_endpoint_event_count")
        != receipt_executor["endpoint_event_count"]
        or performance_document.get("channels") != 2
        or performance_document.get("sample_rate") != sample_rate
        or performance_document.get("duration_seconds") != frame_count / sample_rate
        or performance_document.get("tail_seconds") != 0.0
        or type(runtime_binding) is not dict
    ):
        _fail("identity_mismatch", "performance bundle semantics do not close")
    expected_runtime_binding = {
        key: runtime_executor.get(key)
        for key in (
            "manifest_source_sha256",
            "manifest_raw_sha256",
            "manifest_canonical_sha256",
            "capability_projection_sha256",
            "effective_manifest_canonical_sha256",
            "effective_manifest_sha256",
            "runtime_fingerprint_status",
            "legacy_runtime_fingerprint_sha256",
            "asset_inventory_status",
        )
    }
    expected_runtime_binding.update(
        {
            "render_python_closure_sha256": evidence.get(
                "render_python_closure_sha256"
            ),
            "runtime_dependencies_sha256": evidence.get(
                "runtime_dependencies_sha256"
            ),
            "runtime_asset_graph_sha256": graph.get("sha256"),
        }
    )
    if runtime_binding != expected_runtime_binding:
        _fail("identity_mismatch", "performance runtime binding is inconsistent")

    endpoint_count = 0
    scheduled: dict[tuple[str, str], int] = {}
    for sequence, (sidecar_event, event) in enumerate(zip(sidecar, events)):
        sidecar_event = _exact(
            sidecar_event,
            {"sequence", "occurrence_id", "role", "note_id", "expected_sample"},
            label="performance event sidecar entry",
        )
        expected_sample = sidecar_event.get("expected_sample")
        role = sidecar_event.get("role")
        occurrence_id = sidecar_event.get("occurrence_id")
        if (
            type(event) is not dict
            or sidecar_event.get("sequence") != sequence
            or type(expected_sample) is not int
            or not 0 <= expected_sample <= frame_count
            or role not in {"note_on", "note_off"}
            or occurrence_id not in capability_by_id
            or event.get("type") != role
            or event.get("note_id") != sidecar_event.get("note_id")
            or type(event.get("time")) not in {int, float}
            or abs(float(event["time"]) * sample_rate - expected_sample) > 1e-6
            or (occurrence_id, role) in scheduled
        ):
            _fail("invalid_document", "performance event schedule is inconsistent")
        scheduled[(occurrence_id, role)] = expected_sample
        if expected_sample == frame_count:
            endpoint_count += 1
    for occurrence_id, occurrence in capability_by_id.items():
        if (
            scheduled.get((occurrence_id, "note_on"))
            != occurrence.get("start_sample")
            or scheduled.get((occurrence_id, "note_off"))
            != occurrence.get("end_sample")
        ):
            _fail("identity_mismatch", "performance schedule disagrees with occurrences")
    if endpoint_count != receipt_executor["endpoint_event_count"]:
        _fail("identity_mismatch", "performance endpoint accounting is inconsistent")

    held_sources = acquisition.get("held_sources")
    loaded = acquisition.get("loaded_python_generation")
    projection = loaded.get("projection") if type(loaded) is dict else None
    roots = projection.get("roots") if type(projection) is dict else None
    if type(held_sources) is not list or type(roots) is not list:
        _fail("invalid_authority", "loaded Python generation is incomplete")
    held_by_path: dict[str, dict[str, Any]] = {}
    for source in held_sources:
        if type(source) is not dict or type(source.get("path")) is not str:
            _fail("invalid_authority", "held source evidence is invalid")
        if source["path"] in held_by_path:
            _fail("invalid_authority", "held source paths are duplicated")
        held_by_path[source["path"]] = source
    expected_root_labels = {
        "instrument.class",
        "instrument.init",
        "instrument.bind_factory",
        "instrument.manifest_hash",
        "events.pitch_hz",
        "oscillator.class",
        "oscillator.init",
        "oscillator.from_manifest",
        "oscillator.begin_release",
        "oscillator.handle_event",
        "oscillator.render_frame",
        "oscillator.active_voice_count",
        "oscillator.voice",
        "oscillator.voice_init",
        "oscillator.event_pitch_hz",
        "oscillator.math",
        "tuning.equal_temperament",
        "tuning.math",
        "tuning.note_to_hz",
    }
    observed_labels: set[str] = set()
    for root in roots:
        root = _exact(
            root,
            {"label", "kind", "module", "qualname", "code_sha256", "source"},
            label="loaded Python root",
        )
        source = root.get("source")
        label = root.get("label")
        if (
            type(label) is not str
            or label in observed_labels
            or root.get("kind") not in {"python_function", "python_object"}
            or type(source) is not dict
            or set(source) != {"path", "sha256"}
            or held_by_path.get(source.get("path"), {}).get("sha256")
            != source.get("sha256")
            or (
                root.get("code_sha256") is not None
                and not _sha256(root.get("code_sha256"), label="loaded code")
            )
        ):
            _fail("invalid_authority", "loaded Python root evidence is invalid")
        observed_labels.add(label)
    manifest_record = fingerprint.get("manifest")
    closure_files = closure.get("files") if type(closure) is dict else None
    closure_map = {
        item.get("path"): item.get("sha256")
        for item in closure_files
        if type(item) is dict
    } if type(closure_files) is list else {}
    manifest_path = manifest_record.get("path") if type(manifest_record) is dict else None
    if (
        observed_labels != expected_root_labels
        or type(projection) is not dict
        or set(projection) != {"algorithm", "roots", "runtime_dependencies"}
        or projection.get("algorithm")
        != "tianlai-loaded-python-object-projection-v1"
        or type(manifest_path) is not str
        or held_by_path.get(manifest_path, {}).get("sha256")
        != runtime_manifest_artifact.sha256
        or set(held_by_path) != set(closure_map) | {manifest_path}
        or any(
            held_by_path.get(path, {}).get("sha256") != digest
            for path, digest in closure_map.items()
        )
    ):
        _fail("invalid_authority", "held source and loaded code evidence disagree")


def validate_score_v2_candidate_generation(
    manifest: dict[str, Any],
    receipt: dict[str, Any],
    artifacts: Mapping[str, ScoreV2CandidateArtifact],
) -> None:
    """Validate the complete portable semantic closure of one v3 generation."""

    validate_score_v2_render_receipt(receipt)
    receipt_artifact = artifacts.get(SCORE_V2_RENDER_RECEIPT_NAME)
    if (
        receipt_artifact is None
        or receipt_artifact.sha256 != manifest["render_receipt"]["sha256"]
        or receipt_artifact.payload != canonical_json_bytes(receipt)
    ):
        _fail("hash_mismatch", "render receipt SHA-256 does not match")

    source_artifacts: dict[str, ScoreV2CandidateArtifact] = {}
    for role in ("score", "roster", "execution_profile"):
        source_artifacts[role] = _require_artifact_binding(
            artifacts,
            receipt["bindings"][role],
            label=role,
        )
    score_document = _document(artifacts, SCORE_V2_SOURCE_NAME)
    roster_document = _document(artifacts, SCORE_V2_ROSTER_NAME)
    profile_document = _document(artifacts, SCORE_V2_EXECUTION_PROFILE_NAME)
    try:
        score = parse_score_v2_document(score_document)
        profile = parse_score_v2_execution_profile(profile_document)
    except (TypeError, ValueError) as exc:
        raise ScoreV2CandidateError(
            "invalid_source", "Candidate-v3 source document is invalid"
        ) from exc
    if score.schema_version != 2:
        _fail("unsupported_scope", "Candidate-v3 score must use schema version 2")
    if source_artifacts["roster"].payload != canonical_json_bytes(roster_document):
        _fail("noncanonical_artifact", "roster projection must use canonical JSON bytes")
    plan = _require_canonical_generated_document(artifacts, receipt, "score_v2_plan")
    capability_source = _require_canonical_generated_document(
        artifacts, receipt, "capability_source"
    )
    capability_plan = _require_canonical_generated_document(
        artifacts, receipt, "capability_plan"
    )
    runtime_source = _require_canonical_generated_document(
        artifacts, receipt, "runtime_source"
    )
    runtime_authority = _require_canonical_generated_document(
        artifacts, receipt, "runtime_authority"
    )
    runtime_authority_acquisition = _require_canonical_generated_document(
        artifacts, receipt, "runtime_authority_acquisition"
    )
    runtime_manifest_artifact = _require_artifact_binding(
        artifacts,
        receipt["bindings"]["runtime_manifest"],
        label="runtime_manifest",
    )
    runtime_manifest = _document(artifacts, SCORE_V2_RUNTIME_MANIFEST_NAME)
    performance = _require_canonical_generated_document(
        artifacts, receipt, "performance_bundle"
    )

    score_hash = receipt["bindings"]["score"]["canonical_sha256"]
    roster_hash = receipt["bindings"]["roster"]["canonical_sha256"]
    profile_hash = receipt["bindings"]["execution_profile"]["canonical_sha256"]
    plan_hash = receipt["bindings"]["score_v2_plan"]["canonical_sha256"]
    capability_source_hash = receipt["bindings"]["capability_source"][
        "canonical_sha256"
    ]
    capability_plan_hash = receipt["bindings"]["capability_plan"][
        "canonical_sha256"
    ]
    runtime_source_hash = receipt["bindings"]["runtime_source"][
        "canonical_sha256"
    ]
    runtime_authority_hash = receipt["bindings"]["runtime_authority"][
        "canonical_sha256"
    ]
    runtime_authority_acquisition_hash = receipt["bindings"][
        "runtime_authority_acquisition"
    ]["canonical_sha256"]
    runtime_manifest_hash = receipt["bindings"]["runtime_manifest"][
        "canonical_sha256"
    ]
    performance_hash = receipt["bindings"]["performance_bundle"][
        "canonical_sha256"
    ]

    _validate_generated_semantic_core(
        score=score,
        profile=profile,
        plan=plan,
        capability_source=capability_source,
        capability_plan=capability_plan,
        runtime_source=runtime_source,
        runtime_manifest=runtime_manifest,
        runtime_manifest_artifact=runtime_manifest_artifact,
        acquisition=runtime_authority_acquisition,
        performance=performance,
        receipt=receipt,
    )

    plan_bindings = plan.get("bindings")
    capability_source_executors = capability_source.get("executor_bindings")
    capability_manifest_generations = capability_source.get(
        "manifest_generations"
    )
    capability_plan_bindings = capability_plan.get("bindings")
    runtime_bindings = runtime_source.get("bindings")
    runtime_executors = runtime_source.get("executors")
    authority_bindings = runtime_authority.get("bindings")
    authority_executor = runtime_authority.get("executor")
    authority_assets = runtime_authority.get("assets")
    authority_lifecycle = runtime_authority.get("lifecycle")
    authority_loaded = runtime_authority.get("loaded_python_generation")
    authority_limitations = runtime_authority.get("limitations")
    acquisition_bindings = runtime_authority_acquisition.get("bindings")
    acquisition_executor = runtime_authority_acquisition.get("executor")
    acquisition_loaded = runtime_authority_acquisition.get(
        "loaded_python_generation"
    )
    acquisition_sources = runtime_authority_acquisition.get("held_sources")
    acquisition_assets = runtime_authority_acquisition.get("assets")
    acquisition_limitations = runtime_authority_acquisition.get("limitations")
    performance_bindings = performance.get("bindings")
    performance_executors = performance.get("executors")
    receipt_executor = receipt["executor"]

    if (
        type(plan_bindings) is not dict
        or plan_bindings.get("source_document_sha256") != score_hash
        or plan.get("sample_rate") != receipt_executor["sample_rate"]
        or type(capability_source_executors) is not list
        or len(capability_source_executors) != 1
        or type(capability_manifest_generations) is not list
        or len(capability_manifest_generations) != 1
        or capability_source.get("roster_projection_sha256") != roster_hash
        or capability_source.get("roster_projection") != roster_document
        or type(capability_plan_bindings) is not dict
        or capability_plan_bindings.get("source_document_sha256") != score_hash
        or capability_plan_bindings.get("score_v2_plan_sha256") != plan_hash
        or capability_plan_bindings.get("execution_profile_sha256") != profile_hash
        or capability_plan_bindings.get("capability_source_sha256")
        != capability_source_hash
        or capability_plan_bindings.get("roster_projection_sha256") != roster_hash
        or type(runtime_bindings) is not dict
        or runtime_bindings.get("capability_plan_sha256") != capability_plan_hash
        or runtime_bindings.get("capability_source_sha256") != capability_source_hash
        or runtime_bindings.get("roster_projection_sha256") != roster_hash
        or runtime_bindings.get("sample_rate") != receipt_executor["sample_rate"]
        or type(runtime_executors) is not list
        or len(runtime_executors) != 1
        or runtime_source.get("executor_count") != 1
        or type(performance_bindings) is not dict
        or performance_bindings.get("score_v2_plan_sha256") != plan_hash
        or performance_bindings.get("capability_plan_sha256") != capability_plan_hash
        or performance_bindings.get("runtime_source_sha256") != runtime_source_hash
        or performance_bindings.get("source_document_sha256") != score_hash
        or performance_bindings.get("execution_profile_sha256") != profile_hash
        or performance_bindings.get("capability_source_sha256")
        != capability_source_hash
        or performance_bindings.get("roster_projection_sha256") != roster_hash
        or performance.get("sample_rate") != receipt_executor["sample_rate"]
        or performance.get("frame_count") != receipt_executor["frame_count"]
        or performance.get("executor_count") != 1
        or type(performance_executors) is not list
        or len(performance_executors) != 1
        or performance.get("endpoint_dispatch_status") != ENDPOINT_DISPATCH_STATUS
    ):
        _fail("identity_mismatch", "Score-v2 artifact hash chain does not close")

    source_executor = capability_source_executors[0]
    source_manifest_generation = capability_manifest_generations[0]
    runtime_executor = runtime_executors[0]
    performance_executor = performance_executors[0]
    if not all(type(item) is dict for item in (
        source_executor,
        source_manifest_generation,
        runtime_executor,
        performance_executor,
        authority_bindings,
        authority_executor,
        authority_assets,
        authority_lifecycle,
        authority_loaded,
        authority_limitations,
        acquisition_bindings,
        acquisition_executor,
        acquisition_loaded,
        acquisition_assets,
        acquisition_limitations,
    )):
        _fail("invalid_authority", "runtime executor/authority evidence is invalid")
    executor_id = receipt_executor["executor_id"]
    part_id = receipt_executor["part_id"]
    effective_manifest_hash = receipt_executor["effective_manifest_sha256"]
    if (
        source_executor.get("executor_order") != 0
        or source_executor.get("executor_id") != executor_id
        or source_executor.get("part_id") != part_id
        or source_executor.get("effective_manifest_sha256")
        != effective_manifest_hash
        or source_executor.get("overrides") != {}
        or source_executor.get("custom_implementation_blocked") is not False
        or source_manifest_generation.get("source_sha256")
        != source_executor.get("manifest_source_sha256")
        or source_manifest_generation.get("raw_sha256")
        != receipt["bindings"]["runtime_manifest"]["file_sha256"]
        or source_manifest_generation.get("manifest_canonical_sha256")
        != runtime_manifest_hash
        or canonical_json_sha256(runtime_manifest)
        != source_executor.get("effective_manifest_canonical_sha256")
        or factory_manifest_sha256(runtime_manifest) != effective_manifest_hash
        or runtime_manifest.get("type") != "oscillator"
        or runtime_manifest.get("implementation") is not None
        or runtime_executor.get("executor_order") != 0
        or runtime_executor.get("executor_id") != executor_id
        or runtime_executor.get("part_id") != part_id
        or runtime_executor.get("effective_manifest_sha256")
        != effective_manifest_hash
        or runtime_executor.get("asset_inventory_status")
        != NO_EXTERNAL_ASSET_INVENTORY_STATUS
        or performance_executor.get("executor_order") != 0
        or performance_executor.get("executor_id") != executor_id
        or performance_executor.get("part_id") != part_id
        or performance_executor.get("performance_sha256")
        != receipt_executor["performance_sha256"]
        or performance_executor.get("event_sidecar_sha256")
        != receipt_executor["event_sidecar_sha256"]
    ):
        _fail("identity_mismatch", "executor identity chain does not close")

    # A serialized authority is evidence of a consumed live lease only.  It
    # must never be accepted by a renderer as authority in its own right.
    expected_authority_executor = {
        "executor_order": 0,
        "executor_id": executor_id,
        "part_id": part_id,
    }
    expected_assets = {
        "policy": "no_external_audio_assets",
        "descriptor_count": 0,
        "descriptors": [],
        "inventory_status": NO_EXTERNAL_ASSET_INVENTORY_STATUS,
    }
    if (
        set(runtime_authority)
        != {
            "kind",
            "schema_version",
            "contract",
            "historical_evidence_only",
            "document_authority",
            "status",
            "bindings",
            "executor",
            "assets",
            "loaded_python_generation",
            "lifecycle",
            "factory_generation_sha256",
            "limitations",
        }
        or runtime_authority.get("historical_evidence_only") is not True
        or runtime_authority.get("document_authority") is not False
        or runtime_authority.get("status") != "consumed"
        or set(authority_bindings)
        != {
            "performance_bundle_sha256",
            "runtime_source_sha256",
            "capability_plan_sha256",
            "capability_source_sha256",
            "roster_projection_sha256",
            "effective_manifest_sha256",
            "manifest_raw_sha256",
            "sample_rate",
            "acquisition_sha256",
        }
        or authority_bindings.get("performance_bundle_sha256")
        != performance_hash
        or authority_bindings.get("capability_plan_sha256")
        != capability_plan_hash
        or authority_bindings.get("capability_source_sha256")
        != capability_source_hash
        or authority_bindings.get("runtime_source_sha256") != runtime_source_hash
        or authority_bindings.get("roster_projection_sha256") != roster_hash
        or authority_bindings.get("effective_manifest_sha256")
        != effective_manifest_hash
        or authority_bindings.get("manifest_raw_sha256")
        != receipt["bindings"]["runtime_manifest"]["file_sha256"]
        or authority_bindings.get("sample_rate") != receipt_executor["sample_rate"]
        or authority_bindings.get("acquisition_sha256")
        != runtime_authority_acquisition_hash
        or authority_executor != expected_authority_executor
        or authority_assets != expected_assets
        or set(authority_loaded) != {"projection_sha256", "held_source_count"}
        or not _sha256(
            authority_loaded.get("projection_sha256"),
            label="loaded Python projection",
        )
        or type(authority_loaded.get("held_source_count")) is not int
        or authority_loaded.get("held_source_count") < 1
        or authority_lifecycle
        != {
            "lease_consumed_once": True,
            "execution_retired_before_receipt": True,
            "source_descriptors_held_until_context_exit": True,
            "dispatched_event_count": receipt_executor["event_count"],
            "rendered_frame_count": receipt_executor["frame_count"],
        }
        or runtime_authority.get("factory_generation_sha256")
        != receipt_executor["factory_generation_sha256"]
        or authority_limitations
        != {
            "reusable_runtime_authority": False,
            "authorship_proof": False,
            "hostile_interpreter_resistance": False,
            "external_asset_support": False,
        }
    ):
        _fail("invalid_authority", "consumed runtime authority evidence is invalid")

    if (
        set(runtime_authority_acquisition)
        != {
            "kind",
            "schema_version",
            "contract",
            "document_authority",
            "active_lease_required",
            "bindings",
            "executor",
            "loaded_python_generation",
            "held_sources",
            "assets",
            "factory_generation_sha256",
            "limitations",
        }
        or runtime_authority_acquisition.get("document_authority") is not False
        or runtime_authority_acquisition.get("active_lease_required") is not True
        or set(acquisition_bindings)
        != {
            "performance_bundle_sha256",
            "runtime_source_sha256",
            "capability_plan_sha256",
            "capability_source_sha256",
            "roster_projection_sha256",
            "effective_manifest_sha256",
            "manifest_raw_sha256",
            "sample_rate",
        }
        or any(
            acquisition_bindings.get(key) != authority_bindings.get(key)
            for key in acquisition_bindings
        )
        or acquisition_executor != expected_authority_executor
        or acquisition_assets != expected_assets
        or type(acquisition_sources) is not list
        or len(acquisition_sources) != authority_loaded["held_source_count"]
        or not acquisition_sources
        or any(
            type(source) is not dict
            or set(source) != {"path", "sha256", "size_bytes"}
            or type(source.get("path")) is not str
            or not source["path"]
            or not _sha256(source.get("sha256"), label="held source")
            or type(source.get("size_bytes")) is not int
            or source["size_bytes"] < 1
            for source in acquisition_sources
        )
        or set(acquisition_loaded) != {"projection_sha256", "projection"}
        or canonical_json_sha256(acquisition_loaded.get("projection"))
        != acquisition_loaded.get("projection_sha256")
        or acquisition_loaded.get("projection_sha256")
        != authority_loaded.get("projection_sha256")
        or runtime_authority_acquisition.get("factory_generation_sha256")
        != receipt_executor["factory_generation_sha256"]
        or acquisition_limitations
        != {
            "transferable_authority": False,
            "candidate_authority": False,
            "sampled_backends_supported": False,
            "custom_factories_supported": False,
            "trusted_python_interpreter_required": True,
        }
    ):
        _fail("invalid_authority", "runtime authority acquisition evidence is invalid")

    if (
        runtime_authority_hash
        != receipt["bindings"]["runtime_authority"]["file_sha256"]
        or runtime_authority_acquisition_hash
        != receipt["bindings"]["runtime_authority_acquisition"][
            "file_sha256"
        ]
        or performance_hash
        != receipt["bindings"]["performance_bundle"]["file_sha256"]
    ):
        _fail("hash_mismatch", "canonical execution artifact hash is inconsistent")

    mix_artifact = artifacts.get(SCORE_V2_MIX_NAME)
    mix_binding = receipt["mix"]
    expected_size = 44 + 6 * receipt_executor["frame_count"]
    if (
        mix_artifact is None
        or mix_artifact.sha256 != mix_binding["sha256"]
        or mix_artifact.size_bytes != mix_binding["size_bytes"]
        or mix_artifact.size_bytes != expected_size
    ):
        _fail("invalid_audio", "PCM24 WAV size/hash contract does not close")
    import struct

    header = mix_artifact.prefix
    if type(header) is not bytes or len(header) < 44:
        _fail("invalid_audio", "PCM24 WAV header was not descriptor-captured")
    try:
        (
            riff,
            riff_size,
            wave_tag,
            fmt_tag,
            fmt_size,
            audio_code,
            channels,
            sample_rate,
            byte_rate,
            block_align,
            bits_per_sample,
            data_tag,
            data_size,
        ) = struct.unpack("<4sI4s4sIHHIIHH4sI", header[:44])
    except struct.error as exc:
        raise ScoreV2CandidateError(
            "invalid_audio", "PCM24 WAV header is malformed"
        ) from exc
    if (
        riff != b"RIFF"
        or riff_size != expected_size - 8
        or wave_tag != b"WAVE"
        or fmt_tag != b"fmt "
        or fmt_size != 16
        or audio_code != 1
        or channels != 2
        or sample_rate != receipt_executor["sample_rate"]
        or byte_rate != sample_rate * 6
        or block_align != 6
        or bits_per_sample != 24
        or data_tag != b"data"
        or data_size != receipt_executor["frame_count"] * 6
    ):
        _fail("invalid_audio", "PCM24 WAV header disagrees with the receipt")

    post_artifact = artifacts.get(SCORE_V2_POST_RENDER_CHECK_NAME)
    if (
        post_artifact is None
        or post_artifact.sha256 != receipt["post_render_check"]["sha256"]
    ):
        _fail("hash_mismatch", "post-render check SHA-256 does not match")
    post_document = _document(artifacts, SCORE_V2_POST_RENDER_CHECK_NAME)
    _exact(
        post_document,
        {
            "kind",
            "schema_version",
            "contract",
            "status",
            "bindings",
            "artifact",
            "audio_format",
            "observations",
            "summary",
            "limitations",
        },
        label="Score-v2 post-render check",
    )
    post_bindings = post_document.get("bindings")
    post_artifact_binding = post_document.get("artifact")
    post_audio = post_document.get("audio_format")
    post_observations = post_document.get("observations")
    post_summary = post_document.get("summary")
    post_limitations = post_document.get("limitations")
    if (
        post_document.get("kind") != SCORE_V2_POST_RENDER_CHECK_KIND
        or post_document.get("schema_version")
        != SCORE_V2_POST_RENDER_CHECK_SCHEMA_VERSION
        or post_document.get("contract") != SCORE_V2_POST_RENDER_CHECK_CONTRACT
        or post_document.get("status") != "pass"
        or type(post_bindings) is not dict
        or set(post_bindings)
        != {"performance_bundle_sha256", "runtime_authority_sha256"}
        or post_bindings.get("performance_bundle_sha256") != performance_hash
        or post_bindings.get("runtime_authority_sha256")
        != runtime_authority_hash
        or type(post_artifact_binding) is not dict
        or post_artifact_binding
        != {
            "path": SCORE_V2_MIX_NAME,
            "sha256": mix_binding["sha256"],
            "size_bytes": mix_binding["size_bytes"],
        }
        or type(post_audio) is not dict
        or post_audio
        != {
            "container": "WAV",
            "encoding": "PCM",
            "bits_per_sample": 24,
            "channels": 2,
            "sample_rate": receipt_executor["sample_rate"],
            "frame_count": receipt_executor["frame_count"],
        }
        or type(post_observations) is not dict
        or set(post_observations)
        != {
            "peak",
            "active_sample_count",
            "event_count",
            "endpoint_event_count",
        }
        or post_observations.get("peak") != mix_binding["peak"]
        or type(post_observations.get("active_sample_count")) is not int
        or post_observations["active_sample_count"] < 0
        or post_observations.get("event_count")
        != receipt_executor["event_count"]
        or post_observations.get("endpoint_event_count")
        != receipt_executor["endpoint_event_count"]
        or type(post_summary) is not dict
        or post_summary
        != {
            "can_proceed": True,
            "expected_activity": receipt_executor["event_count"] > 0,
            "observed_activity": post_observations.get("active_sample_count", 0)
            > 0,
        }
        or post_limitations
        != {
            "loudness_standard_measurement": "not_performed",
            "true_peak_measurement": "not_performed",
            "release_tail": "not_present",
            "source": "same_descriptor_stream_evidence",
        }
    ):
        _fail("invalid_postcheck", "post-render check bindings disagree")


def preflight_score_v2_candidate_compilation(
    compilation: ScoreV2ProjectRenderCompilation,
) -> None:
    """Reject Candidate-v3 JSON inputs before target I/O or audio rendering.

    Score compilation has a deliberately broader source-document budget than
    the portable candidate format.  This boundary therefore checks the raw
    bytes that would actually be published instead of waiting for the final
    closed-generation verifier to reject an oversized staging directory.
    """

    from .candidate import MAX_CANDIDATE_JSON_BYTES
    from .score_v2_project_render import ScoreV2ProjectRenderCompilation

    if type(compilation) is not ScoreV2ProjectRenderCompilation:
        raise TypeError(
            "compilation must be ScoreV2ProjectRenderCompilation"
        )
    compilation.revalidate_inputs()
    capability_source_document = compilation.capability_sources.to_dict()
    roster_document = capability_source_document.get("roster_projection")
    if type(roster_document) is not dict:
        raise ScoreV2CandidateError(
            "invalid_source", "compiled roster projection is invalid"
        )
    payloads = {
        SCORE_V2_SOURCE_NAME: compilation.inputs.score_file.source_bytes,
        SCORE_V2_ROSTER_NAME: canonical_json_bytes(roster_document),
        SCORE_V2_EXECUTION_PROFILE_NAME: (
            compilation.inputs.execution_profile_file.source_bytes
        ),
        SCORE_V2_PLAN_NAME: compilation.score_plan.canonical_bytes,
        SCORE_V2_CAPABILITY_SOURCE_NAME: (
            compilation.capability_sources.canonical_bytes
        ),
        SCORE_V2_CAPABILITY_PLAN_NAME: compilation.capability_plan.canonical_bytes,
        SCORE_V2_RUNTIME_SOURCE_NAME: compilation.runtime_sources.canonical_bytes,
        SCORE_V2_PERFORMANCE_BUNDLE_NAME: (
            compilation.performance_bundle.canonical_bytes
        ),
    }
    for name, payload in payloads.items():
        if type(payload) is not bytes or len(payload) > MAX_CANDIDATE_JSON_BYTES:
            raise ScoreV2CandidateError(
                "resource_limit",
                f"{name} exceeds the Candidate-v3 JSON byte limit",
            )


def publish_score_v2_candidate_metadata(
    target: object,
    *,
    title: str,
    compilation: ScoreV2ProjectRenderCompilation,
    generation: ScoreV2FormalRenderGeneration,
    parent_candidate_id: str | None = None,
) -> dict[str, Any]:
    """Publish one closed Candidate-v3 generation from two trusted handles.

    ``compilation`` supplies the fixed Score-v2 artifact chain and
    ``generation`` supplies the result of consuming its live runtime lease.
    The latter must still be active while publication completes.  Caller-made
    dictionaries, legacy local receipts and private WAV stages are not
    accepted as publication authority.
    """

    # Keep this module independent from the legacy candidate implementation at
    # import time.  The lazy import also avoids a circular module dependency.
    from .candidate import (
        CANDIDATE_FORMAT,
        CANDIDATE_MANIFEST_NAME,
        MAX_CANDIDATE_JSON_BYTES,
        CandidateTarget,
        _candidate_json_snapshot,
        validate_candidate_json_size,
    )
    from .candidate_integrity import verify_candidate_integrity
    from .atomic_publish import _publish_bytes_atomic
    from .render_lock import revalidate_plain_directory
    from .score_v2_formal_render import (
        SCORE_V2_FORMAL_RENDER_CONTRACT,
        ScoreV2FormalRenderGeneration,
    )
    from .score_v2_project_render import (
        SCORE_V2_PROJECT_RENDER_SCOPE,
        ScoreV2ProjectRenderCompilation,
    )
    from .utc_timestamp import canonical_utc_now

    if type(target) is not CandidateTarget:
        raise TypeError("target must be CandidateTarget")
    if type(title) is not str:
        raise TypeError("title must be a string")
    if type(compilation) is not ScoreV2ProjectRenderCompilation:
        raise TypeError(
            "compilation must be ScoreV2ProjectRenderCompilation"
        )
    if type(generation) is not ScoreV2FormalRenderGeneration:
        raise TypeError("generation must be ScoreV2FormalRenderGeneration")
    if parent_candidate_id is not None and (
        type(parent_candidate_id) is not str or not parent_candidate_id
    ):
        raise ValueError("parent_candidate_id must be non-empty or None")
    # This is intentionally before any target-directory revalidation or file
    # publication.  CLI callers also invoke the public preflight before they
    # acquire a render target, so an oversized source never consumes a runtime
    # lease merely to fail at candidate verification.
    preflight_score_v2_candidate_compilation(compilation)
    if target.work_directory_identity is not None:
        revalidate_plain_directory(target.work_directory_identity)
    directory = (
        revalidate_plain_directory(target.directory_identity)
        if target.directory_identity is not None
        else target.directory.resolve()
    )
    if directory != target.directory:
        raise ValueError("Candidate-v3 metadata target changed identity")

    def checkpoint() -> None:
        compilation.revalidate_inputs()
        generation.revalidate_generation()

    # First checkpoint: no pathname is changed unless both in-process
    # generations are still registered and every retained source is current.
    checkpoint()
    try:
        authority = generation.runtime_authority()
        acquisition = generation.runtime_authority_acquisition()
        postcheck = generation.post_render_check()
        capability_source_document = compilation.capability_sources.to_dict()
        roster_document = capability_source_document["roster_projection"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("formal Score-v2 generation evidence is invalid") from exc
    if type(roster_document) is not dict:
        raise ValueError("formal Score-v2 roster projection is invalid")

    authority_bindings = authority.get("bindings")
    acquisition_bindings = acquisition.get("bindings")
    expected_bindings = {
        "performance_bundle_sha256": (
            compilation.performance_bundle.artifact_sha256
        ),
        "runtime_source_sha256": compilation.runtime_sources.artifact_sha256,
        "capability_plan_sha256": compilation.capability_plan.artifact_sha256,
        "capability_source_sha256": (
            compilation.capability_sources.artifact_sha256
        ),
        "roster_projection_sha256": (
            compilation.capability_sources.roster_projection_sha256
        ),
        "effective_manifest_sha256": compilation.effective_manifest_sha256,
        "manifest_raw_sha256": generation.runtime_manifest_sha256,
        "sample_rate": compilation.sample_rate,
    }
    expected_executor = {
        "executor_order": 0,
        "executor_id": compilation.executor_id,
        "part_id": compilation.part_id,
    }
    if (
        generation.contract != SCORE_V2_FORMAL_RENDER_CONTRACT
        or compilation.scope != SCORE_V2_PROJECT_RENDER_SCOPE
        or Path(generation.mix_path) != directory / SCORE_V2_MIX_NAME
        or generation.sample_rate != compilation.sample_rate
        or generation.frame_count
        != compilation.performance_bundle.frame_count
        or generation.effective_manifest_sha256
        != compilation.effective_manifest_sha256
        or type(authority_bindings) is not dict
        or type(acquisition_bindings) is not dict
        or any(
            authority_bindings.get(key) != value
            or acquisition_bindings.get(key) != value
            for key, value in expected_bindings.items()
        )
        or authority_bindings.get("acquisition_sha256")
        != generation.runtime_authority_acquisition_sha256
        or authority.get("executor") != expected_executor
        or acquisition.get("executor") != expected_executor
        or authority.get("factory_generation_sha256")
        != generation.factory_generation_sha256
        or acquisition.get("factory_generation_sha256")
        != generation.factory_generation_sha256
        or postcheck.get("bindings")
        != {
            "performance_bundle_sha256": (
                compilation.performance_bundle.artifact_sha256
            ),
            "runtime_authority_sha256": generation.runtime_authority_sha256,
        }
        or postcheck.get("artifact")
        != {
            "path": SCORE_V2_MIX_NAME,
            "sha256": generation.mix_sha256,
            "size_bytes": generation.mix_size_bytes,
        }
    ):
        raise ValueError(
            "formal render generation does not bind the supplied compilation"
        )

    score_payload = compilation.inputs.score_file.source_bytes
    roster_payload = canonical_json_bytes(roster_document)
    profile_payload = compilation.inputs.execution_profile_file.source_bytes
    artifact_payloads = {
        SCORE_V2_SOURCE_NAME: score_payload,
        SCORE_V2_ROSTER_NAME: roster_payload,
        SCORE_V2_EXECUTION_PROFILE_NAME: profile_payload,
        SCORE_V2_PLAN_NAME: compilation.score_plan.canonical_bytes,
        SCORE_V2_CAPABILITY_SOURCE_NAME: (
            compilation.capability_sources.canonical_bytes
        ),
        SCORE_V2_CAPABILITY_PLAN_NAME: compilation.capability_plan.canonical_bytes,
        SCORE_V2_RUNTIME_SOURCE_NAME: compilation.runtime_sources.canonical_bytes,
        SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_NAME: (
            generation.runtime_authority_acquisition_canonical_bytes
        ),
        SCORE_V2_RUNTIME_AUTHORITY_NAME: (
            generation.runtime_authority_canonical_bytes
        ),
        SCORE_V2_RUNTIME_MANIFEST_NAME: generation.runtime_manifest_bytes,
        SCORE_V2_PERFORMANCE_BUNDLE_NAME: (
            compilation.performance_bundle.canonical_bytes
        ),
        SCORE_V2_POST_RENDER_CHECK_NAME: (
            generation.post_render_check_canonical_bytes
        ),
    }
    for name, payload in artifact_payloads.items():
        if type(payload) is not bytes or len(payload) > MAX_CANDIDATE_JSON_BYTES:
            raise ScoreV2CandidateError(
                "resource_limit",
                f"{name} exceeds the Candidate-v3 JSON byte limit",
            )
    for name, payload in artifact_payloads.items():
        _publish_bytes_atomic(directory / name, payload, overwrite=False)

    def captured_binding(role: str) -> dict[str, Any]:
        name = _RECEIPT_BINDING_NAMES[role]
        document, file_sha256 = _candidate_json_snapshot(
            directory / name,
            invalid_json_message=f"formal Score-v2 {role} is invalid JSON",
            map_read_error_to_invalid_json=True,
        )
        if type(document) is not dict:
            raise ValueError(f"formal Score-v2 {role} must be a JSON object")
        return {
            "path": name,
            "canonical_sha256": canonical_json_sha256(document),
            "file_sha256": file_sha256,
        }

    bindings = {
        role: captured_binding(role) for role in _RECEIPT_BINDING_NAMES
    }
    observed_postcheck, postcheck_file_sha256 = _candidate_json_snapshot(
        directory / SCORE_V2_POST_RENDER_CHECK_NAME,
        invalid_json_message="formal Score-v2 post-check is invalid JSON",
        map_read_error_to_invalid_json=True,
    )
    if (
        bindings["runtime_authority"]["canonical_sha256"]
        != generation.runtime_authority_sha256
        or bindings["runtime_authority_acquisition"]["canonical_sha256"]
        != generation.runtime_authority_acquisition_sha256
        or bindings["runtime_manifest"]["file_sha256"]
        != generation.runtime_manifest_sha256
        or observed_postcheck != postcheck
        or postcheck_file_sha256 != generation.post_render_check_sha256
    ):
        raise ValueError("formal Score-v2 written evidence changed identity")

    # Second checkpoint: the receipt is derived only while the consumed lease
    # remains retained by its still-active authority context.
    checkpoint()
    receipt: dict[str, Any] = {
        "kind": SCORE_V2_RENDER_RECEIPT_KIND,
        "schema_version": SCORE_V2_RENDER_RECEIPT_SCHEMA_VERSION,
        "contract": SCORE_V2_RENDER_RECEIPT_CONTRACT,
        "status": SCORE_V2_RENDER_RECEIPT_STATUS,
        "scope": {
            "executor_count": 1,
            "backend": "builtin_oscillator",
            "external_audio_assets": "none",
            "tail_samples": 0,
            "mix_policy": "single_executor_identity_no_gain",
            "normalization": "disabled",
            "space": "disabled",
            "stems": "not_written",
        },
        "bindings": bindings,
        "executor": {
            "executor_id": compilation.executor_id,
            "part_id": compilation.part_id,
            "authority_consumption_status": (
                "active_single_use_runtime_lease_consumed"
            ),
            "backend_scope": (
                "builtin_oscillator_manifest_route_declared_"
                "no_external_audio_assets"
            ),
            "effective_manifest_sha256": generation.effective_manifest_sha256,
            "factory_generation_sha256": generation.factory_generation_sha256,
            "performance_sha256": generation.performance_sha256,
            "event_sidecar_sha256": generation.event_sidecar_sha256,
            "sample_rate": generation.sample_rate,
            "frame_count": generation.frame_count,
            "block_count": generation.block_count,
            "event_count": generation.event_count,
            "endpoint_event_count": generation.endpoint_event_count,
            "peak_active_voices": generation.peak_active_voices,
            "float_stream_encoding": (
                "little_endian_float64_stereo_interleaved"
            ),
            "float_stream_sha256": generation.float_stream_sha256,
        },
        "audio_format": {
            "container": "WAV",
            "encoding": "PCM",
            "bits_per_sample": 24,
            "channels": 2,
            "sample_rate": generation.sample_rate,
            "pcm24_contract": "tianlai-pcm24-stereo-le-v1",
        },
        "mix": {
            "path": SCORE_V2_MIX_NAME,
            "sha256": generation.mix_sha256,
            "size_bytes": generation.mix_size_bytes,
            "frame_count": generation.frame_count,
            "float_stream_sha256": generation.float_stream_sha256,
            "peak": generation.peak,
        },
        "post_render_check": {
            "path": SCORE_V2_POST_RENDER_CHECK_NAME,
            "sha256": generation.post_render_check_sha256,
            "format": SCORE_V2_POST_RENDER_CHECK_KIND,
            "version": SCORE_V2_POST_RENDER_CHECK_SCHEMA_VERSION,
        },
        "limitations": {
            "authorship_verified": False,
            "provenance_verified": False,
            "live_tree_immutable_after_return": False,
            "runtime_authority_reusable": False,
            "external_assets_supported": False,
            "executor_count_limit": 1,
            "release_tail": "transport_frame_count_only_no_implicit_tail",
        },
    }
    validate_score_v2_render_receipt(receipt)
    validate_candidate_json_size(receipt, label="Candidate-v3 render receipt")
    receipt_file = directory / SCORE_V2_RENDER_RECEIPT_NAME
    _publish_bytes_atomic(
        receipt_file,
        canonical_json_bytes(receipt),
        overwrite=False,
    )
    observed_receipt, receipt_sha256 = _candidate_json_snapshot(
        receipt_file,
        invalid_json_message="formal Score-v2 receipt is invalid JSON",
        map_read_error_to_invalid_json=True,
    )
    if observed_receipt != receipt:
        raise ValueError("formal Score-v2 receipt changed while publishing")

    manifest: dict[str, Any] = {
        "format": CANDIDATE_FORMAT,
        "version": SCORE_V2_CANDIDATE_VERSION,
        "pipeline": {
            "kind": SCORE_V2_CANDIDATE_PIPELINE_KIND,
            "contract": SCORE_V2_CANDIDATE_CONTRACT,
            "score_schema_version": 2,
            "executor_count": 1,
        },
        "candidate_id": target.candidate_id,
        "work_id": target.work_id,
        "title": title,
        "created_at_utc": canonical_utc_now(),
        "parent_candidate_id": parent_candidate_id,
        "project": {
            "score": dict(receipt["bindings"]["score"]),
            "roster": dict(receipt["bindings"]["roster"]),
            "execution_profile": dict(
                receipt["bindings"]["execution_profile"]
            ),
            "score_v2_plan_sha256": receipt["bindings"]["score_v2_plan"]
            ["canonical_sha256"],
            "performance_bundle_sha256": receipt["bindings"]
            ["performance_bundle"]["canonical_sha256"],
        },
        "render_receipt": {
            "path": SCORE_V2_RENDER_RECEIPT_NAME,
            "sha256": receipt_sha256,
            "kind": SCORE_V2_RENDER_RECEIPT_KIND,
            "schema_version": SCORE_V2_RENDER_RECEIPT_SCHEMA_VERSION,
        },
    }
    # Third checkpoint: no manifest can confer Candidate status after the live
    # authority context that backed this render generation has gone inactive.
    checkpoint()
    validate_score_v2_candidate_manifest(
        manifest,
        expected_work_id=target.work_id,
        expected_candidate_id=target.candidate_id,
    )
    validate_candidate_json_size(manifest, label="Candidate-v3 manifest")
    _publish_bytes_atomic(
        directory / CANDIDATE_MANIFEST_NAME,
        canonical_json_bytes(manifest),
        overwrite=False,
    )
    verify_candidate_integrity(
        directory,
        expected_work_id=target.work_id,
        expected_candidate_id=target.candidate_id,
    )
    return manifest


__all__ = [
    "SCORE_V2_CANDIDATE_CONTRACT",
    "SCORE_V2_CANDIDATE_JSON_NAMES",
    "SCORE_V2_CANDIDATE_PIPELINE_KIND",
    "SCORE_V2_CANDIDATE_VERSION",
    "SCORE_V2_CAPABILITY_PLAN_NAME",
    "SCORE_V2_CAPABILITY_SOURCE_NAME",
    "SCORE_V2_EXECUTION_PROFILE_NAME",
    "SCORE_V2_MIX_NAME",
    "SCORE_V2_PERFORMANCE_BUNDLE_NAME",
    "SCORE_V2_PLAN_NAME",
    "SCORE_V2_POST_RENDER_CHECK_NAME",
    "SCORE_V2_POST_RENDER_CHECK_CONTRACT",
    "SCORE_V2_POST_RENDER_CHECK_KIND",
    "SCORE_V2_POST_RENDER_CHECK_SCHEMA_VERSION",
    "SCORE_V2_RENDER_RECEIPT_CONTRACT",
    "SCORE_V2_RENDER_RECEIPT_KIND",
    "SCORE_V2_RENDER_RECEIPT_NAME",
    "SCORE_V2_RENDER_RECEIPT_SCHEMA_VERSION",
    "SCORE_V2_RENDER_RECEIPT_STATUS",
    "SCORE_V2_ROSTER_NAME",
    "SCORE_V2_RUNTIME_AUTHORITY_CONTRACT",
    "SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_NAME",
    "SCORE_V2_RUNTIME_AUTHORITY_KIND",
    "SCORE_V2_RUNTIME_AUTHORITY_NAME",
    "SCORE_V2_RUNTIME_AUTHORITY_SCHEMA_VERSION",
    "SCORE_V2_RUNTIME_MANIFEST_NAME",
    "SCORE_V2_RUNTIME_SOURCE_NAME",
    "SCORE_V2_SOURCE_NAME",
    "ScoreV2CandidateArtifact",
    "ScoreV2CandidateError",
    "preflight_score_v2_candidate_compilation",
    "score_v2_candidate_expected_files",
    "publish_score_v2_candidate_metadata",
    "validate_score_v2_candidate_generation",
    "validate_score_v2_candidate_manifest",
    "validate_score_v2_render_receipt",
]
