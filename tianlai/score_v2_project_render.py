"""Descriptor-bound Score-v2 project inputs and fixed compilation chain.

This module is the non-rendering front half of ``project-render-v2``.  It
captures the three creator-controlled JSON files through one descriptor each,
resolves one formal roster against a source-workspace catalogue, and compiles
the complete Score-v2 plan/capability/runtime/performance chain.  The returned
artifact is deliberately *not* runtime, render, publish, or candidate
authority; a later live runtime lease must consume its performance bundle.

The first executable slice is intentionally narrow: one complete score part,
one formal roster executor, and the built-in oscillator route with an explicit
no-external-audio-assets declaration.  Migration wrappers, legacy tail
settings, and migration performance facts are never consumed implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, NamedTuple
import weakref

from .authoring_json import (
    AuthoringJsonError,
    AuthoringJsonLimits,
    bounded_canonical_json_bytes,
    strict_json_loads,
)
from .canonical_json import canonical_json_bytes
from .capability import load_capabilities
from .plain_file import PlainFileIdentity, read_plain_file_bytes
from .preflight import enforce_roster_availability
from .render_lock import (
    PlainDirectoryIdentity,
    capture_plain_directory,
    revalidate_plain_directory,
)
from .resource_limits import ProjectLimits, ResourceLimitError
from .roster import Roster, parse_roster_document
from .score_source import ScoreSourceSnapshot, snapshot_score_bytes
from .score_v2 import Rational, ScoreV2Document
from .score_v2_capability_adapter import (
    ScoreV2CapabilityPlan,
    compile_score_v2_capability_plan,
)
from .score_v2_capability_source import (
    DEFAULT_MAX_MANIFEST_BYTES,
    ScoreV2CapabilitySourceError,
    ScoreV2CapabilitySourceSnapshot,
    capture_score_v2_capability_sources,
)
from .score_v2_execution_profile import (
    HARD_MAX_EXECUTION_PROFILE_JSON_BYTES,
    ScoreV2ExecutionProfile,
    parse_score_v2_execution_profile,
)
from .score_v2_performance import (
    ScoreV2PerformanceBundle,
    compile_score_v2_performance_bundle,
)
from .score_v2_plan import ScoreV2Plan, compile_score_v2_plan
from .score_v2_runtime_source import (
    NO_EXTERNAL_ASSET_INVENTORY_STATUS,
    ScoreV2RuntimeSourceSnapshot,
    capture_score_v2_runtime_sources,
)


SCORE_V2_PROJECT_INPUT_KIND = "tianlai.score_v2_project_inputs"
SCORE_V2_PROJECT_INPUT_SCHEMA_VERSION = 1
SCORE_V2_PROJECT_INPUT_CONTRACT = (
    "score-v2-project-inputs-v1-descriptor-bound-not-render-authority"
)
SCORE_V2_PROJECT_RENDER_COMPILATION_KIND = (
    "tianlai.score_v2_project_render_compilation"
)
SCORE_V2_PROJECT_RENDER_COMPILATION_SCHEMA_VERSION = 1
SCORE_V2_PROJECT_RENDER_COMPILATION_CONTRACT = (
    "score-v2-project-render-compilation-v1-not-render-authority"
)
SCORE_V2_PROJECT_RENDER_SCOPE = (
    "single_executor_builtin_oscillator_declared_no_external_audio_assets_v1"
)
SCORE_V2_PROJECT_RENDER_STATUS = (
    "ready_for_live_runtime_lease_not_render_or_publish_authority"
)

_PACKAGE_SOURCE_ROOT = Path(__file__).resolve().parent
_HEX = frozenset("0123456789abcdef")
_INPUT_LIMITATIONS = {
    "capture_atomicity": "three_sequential_descriptor_bound_file_generations",
    "cross_file_atomicity": "not_claimed",
    "ordinary_replacement": "rejected_when_revalidated",
    "malicious_aba_resistance": "not_claimed",
    "migration_bundle": "not_accepted",
    "legacy_tail_settings": "not_accepted",
    "performance_facts": "not_accepted",
}
_COMPILATION_LIMITATIONS = {
    "runtime_authority": "not_acquired_live_runtime_lease_required",
    "render_authority": "not_granted",
    "publish_authority": "not_granted",
    "candidate_authority": "not_granted",
    "executor_scope": SCORE_V2_PROJECT_RENDER_SCOPE,
    "release_tail": "transport_frame_count_only_no_implicit_tail",
}


class ScoreV2ProjectRenderError(ValueError):
    """Stable failure at the first formal Score-v2 project boundary."""

    def __init__(
        self,
        code: str,
        *,
        actual: int | None = None,
        limit: int | None = None,
    ) -> None:
        self.code = code
        self.message_key = f"scoreV2ProjectRender.{code.replace('.', '_')}"
        self.actual = actual
        self.limit = limit
        super().__init__(code)


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _active_limits(limits: ProjectLimits | None) -> ProjectLimits:
    if limits is None:
        return ProjectLimits.from_environment()
    if type(limits) is not ProjectLimits:
        raise TypeError("limits must be ProjectLimits or None")
    values = {
        name: getattr(limits, name)
        for name in ProjectLimits.__dataclass_fields__
    }
    if any(type(value) is not int or value <= 0 for value in values.values()):
        raise ValueError("ProjectLimits fields must retain positive integers")
    return ProjectLimits(**values)


def _json_limits(maximum_bytes: int, *, limits: ProjectLimits) -> AuthoringJsonLimits:
    return AuthoringJsonLimits(
        max_document_bytes=maximum_bytes,
        max_depth=128,
        max_nodes=max(
            1_000_000,
            limits.max_notes * 12,
            limits.max_executors * 64,
        ),
        max_string_bytes=min(maximum_bytes, 4 * 1024 * 1024),
        max_array_items=max(
            limits.max_notes,
            limits.max_executors,
            65_536,
        ),
        max_object_members=65_536,
    )


def _directory_dict(identity: PlainDirectoryIdentity) -> dict[str, str]:
    return {
        "path": str(identity.path),
        "device": str(identity.device),
        "inode": str(identity.inode),
    }


def _file_identity_dict(identity: PlainFileIdentity) -> dict[str, object]:
    return {
        "path": str(identity.path),
        "device": str(identity.device),
        "inode": str(identity.inode),
        "size": identity.size,
        "modified_ns": str(identity.modified_ns),
        "changed_ns": str(identity.changed_ns),
        "parent": _directory_dict(identity.parent_identity),
    }


class ScoreV2ProjectInputFile(NamedTuple):
    """One exact source-file generation retained by the project snapshot."""

    role: str
    identity: PlainFileIdentity
    source_bytes: bytes
    source_sha256: str
    canonical_bytes: bytes
    canonical_sha256: str
    maximum_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "file_identity": _file_identity_dict(self.identity),
            "source_bytes_size": len(self.source_bytes),
            "source_sha256": self.source_sha256,
            "canonical_bytes_size": len(self.canonical_bytes),
            "canonical_sha256": self.canonical_sha256,
        }


def _capture_json_object(
    path: str | os.PathLike[str],
    *,
    role: str,
    maximum_bytes: int,
    limits: ProjectLimits,
) -> tuple[ScoreV2ProjectInputFile, dict[str, Any]]:
    try:
        identity, source_bytes = read_plain_file_bytes(
            path,
            maximum_bytes=maximum_bytes,
        )
    except OSError as exc:
        raise ScoreV2ProjectRenderError(
            f"project_render_v2.{role}_source_unavailable"
        ) from exc
    active_json_limits = _json_limits(maximum_bytes, limits=limits)
    try:
        document = strict_json_loads(
            source_bytes,
            limits=active_json_limits,
            require_object=True,
            require_js_safe_integers=True,
        )
        canonical_bytes_value = bounded_canonical_json_bytes(
            document,
            limits=active_json_limits,
            require_object=True,
            require_js_safe_integers=True,
        )
    except AuthoringJsonError as exc:
        raise ScoreV2ProjectRenderError(
            f"project_render_v2.{role}_json.{exc.code}",
            actual=exc.actual,
            limit=exc.limit,
        ) from exc
    assert type(document) is dict
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    canonical_sha256_value = hashlib.sha256(
        canonical_bytes_value
    ).hexdigest()
    return (
        ScoreV2ProjectInputFile(
            role=role,
            identity=identity,
            source_bytes=source_bytes,
            source_sha256=source_sha256,
            canonical_bytes=canonical_bytes_value,
            canonical_sha256=canonical_sha256_value,
            maximum_bytes=maximum_bytes,
        ),
        document,
    )


def _validate_file_value(value: object, *, role: str) -> ScoreV2ProjectInputFile:
    if type(value) is not ScoreV2ProjectInputFile:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.input_artifact_integrity_mismatch"
        )
    if (
        value.role != role
        or type(value.identity) is not PlainFileIdentity
        or type(value.source_bytes) is not bytes
        or not value.source_bytes
        or not _is_sha256(value.source_sha256)
        or hashlib.sha256(value.source_bytes).hexdigest()
        != value.source_sha256
        or type(value.canonical_bytes) is not bytes
        or not value.canonical_bytes
        or not _is_sha256(value.canonical_sha256)
        or hashlib.sha256(value.canonical_bytes).hexdigest()
        != value.canonical_sha256
        or type(value.maximum_bytes) is not int
        or value.maximum_bytes < 1
        or len(value.source_bytes) > value.maximum_bytes
    ):
        raise ScoreV2ProjectRenderError(
            "project_render_v2.input_artifact_integrity_mismatch"
        )
    return value


def _revalidate_file(value: ScoreV2ProjectInputFile) -> None:
    try:
        current_identity, current_bytes = read_plain_file_bytes(
            value.identity.path,
            maximum_bytes=value.maximum_bytes,
        )
    except OSError as exc:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.input_generation_changed"
        ) from exc
    if (
        current_identity != value.identity
        or current_bytes != value.source_bytes
        or hashlib.sha256(current_bytes).hexdigest() != value.source_sha256
    ):
        raise ScoreV2ProjectRenderError(
            "project_render_v2.input_generation_changed"
        )


class _InputSnapshotGeneration(NamedTuple):
    score_file: ScoreV2ProjectInputFile
    roster_file: ScoreV2ProjectInputFile
    execution_profile_file: ScoreV2ProjectInputFile
    score_source: ScoreSourceSnapshot
    roster: Roster
    execution_profile: ScoreV2ExecutionProfile
    roster_projection_bytes: bytes
    roster_projection_sha256: str
    sample_rate: int
    project_root: PlainDirectoryIdentity
    catalogue_root: PlainDirectoryIdentity
    package_root: PlainDirectoryIdentity
    limits: ProjectLimits
    canonical_bytes: bytes
    artifact_sha256: str


_INPUT_GENERATIONS: dict[
    int,
    tuple[
        weakref.ReferenceType[object],
        _InputSnapshotGeneration,
    ],
] = {}


def _input_artifact_document(
    generation: _InputSnapshotGeneration,
) -> dict[str, object]:
    return {
        "kind": SCORE_V2_PROJECT_INPUT_KIND,
        "schema_version": SCORE_V2_PROJECT_INPUT_SCHEMA_VERSION,
        "contract": SCORE_V2_PROJECT_INPUT_CONTRACT,
        "render_authority": False,
        "publish_authority": False,
        "sample_rate": generation.sample_rate,
        "layout": {
            "project_root": _directory_dict(generation.project_root),
            "catalogue_root": _directory_dict(generation.catalogue_root),
            "package_root": _directory_dict(generation.package_root),
        },
        "inputs": {
            "score": generation.score_file.to_dict(),
            "roster": generation.roster_file.to_dict(),
            "execution_profile": generation.execution_profile_file.to_dict(),
        },
        "bindings": {
            "score_document_sha256": (
                generation.score_source.document_sha256
            ),
            "roster_projection_sha256": (
                generation.roster_projection_sha256
            ),
            "execution_profile_sha256": (
                generation.execution_profile.artifact_sha256
            ),
        },
        "resource_limits": generation.limits.to_dict(),
        "limitations": dict(_INPUT_LIMITATIONS),
    }


@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class ScoreV2ProjectInputSnapshot:
    """Three descriptor-bound creator inputs plus one resolved formal roster."""

    score_file: ScoreV2ProjectInputFile
    roster_file: ScoreV2ProjectInputFile
    execution_profile_file: ScoreV2ProjectInputFile
    score_source: ScoreSourceSnapshot
    roster: Roster
    execution_profile: ScoreV2ExecutionProfile
    roster_projection_sha256: str
    sample_rate: int
    project_root: Path
    catalogue_root: Path
    limits: ProjectLimits
    _canonical_bytes: bytes
    _artifact_sha256: str

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ScoreV2ProjectInputSnapshot cannot be subclassed")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "ScoreV2ProjectInputSnapshot must be created by "
            "capture_score_v2_project_inputs"
        )

    def _trusted_generation(self) -> _InputSnapshotGeneration:
        registered = _INPUT_GENERATIONS.get(id(self))
        try:
            if registered is None or registered[0]() is not self:
                raise ValueError
            generation = registered[1]
            score_file = _validate_file_value(
                generation.score_file,
                role="score",
            )
            roster_file = _validate_file_value(
                generation.roster_file,
                role="roster",
            )
            profile_file = _validate_file_value(
                generation.execution_profile_file,
                role="execution_profile",
            )
            if (
                self.score_file is not score_file
                or self.roster_file is not roster_file
                or self.execution_profile_file is not profile_file
                or self.score_source is not generation.score_source
                or self.roster is not generation.roster
                or self.execution_profile is not generation.execution_profile
                or self.roster_projection_sha256
                != generation.roster_projection_sha256
                or self.sample_rate != generation.sample_rate
                or self.project_root != generation.project_root.path
                or self.catalogue_root != generation.catalogue_root.path
                or self.limits is not generation.limits
                or self._canonical_bytes is not generation.canonical_bytes
                or self._artifact_sha256 != generation.artifact_sha256
                or type(generation.score_source) is not ScoreSourceSnapshot
                or generation.score_source.canonical_bytes
                != score_file.canonical_bytes
                or generation.score_source.document_sha256
                != score_file.canonical_sha256
                or type(generation.score_source.score) is not ScoreV2Document
                or type(generation.roster) is not Roster
                or canonical_json_bytes(Roster.to_dict(generation.roster))
                != generation.roster_projection_bytes
                or hashlib.sha256(generation.roster_projection_bytes).hexdigest()
                != generation.roster_projection_sha256
                or type(generation.execution_profile)
                is not ScoreV2ExecutionProfile
                or generation.execution_profile.artifact_sha256
                != hashlib.sha256(
                    generation.execution_profile.canonical_bytes
                ).hexdigest()
                or type(generation.sample_rate) is not int
                or not 8_000 <= generation.sample_rate <= 384_000
                or type(generation.project_root) is not PlainDirectoryIdentity
                or type(generation.catalogue_root) is not PlainDirectoryIdentity
                or type(generation.package_root) is not PlainDirectoryIdentity
                or type(generation.limits) is not ProjectLimits
                or type(generation.canonical_bytes) is not bytes
                or not _is_sha256(generation.artifact_sha256)
                or hashlib.sha256(generation.canonical_bytes).hexdigest()
                != generation.artifact_sha256
                or canonical_json_bytes(_input_artifact_document(generation))
                != generation.canonical_bytes
            ):
                raise ValueError
            return generation
        except (
            AttributeError,
            TypeError,
            ValueError,
        ) as exc:
            raise ScoreV2ProjectRenderError(
                "project_render_v2.input_artifact_integrity_mismatch"
            ) from exc

    @property
    def canonical_bytes(self) -> bytes:
        return self._trusted_generation().canonical_bytes

    @property
    def artifact_sha256(self) -> str:
        return self._trusted_generation().artifact_sha256

    def to_dict(self) -> dict[str, object]:
        try:
            value = json.loads(self._trusted_generation().canonical_bytes)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScoreV2ProjectRenderError(
                "project_render_v2.input_artifact_integrity_mismatch"
            ) from exc
        if type(value) is not dict:
            raise ScoreV2ProjectRenderError(
                "project_render_v2.input_artifact_integrity_mismatch"
            )
        return value

    def _source_document_copy(self, role: str) -> dict[str, Any]:
        generation = self._trusted_generation()
        source = {
            "score": generation.score_file,
            "roster": generation.roster_file,
            "execution_profile": generation.execution_profile_file,
        }.get(role)
        if source is None:
            raise ScoreV2ProjectRenderError(
                "project_render_v2.input_artifact_integrity_mismatch"
            )
        try:
            value = json.loads(source.canonical_bytes)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScoreV2ProjectRenderError(
                "project_render_v2.input_artifact_integrity_mismatch"
            ) from exc
        if type(value) is not dict:
            raise ScoreV2ProjectRenderError(
                "project_render_v2.input_artifact_integrity_mismatch"
            )
        return value

    def score_document_copy(self) -> dict[str, Any]:
        """Return a detached copy of the retained direct Score-v2 document."""

        return self._source_document_copy("score")

    def roster_document_copy(self) -> dict[str, Any]:
        """Return a detached copy of the retained authored formal roster."""

        return self._source_document_copy("roster")

    def execution_profile_document_copy(self) -> dict[str, Any]:
        """Return a detached copy of the retained execution-profile input."""

        return self._source_document_copy("execution_profile")

    def revalidate_inputs(self) -> None:
        """Fail unless all three paths still name the captured generations."""

        generation = self._trusted_generation()
        try:
            project = revalidate_plain_directory(generation.project_root)
            catalogue = revalidate_plain_directory(generation.catalogue_root)
            package = revalidate_plain_directory(generation.package_root)
        except OSError as exc:
            raise ScoreV2ProjectRenderError(
                "project_render_v2.input_generation_changed"
            ) from exc
        if (
            catalogue.parent != project
            or package != project / "tianlai"
        ):
            raise ScoreV2ProjectRenderError(
                "project_render_v2.input_generation_changed"
            )
        for value in (
            generation.score_file,
            generation.roster_file,
            generation.execution_profile_file,
        ):
            _revalidate_file(value)
        try:
            revalidate_plain_directory(generation.package_root)
            revalidate_plain_directory(generation.catalogue_root)
            revalidate_plain_directory(generation.project_root)
        except OSError as exc:
            raise ScoreV2ProjectRenderError(
                "project_render_v2.input_generation_changed"
            ) from exc


def _register_input_snapshot(
    snapshot: ScoreV2ProjectInputSnapshot,
    generation: _InputSnapshotGeneration,
) -> None:
    snapshot_id = id(snapshot)

    def retire(
        reference: weakref.ReferenceType[object],
        *,
        expected_id: int = snapshot_id,
    ) -> None:
        current = _INPUT_GENERATIONS.get(expected_id)
        if current is not None and current[0] is reference:
            _INPUT_GENERATIONS.pop(expected_id, None)

    reference = weakref.ref(snapshot, retire)
    _INPUT_GENERATIONS[snapshot_id] = (reference, generation)


def _score_scope_diagnostic(document: dict[str, Any]) -> None:
    if document.get("kind") == "tianlai.score_v2_migration":
        raise ScoreV2ProjectRenderError(
            "project_render_v2.migration_bundle_not_supported"
        )
    if document.get("kind") != "tianlai.score" or document.get(
        "schema_version"
    ) != 2:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.direct_score_v2_required"
        )
    if "tail_seconds" in document:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.tail_not_supported"
        )
    if "performance_facts" in document:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.performance_facts_not_supported"
        )
    if "render_settings" in document:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.render_settings_not_supported"
        )


def _formal_roster_shape(document: dict[str, Any]) -> None:
    assignments = document.get("assignments")
    if type(assignments) is not list or not assignments:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.formal_roster_required"
        )
    if len(assignments) != 1:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.single_executor_required"
        )
    assignment = assignments[0]
    if type(assignment) is not dict:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.formal_roster_required"
        )
    if "kit" in assignment:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.kit_roster_not_supported"
        )
    if type(assignment.get("instrument")) is not str or not assignment[
        "instrument"
    ].strip():
        raise ScoreV2ProjectRenderError(
            "project_render_v2.formal_roster_required"
        )
    dropped = document.get("drop_parts", [])
    if type(dropped) is list and dropped:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.dropped_parts_not_supported"
        )


def _capture_layout(
    catalogue_root: str | os.PathLike[str],
    project_root: str | os.PathLike[str] | None,
) -> tuple[
    PlainDirectoryIdentity,
    PlainDirectoryIdentity,
    PlainDirectoryIdentity,
]:
    try:
        requested_catalogue = Path(
            os.path.abspath(os.fspath(catalogue_root))
        )
        requested_project = (
            requested_catalogue.parent
            if project_root is None
            else Path(os.path.abspath(os.fspath(project_root)))
        )
        project_identity = capture_plain_directory(requested_project)
        project = revalidate_plain_directory(project_identity)
        catalogue_identity = capture_plain_directory(requested_catalogue)
        catalogue = revalidate_plain_directory(catalogue_identity)
        package_identity = capture_plain_directory(project / "tianlai")
        package = revalidate_plain_directory(package_identity)
    except (OSError, TypeError, ValueError) as exc:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.runtime_layout_unsupported"
        ) from exc
    try:
        expected_package = _PACKAGE_SOURCE_ROOT.resolve(strict=True)
    except OSError as exc:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.runtime_layout_unsupported"
        ) from exc
    if (
        catalogue.parent != project
        or package != expected_package
        or package != project / "tianlai"
    ):
        raise ScoreV2ProjectRenderError(
            "project_render_v2.runtime_layout_unsupported"
        )
    return project_identity, catalogue_identity, package_identity


def _require_distinct_inputs(
    values: tuple[ScoreV2ProjectInputFile, ...],
) -> None:
    paths = [value.identity.path for value in values]
    keys = [(value.identity.device, value.identity.inode) for value in values]
    if len(set(paths)) != len(paths) or len(set(keys)) != len(keys):
        raise ScoreV2ProjectRenderError(
            "project_render_v2.input_paths_must_be_distinct"
        )


def capture_score_v2_project_inputs(
    score_path: str | os.PathLike[str],
    roster_path: str | os.PathLike[str],
    execution_profile_path: str | os.PathLike[str],
    *,
    sample_rate: int,
    catalogue_root: str | os.PathLike[str],
    project_root: str | os.PathLike[str] | None = None,
    limits: ProjectLimits | None = None,
) -> ScoreV2ProjectInputSnapshot:
    """Capture and resolve the three required project-render-v2 inputs."""

    if type(sample_rate) is not int or not 8_000 <= sample_rate <= 384_000:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.sample_rate_invalid"
        )
    active_limits = _active_limits(limits)
    score_file, score_document = _capture_json_object(
        score_path,
        role="score",
        maximum_bytes=active_limits.max_score_json_bytes,
        limits=active_limits,
    )
    _score_scope_diagnostic(score_document)
    roster_file, roster_document = _capture_json_object(
        roster_path,
        role="roster",
        maximum_bytes=active_limits.max_plan_json_bytes,
        limits=active_limits,
    )
    _formal_roster_shape(roster_document)
    profile_maximum = min(
        active_limits.max_plan_json_bytes,
        HARD_MAX_EXECUTION_PROFILE_JSON_BYTES,
    )
    profile_file, _profile_document = _capture_json_object(
        execution_profile_path,
        role="execution_profile",
        maximum_bytes=profile_maximum,
        limits=active_limits,
    )
    _require_distinct_inputs((score_file, roster_file, profile_file))
    project_identity, catalogue_identity, package_identity = _capture_layout(
        catalogue_root,
        project_root,
    )
    try:
        score_source = snapshot_score_bytes(
            score_file.source_bytes,
            limits=active_limits,
        )
    except (ResourceLimitError, TypeError, ValueError):
        raise
    if type(score_source.score) is not ScoreV2Document:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.direct_score_v2_required"
        )
    try:
        execution_profile = parse_score_v2_execution_profile(
            profile_file.source_bytes,
            max_document_bytes=profile_maximum,
        )
    except (TypeError, ValueError):
        raise

    # Loading the catalogue is deliberately followed by exact revalidation of
    # all three creator files.  Only selected manifests become later authority
    # through ``capture_score_v2_capability_sources``.
    for value in (score_file, roster_file, profile_file):
        _revalidate_file(value)
    try:
        capabilities = load_capabilities(catalogue_identity.path)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.catalogue_load_failed"
        ) from exc
    for value in (score_file, roster_file, profile_file):
        _revalidate_file(value)
    try:
        roster = parse_roster_document(roster_document, capabilities)
        enforce_roster_availability(roster)
    except (KeyError, TypeError, ValueError) as exc:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.formal_roster_invalid"
        ) from exc
    if len(roster.executors) != 1:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.single_executor_required"
        )
    if roster.dropped_parts:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.dropped_parts_not_supported"
        )
    raw_reference = str(roster_document["assignments"][0]["instrument"])
    if raw_reference.strip().strip("/") != roster.executors[0].capability.relative_path:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.roster_reference_must_be_catalogue_relative"
        )
    score = score_source.score
    assert type(score) is ScoreV2Document
    if len(score.parts) != 1:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.single_executor_required"
        )
    if score.parts[0].part_id != roster.executors[0].part_id:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.formal_roster_invalid"
        )
    roster_projection_bytes = canonical_json_bytes(Roster.to_dict(roster))
    if len(roster_projection_bytes) > active_limits.max_plan_json_bytes:
        raise ResourceLimitError(
            "project_render_v2.roster_projection_too_large",
            "formal roster projection exceeds max_plan_json_bytes",
            actual=len(roster_projection_bytes),
            limit=active_limits.max_plan_json_bytes,
        )
    roster_projection_sha256 = hashlib.sha256(
        roster_projection_bytes
    ).hexdigest()
    provisional = _InputSnapshotGeneration(
        score_file=score_file,
        roster_file=roster_file,
        execution_profile_file=profile_file,
        score_source=score_source,
        roster=roster,
        execution_profile=execution_profile,
        roster_projection_bytes=roster_projection_bytes,
        roster_projection_sha256=roster_projection_sha256,
        sample_rate=sample_rate,
        project_root=project_identity,
        catalogue_root=catalogue_identity,
        package_root=package_identity,
        limits=active_limits,
        canonical_bytes=b"",
        artifact_sha256="0" * 64,
    )
    document = _input_artifact_document(provisional)
    payload = canonical_json_bytes(document)
    if len(payload) > active_limits.max_plan_json_bytes:
        raise ResourceLimitError(
            "project_render_v2.input_artifact_too_large",
            "Score-v2 project input artifact exceeds max_plan_json_bytes",
            actual=len(payload),
            limit=active_limits.max_plan_json_bytes,
        )
    artifact_sha256 = hashlib.sha256(payload).hexdigest()
    generation = provisional._replace(
        canonical_bytes=payload,
        artifact_sha256=artifact_sha256,
    )
    snapshot = object.__new__(ScoreV2ProjectInputSnapshot)
    for name, value in (
        ("score_file", score_file),
        ("roster_file", roster_file),
        ("execution_profile_file", profile_file),
        ("score_source", score_source),
        ("roster", roster),
        ("execution_profile", execution_profile),
        ("roster_projection_sha256", roster_projection_sha256),
        ("sample_rate", sample_rate),
        ("project_root", project_identity.path),
        ("catalogue_root", catalogue_identity.path),
        ("limits", active_limits),
        ("_canonical_bytes", payload),
        ("_artifact_sha256", artifact_sha256),
    ):
        object.__setattr__(snapshot, name, value)
    _register_input_snapshot(snapshot, generation)
    snapshot.revalidate_inputs()
    return snapshot


def _checkpoint(
    inputs: ScoreV2ProjectInputSnapshot,
    capability_sources: ScoreV2CapabilitySourceSnapshot | None = None,
) -> None:
    inputs.revalidate_inputs()
    if capability_sources is None:
        return
    try:
        capability_sources.revalidate_sources()
    except ScoreV2CapabilitySourceError as exc:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.input_generation_changed"
        ) from exc


def _effective_oscillator_manifest(
    sources: ScoreV2CapabilitySourceSnapshot,
) -> tuple[dict[str, Any], str, str, str]:
    if (
        len(sources.executor_bindings) != 1
        or len(sources.manifest_generations) != 1
    ):
        raise ScoreV2ProjectRenderError(
            "project_render_v2.single_executor_required"
        )
    binding = sources.executor_bindings[0]
    source = sources.manifest_generations[0]
    if binding.manifest_source_sha256 != source.source_sha256:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.input_artifact_integrity_mismatch"
        )
    try:
        manifest = {**source.manifest_copy(), **dict(binding.overrides)}
        manifest_bytes = canonical_json_bytes(manifest)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.input_artifact_integrity_mismatch"
        ) from exc
    if (
        hashlib.sha256(manifest_bytes).hexdigest()
        != binding.effective_manifest_canonical_sha256
    ):
        raise ScoreV2ProjectRenderError(
            "project_render_v2.input_artifact_integrity_mismatch"
        )
    if binding.custom_implementation_blocked or manifest.get(
        "implementation"
    ) is not None:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.local_implementation_not_supported"
        )
    if manifest.get("type") != "oscillator":
        raise ScoreV2ProjectRenderError(
            "project_render_v2.backend_scope_unsupported"
        )
    external_assets = manifest.get("external_audio_assets")
    malformed_or_present_asset_fields = (
        (
            "external_audio_assets" in manifest
            and type(external_assets) is not list
        )
        or any(
            key in manifest
            for key in ("asset_root", "soundfont", "sample", "regions")
        )
    )
    declared_asset_free = (
        manifest.get("runtime_asset_policy") == "no_external_audio_assets"
    )
    if (
        malformed_or_present_asset_fields
        or (type(external_assets) is list and bool(external_assets))
        or not declared_asset_free
    ):
        raise ScoreV2ProjectRenderError(
            "project_render_v2.external_assets_not_supported"
        )
    return (
        manifest,
        binding.executor_id,
        binding.part_id,
        binding.effective_manifest_sha256,
    )


class _CompilationGeneration(NamedTuple):
    inputs: ScoreV2ProjectInputSnapshot
    score_plan: ScoreV2Plan
    capability_sources: ScoreV2CapabilitySourceSnapshot
    capability_plan: ScoreV2CapabilityPlan
    runtime_sources: ScoreV2RuntimeSourceSnapshot
    performance_bundle: ScoreV2PerformanceBundle
    executor_id: str
    part_id: str
    sample_rate: int
    effective_manifest_sha256: str
    canonical_bytes: bytes
    artifact_sha256: str


_COMPILATION_GENERATIONS: dict[
    int,
    tuple[weakref.ReferenceType[object], _CompilationGeneration],
] = {}


def _compilation_document(
    generation: _CompilationGeneration,
) -> dict[str, object]:
    return {
        "kind": SCORE_V2_PROJECT_RENDER_COMPILATION_KIND,
        "schema_version": (
            SCORE_V2_PROJECT_RENDER_COMPILATION_SCHEMA_VERSION
        ),
        "contract": SCORE_V2_PROJECT_RENDER_COMPILATION_CONTRACT,
        "status": SCORE_V2_PROJECT_RENDER_STATUS,
        "render_authority": False,
        "publish_authority": False,
        "candidate_authority": False,
        "scope": SCORE_V2_PROJECT_RENDER_SCOPE,
        "sample_rate": generation.sample_rate,
        "executor": {
            "executor_id": generation.executor_id,
            "part_id": generation.part_id,
            "effective_manifest_sha256": (
                generation.effective_manifest_sha256
            ),
        },
        "bindings": {
            "project_inputs_sha256": generation.inputs.artifact_sha256,
            "score_v2_plan_sha256": generation.score_plan.artifact_sha256,
            "capability_source_sha256": (
                generation.capability_sources.artifact_sha256
            ),
            "capability_plan_sha256": (
                generation.capability_plan.artifact_sha256
            ),
            "runtime_source_sha256": (
                generation.runtime_sources.artifact_sha256
            ),
            "performance_bundle_sha256": (
                generation.performance_bundle.artifact_sha256
            ),
        },
        "limitations": dict(_COMPILATION_LIMITATIONS),
    }


@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class ScoreV2ProjectRenderCompilation:
    """Sealed result of the fixed chain, awaiting a live runtime lease."""

    inputs: ScoreV2ProjectInputSnapshot
    score_plan: ScoreV2Plan
    capability_sources: ScoreV2CapabilitySourceSnapshot
    capability_plan: ScoreV2CapabilityPlan
    runtime_sources: ScoreV2RuntimeSourceSnapshot
    performance_bundle: ScoreV2PerformanceBundle
    executor_id: str
    part_id: str
    sample_rate: int
    effective_manifest_sha256: str
    scope: str
    _canonical_bytes: bytes
    _artifact_sha256: str

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ScoreV2ProjectRenderCompilation cannot be subclassed")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "ScoreV2ProjectRenderCompilation must be created by "
            "compile_score_v2_project_render"
        )

    def _trusted_generation(self) -> _CompilationGeneration:
        registered = _COMPILATION_GENERATIONS.get(id(self))
        try:
            if registered is None or registered[0]() is not self:
                raise ValueError
            generation = registered[1]
            if (
                self.inputs is not generation.inputs
                or self.score_plan is not generation.score_plan
                or self.capability_sources is not generation.capability_sources
                or self.capability_plan is not generation.capability_plan
                or self.runtime_sources is not generation.runtime_sources
                or self.performance_bundle is not generation.performance_bundle
                or self.executor_id != generation.executor_id
                or self.part_id != generation.part_id
                or self.sample_rate != generation.sample_rate
                or self.effective_manifest_sha256
                != generation.effective_manifest_sha256
                or self.scope != SCORE_V2_PROJECT_RENDER_SCOPE
                or self._canonical_bytes is not generation.canonical_bytes
                or self._artifact_sha256 != generation.artifact_sha256
                or type(generation.inputs) is not ScoreV2ProjectInputSnapshot
                or type(generation.score_plan) is not ScoreV2Plan
                or type(generation.capability_sources)
                is not ScoreV2CapabilitySourceSnapshot
                or type(generation.capability_plan) is not ScoreV2CapabilityPlan
                or type(generation.runtime_sources)
                is not ScoreV2RuntimeSourceSnapshot
                or type(generation.performance_bundle)
                is not ScoreV2PerformanceBundle
                or generation.sample_rate != generation.inputs.sample_rate
                or generation.sample_rate != generation.score_plan.sample_rate
                or generation.sample_rate != generation.capability_plan.sample_rate
                or generation.sample_rate != generation.runtime_sources.sample_rate
                or generation.sample_rate
                != generation.performance_bundle.sample_rate
                or not _is_sha256(generation.effective_manifest_sha256)
                or type(generation.canonical_bytes) is not bytes
                or not _is_sha256(generation.artifact_sha256)
                or hashlib.sha256(generation.canonical_bytes).hexdigest()
                != generation.artifact_sha256
                or canonical_json_bytes(_compilation_document(generation))
                != generation.canonical_bytes
            ):
                raise ValueError
            return generation
        except (AttributeError, TypeError, ValueError) as exc:
            raise ScoreV2ProjectRenderError(
                "project_render_v2.compilation_integrity_mismatch"
            ) from exc

    @property
    def canonical_bytes(self) -> bytes:
        return self._trusted_generation().canonical_bytes

    @property
    def artifact_sha256(self) -> str:
        return self._trusted_generation().artifact_sha256

    def to_dict(self) -> dict[str, object]:
        try:
            value = json.loads(self._trusted_generation().canonical_bytes)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScoreV2ProjectRenderError(
                "project_render_v2.compilation_integrity_mismatch"
            ) from exc
        if type(value) is not dict:
            raise ScoreV2ProjectRenderError(
                "project_render_v2.compilation_integrity_mismatch"
            )
        return value

    def revalidate_inputs(self) -> None:
        generation = self._trusted_generation()
        _checkpoint(generation.inputs, generation.capability_sources)
        try:
            generation.runtime_sources.revalidate_runtime_sources()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ScoreV2ProjectRenderError(
                "project_render_v2.input_generation_changed"
            ) from exc
        self._trusted_generation()


def _register_compilation(
    compilation: ScoreV2ProjectRenderCompilation,
    generation: _CompilationGeneration,
) -> None:
    compilation_id = id(compilation)

    def retire(
        reference: weakref.ReferenceType[object],
        *,
        expected_id: int = compilation_id,
    ) -> None:
        current = _COMPILATION_GENERATIONS.get(expected_id)
        if current is not None and current[0] is reference:
            _COMPILATION_GENERATIONS.pop(expected_id, None)

    reference = weakref.ref(compilation, retire)
    _COMPILATION_GENERATIONS[compilation_id] = (reference, generation)


def _runtime_scope(
    runtime_sources: ScoreV2RuntimeSourceSnapshot,
    *,
    executor_id: str,
    part_id: str,
) -> None:
    if len(runtime_sources.executor_bindings) != 1:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.single_executor_required"
        )
    binding = runtime_sources.executor_bindings[0]
    fingerprint = binding.fingerprint_copy()
    graph = fingerprint.get("runtime_asset_graph")
    if (
        binding.executor_id != executor_id
        or binding.part_id != part_id
        or binding.asset_inventory_status
        != NO_EXTERNAL_ASSET_INVENTORY_STATUS
        or type(graph) is not dict
        or graph.get("file_count") != 0
        or graph.get("total_bytes") != 0
        or graph.get("region_count") != 0
    ):
        raise ScoreV2ProjectRenderError(
            "project_render_v2.external_assets_not_supported"
        )


def compile_score_v2_project_render(
    inputs: ScoreV2ProjectInputSnapshot,
    *,
    limits: ProjectLimits | None = None,
) -> ScoreV2ProjectRenderCompilation:
    """Run the one fixed Score-v2 compilation chain without rendering."""

    if type(inputs) is not ScoreV2ProjectInputSnapshot:
        raise TypeError("inputs must be ScoreV2ProjectInputSnapshot")
    generation = inputs._trusted_generation()
    active_limits = generation.limits if limits is None else _active_limits(limits)
    if active_limits != generation.limits:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.resource_limits_changed"
        )
    _checkpoint(inputs)
    profile = generation.execution_profile
    dynamic_profile = {
        level.mark: Rational(level.value.numerator, level.value.denominator)
        for level in profile.dynamic_profile
    }
    score_plan = compile_score_v2_plan(
        generation.score_source,
        sample_rate=generation.sample_rate,
        sample_time_policy=profile.sample_time_policy,  # type: ignore[arg-type]
        dynamic_profile=dynamic_profile,
        limits=active_limits,
    )
    _checkpoint(inputs)
    capability_sources = capture_score_v2_capability_sources(
        generation.roster,
        catalogue_root=generation.catalogue_root.path,
        maximum_manifest_bytes=DEFAULT_MAX_MANIFEST_BYTES,
        maximum_executors=active_limits.max_executors,
    )
    _checkpoint(inputs, capability_sources)
    (
        _effective_manifest,
        executor_id,
        part_id,
        effective_manifest_sha256,
    ) = _effective_oscillator_manifest(capability_sources)
    del _effective_manifest
    capability_plan = compile_score_v2_capability_plan(
        generation.score_source,
        score_plan,
        profile,
        generation.roster,
        capability_sources,
        limits=active_limits,
    )
    _checkpoint(inputs, capability_sources)
    runtime_sources = capture_score_v2_runtime_sources(
        capability_plan,
        capability_sources,
        project_root=generation.project_root.path,
        limits=active_limits,
    )
    _checkpoint(inputs, capability_sources)
    _runtime_scope(
        runtime_sources,
        executor_id=executor_id,
        part_id=part_id,
    )
    performance_bundle = compile_score_v2_performance_bundle(
        score_plan,
        capability_plan,
        runtime_sources,
        limits=active_limits,
    )
    _checkpoint(inputs, capability_sources)
    try:
        runtime_sources.revalidate_runtime_sources()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.input_generation_changed"
        ) from exc
    if performance_bundle.executor_count != 1:
        raise ScoreV2ProjectRenderError(
            "project_render_v2.single_executor_required"
        )
    provisional = _CompilationGeneration(
        inputs=inputs,
        score_plan=score_plan,
        capability_sources=capability_sources,
        capability_plan=capability_plan,
        runtime_sources=runtime_sources,
        performance_bundle=performance_bundle,
        executor_id=executor_id,
        part_id=part_id,
        sample_rate=generation.sample_rate,
        effective_manifest_sha256=effective_manifest_sha256,
        canonical_bytes=b"",
        artifact_sha256="0" * 64,
    )
    payload = canonical_json_bytes(_compilation_document(provisional))
    if len(payload) > active_limits.max_plan_json_bytes:
        raise ResourceLimitError(
            "project_render_v2.compilation_artifact_too_large",
            "Score-v2 project compilation artifact exceeds max_plan_json_bytes",
            actual=len(payload),
            limit=active_limits.max_plan_json_bytes,
        )
    artifact_sha256 = hashlib.sha256(payload).hexdigest()
    result_generation = provisional._replace(
        canonical_bytes=payload,
        artifact_sha256=artifact_sha256,
    )
    result = object.__new__(ScoreV2ProjectRenderCompilation)
    for name, value in (
        ("inputs", inputs),
        ("score_plan", score_plan),
        ("capability_sources", capability_sources),
        ("capability_plan", capability_plan),
        ("runtime_sources", runtime_sources),
        ("performance_bundle", performance_bundle),
        ("executor_id", executor_id),
        ("part_id", part_id),
        ("sample_rate", generation.sample_rate),
        ("effective_manifest_sha256", effective_manifest_sha256),
        ("scope", SCORE_V2_PROJECT_RENDER_SCOPE),
        ("_canonical_bytes", payload),
        ("_artifact_sha256", artifact_sha256),
    ):
        object.__setattr__(result, name, value)
    _register_compilation(result, result_generation)
    # Finalise only while the complete creator/catalogue/runtime generation is
    # still live.  The later authority lease repeats the retained-generation
    # checks; this front half never turns a stale compilation into authority.
    result.revalidate_inputs()
    return result


def compile_score_v2_project_render_files(
    score_path: str | os.PathLike[str],
    roster_path: str | os.PathLike[str],
    execution_profile_path: str | os.PathLike[str],
    *,
    sample_rate: int,
    catalogue_root: str | os.PathLike[str],
    project_root: str | os.PathLike[str] | None = None,
    limits: ProjectLimits | None = None,
) -> ScoreV2ProjectRenderCompilation:
    """Capture the three files and run the fixed compilation chain."""

    active_limits = _active_limits(limits)
    inputs = capture_score_v2_project_inputs(
        score_path,
        roster_path,
        execution_profile_path,
        sample_rate=sample_rate,
        catalogue_root=catalogue_root,
        project_root=project_root,
        limits=active_limits,
    )
    return compile_score_v2_project_render(inputs, limits=active_limits)


__all__ = [
    "SCORE_V2_PROJECT_INPUT_CONTRACT",
    "SCORE_V2_PROJECT_INPUT_KIND",
    "SCORE_V2_PROJECT_INPUT_SCHEMA_VERSION",
    "SCORE_V2_PROJECT_RENDER_COMPILATION_CONTRACT",
    "SCORE_V2_PROJECT_RENDER_COMPILATION_KIND",
    "SCORE_V2_PROJECT_RENDER_COMPILATION_SCHEMA_VERSION",
    "SCORE_V2_PROJECT_RENDER_SCOPE",
    "SCORE_V2_PROJECT_RENDER_STATUS",
    "ScoreV2ProjectInputFile",
    "ScoreV2ProjectInputSnapshot",
    "ScoreV2ProjectRenderCompilation",
    "ScoreV2ProjectRenderError",
    "capture_score_v2_project_inputs",
    "compile_score_v2_project_render",
    "compile_score_v2_project_render_files",
]
