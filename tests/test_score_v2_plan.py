from __future__ import annotations

import copy
import hashlib

import pytest

import tianlai.score_v2_plan as score_v2_plan_module
from tianlai.canonical_json import canonical_json_bytes
from tianlai.resource_limits import ProjectLimits, ResourceLimitError
from tianlai.score_source import snapshot_score_document
from tianlai.score_v2 import Rational
from tianlai.score_v2_plan import (
    SCORE_V2_PLAN_CONTRACT,
    ScoreV2Plan,
    ScoreV2PlanError,
    compile_score_v2_plan,
)
from tianlai.score_v2_time import ExactFraction


def _r(numerator: int, denominator: int = 1) -> dict[str, int]:
    return {"numerator": numerator, "denominator": denominator}


def _note(
    event_id: str,
    *,
    offset: tuple[int, int],
    duration: tuple[int, int],
    dynamic: str | None = None,
    articulations: list[str] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "event_id": event_id,
        "position": {
            "measure_id": "m1",
            "offset_quarters": _r(*offset),
        },
        "duration_quarters": _r(*duration),
        "written_pitch": {
            "step": "C",
            "alter": _r(0),
            "octave": 4,
        },
        "sounding_pitch": {"midi_note": _r(60)},
    }
    if dynamic is not None:
        result["dynamic"] = dynamic
    if articulations is not None:
        result["articulations"] = articulations
    return result


def _score() -> dict[str, object]:
    return {
        "kind": "tianlai.score",
        "schema_version": 2,
        "title": "plan foundation",
        "timeline": {
            "measures": [
                {
                    "measure_id": "m1",
                    "actual_duration_quarters": _r(4),
                }
            ],
            "meter_events": [
                {
                    "meter_id": "meter-1",
                    "at": {
                        "measure_id": "m1",
                        "offset_quarters": _r(0),
                    },
                    "groups": [4],
                    "beat_unit": 4,
                }
            ],
            "tempo_events": [
                {
                    "tempo_id": "tempo-60",
                    "at": {
                        "measure_id": "m1",
                        "offset_quarters": _r(0),
                    },
                    "quarter_bpm": _r(60),
                }
            ],
        },
        "tuning": {
            "tuning_id": "a440",
            "system": "equal_temperament",
            "divisions_per_octave": 12,
            "reference_midi_note": _r(69),
            "reference_frequency_hz": _r(440),
        },
        "parts": [
            {
                "part_id": "lead",
                "default_dynamic": "mf",
                "default_articulation": "legato",
                "notes": [
                    _note("n1", offset=(0, 1), duration=(1, 1)),
                    _note("n2", offset=(1, 1), duration=(1, 1)),
                ],
            }
        ],
        "form": {"mode": "linear"},
    }


def _profile() -> dict[str, Rational]:
    return {"mf": Rational(3, 5), "f": Rational(4, 5)}


def _compile(
    document: dict[str, object] | None = None,
    *,
    policy: str = "exact",
    profile: dict[str, Rational] | None = None,
    limits: ProjectLimits | None = None,
):
    return compile_score_v2_plan(
        snapshot_score_document(document or _score()),
        sample_rate=8_000,
        sample_time_policy=policy,  # type: ignore[arg-type]
        dynamic_profile=profile or _profile(),
        limits=limits,
    )


def test_exact_plan_is_sealed_and_binds_source_projection_time_and_profile() -> None:
    plan = _compile()
    raw = plan.to_dict()

    assert raw["contract"] == SCORE_V2_PLAN_CONTRACT
    assert raw["sample_time_policy_scope"] == "occurrence_endpoints"
    assert raw["occurrence_order"] == [
        "resolved_start_sample",
        "requested_start_seconds",
        "source_order",
        "occurrence_id",
    ]
    assert raw["occurrence_count"] == 2
    assert raw["occurrences"][0]["start"]["resolved_sample"] == 0
    assert raw["occurrences"][0]["end"]["resolved_sample"] == 8_000
    assert plan.score_duration.to_dict() == raw["score_duration"]
    assert raw["occurrences"][0]["velocity"] == {
        "numerator": "3",
        "denominator": "5",
    }
    assert len(raw["bindings"]["source_document_sha256"]) == 64
    assert len(raw["bindings"]["score_render_projection_sha256"]) == 64
    assert len(raw["bindings"]["time_index_sha256"]) == 64
    assert len(raw["bindings"]["dynamic_profile_sha256"]) == 64
    encoded = canonical_json_bytes(raw)
    assert plan.canonical_json_bytes_size == len(encoded)
    assert plan.artifact_sha256 == hashlib.sha256(encoded).hexdigest()


def test_rounded_endpoint_requires_explicit_adapt_policy() -> None:
    document = _score()
    document["parts"][0]["notes"] = [  # type: ignore[index]
        _note("seventh", offset=(0, 1), duration=(1, 7))
    ]
    with pytest.raises(ScoreV2PlanError) as caught:
        _compile(document)
    assert caught.value.code == "plan.sample_adaptation_not_authorized"

    plan = _compile(document, policy="adapt")
    occurrence = plan.to_dict()["occurrences"][0]
    assert occurrence["end"]["fidelity"] == "rounded"
    assert occurrence["end"]["requested_sample"] == {
        "numerator": "8000",
        "denominator": "7",
    }
    assert occurrence["end"]["resolved_sample"] == 1_143


def test_positive_exact_time_that_collapses_to_one_sample_is_rejected() -> None:
    document = _score()
    document["parts"][0]["notes"] = [  # type: ignore[index]
        _note("tiny", offset=(0, 1), duration=(1, 16001))
    ]
    with pytest.raises(ScoreV2PlanError) as caught:
        _compile(document, policy="adapt")
    assert caught.value.code == "plan.zero_sample_duration"


def test_tie_chain_becomes_one_occurrence_with_all_source_evidence() -> None:
    document = _score()
    document["parts"][0]["notes"][1]["staff"] = 2  # type: ignore[index]
    document["parts"][0]["notes"][1]["voice"] = "lower"  # type: ignore[index]
    document["ties"] = [
        {"tie_id": "tie-1", "from_event_id": "n1", "to_event_id": "n2"}
    ]
    plan = _compile(document)
    assert len(plan.occurrences) == 1
    occurrence = plan.to_dict()["occurrences"][0]
    assert occurrence["source_event_ids"] == ["n1", "n2"]
    assert occurrence["source_tie_ids"] == ["tie-1"]
    assert occurrence["start"]["resolved_sample"] == 0
    assert occurrence["end"]["resolved_sample"] == 16_000
    assert len(occurrence["source_notes"]) == 2
    assert occurrence["source_notes"][1]["staff"] == 2
    assert occurrence["source_notes"][1]["voice"] == "lower"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("dynamic", "f", "plan.tie_dynamic_conflict"),
        ("articulations", ["tenuto"], "plan.tie_articulation_conflict"),
    ],
)
def test_tie_chain_rejects_unresolved_semantic_changes(
    field: str,
    value: object,
    code: str,
) -> None:
    document = _score()
    document["parts"][0]["notes"][1][field] = value  # type: ignore[index]
    document["ties"] = [
        {"tie_id": "tie-1", "from_event_id": "n1", "to_event_id": "n2"}
    ]
    with pytest.raises(ScoreV2PlanError) as caught:
        _compile(document)
    assert caught.value.code == code


def test_multiple_articulations_fail_closed() -> None:
    document = _score()
    document["parts"][0]["notes"][0]["articulations"] = [  # type: ignore[index]
        "tenuto",
        "accent",
    ]
    with pytest.raises(ScoreV2PlanError) as caught:
        _compile(document)
    assert caught.value.code == "plan.multiple_articulations_unsupported"


def test_missing_or_unmapped_dynamic_fails_closed() -> None:
    missing = _score()
    del missing["parts"][0]["default_dynamic"]  # type: ignore[index]
    with pytest.raises(ScoreV2PlanError) as caught:
        _compile(missing)
    assert caught.value.code == "plan.dynamic_unresolved"

    with pytest.raises(ScoreV2PlanError) as caught:
        _compile(profile={"f": Rational(4, 5)})
    assert caught.value.code == "plan.dynamic_unmapped"


def test_phrase_and_extension_semantics_are_not_silently_dropped() -> None:
    phrase = _score()
    phrase["phrases"] = [
        {
            "phrase_id": "phrase-1",
            "part_id": "lead",
            "start": {"measure_id": "m1", "offset_quarters": _r(0)},
            "end": {"measure_id": "m1", "offset_quarters": _r(2)},
        }
    ]
    with pytest.raises(ScoreV2PlanError) as caught:
        _compile(phrase)
    assert caught.value.code == "plan.phrases_unsupported"

    extension = _score()
    extension["extensions"] = [
        {
            "namespace": "example.optional.visual",
            "version": 1,
            "required": False,
            "audible": False,
            "payload": {},
        }
    ]
    with pytest.raises(ScoreV2PlanError) as caught:
        _compile(extension)
    assert caught.value.code == "plan.extensions_unsupported"


def test_dynamic_profile_is_snapshotted_and_validated() -> None:
    profile = _profile()
    plan = _compile(profile=profile)
    profile["mf"] = Rational(1, 10)
    assert plan.to_dict()["occurrences"][0]["velocity"] == {
        "numerator": "3",
        "denominator": "5",
    }

    with pytest.raises(ScoreV2PlanError) as caught:
        _compile(profile={"mf": Rational(0)})
    assert caught.value.code == "plan.invalid_dynamic_profile"


def test_non_v2_snapshot_and_manual_plan_construction_are_rejected() -> None:
    v1 = {
        "schema_version": 1,
        "title": "v1",
        "sample_rate": 8_000,
        "tail_seconds": 0,
        "tempo_map": [
            {"bar": 1, "beat": 1, "bpm": 60, "beats_per_bar": 4, "beat_unit": 4}
        ],
        "parts": [
            {
                "id": "lead",
                "notes": [
                    {
                        "event_id": "n1",
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "C4",
                    }
                ],
            }
        ],
    }
    with pytest.raises(ScoreV2PlanError) as caught:
        compile_score_v2_plan(
            snapshot_score_document(v1),
            sample_rate=8_000,
            sample_time_policy="exact",
            dynamic_profile=_profile(),
        )
    assert caught.value.code == "plan.unsupported_score_contract"
    with pytest.raises(TypeError, match="compile_score_v2_plan"):
        ScoreV2Plan()  # type: ignore[call-arg]


def test_plan_budget_rejects_before_returning_an_oversized_artifact() -> None:
    tiny = ProjectLimits(max_plan_json_bytes=500)
    with pytest.raises(ResourceLimitError) as caught:
        _compile(limits=tiny)
    assert caught.value.code == "plan.document_too_large"

    forged = ProjectLimits()
    object.__setattr__(forged, "max_notes", "250000")
    with pytest.raises(ValueError, match="positive integers"):
        _compile(limits=forged)


def test_plan_budget_is_exact_at_the_public_byte_ceiling() -> None:
    baseline = _compile()
    exact = ProjectLimits(max_plan_json_bytes=baseline.canonical_json_bytes_size)
    assert _compile(limits=exact).artifact_sha256 == baseline.artifact_sha256

    one_byte_short = ProjectLimits(
        max_plan_json_bytes=baseline.canonical_json_bytes_size - 1
    )
    with pytest.raises(ResourceLimitError) as caught:
        _compile(limits=one_byte_short)
    assert caught.value.code == "plan.document_too_large"


def test_occurrence_order_is_sample_then_exact_time_then_source_order() -> None:
    document = _score()
    document["parts"] = [
        {
            "part_id": "a",
            "default_dynamic": "mf",
            "notes": [
                _note("a-zero", offset=(0, 1), duration=(1, 8_000)),
                _note("a-half", offset=(1, 16_000), duration=(1, 8_000)),
            ],
        },
        {
            "part_id": "b",
            "default_dynamic": "mf",
            "notes": [
                _note("b-zero", offset=(0, 1), duration=(1, 8_000)),
            ],
        },
    ]
    plan = _compile(document, policy="adapt")
    assert [item.occurrence_id for item in plan.occurrences] == [
        "a-zero",
        "b-zero",
        "a-half",
    ]


def test_seal_detects_public_field_replacement() -> None:
    plan = _compile()
    object.__setattr__(plan, "sample_rate", 48_000)
    with pytest.raises(ScoreV2PlanError) as caught:
        plan.to_dict()
    assert caught.value.code == "plan.integrity_mismatch"


def test_nested_exact_values_cannot_diverge_from_the_sealed_artifact() -> None:
    plan = _compile()
    velocity = plan.occurrences[0].velocity
    with pytest.raises((AttributeError, TypeError)):
        object.__setattr__(velocity, "_numerator", 1)
    assert str(velocity) == "3/5"
    assert plan.to_dict()["occurrences"][0]["velocity"] == {
        "numerator": "3",
        "denominator": "5",
    }


def test_resealed_typed_occurrence_must_match_canonical_artifact() -> None:
    plan = _compile()
    forged_occurrences = (
        plan.occurrences[0]._replace(velocity=ExactFraction(1, 10)),
    )
    forged_seal = list(plan._identity_seal)
    forged_seal[7] = forged_occurrences
    object.__setattr__(plan, "occurrences", forged_occurrences)
    object.__setattr__(plan, "_identity_seal", tuple(forged_seal))

    with pytest.raises(ScoreV2PlanError) as caught:
        plan.to_dict()
    assert caught.value.code == "plan.integrity_mismatch"


def test_compiler_uses_one_local_score_generation_across_time_compilation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_a = _score()
    source_b = copy.deepcopy(source_a)
    source_b["parts"][0]["notes"][0]["sounding_pitch"]["midi_note"] = _r(62)  # type: ignore[index]
    snapshot_a = snapshot_score_document(source_a)
    snapshot_b = snapshot_score_document(source_b)
    original_hash = snapshot_a.document_sha256
    real_compile = score_v2_plan_module.compile_score_v2_time

    def mutate_original_then_compile(local_snapshot, **kwargs):
        for name in (
            "canonical_bytes",
            "document_sha256",
            "document",
            "_score",
            "file_identity",
        ):
            object.__setattr__(snapshot_a, name, getattr(snapshot_b, name))
        return real_compile(local_snapshot, **kwargs)

    monkeypatch.setattr(
        score_v2_plan_module,
        "compile_score_v2_time",
        mutate_original_then_compile,
    )
    plan = compile_score_v2_plan(
        snapshot_a,
        sample_rate=8_000,
        sample_time_policy="exact",
        dynamic_profile=_profile(),
    )
    assert plan.source_document_sha256 == original_hash
    assert plan.to_dict()["occurrences"][0]["sounding_pitch"]["midi_note"] == {
        "numerator": "60",
        "denominator": "1",
    }


def test_internally_inconsistent_source_snapshot_is_rejected() -> None:
    snapshot = snapshot_score_document(_score())
    object.__setattr__(snapshot, "document_sha256", "0" * 64)
    with pytest.raises(ScoreV2PlanError) as caught:
        compile_score_v2_plan(
            snapshot,
            sample_rate=8_000,
            sample_time_policy="exact",
            dynamic_profile=_profile(),
        )
    assert caught.value.code == "plan.source_snapshot_mismatch"

    # The canonical bytes and their digest are the authoritative captured
    # generation.  A corrupted cached object graph is never traversed and is
    # safely reconstructed from those bytes.
    snapshot = snapshot_score_document(_score())
    object.__setattr__(snapshot, "document", ())
    plan = compile_score_v2_plan(
        snapshot,
        sample_rate=8_000,
        sample_time_policy="exact",
        dynamic_profile=_profile(),
    )
    assert plan.source_document_sha256 == snapshot.document_sha256


def test_source_byte_limit_is_checked_before_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = snapshot_score_document(_score())
    called = False

    def unexpected_rebind(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("oversized source reached snapshot rebind")

    monkeypatch.setattr(
        score_v2_plan_module,
        "snapshot_score_bytes",
        unexpected_rebind,
    )
    limits = ProjectLimits(
        max_score_json_bytes=len(snapshot.canonical_bytes) - 1,
    )
    with pytest.raises(ResourceLimitError) as caught:
        compile_score_v2_plan(
            snapshot,
            sample_rate=8_000,
            sample_time_policy="exact",
            dynamic_profile=_profile(),
            limits=limits,
        )
    assert caught.value.code == "score.document_too_large"
    assert not called


def test_hash_is_stable_for_equivalent_profile_insertion_order() -> None:
    first = _compile(profile={"mf": Rational(3, 5), "f": Rational(4, 5)})
    second = _compile(profile={"f": Rational(4, 5), "mf": Rational(3, 5)})
    assert first.artifact_sha256 == second.artifact_sha256
    assert first.to_dict() == second.to_dict()
