from __future__ import annotations

import copy
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import pickle

import pytest

from tianlai.authoring_json import AuthoringJsonError, JS_SAFE_INTEGER
from tianlai.canonical_json import canonical_json_bytes
from tianlai.capability import InstrumentCapability
from tianlai.conductor import build_plan
from tianlai.resource_limits import ProjectLimits, ResourceLimitError
from tianlai.roster import parse_roster_document
from tianlai.score import ScoreDocument
from tianlai.score_v2 import Rational, ScoreV2Document
import tianlai.score_source as score_source_module
from tianlai.score_source import (
    ScoreSourceSnapshot,
    read_score_snapshot,
    snapshot_score_bytes,
    snapshot_score_document,
)


def test_snapshot_score_bytes_rebinds_one_strict_canonical_generation() -> None:
    source = _score_v2()
    canonical = canonical_json_bytes(source)
    padded = b" \r\n" + canonical + b"\n"

    snapshot = snapshot_score_bytes(padded)

    assert snapshot.canonical_bytes == canonical
    assert snapshot.document_sha256 == hashlib.sha256(canonical).hexdigest()
    assert isinstance(snapshot.score, ScoreV2Document)


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b'{"schema_version":2,"schema_version":2}', "duplicate_object_member"),
        (b'{"schema_version":2,"value":NaN}', "non_finite_number"),
    ],
)
def test_snapshot_score_bytes_rejects_non_strict_json(
    payload: bytes,
    code: str,
) -> None:
    with pytest.raises(AuthoringJsonError) as caught:
        snapshot_score_bytes(payload)
    assert caught.value.code == code


def _score(*, versioned: bool = True, title: str = "snapshot") -> dict:
    document = {
        "title": title,
        "sample_rate": 48_000,
        "tail_seconds": 1.0,
        "tuning": {"temperament": "equal", "a4_hz": 442.0},
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
                "id": "lead",
                "name": "Lead",
                "notes": [
                    {
                        "bar": 1,
                        "beat": 1.0,
                        "duration_beats": 1.0,
                        "pitch": "C4",
                    }
                ],
            }
        ],
    }
    if versioned:
        document["schema_version"] = 1
        document["parts"][0]["notes"][0]["event_id"] = "note-1"
    return document


def _r(numerator: int, denominator: int = 1) -> dict[str, int]:
    return {"numerator": numerator, "denominator": denominator}


def _score_v2(*, schema_version: int | float = 2) -> dict:
    return {
        "kind": "tianlai.score",
        "schema_version": schema_version,
        "title": "exact snapshot",
        "timeline": {
            "measures": [
                {
                    "measure_id": "measure-1",
                    "actual_duration_quarters": _r(4),
                }
            ],
            "meter_events": [
                {
                    "meter_id": "meter-1",
                    "at": {
                        "measure_id": "measure-1",
                        "offset_quarters": _r(0),
                    },
                    "groups": [4],
                    "beat_unit": 4,
                }
            ],
            "tempo_events": [
                {
                    "tempo_id": "tempo-1",
                    "at": {
                        "measure_id": "measure-1",
                        "offset_quarters": _r(0),
                    },
                    "quarter_bpm": _r(120),
                }
            ],
        },
        "tuning": {
            "tuning_id": "concert-a",
            "system": "equal_temperament",
            "divisions_per_octave": 12,
            "reference_midi_note": _r(69),
            "reference_frequency_hz": _r(440),
        },
        "parts": [
            {
                "part_id": "lead",
                "notes": [
                    {
                        "event_id": "event-1",
                        "position": {
                            "measure_id": "measure-1",
                            "offset_quarters": _r(0),
                        },
                        "duration_quarters": _r(1, 7),
                        "written_pitch": {
                            "step": "C",
                            "alter": _r(0),
                            "octave": 4,
                        },
                        "sounding_pitch": {"midi_note": _r(60)},
                    }
                ],
            }
        ],
    }


def test_snapshot_supports_legacy_v1_and_v2_contracts() -> None:
    versioned = snapshot_score_document(_score())
    assert isinstance(versioned.score, ScoreDocument)
    assert versioned.score.schema_version == 1
    assert versioned.identity_contract == "stable-event-v1"
    assert versioned.time_contract == "float-bar-beat-v1"

    legacy = snapshot_score_document(_score(versioned=False))
    assert legacy.score.schema_version is None
    assert legacy.identity_contract == "legacy-position-v0"
    assert legacy.time_contract == "legacy-float-bar-beat-v0"

    exact = snapshot_score_document(_score_v2())
    assert isinstance(exact.score, ScoreV2Document)
    assert exact.score.schema_version == 2
    assert exact.identity_contract == "stable-event-v2"
    assert exact.time_contract == "rational-measure-offset-v2"


@pytest.mark.parametrize(
    "version",
    [True, False, 1.0, 2.5, 3, "1", "2", None],
)
def test_snapshot_version_dispatch_fails_closed(version: object) -> None:
    document = _score()
    document["schema_version"] = version
    with pytest.raises(ValueError, match="schema_version|legacy"):
        snapshot_score_document(document)


def test_v2_integral_float_version_normalizes_without_changing_source_hash() -> None:
    integer_source = _score_v2(schema_version=2)
    float_source = _score_v2(schema_version=2.0)

    integer_snapshot = snapshot_score_document(integer_source)
    float_snapshot = snapshot_score_document(float_source)

    assert integer_snapshot.score == float_snapshot.score
    assert integer_snapshot.score.schema_version == 2
    assert float_snapshot.score.schema_version == 2
    assert integer_snapshot.document["schema_version"] == 2
    assert type(integer_snapshot.document["schema_version"]) is int
    assert float_snapshot.document["schema_version"] == 2.0
    assert type(float_snapshot.document["schema_version"]) is float
    assert integer_snapshot.canonical_bytes != float_snapshot.canonical_bytes
    assert integer_snapshot.document_sha256 != float_snapshot.document_sha256


def test_snapshot_binds_one_generation_before_hash_and_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _score(title="captured")
    expected_document = copy.deepcopy(source)
    expected_bytes = canonical_json_bytes(expected_document)
    real_sha256 = hashlib.sha256
    real_parse = score_source_module.parse_score_document
    parse_calls = 0

    def mutating_sha256(payload: bytes):
        source["title"] = "changed while hashing"
        source["tuning"]["a4_hz"] = 430.0
        return real_sha256(payload)

    def mutating_parse(document: dict):
        nonlocal parse_calls
        parse_calls += 1
        source["parts"][0]["notes"][0]["pitch"] = "G9"
        return real_parse(document)

    monkeypatch.setattr(score_source_module.hashlib, "sha256", mutating_sha256)
    monkeypatch.setattr(score_source_module, "parse_score_document", mutating_parse)

    snapshot = snapshot_score_document(source)

    assert parse_calls == 1
    assert snapshot.canonical_bytes == expected_bytes
    assert snapshot.document_sha256 == real_sha256(expected_bytes).hexdigest()
    assert snapshot.document_copy() == expected_document
    assert snapshot.score.title == "captured"
    assert snapshot.score.parts[0].notes[0].midi == 60.0


def test_snapshot_does_not_expose_mutable_retained_state() -> None:
    snapshot = snapshot_score_document(_score())

    with pytest.raises(TypeError):
        snapshot.document["title"] = "mutated"
    with pytest.raises(TypeError):
        snapshot.document["parts"][0]["notes"] += ({},)
    with pytest.raises(TypeError):
        dict.__setitem__(snapshot.document, "title", "bypass")
    with pytest.raises(TypeError):
        list.append(snapshot.document["parts"], {})
    with pytest.raises(TypeError):
        pickle.dumps(snapshot.document)

    detached = snapshot.document_copy()
    detached_score = snapshot.score
    detached["title"] = "copy"
    detached["parts"][0]["notes"][0]["pitch"] = "G9"
    detached["tuning"]["a4_hz"] = 430.0
    detached_score.tuning["a4_hz"] = 431.0
    object.__setattr__(detached_score.parts[0].notes[0], "midi", 72.0)
    assert detached_score.parts[0].notes[0].midi == 72.0
    assert snapshot.document["title"] == "snapshot"
    assert snapshot.document["parts"][0]["notes"][0]["pitch"] == "C4"
    assert snapshot.score.tuning["a4_hz"] == 442.0
    assert snapshot.score.parts[0].notes[0].midi == 60.0


def test_v2_snapshot_never_exposes_its_retained_typed_value() -> None:
    source = _score_v2()
    expected_bytes = canonical_json_bytes(source)
    snapshot = snapshot_score_document(source)
    typed = snapshot.score

    assert isinstance(typed, ScoreV2Document)
    assert typed is not snapshot.score
    assert typed.parts[0].notes[0].duration_quarters == Rational(1, 7)

    source["title"] = "mutated caller"
    source["parts"][0]["notes"][0]["sounding_pitch"]["midi_note"] = _r(72)
    detached = snapshot.document_copy()
    detached["title"] = "mutated copy"
    detached["parts"][0]["notes"][0]["event_id"] = "changed"

    with pytest.raises(FrozenInstanceError):
        typed.title = "forbidden"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        typed.parts.append(typed.parts[0])  # type: ignore[attr-defined]

    # ``frozen=True`` protects normal callers, not a deliberate call through
    # Python's base object descriptor.  The public typed graph is detached, so
    # even that escape hatch cannot desynchronise the retained document/hash.
    object.__setattr__(
        typed.parts[0].notes[0].sounding_pitch.midi_note,
        "numerator",
        72,
    )
    assert typed.parts[0].notes[0].sounding_pitch.midi_note == Rational(72)

    assert snapshot.canonical_bytes == expected_bytes
    assert snapshot.document["title"] == "exact snapshot"
    assert snapshot.score.title == "exact snapshot"
    assert snapshot.score.parts[0].notes[0].event_id == "event-1"
    assert snapshot.score.parts[0].notes[0].sounding_pitch.midi_note == Rational(60)


def test_snapshot_score_remains_compatible_with_existing_consumers() -> None:
    snapshot = snapshot_score_document(_score())
    exposed = snapshot.score
    assert isinstance(exposed.tuning, dict)
    assert canonical_json_bytes(exposed.tuning) == (
        b'{"a4_hz":442.0,"temperament":"equal"}'
    )

    capability = InstrumentCapability(
        name="snapshot test oscillator",
        relative_path="snapshot-test-oscillator",
        manifest_path="snapshot-test-oscillator/instrument.json",
        implementation_type="oscillator",
        pitched=True,
        note_min=0.0,
        note_max=127.0,
        articulations=("sustain",),
        default_articulation="sustain",
        articulation_source="test",
        onset_seconds=None,
        quality_tier="formal",
        license_status="approved",
    )
    roster = parse_roster_document(
        {
            "name": "snapshot compatibility",
            "assignments": [
                {
                    "part": "lead",
                    "instrument": "snapshot-test-oscillator",
                }
            ],
        },
        {"snapshot-test-oscillator": capability},
    )
    plan = build_plan(snapshot.score, roster)
    assert plan.parts[0].performance["tuning"] == {
        "temperament": "equal",
        "a4_hz": 442.0,
    }
    canonical_json_bytes(plan.to_dict())


def test_snapshot_rejects_nonportable_integer_before_score_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _score()
    document["parts"][0]["notes"][0]["bar"] = JS_SAFE_INTEGER + 1
    parse_called = False

    def unexpected_parse(_document: dict) -> object:
        nonlocal parse_called
        parse_called = True
        raise AssertionError("nonportable integer reached the score parser")

    monkeypatch.setattr(
        score_source_module,
        "parse_score_document",
        unexpected_parse,
    )
    with pytest.raises(AuthoringJsonError) as caught:
        snapshot_score_document(document)
    assert caught.value.code == "integer_outside_js_safe_range"
    assert not parse_called


def test_snapshot_hash_is_sha256_of_the_exposed_canonical_bytes() -> None:
    document = _score()
    document["title"] = "天籁"
    snapshot = snapshot_score_document(document)
    expected = canonical_json_bytes(document)

    assert snapshot.canonical_bytes == expected
    assert snapshot.document_sha256 == hashlib.sha256(expected).hexdigest()
    assert canonical_json_bytes(snapshot.document_copy()) == expected
    assert snapshot.file_identity is None


def test_snapshot_rejects_manual_inconsistent_construction() -> None:
    valid = snapshot_score_document(_score())
    assert not hasattr(
        score_source_module,
        "_SCORE_SNAPSHOT_CONSTRUCTION_TOKEN",
    )
    with pytest.raises(TypeError, match="score-source factory"):
        ScoreSourceSnapshot(
            canonical_bytes=b"{}",
            document_sha256="0" * 64,
            document={},
            score=valid.score,
        )
    with pytest.raises(TypeError, match="cannot be subclassed"):
        class ForgedSnapshot(ScoreSourceSnapshot):
            pass


def test_snapshot_fails_closed_if_parser_mutates_bound_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_parse = score_source_module.parse_score_document

    def mutating_parse(document: dict):
        parsed = real_parse(document)
        document["title"] = "parser mutation"
        return parsed

    monkeypatch.setattr(
        score_source_module,
        "parse_score_document",
        mutating_parse,
    )
    with pytest.raises(RuntimeError, match="parser mutated"):
        snapshot_score_document(_score())


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            b'{"title":"first","title":"second"}',
            "duplicate_object_member",
        ),
        (b'{"title":NaN}', "non_finite_number"),
    ],
)
def test_read_snapshot_rejects_non_strict_json(
    tmp_path: Path,
    payload: bytes,
    code: str,
) -> None:
    path = tmp_path / "score.json"
    path.write_bytes(payload)
    with pytest.raises(AuthoringJsonError) as caught:
        read_score_snapshot(path)
    assert caught.value.code == code


def test_read_snapshot_applies_file_byte_limit_before_json_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "score.json"
    path.write_bytes(b"{}\n")
    parse_called = False

    def unexpected_parse(*_args: object, **_kwargs: object) -> object:
        nonlocal parse_called
        parse_called = True
        raise AssertionError("oversized input reached the JSON parser")

    monkeypatch.setattr(score_source_module, "strict_json_loads", unexpected_parse)
    with pytest.raises(OSError, match="byte limit"):
        read_score_snapshot(path, ProjectLimits(max_score_json_bytes=2))
    assert not parse_called


def test_snapshot_rejects_excessive_depth_and_nodes() -> None:
    nested: object = None
    for _ in range(130):
        nested = [nested]
    too_deep = _score()
    too_deep["unexpected"] = nested
    with pytest.raises(AuthoringJsonError) as depth_error:
        snapshot_score_document(too_deep)
    assert depth_error.value.code == "too_deep"

    too_many_nodes = _score()
    too_many_nodes["unexpected"] = [None] * 5000
    small_limits = ProjectLimits(max_parts=1, max_notes=1)
    with pytest.raises(AuthoringJsonError) as node_error:
        snapshot_score_document(too_many_nodes, small_limits)
    assert node_error.value.code == "too_many_nodes"


def test_in_memory_preflight_handles_deep_values_and_huge_integers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_called = False

    def unexpected_canonical(_document: object) -> bytes:
        nonlocal canonical_called
        canonical_called = True
        raise AssertionError("invalid input reached recursive serialization")

    monkeypatch.setattr(
        score_source_module,
        "canonical_json_bytes",
        unexpected_canonical,
    )

    nested: object = None
    for _ in range(2_000):
        nested = [nested]
    too_deep = _score()
    too_deep["unexpected"] = nested
    with pytest.raises(AuthoringJsonError) as depth_error:
        snapshot_score_document(too_deep)
    assert depth_error.value.code == "too_deep"

    huge_integer = _score()
    huge_integer["schema_version"] = 10**5_000
    with pytest.raises(AuthoringJsonError) as integer_error:
        snapshot_score_document(huge_integer)
    assert integer_error.value.code == "integer_outside_js_safe_range"
    assert not canonical_called


def test_in_memory_document_budget_precedes_canonical_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _score()
    shared = "x" * 1_024
    document["unexpected"] = [shared] * 100
    canonical_called = False

    def unexpected_canonical(_document: object) -> bytes:
        nonlocal canonical_called
        canonical_called = True
        raise AssertionError("oversized value reached canonical materialization")

    monkeypatch.setattr(
        score_source_module,
        "canonical_json_bytes",
        unexpected_canonical,
    )
    with pytest.raises(AuthoringJsonError) as caught:
        snapshot_score_document(
            document,
            ProjectLimits(max_score_json_bytes=4_096),
        )
    assert caught.value.code == "document_too_large"
    assert not canonical_called


def test_in_memory_container_subclass_cannot_bypass_byte_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LyingList(list):
        def __len__(self) -> int:
            return 0

    document = _score()
    document["unexpected"] = LyingList(["x" * 1_024] * 100)
    canonical_called = False

    def unexpected_canonical(_document: object) -> bytes:
        nonlocal canonical_called
        canonical_called = True
        raise AssertionError("container subclass reached canonical encoding")

    monkeypatch.setattr(
        score_source_module,
        "canonical_json_bytes",
        unexpected_canonical,
    )
    with pytest.raises(AuthoringJsonError) as caught:
        snapshot_score_document(document)
    assert caught.value.code == "unsupported_value_type"
    assert not canonical_called


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        ({1: "not a JSON key"}, "non_string_object_key"),
        (("not", "a", "JSON", "array"), "unsupported_value_type"),
    ],
)
def test_in_memory_snapshot_does_not_coerce_non_json_values(
    replacement: object,
    code: str,
) -> None:
    document = _score()
    document["tuning"] = replacement
    with pytest.raises(AuthoringJsonError) as caught:
        snapshot_score_document(document)
    assert caught.value.code == code


def test_snapshot_hash_identifies_source_document_not_parser_projection() -> None:
    implicit_default = _score()
    implicit_default.pop("tail_seconds")
    explicit_default = copy.deepcopy(implicit_default)
    explicit_default["tail_seconds"] = 2.0

    implicit = snapshot_score_document(implicit_default)
    explicit = snapshot_score_document(explicit_default)

    assert implicit.score == explicit.score
    assert implicit.canonical_bytes != explicit.canonical_bytes
    assert implicit.document_sha256 != explicit.document_sha256


def test_snapshot_applies_semantic_resource_limits() -> None:
    document = _score()
    second_note = dict(document["parts"][0]["notes"][0])
    second_note["event_id"] = "note-2"
    second_note["beat"] = 2.0
    document["parts"][0]["notes"].append(second_note)
    with pytest.raises(ResourceLimitError) as caught:
        snapshot_score_document(
            document,
            ProjectLimits(max_notes=1, max_parts=1),
        )
    assert caught.value.code == "score.too_many_notes"


def test_v2_snapshot_applies_shared_part_and_note_resource_limits() -> None:
    document = _score_v2()
    second_note = copy.deepcopy(document["parts"][0]["notes"][0])
    second_note["event_id"] = "event-2"
    second_note["position"]["offset_quarters"] = _r(1)
    document["parts"][0]["notes"].append(second_note)

    with pytest.raises(ResourceLimitError) as caught:
        snapshot_score_document(
            document,
            ProjectLimits(max_notes=1, max_parts=1),
        )
    assert caught.value.code == "score.too_many_notes"


def test_resource_count_gate_runs_before_typed_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _score_v2()
    second = copy.deepcopy(document["parts"][0]["notes"][0])
    second["event_id"] = "event-2"
    second["position"]["offset_quarters"] = _r(1, 7)
    document["parts"][0]["notes"].append(second)
    parser_called = False

    def unexpected_parser(_document: dict) -> object:
        nonlocal parser_called
        parser_called = True
        raise AssertionError("over-budget score reached the typed parser")

    monkeypatch.setattr(
        score_source_module,
        "parse_score_v2_document",
        unexpected_parser,
    )
    with pytest.raises(ResourceLimitError) as caught:
        snapshot_score_document(
            document,
            ProjectLimits(max_notes=1),
        )
    assert caught.value.code == "score.too_many_notes"
    assert not parser_called


@pytest.mark.parametrize("schema_version", [2, 2.0])
def test_read_v2_snapshot_from_path(
    tmp_path: Path,
    schema_version: int | float,
) -> None:
    document = _score_v2(schema_version=schema_version)
    path = tmp_path / "score-v2.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    snapshot = read_score_snapshot(path)

    assert isinstance(snapshot.score, ScoreV2Document)
    assert snapshot.score.schema_version == 2
    assert snapshot.document_copy() == document
    assert snapshot.canonical_bytes == canonical_json_bytes(document)
    assert snapshot.document_sha256 == hashlib.sha256(
        snapshot.canonical_bytes
    ).hexdigest()
    assert snapshot.file_identity is not None


def test_read_snapshot_uses_captured_payload_after_path_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "score.json"
    replacement = tmp_path / "replacement.json"
    path.write_text(json.dumps(_score(title="captured")), encoding="utf-8")
    replacement.write_text(json.dumps(_score(title="replacement")), encoding="utf-8")
    real_read = score_source_module.read_plain_file_bytes

    def replacing_read(value: str | Path, *, maximum_bytes: int):
        identity, payload = real_read(value, maximum_bytes=maximum_bytes)
        replacement.replace(path)
        return identity, payload

    monkeypatch.setattr(
        score_source_module,
        "read_plain_file_bytes",
        replacing_read,
    )
    snapshot = read_score_snapshot(path)

    assert snapshot.document["title"] == "captured"
    assert snapshot.score.title == "captured"
    assert snapshot.file_identity is not None
    assert json.loads(path.read_text(encoding="utf-8"))["title"] == "replacement"
