from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tianlai.canonical_json import canonical_json_bytes
from tianlai.capability import read_capability
from tianlai.instrument import factory_manifest_sha256
from tianlai.roster import parse_roster_document
import tianlai.score_v2_capability_source as source_module
from tianlai.score_v2_capability_source import (
    RUNTIME_FINGERPRINT_STATUS,
    SCORE_V2_CAPABILITY_SOURCE_CONTRACT,
    ScoreV2CapabilitySourceError,
    ScoreV2CapabilitySourceSnapshot,
    capture_score_v2_capability_sources,
)


def _manifest_document(*, custom: bool = False) -> dict[str, object]:
    document: dict[str, object] = {
        "name": "audit oscillator",
        "type": "oscillator",
        "note_min": 0,
        "note_max": 127,
    }
    if custom:
        document.update(
            {
                "implementation": "local_backend.py",
                "allowed_articulations": ["sustain"],
                "default_articulation": "sustain",
            }
        )
    return document


def _write_manifest(
    root: Path,
    *,
    custom: bool = False,
    pretty: bool = True,
) -> tuple[Path, bytes]:
    directory = root / "oscillator"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "instrument.json"
    payload = (
        json.dumps(
            _manifest_document(custom=custom),
            ensure_ascii=False,
            indent=(2 if pretty else None),
            sort_keys=not pretty,
        )
        + "\n"
    ).encode("utf-8")
    path.write_bytes(payload)
    return path, payload


def _resolved_roster(
    root: Path,
    manifest_path: Path,
    *,
    assignments: list[dict[str, object]] | None = None,
):
    capability = read_capability(manifest_path, root=root)
    capabilities = {capability.relative_path: capability}
    document = {
        "name": "snapshot fixture",
        "assignments": assignments
        or [
            {
                "part": "lead",
                "instrument": capability.relative_path,
            }
        ],
    }
    return capability, parse_roster_document(document, capabilities)


def _error_code(callable_: object) -> str:
    with pytest.raises(ScoreV2CapabilitySourceError) as caught:
        callable_()  # type: ignore[operator]
    assert str(caught.value) == caught.value.code
    return caught.value.code


def test_capture_binds_raw_canonical_capability_and_effective_identities(
    tmp_path: Path,
) -> None:
    manifest_path, raw_bytes = _write_manifest(tmp_path)
    capability, roster = _resolved_roster(
        tmp_path,
        manifest_path,
        assignments=[
            {
                "part": "lead",
                "instrument": "oscillator",
                "overrides": {"release_seconds": 0.5},
            }
        ],
    )

    snapshot = capture_score_v2_capability_sources(
        roster,
        catalogue_root=tmp_path,
    )

    assert type(snapshot) is ScoreV2CapabilitySourceSnapshot
    assert len(snapshot.manifest_generations) == 1
    assert len(snapshot.capability_projections) == 1
    assert len(snapshot.executor_bindings) == 1

    source = snapshot.manifest_generations[0]
    projection = snapshot.capability_projections[0]
    binding = snapshot.executor_bindings[0]
    manifest = _manifest_document()
    effective = {**manifest, "release_seconds": 0.5}

    assert source.raw_bytes == raw_bytes
    assert source.raw_sha256 == hashlib.sha256(raw_bytes).hexdigest()
    assert source.manifest_canonical_bytes == canonical_json_bytes(manifest)
    assert source.manifest_canonical_sha256 == hashlib.sha256(
        canonical_json_bytes(manifest)
    ).hexdigest()
    assert source.file_identity.size == len(raw_bytes)
    assert source.file_identity.path == str(manifest_path.resolve())
    assert source.custom_implementation_blocked is False

    assert projection.projection_copy() == capability.to_dict()
    assert projection.canonical_sha256 == hashlib.sha256(
        canonical_json_bytes(capability.to_dict())
    ).hexdigest()
    assert binding.overrides == (("release_seconds", 0.5),)
    assert binding.effective_manifest_canonical_sha256 == hashlib.sha256(
        canonical_json_bytes(effective)
    ).hexdigest()
    assert binding.effective_manifest_sha256 == factory_manifest_sha256(
        effective
    )
    assert binding.execution_eligibility == "pending_runtime_fingerprint"
    assert binding.runtime_fingerprint_status == RUNTIME_FINGERPRINT_STATUS
    assert binding.runtime_fingerprint_sha256 is None

    document = snapshot.to_dict()
    assert document["contract"] == SCORE_V2_CAPABILITY_SOURCE_CONTRACT
    assert document["runtime_fingerprint_policy"] == "not_captured"
    assert snapshot.canonical_bytes == canonical_json_bytes(document)
    assert snapshot.canonical_json_bytes == snapshot.canonical_bytes
    assert snapshot.artifact_sha256 == hashlib.sha256(
        snapshot.canonical_bytes
    ).hexdigest()
    assert snapshot.roster_projection_copy() == roster.to_dict()
    assert snapshot.roster_projection_sha256 == hashlib.sha256(
        canonical_json_bytes(roster.to_dict())
    ).hexdigest()


def test_shared_manifest_is_deduplicated_but_overrides_remain_per_executor(
    tmp_path: Path,
) -> None:
    manifest_path, _raw = _write_manifest(tmp_path)
    _capability, roster = _resolved_roster(
        tmp_path,
        manifest_path,
        assignments=[
            {
                "part": "first",
                "instrument": "oscillator",
                "overrides": {"release_seconds": 0.25},
            },
            {
                "part": "second",
                "instrument": "oscillator",
                "overrides": {"release_seconds": 0.75},
            },
        ],
    )

    snapshot = capture_score_v2_capability_sources(
        roster,
        catalogue_root=tmp_path,
    )

    assert len(snapshot.manifest_generations) == 1
    assert len(snapshot.capability_projections) == 1
    assert len(snapshot.executor_bindings) == 2
    assert {
        binding.manifest_source_sha256
        for binding in snapshot.executor_bindings
    } == {snapshot.manifest_generations[0].source_sha256}
    assert {
        binding.capability_projection_sha256
        for binding in snapshot.executor_bindings
    } == {snapshot.capability_projections[0].canonical_sha256}
    assert (
        snapshot.executor_bindings[0].effective_manifest_sha256
        != snapshot.executor_bindings[1].effective_manifest_sha256
    )


def test_custom_implementation_is_retained_but_fail_closed(
    tmp_path: Path,
) -> None:
    manifest_path, _raw = _write_manifest(tmp_path, custom=True)
    _capability, roster = _resolved_roster(tmp_path, manifest_path)

    snapshot = capture_score_v2_capability_sources(
        roster,
        catalogue_root=tmp_path,
    )

    source = snapshot.manifest_generations[0]
    binding = snapshot.executor_bindings[0]
    assert source.custom_implementation_blocked is True
    assert binding.custom_implementation_blocked is True
    assert binding.execution_eligibility == "blocked_custom_implementation"
    assert binding.runtime_fingerprint_sha256 is None


def test_raw_formatting_and_semantic_manifest_identity_remain_distinct(
    tmp_path: Path,
) -> None:
    manifest_path, first_raw = _write_manifest(tmp_path, pretty=True)
    _capability, roster = _resolved_roster(tmp_path, manifest_path)
    first = capture_score_v2_capability_sources(
        roster,
        catalogue_root=tmp_path,
    )

    _same_path, second_raw = _write_manifest(tmp_path, pretty=False)
    _capability, second_roster = _resolved_roster(tmp_path, manifest_path)
    second = capture_score_v2_capability_sources(
        second_roster,
        catalogue_root=tmp_path,
    )

    assert first_raw != second_raw
    assert (
        first.manifest_generations[0].raw_sha256
        != second.manifest_generations[0].raw_sha256
    )
    assert (
        first.manifest_generations[0].manifest_canonical_sha256
        == second.manifest_generations[0].manifest_canonical_sha256
    )
    assert (
        first.capability_projections[0].canonical_sha256
        == second.capability_projections[0].canonical_sha256
    )


def test_caller_mutation_after_capture_cannot_change_snapshot(
    tmp_path: Path,
) -> None:
    manifest_path, _raw = _write_manifest(tmp_path)
    capability, roster = _resolved_roster(tmp_path, manifest_path)
    snapshot = capture_score_v2_capability_sources(
        roster,
        catalogue_root=tmp_path,
    )
    identity = snapshot.artifact_sha256

    object.__setattr__(capability, "name", "mutated capability")
    object.__setattr__(roster, "executors", ())
    detached = snapshot.to_dict()
    detached["manifest_generations"] = []
    detached["roster_projection"] = {}

    assert snapshot.artifact_sha256 == identity
    assert snapshot.capability_projections[0].projection_copy()["name"] == (
        "audit oscillator"
    )
    assert snapshot.roster_projection_copy()["executors"]


def test_capture_time_roster_capability_swap_is_not_mixed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _raw = _write_manifest(tmp_path)
    capability, roster = _resolved_roster(tmp_path, manifest_path)
    original = source_module.bounded_canonical_json_bytes
    changed = False

    def capture_then_change(value: object, **kwargs: object) -> bytes:
        nonlocal changed
        payload = original(value, **kwargs)
        if (
            not changed
            and type(value) is dict
            and value.get("name") == "snapshot fixture"
            and type(value.get("executors")) is list
        ):
            changed = True
            object.__setattr__(capability, "name", "swapped generation")
        return payload

    monkeypatch.setattr(
        source_module,
        "bounded_canonical_json_bytes",
        capture_then_change,
    )
    assert _error_code(
        lambda: capture_score_v2_capability_sources(
            roster,
            catalogue_root=tmp_path,
        )
    ) in {
        "capability_source.capability_projection_mismatch",
        "capability_source.integrity_mismatch",
    }


def test_attached_capability_must_match_fresh_manifest_resolution(
    tmp_path: Path,
) -> None:
    manifest_path, _raw = _write_manifest(tmp_path)
    capability, roster = _resolved_roster(tmp_path, manifest_path)
    object.__setattr__(capability, "name", "forged but structurally valid")

    assert _error_code(
        lambda: capture_score_v2_capability_sources(
            roster,
            catalogue_root=tmp_path,
        )
    ) == "capability_source.capability_projection_mismatch"


def test_forged_structural_manifest_override_is_rejected(
    tmp_path: Path,
) -> None:
    manifest_path, _raw = _write_manifest(tmp_path)
    _capability, roster = _resolved_roster(tmp_path, manifest_path)
    object.__setattr__(
        roster.executors[0],
        "overrides",
        (("implementation", "unreviewed.py"),),
    )

    assert _error_code(
        lambda: capture_score_v2_capability_sources(
            roster,
            catalogue_root=tmp_path,
        )
    ) == "capability_source.invalid_executor_overrides"


def test_strict_json_and_byte_ceiling_apply_before_capability_resolution(
    tmp_path: Path,
) -> None:
    manifest_path, _raw = _write_manifest(tmp_path)
    _capability, roster = _resolved_roster(tmp_path, manifest_path)
    manifest_path.write_bytes(
        b'{"name":"first","name":"second","type":"oscillator"}'
    )
    assert _error_code(
        lambda: capture_score_v2_capability_sources(
            roster,
            catalogue_root=tmp_path,
        )
    ) == "capability_source.manifest.duplicate_object_member"

    manifest_path.write_bytes(b'{"name":"too large","type":"oscillator"}')
    assert _error_code(
        lambda: capture_score_v2_capability_sources(
            roster,
            catalogue_root=tmp_path,
            maximum_manifest_bytes=8,
        )
    ) == "capability_source.manifest_too_large"


def test_generation_change_during_resolution_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path, _raw = _write_manifest(tmp_path)
    _capability, roster = _resolved_roster(tmp_path, manifest_path)
    original = source_module.read_capability

    def resolve_then_change(*args: object, **kwargs: object):
        capability = original(*args, **kwargs)
        changed = _manifest_document()
        changed["note_max"] = 126
        manifest_path.write_text(json.dumps(changed), encoding="utf-8")
        return capability

    monkeypatch.setattr(source_module, "read_capability", resolve_then_change)

    assert _error_code(
        lambda: capture_score_v2_capability_sources(
            roster,
            catalogue_root=tmp_path,
        )
    ) == "capability_source.source_changed_during_capture"


def test_revalidation_checks_identity_and_exact_bytes(tmp_path: Path) -> None:
    manifest_path, _raw = _write_manifest(tmp_path)
    _capability, roster = _resolved_roster(tmp_path, manifest_path)
    snapshot = capture_score_v2_capability_sources(
        roster,
        catalogue_root=tmp_path,
    )

    snapshot.revalidate_sources()
    changed = _manifest_document()
    changed["note_max"] = 126
    manifest_path.write_text(json.dumps(changed), encoding="utf-8")
    assert _error_code(snapshot.revalidate_sources) == (
        "capability_source.source_generation_changed"
    )


def test_constructor_subclassing_and_attribute_bypass_fail_closed(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError, match="must be created"):
        ScoreV2CapabilitySourceSnapshot()
    with pytest.raises(TypeError, match="cannot be subclassed"):

        class ForgedSnapshot(ScoreV2CapabilitySourceSnapshot):
            pass

    manifest_path, _raw = _write_manifest(tmp_path)
    _capability, roster = _resolved_roster(tmp_path, manifest_path)
    snapshot = capture_score_v2_capability_sources(
        roster,
        catalogue_root=tmp_path,
    )
    object.__setattr__(snapshot, "roster_projection_sha256", "0" * 64)
    assert _error_code(snapshot.to_dict) == (
        "capability_source.integrity_mismatch"
    )

    empty = object.__new__(ScoreV2CapabilitySourceSnapshot)
    assert _error_code(empty.to_dict) == (
        "capability_source.integrity_mismatch"
    )

    class HostileValue:
        def __eq__(self, _other: object) -> bool:
            raise RuntimeError("caller detail must not escape")

        def __ne__(self, _other: object) -> bool:
            raise RuntimeError("caller detail must not escape")

    hostile = capture_score_v2_capability_sources(
        roster,
        catalogue_root=tmp_path,
    )
    object.__setattr__(hostile, "roster_projection_sha256", HostileValue())
    assert _error_code(hostile.to_dict) == (
        "capability_source.integrity_mismatch"
    )


@pytest.mark.parametrize(
    ("keyword", "value", "code"),
    [
        (
            "maximum_manifest_bytes",
            0,
            "capability_source.invalid_manifest_byte_limit",
        ),
        (
            "maximum_manifest_bytes",
            True,
            "capability_source.invalid_manifest_byte_limit",
        ),
        (
            "maximum_executors",
            0,
            "capability_source.invalid_executor_limit",
        ),
    ],
)
def test_resource_limit_arguments_fail_closed(
    tmp_path: Path,
    keyword: str,
    value: object,
    code: str,
) -> None:
    manifest_path, _raw = _write_manifest(tmp_path)
    _capability, roster = _resolved_roster(tmp_path, manifest_path)
    arguments: dict[str, object] = {
        "catalogue_root": tmp_path,
        keyword: value,
    }
    assert _error_code(
        lambda: capture_score_v2_capability_sources(
            roster,
            **arguments,  # type: ignore[arg-type]
        )
    ) == code
