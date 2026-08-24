"""Render one immutable authoring revision as a verified candidate.

This is the transport-independent render boundary for authoring projects.
It intentionally reuses the CLI's ``candidate_publication`` transaction;
MCP rendering is not imported and cannot weaken candidate publication.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Callable

from .authoring_core import validate_project_readiness
from .authoring_project import (
    AuthoringProjectError,
    PRIVATE_DIRECTORY_NAME,
    RENDERS_DIRECTORY_NAME,
    open_authoring_project,
)
from .authoring_roster import to_formal_roster
from .candidate import (
    CANDIDATE_MANIFEST_NAME,
    CandidateAlreadyExistsError,
    build_candidate_playback_map,
    candidate_publication,
    canonical_json_sha256,
    load_candidate,
    portable_slug,
    prepare_candidate_target,
    publish_candidate_metadata,
)
from .capability import load_capabilities
from .conductor import ExpressionSettings, build_plan
from .ensemble import render_plan
from .preflight import enforce_roster_availability
from .plain_file import sha256_plain_file
from .render_lock import (
    PlainDirectoryIdentity,
    RenderLockError,
    capture_plain_directory,
    ensure_authorized_child_directory,
    revalidate_plain_directory,
)
from .render_profile import parse_render_profile
from .resource_limits import validate_render_request_resource_limits
from .roster import parse_roster_document
from .runtime_layout import discover_runtime_layout
from .score import parse_score_document
from .workflow_binding import validate_workflow_authorization


RENDER_STAGES = (
    "validate",
    "plan",
    "render_parts",
    "mix",
    "post_check",
    "publish",
)
_STAGE_ORDER = {stage: index for index, stage in enumerate(RENDER_STAGES)}
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MANAGED_REUSE_WAIT_SECONDS = 30.0
_MANAGED_REUSE_POLL_SECONDS = 0.02


class AuthoringRenderError(RuntimeError):
    """A safe caller-facing render failure with no local path or exception."""

    def __init__(
        self,
        code: str,
        *,
        stage: str,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.message_key = f"authoringRender.{code.replace('.', '_')}"
        self.stage = stage if stage in _STAGE_ORDER else "validate"
        self.retryable = bool(retryable)
        super().__init__(code)


class AuthoringRenderCancelled(AuthoringRenderError):
    def __init__(self, *, stage: str) -> None:
        super().__init__("render.cancelled", stage=stage, retryable=True)


@dataclass(frozen=True, slots=True)
class RenderCheckpoint:
    stage: str
    completed_units: int
    total_units: int

    @property
    def ratio(self) -> float:
        return self.completed_units / self.total_units


ControlCallback = Callable[[RenderCheckpoint], bool | None]


class _Controller:
    def __init__(self, callback: ControlCallback | None) -> None:
        self.callback = callback
        self.stage = "validate"
        self._last_stage = -1
        self._last_ratio_by_stage: dict[str, float] = {}

    def checkpoint(
        self,
        stage: str,
        completed: int,
        total: int,
        *,
        cancellable: bool = True,
    ) -> None:
        if (
            stage not in _STAGE_ORDER
            or isinstance(completed, bool)
            or isinstance(total, bool)
            or not isinstance(completed, int)
            or not isinstance(total, int)
            or total < 1
            or completed < 0
            or completed > total
        ):
            raise AuthoringRenderError(
                "render.invalid_checkpoint", stage=self.stage
            )
        order = _STAGE_ORDER[stage]
        ratio = completed / total
        if order < self._last_stage or ratio < self._last_ratio_by_stage.get(stage, 0.0):
            raise AuthoringRenderError(
                "render.nonmonotonic_checkpoint", stage=self.stage
            )
        self.stage = stage
        self._last_stage = order
        self._last_ratio_by_stage[stage] = ratio
        if self.callback is None:
            return
        try:
            decision = self.callback(
                RenderCheckpoint(
                    stage=stage,
                    completed_units=completed,
                    total_units=total,
                )
            )
        except AuthoringRenderCancelled:
            raise
        except AuthoringRenderError:
            raise
        except Exception as exc:
            raise AuthoringRenderError(
                "render.control_failed", stage=stage, retryable=True
            ) from exc
        if cancellable and decision is False:
            raise AuthoringRenderCancelled(stage=stage)


def _canonical_plain_directory(
    value: str | os.PathLike[str],
    *,
    code: str,
) -> Path:
    try:
        requested = Path(value).expanduser()
    except (TypeError, ValueError, OSError) as exc:
        raise AuthoringRenderError(code, stage="validate") from exc
    if not requested.is_absolute():
        raise AuthoringRenderError(code, stage="validate")
    try:
        return capture_plain_directory(requested).path
    except (OSError, RuntimeError) as exc:
        raise AuthoringRenderError(code, stage="validate") from exc


def _extended_windows_path(path: Path) -> Path:
    """Keep private render staging usable beyond the legacy MAX_PATH limit."""

    if os.name != "nt":
        return path
    text = str(path)
    if text.startswith("\\\\?\\"):
        return path
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def _safe_cache_directories(project_root: Path) -> tuple[Path, Path]:
    private = project_root / PRIVATE_DIRECTORY_NAME
    try:
        private_identity = capture_plain_directory(private)
    except OSError as exc:
        raise AuthoringRenderError(
            "project.private_directory_unsafe", stage="validate"
        ) from exc

    identities: list[PlainDirectoryIdentity] = [private_identity]

    def child(
        parent_identity: PlainDirectoryIdentity,
        name: str,
    ) -> PlainDirectoryIdentity:
        directory = parent_identity.path / name
        try:
            identity = ensure_authorized_child_directory(parent_identity, name)
        except OSError as exc:
            # A concurrent creator is accepted by
            # ensure_authorized_child_directory only after the resulting path
            # has been reopened and identity-checked.  Anything that remains
            # at the path after a failed check is therefore unsafe (including
            # symlinks, junctions, files, and identity swaps); a genuinely
            # absent path is an availability failure.
            if os.path.lexists(directory):
                raise AuthoringRenderError(
                    "project.cache_directory_unsafe", stage="validate"
                ) from exc
            raise AuthoringRenderError(
                "project.cache_unavailable",
                stage="validate",
                retryable=True,
            ) from exc
        identities.append(identity)
        return identity

    cache_identity = child(private_identity, "cache")
    stems_identity = child(cache_identity, "stems")
    analysis_identity = child(cache_identity, "analysis")
    try:
        for identity in identities:
            revalidate_plain_directory(identity)
    except OSError as exc:
        raise AuthoringRenderError(
            "project.cache_directory_unsafe", stage="validate"
        ) from exc
    return stems_identity.path, analysis_identity.path


def _portable_segment(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and len(value) <= 128
        and not any(character in value for character in "/\\:")
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _managed_candidate_result(
    *,
    output_root: Path,
    title: str,
    project_id: str,
    revision: str,
    workflow_authorization: dict[str, Any],
) -> dict[str, Any]:
    """Verify and reuse the exact candidate from one render reservation."""

    work_id = portable_slug(title)
    candidate_id = workflow_authorization["candidate_id"]
    if work_id != workflow_authorization["candidate_work_id"]:
        raise AuthoringRenderError(
            "workflow.candidate_identity_mismatch", stage="plan"
        )
    identities: list[PlainDirectoryIdentity] = []
    try:
        root_identity = capture_plain_directory(output_root)
        identities.append(root_identity)
        work_identity = capture_plain_directory(output_root / work_id)
        identities.append(work_identity)
        candidate_identity = capture_plain_directory(
            work_identity.path / candidate_id
        )
        identities.append(candidate_identity)
        if (
            work_identity.path.parent != root_identity.path
            or candidate_identity.path.parent != work_identity.path
        ):
            raise OSError("managed candidate escaped its authorised root")
        directory, manifest = load_candidate(
            candidate_identity.path,
            verify=True,
            expected_work_id=work_id,
            expected_candidate_id=candidate_id,
        )
        if directory != candidate_identity.path:
            raise OSError("managed candidate changed identity")
        if manifest.get("authoring_workflow") != workflow_authorization:
            raise OSError("managed candidate authorization mismatch")
        authoring = manifest.get("authoring_project")
        if (
            not isinstance(authoring, dict)
            or authoring.get("project_id") != project_id
            or authoring.get("revision") != revision
        ):
            raise OSError("managed candidate authoring identity mismatch")
        playback = build_candidate_playback_map(
            directory,
            expected_work_id=work_id,
            expected_candidate_id=candidate_id,
        )
        timeline = playback.get("timeline")
        if not isinstance(timeline, dict):
            raise OSError("managed candidate playback timeline is missing")
        for identity in identities:
            revalidate_plain_directory(identity)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise AuthoringRenderError(
            "candidate.managed_reuse_invalid", stage="plan"
        ) from exc

    return {
        "status": "completed",
        "project_id": project_id,
        "revision": revision,
        "workflow_managed": True,
        "reused_existing": True,
        "candidate": {
            "candidate_id": candidate_id,
            "work_id": work_id,
            "title": title,
            "duration_seconds": float(timeline["duration_seconds"]),
            "sample_rate": int(timeline["sample_rate"]),
            "frame_count": int(timeline["frame_count"]),
        },
    }


def _await_managed_candidate_result(
    *,
    output_root: Path,
    title: str,
    project_id: str,
    revision: str,
    workflow_authorization: dict[str, Any],
) -> dict[str, Any]:
    """Wait briefly for the owner of an identical reservation to publish.

    Render locks remain non-blocking for ordinary callers.  A managed render,
    however, has one deterministic candidate identity, so a concurrent retry
    may safely wait for and verify that exact result instead of doing the
    expensive render twice.  If the owner fails, the caller receives a
    retryable busy result and can acquire the now-released lock on its next
    request.
    """

    deadline = time.monotonic() + _MANAGED_REUSE_WAIT_SECONDS
    while True:
        try:
            return _managed_candidate_result(
                output_root=output_root,
                title=title,
                project_id=project_id,
                revision=revision,
                workflow_authorization=workflow_authorization,
            )
        except AuthoringRenderError as exc:
            if exc.code != "candidate.managed_reuse_invalid":
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            raise AuthoringRenderError(
                "candidate.render_busy", stage="plan", retryable=True
            )
        time.sleep(min(_MANAGED_REUSE_POLL_SECONDS, remaining))


def _verify_managed_parent(
    output_root: Path,
    *,
    project_id: str,
    workflow_id: str,
    parent_work_id: str | None,
    parent_candidate_id: str | None,
    parent_manifest_sha256: str | None,
) -> None:
    if parent_candidate_id is None:
        if parent_work_id is not None or parent_manifest_sha256 is not None:
            raise AuthoringRenderError(
                "candidate.parent_invalid", stage="plan"
            )
        return
    try:
        if parent_work_id is None or parent_manifest_sha256 is None:
            raise OSError("managed parent locator is incomplete")
        root_identity = capture_plain_directory(output_root)
        work_identity = capture_plain_directory(
            root_identity.path / parent_work_id
        )
        parent_identity = capture_plain_directory(
            work_identity.path / parent_candidate_id
        )
        if (
            work_identity.path.parent != root_identity.path
            or parent_identity.path.parent != work_identity.path
        ):
            raise OSError("managed parent escaped its authorised root")
        directory, manifest = load_candidate(
            parent_identity.path,
            verify=True,
            expected_work_id=parent_work_id,
            expected_candidate_id=parent_candidate_id,
        )
        authoring = manifest.get("authoring_project")
        workflow = manifest.get("authoring_workflow")
        if (
            directory != parent_identity.path
            or not isinstance(authoring, dict)
            or authoring.get("project_id") != project_id
            or not isinstance(workflow, dict)
            or workflow.get("workflow_id") != workflow_id
            or sha256_plain_file(directory / CANDIDATE_MANIFEST_NAME)[1]
            != parent_manifest_sha256
        ):
            raise OSError("managed parent candidate identity mismatch")
        revalidate_plain_directory(root_identity)
        revalidate_plain_directory(work_identity)
        revalidate_plain_directory(parent_identity)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise AuthoringRenderError(
            "candidate.parent_invalid", stage="plan"
        ) from exc


def render_project_candidate(
    project_root: str | os.PathLike[str],
    *,
    expected_revision: str,
    output_root: str | os.PathLike[str] | None = None,
    workflow_authorization: dict[str, Any] | None = None,
    control_callback: ControlCallback | None = None,
) -> dict[str, Any]:
    """Render a fixed revision; return no filesystem paths or raw exceptions."""

    controller = _Controller(control_callback)
    try:
        try:
            managed_authorization = validate_workflow_authorization(
                workflow_authorization
            )
        except ValueError as exc:
            raise AuthoringRenderError(
                "workflow.authorization_invalid", stage="validate"
            ) from exc
        if (
            not isinstance(expected_revision, str)
            or _SHA256_PATTERN.fullmatch(expected_revision) is None
        ):
            raise AuthoringRenderError(
                "project.invalid_revision", stage="validate"
            )
        controller.checkpoint("validate", 0, 1)
        root = _canonical_plain_directory(
            project_root, code="project.root_unsafe"
        )
        try:
            state = open_authoring_project(root, revision=expected_revision)
        except AuthoringProjectError as exc:
            raise AuthoringRenderError(
                "project.revision_unavailable", stage="validate"
            ) from exc
        if managed_authorization is not None and (
            managed_authorization["project_id"] != state.project_id
            or managed_authorization["authoring_revision"] != state.revision
        ):
            raise AuthoringRenderError(
                "workflow.authoring_binding_mismatch", stage="validate"
            )
        if managed_authorization is not None:
            # The receipt binding is not a bearer token and its exact JSON
            # shape is not authority by itself.  Resolve it against the
            # workflow's current immutable reservation before planning or
            # starting any expensive render work.  A stopped, consumed,
            # superseded, copied, or caller-invented reservation therefore
            # fails here even if every field is syntactically valid.
            try:
                from .creative_workflow import (
                    CreativeWorkflowError,
                    verify_active_render_reservation,
                )

                reservation = verify_active_render_reservation(
                    root, managed_authorization
                )
            except (CreativeWorkflowError, OSError, RuntimeError, ValueError) as exc:
                raise AuthoringRenderError(
                    "workflow.reservation_inactive", stage="validate"
                ) from exc
            if (
                reservation.workflow_id
                != managed_authorization["workflow_id"]
                or reservation.project_id != state.project_id
                or reservation.revision
                != managed_authorization["reservation_revision"]
            ):
                raise AuthoringRenderError(
                    "workflow.reservation_inactive", stage="validate"
                )
        project_output = _canonical_plain_directory(
            root / RENDERS_DIRECTORY_NAME, code="output.root_unsafe"
        )
        if output_root is None:
            selected_output = project_output
        else:
            selected_output = _canonical_plain_directory(
                output_root, code="output.root_unsafe"
            )
        if (
            managed_authorization is not None
            and selected_output != project_output
        ):
            # One workflow reservation names one deterministic candidate in
            # the project's durable render namespace.  Allowing the same
            # reservation to fan out across caller-selected roots would evade
            # the render-attempt budget and create multiple artifacts with the
            # same workflow operation identity.
            raise AuthoringRenderError(
                "workflow.output_root_mismatch", stage="validate"
            )
        private = root / PRIVATE_DIRECTORY_NAME
        try:
            selected_output.relative_to(private)
        except ValueError:
            pass
        else:
            raise AuthoringRenderError(
                "output.inside_project_private_directory", stage="validate"
            )
        readiness = validate_project_readiness(
            state,
            project_root=root,
            render_output_root=selected_output,
        )
        if not readiness["render_allowed"]:
            raise AuthoringRenderError(
                "project.not_renderable", stage="validate"
            )
        controller.checkpoint("validate", 1, 1)

        controller.checkpoint("plan", 0, 1)
        documents = state.documents
        score = parse_score_document(documents["score"])
        profile = parse_render_profile(documents["render_profile"])
        layout = discover_runtime_layout(require_catalog=True)
        capabilities = load_capabilities(layout.catalog)
        formal_document = to_formal_roster(
            documents["authoring_roster"], score, capabilities
        )
        roster = parse_roster_document(formal_document, capabilities)
        enforce_roster_availability(roster)
        expression = ExpressionSettings.from_dict(
            {
                "mode": profile.expression,
                "range_mode": profile.range_mode,
                "humanize": {"seed": profile.seed},
            }
        )
        plan = build_plan(score, roster, expression)
        validate_render_request_resource_limits(
            plan,
            write_stems=profile.write_stems,
            space=profile.space,
            collaboration_mode=profile.collaboration_mode,
            stem_cache_enabled=profile.use_stem_cache,
        )
        plan_sha256 = canonical_json_sha256(plan.to_dict())
        authoring_roster_sha256 = canonical_json_sha256(
            documents["authoring_roster"]
        )
        authoring_receipt_binding = {
            "project_id": state.project_id,
            "revision": state.revision,
            "authoring_roster_canonical_sha256": authoring_roster_sha256,
        }
        title = str(documents["score"].get("title", "")).strip() or state.title
        # Candidate publication and the render engine each use a private
        # sibling staging directory.  Their combined safe names can exceed
        # the legacy 260-character Windows path ceiling even when the final
        # user-visible candidate path is ordinary.  Use an extended path only
        # after the caller-authorised output root has been canonicalised and
        # probed; the public result never exposes it.
        operation_output = _extended_windows_path(selected_output)
        output_id = None
        if managed_authorization is not None:
            output_id = f"workflow-{managed_authorization['operation_id']}"
            if (
                portable_slug(output_id, maximum_length=96)
                != managed_authorization["candidate_id"]
                or portable_slug(title)
                != managed_authorization["candidate_work_id"]
            ):
                raise AuthoringRenderError(
                    "workflow.candidate_identity_mismatch", stage="plan"
                )
        try:
            target = prepare_candidate_target(
                operation_output,
                title,
                plan_sha256=plan_sha256,
                output_id=output_id,
                overwrite=False,
                clean_work_directory=False,
            )
        except CandidateAlreadyExistsError:
            if managed_authorization is None:
                raise
            reused = _managed_candidate_result(
                output_root=selected_output,
                title=title,
                project_id=state.project_id,
                revision=state.revision,
                workflow_authorization=managed_authorization,
            )
            controller.checkpoint("plan", 1, 1)
            return reused
        if not _portable_segment(target.work_id) or not _portable_segment(
            target.candidate_id
        ):
            raise AuthoringRenderError(
                "candidate.identity_unsafe", stage="plan"
            )
        if managed_authorization is not None:
            if target.candidate_id != managed_authorization["candidate_id"]:
                raise AuthoringRenderError(
                    "workflow.candidate_identity_mismatch", stage="plan"
                )
            _verify_managed_parent(
                selected_output,
                project_id=state.project_id,
                workflow_id=managed_authorization["workflow_id"],
                parent_work_id=managed_authorization["parent_work_id"],
                parent_candidate_id=managed_authorization[
                    "parent_candidate_id"
                ],
                parent_manifest_sha256=managed_authorization[
                    "parent_manifest_sha256"
                ],
            )
        cache_stems: Path | None = None
        cache_analysis: Path | None = None
        if profile.use_stem_cache:
            cache_stems, cache_analysis = _safe_cache_directories(root)
        controller.checkpoint("plan", 1, 1)

        def engine_progress(stage: str, completed: int, total: int) -> None:
            # The inner render generation has its own publication transaction.
            # Candidate publication remains the user-visible publish stage, so
            # suppress the inner publish checkpoint here.
            if stage == "publish":
                return
            controller.checkpoint(stage, completed, total)

        with candidate_publication(target) as staging:
            result = render_plan(
                plan,
                staging.directory,
                write_stems=profile.write_stems,
                master_gain_db=profile.master_gain_db,
                normalize_peak_db=profile.normalize_peak_db,
                space=profile.space,
                collaboration_mode=profile.collaboration_mode,
                stem_cache_directory=cache_stems,
                analysis_cache_directory=cache_analysis,
                refresh_stem_cache=profile.refresh_stem_cache,
                _acquire_output_lock=False,
                _authoring_project_binding=authoring_receipt_binding,
                _authoring_workflow_binding=managed_authorization,
                _progress_callback=engine_progress,
            )
            if not result.receipt_path or not result.post_render_check_path:
                raise AuthoringRenderError(
                    "candidate.render_generation_incomplete",
                    stage="post_check",
                )
            controller.checkpoint("publish", 0, 1)
            publish_candidate_metadata(
                staging,
                title=title,
                score=documents["score"],
                roster=formal_document,
                render_profile=documents["render_profile"],
                receipt_path=result.receipt_path,
                plan_sha256=plan_sha256,
                parent_candidate_id=(
                    None
                    if managed_authorization is None
                    else managed_authorization["parent_candidate_id"]
                ),
                authoring_project={
                    "project_id": state.project_id,
                    "revision": state.revision,
                    "authoring_roster": documents["authoring_roster"],
                },
                authoring_workflow=managed_authorization,
            )
            playback_map = build_candidate_playback_map(
                staging.directory,
                expected_work_id=target.work_id,
                expected_candidate_id=target.candidate_id,
            )
            candidate = playback_map.get("candidate")
            if (
                not isinstance(candidate, dict)
                or candidate.get("work_id") != target.work_id
                or candidate.get("candidate_id") != target.candidate_id
            ):
                raise AuthoringRenderError(
                    "candidate.playback_map_mismatch", stage="publish"
                )
            if (
                not math.isfinite(float(result.duration_seconds))
                or result.duration_seconds < 0.0
                or isinstance(result.sample_rate, bool)
                or not isinstance(result.sample_rate, int)
                or isinstance(result.frame_count, bool)
                or not isinstance(result.frame_count, int)
                or result.frame_count < 1
            ):
                raise AuthoringRenderError(
                    "candidate.result_invalid", stage="publish"
                )
            safe_result = {
                "status": "completed",
                "project_id": state.project_id,
                "revision": state.revision,
                "workflow_managed": managed_authorization is not None,
                "reused_existing": False,
                "candidate": {
                    "candidate_id": target.candidate_id,
                    "work_id": target.work_id,
                    "title": title,
                    "duration_seconds": float(result.duration_seconds),
                    "sample_rate": result.sample_rate,
                    "frame_count": result.frame_count,
                },
            }
            # This is the last cancellable boundary.  Every staged artifact
            # and the bounded result have been verified, but the atomic
            # candidate-directory publication has not happened yet.
            controller.checkpoint("publish", 1, 1)

        return safe_result
    except CandidateAlreadyExistsError as exc:
        if managed_authorization is None:
            raise AuthoringRenderError(
                "candidate.already_exists", stage=controller.stage
            ) from exc
        return _managed_candidate_result(
            output_root=selected_output,
            title=title,
            project_id=state.project_id,
            revision=state.revision,
            workflow_authorization=managed_authorization,
        )
    except RenderLockError as exc:
        if managed_authorization is None:
            raise AuthoringRenderError(
                "candidate.render_busy",
                stage=controller.stage,
                retryable=True,
            ) from exc
        return _await_managed_candidate_result(
            output_root=selected_output,
            title=title,
            project_id=state.project_id,
            revision=state.revision,
            workflow_authorization=managed_authorization,
        )
    except AuthoringRenderCancelled:
        raise
    except AuthoringRenderError:
        raise
    except Exception as exc:
        raise AuthoringRenderError(
            "render.failed",
            stage=controller.stage,
            retryable=False,
        ) from exc


__all__ = [
    "AuthoringRenderCancelled",
    "AuthoringRenderError",
    "ControlCallback",
    "RENDER_STAGES",
    "RenderCheckpoint",
    "render_project_candidate",
]
