from __future__ import annotations

import copy
from fractions import Fraction
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from tianlai.canonical_json import canonical_json_bytes
from tianlai.resource_limits import ProjectLimits
from tianlai.score_v2 import (
    MAX_SAFE_INTEGER,
    Rational,
    parse_score_v2_document,
    score_render_projection_sha256,
)
from tianlai.score_source import snapshot_score_document
from tianlai.score_v2_migration import (
    MIGRATION_RECEIPT_DOMAIN,
    EventPerformanceFact,
    MigratedPerformanceFacts,
    MigratedRenderSettings,
    MigrationError,
    ScoreV2Migration,
    ScoreV2MigrationReceipt,
    migrate_score_v1_snapshot,
    migrate_score_v1_to_v2,
    migrate_v1_score_to_v2,
    parse_score_v2_migration_document,
    score_v2_migration_json_bytes,
    verify_score_v2_migration_document,
)
from tianlai.score_v2_migration import (
    _IssueCollector,
    _MigratedNote,
    _tie_documents,
)


def _score_v1() -> dict:
    return {
        "schema_version": 1,
        "title": "v1 to exact v2",
        "sample_rate": 96_000,
        "tail_seconds": 1.25,
        "tuning": {"temperament": "equal", "a4_hz": 442.5},
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1,
                "bpm": 120,
                "beats_per_bar": 4,
                "beat_unit": 4,
            },
            {
                "bar": 2,
                "beat": 1,
                "bpm": 96,
                "beats_per_bar": 7,
                "beat_unit": 8,
            },
            {"bar": 2, "beat": 3, "bpm": 123.5},
        ],
        "parts": [
            {
                "id": "violin",
                "name": "Violin",
                "default_dynamic": "mf",
                "default_articulation": "ordinary",
                "notes": [
                    {
                        "event_id": "event-b-flat-start",
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "Bb3",
                        "dynamic": "mp",
                        "articulation": "tenuto",
                        "tie": True,
                        "staff": 1,
                        "voice": "upper",
                        "velocity": 0.75,
                    },
                    {
                        "event_id": "event-b-flat-end",
                        "bar": 1,
                        "beat": 2,
                        "duration_beats": 1,
                        "pitch": "Bb3",
                        "staff": 1,
                        "voice": "upper",
                    },
                    {
                        "event_id": "event-quarter-tone",
                        "bar": 2,
                        "beat": 1,
                        "duration_beats": 0.25,
                        "pitch": 60.5,
                    },
                    {
                        "event_id": "event-midbar",
                        "bar": 2,
                        "beat": 3,
                        "duration_beats": 0.5,
                        "pitch": 61.25,
                    },
                ],
                "phrases": [
                    {
                        "start_bar": 1,
                        "start_beat": 1,
                        "end_bar": 2,
                        "end_beat": 2,
                    }
                ],
            }
        ],
    }


def _notes_by_id(migration: ScoreV2Migration) -> dict[str, object]:
    return {
        note.event_id: note
        for part in migration.score.parts
        for note in part.notes
    }


def test_migration_round_trips_and_preserves_stable_part_and_event_ids() -> None:
    migration = migrate_score_v1_to_v2(_score_v1())

    assert parse_score_v2_document(migration.score.to_dict()) == migration.score
    assert [part.part_id for part in migration.score.parts] == ["violin"]
    assert set(_notes_by_id(migration)) == {
        "event-b-flat-start",
        "event-b-flat-end",
        "event-quarter-tone",
        "event-midbar",
    }
    assert migration.score.identity_contract == "stable-event-v2"
    assert migration.score.time_contract == "rational-measure-offset-v2"
    assert migration.score.form is not None
    assert migration.score.form.mode == "linear"

    serialized = migration.to_dict()
    assert serialized["score"] == migration.score.to_dict()
    assert serialized["receipt_sha256"] == migration.receipt_sha256


def test_migration_is_deterministic_and_does_not_retain_the_source() -> None:
    source = _score_v1()
    first = migrate_score_v1_to_v2(source)
    second = migrate_v1_score_to_v2(copy.deepcopy(source))
    assert first == second
    assert first.to_dict() == second.to_dict()

    source["title"] = "mutated after migration"
    source["parts"][0]["notes"][0]["pitch"] = 12
    assert first.score.title == "v1 to exact v2"
    assert _notes_by_id(first)["event-b-flat-start"].sounding_pitch.midi_note == (
        Rational(58)
    )


def test_trusted_snapshot_core_matches_the_in_memory_convenience_api() -> None:
    source = _score_v1()
    snapshot = snapshot_score_document(source)
    assert migrate_score_v1_snapshot(snapshot) == migrate_score_v1_to_v2(source)
    with pytest.raises(TypeError, match="ScoreSourceSnapshot"):
        migrate_score_v1_snapshot(object())  # type: ignore[arg-type]


def test_exact_mixed_meter_and_midbar_tempo_conversion() -> None:
    migration = migrate_score_v1_to_v2(_score_v1())
    timeline = migration.score.timeline

    assert [measure.measure_id for measure in timeline.measures] == [
        "measure-v1-000001",
        "measure-v1-000002",
    ]
    assert [measure.actual_duration_quarters for measure in timeline.measures] == [
        Rational(4),
        Rational(7, 2),
    ]
    assert [event.groups for event in timeline.meter_events] == [(4,), (7,)]
    assert [event.beat_unit for event in timeline.meter_events] == [4, 8]
    assert [event.meter_id for event in timeline.meter_events] == [
        "meter-v1-000001",
        "meter-v1-000002",
    ]
    assert [event.tempo_id for event in timeline.tempo_events] == [
        "tempo-v1-000001",
        "tempo-v1-000002",
        "tempo-v1-000003",
    ]
    assert timeline.tempo_events[-1].at.measure_id == "measure-v1-000002"
    assert timeline.tempo_events[-1].at.offset_quarters == Rational(1)
    assert timeline.tempo_events[-1].quarter_bpm == Rational(247, 2)

    defaults = [
        issue
        for issue in migration.receipt.issues
        if issue.code == "meter.additive_grouping_defaulted"
    ]
    assert len(defaults) == 1
    assert defaults[0].category == "notation_default"
    assert "2 materialized meter events" in defaults[0].message


def test_named_spelling_survives_and_numeric_microtone_is_derived_explicitly() -> None:
    migration = migrate_score_v1_to_v2(_score_v1())
    notes = _notes_by_id(migration)

    named = notes["event-b-flat-start"]
    assert named.written_pitch.step == "B"
    assert named.written_pitch.alter == Rational(-1)
    assert named.written_pitch.octave == 3
    assert named.written_pitch.accidental == "b"
    assert named.sounding_pitch.midi_note == Rational(58)

    numeric = notes["event-quarter-tone"]
    assert numeric.written_pitch.step == "C"
    assert numeric.written_pitch.alter == Rational(1, 2)
    assert numeric.written_pitch.octave == 4
    assert numeric.sounding_pitch.midi_note == Rational(121, 2)
    assert any(
        issue.code == "pitch.written_spelling_derived"
        and issue.location.endswith("notes[2].pitch")
        for issue in migration.receipt.issues
    )


def test_velocity_and_render_settings_are_separated_without_float_loss() -> None:
    migration = migrate_score_v1_to_v2(_score_v1())
    score_document = migration.score.to_dict()

    assert all(
        "velocity" not in note
        for part in score_document["parts"]
        for note in part["notes"]
    )
    assert migration.render_settings == MigratedRenderSettings(
        sample_rate=96_000,
        tail_seconds=Rational(5, 4),
    )
    assert migration.performance_facts.events == (
        EventPerformanceFact(
            part_id="violin",
            event_id="event-b-flat-start",
            velocity=Rational(3, 4),
        ),
    )
    assert (
        migration.performance_facts.score_document_sha256
        == migration.receipt.target_document_sha256
    )
    assert migration.receipt.render_settings_sha256 == hashlib.sha256(
        canonical_json_bytes(migration.render_settings.to_dict())
    ).hexdigest()
    assert migration.receipt.performance_facts_sha256 == hashlib.sha256(
        canonical_json_bytes(migration.performance_facts.to_dict())
    ).hexdigest()


def test_ties_become_explicit_edges_and_phrases_gain_part_ownership() -> None:
    migration = migrate_score_v1_to_v2(_score_v1())

    assert [tie.to_dict() for tie in migration.score.ties] == [
        {
            "tie_id": "tie-v1-000001",
            "from_event_id": "event-b-flat-start",
            "to_event_id": "event-b-flat-end",
        }
    ]
    assert len(migration.score.phrases) == 1
    phrase = migration.score.phrases[0]
    assert phrase.part_id == "violin"
    assert phrase.phrase_id == "phrase-v1-000001"
    assert phrase.start.measure_id == "measure-v1-000001"
    assert phrase.end.measure_id == "measure-v1-000002"
    assert phrase.end.offset_quarters == Rational(1, 2)


def test_dangling_and_noncontiguous_tie_intent_is_reported_not_silenced() -> None:
    dangling = _score_v1()
    dangling["parts"][0]["notes"][1]["pitch"] = "C4"
    migration = migrate_score_v1_to_v2(dangling)
    assert migration.score.ties == ()
    assert any(
        issue.code == "tie.intent_dangling"
        and issue.location.endswith("notes[0].tie")
        for issue in migration.receipt.issues
    )

    noncontiguous = _score_v1()
    noncontiguous["parts"][0]["notes"][1]["beat"] = 3
    migration = migrate_score_v1_to_v2(noncontiguous)
    assert migration.score.ties == ()
    assert any(
        issue.code == "tie.intent_not_contiguous"
        for issue in migration.receipt.issues
    )


def test_legacy_score_requires_an_explicit_stable_id_upgrade_first() -> None:
    legacy = _score_v1()
    del legacy["schema_version"]
    for note in legacy["parts"][0]["notes"]:
        del note["event_id"]

    with pytest.raises(MigrationError) as captured:
        migrate_score_v1_to_v2(legacy)
    assert captured.value.code == "source.explicit_v1_required"
    assert captured.value.location == "score.schema_version"


def test_one_seventh_float_is_not_silently_approximated() -> None:
    source = _score_v1()
    source["parts"][0]["notes"][2]["duration_beats"] = 1 / 7

    with pytest.raises(MigrationError) as captured:
        migrate_score_v1_to_v2(source)
    assert captured.value.code == "numeric.denominator_exceeds_v2_limit"
    assert captured.value.location.endswith("duration_beats")
    assert "will not approximate" in captured.value.detail


def test_decimal_denominator_boundary_fails_closed() -> None:
    source = _score_v1()
    source["tail_seconds"] = 0.0000001

    with pytest.raises(MigrationError) as captured:
        migrate_score_v1_to_v2(source)
    assert captured.value.code == "numeric.denominator_exceeds_v2_limit"
    assert captured.value.location == "score.tail_seconds"


def test_v1_accepted_numeric_strings_preserve_their_parsed_semantics() -> None:
    source = _score_v1()
    source["tail_seconds"] = "1.25"
    source["tuning"]["a4_hz"] = "442.5"
    source["tempo_map"][2]["bpm"] = "123.5"
    source["parts"][0]["notes"][0]["velocity"] = "0.75"
    source["parts"][0]["notes"][2]["beat"] = "1.0"
    source["parts"][0]["notes"][2]["duration_beats"] = "0.25"

    migration = migrate_score_v1_to_v2(source)
    assert migration.render_settings.tail_seconds == Rational(5, 4)
    assert migration.score.tuning.reference_frequency_hz == Rational(885, 2)
    assert migration.score.timeline.tempo_events[-1].quarter_bpm == Rational(
        247, 2
    )
    assert migration.performance_facts.events[0].velocity == Rational(3, 4)
    assert _notes_by_id(migration)["event-quarter-tone"].duration_quarters == (
        Rational(1, 8)
    )


def test_legacy_float_tolerance_tie_cannot_claim_exact_parity() -> None:
    source = _score_v1()
    source["tempo_map"][0]["beats_per_bar"] = 1_000_002
    source["parts"][0]["notes"][0]["beat"] = 1_000_000
    source["parts"][0]["notes"][1]["beat"] = 1_000_001.000001

    with pytest.raises(MigrationError) as captured:
        migrate_score_v1_to_v2(source)
    assert captured.value.code == "tie.float_tolerance_cannot_be_preserved"
    assert captured.value.location.endswith("notes[0].tie")


def test_tie_parity_uses_legacy_chain_accumulation_not_pairwise_endpoints() -> None:
    def migrated_note(
        index: int,
        *,
        exact_start: int,
        legacy_start: float,
        starts_tie: bool,
    ) -> _MigratedNote:
        return _MigratedNote(
            event_id=f"event-{index}",
            source_location=f"score.parts[0].notes[{index}]",
            start_absolute=Fraction(exact_start),
            end_absolute=Fraction(exact_start + 1),
            legacy_start=legacy_start,
            legacy_duration=1.0,
            sounding=Rational(60),
            staff=None,
            voice=None,
            starts_tie=starts_tie,
        )

    # Each adjacent legacy endpoint is within the old 1e-6 tolerance.  The
    # conductor, however, keeps the chain's first start and accumulated
    # duration, so the two 9e-7 offsets have compounded before event 2.
    notes = (
        migrated_note(0, exact_start=0, legacy_start=0.0, starts_tie=True),
        migrated_note(
            1,
            exact_start=1,
            legacy_start=1.0000009,
            starts_tie=True,
        ),
        migrated_note(
            2,
            exact_start=2,
            legacy_start=2.0000018,
            starts_tie=False,
        ),
    )
    with pytest.raises(MigrationError) as captured:
        _tie_documents((notes,), _IssueCollector())
    assert captured.value.code == "tie.legacy_chain_cannot_be_preserved"
    assert captured.value.location.endswith("notes[1].tie")


def test_source_target_projection_and_receipt_hashes_are_verifiable() -> None:
    source = _score_v1()
    migration = migrate_score_v1_to_v2(source)

    expected_source = hashlib.sha256(canonical_json_bytes(source)).hexdigest()
    expected_target = hashlib.sha256(
        canonical_json_bytes(migration.score.to_dict())
    ).hexdigest()
    expected_receipt = hashlib.sha256(
        MIGRATION_RECEIPT_DOMAIN
        + canonical_json_bytes(migration.receipt.to_dict())
    ).hexdigest()
    assert migration.receipt.source_document_sha256 == expected_source
    assert migration.receipt.target_document_sha256 == expected_target
    assert (
        migration.receipt.target_render_projection_sha256
        == score_render_projection_sha256(migration.score)
    )
    assert migration.receipt_sha256 == expected_receipt
    assert migration.receipt_sha256 != hashlib.sha256(
        canonical_json_bytes(migration.receipt.to_dict())
    ).hexdigest()


def test_empty_v1_phrase_is_an_explicit_nonrepresentable_error() -> None:
    source = _score_v1()
    phrase = source["parts"][0]["phrases"][0]
    phrase.update(
        {
            "end_bar": phrase["start_bar"],
            "end_beat": phrase["start_beat"],
        }
    )
    with pytest.raises(MigrationError) as captured:
        migrate_score_v1_to_v2(source)
    assert captured.value.code == "phrase.nonpositive_extent_not_supported"


def test_public_result_dataclasses_reject_forged_cross_bindings() -> None:
    migration = migrate_score_v1_to_v2(_score_v1())
    wrong_hash = "0" * 64
    with pytest.raises(ValueError, match="bound|hash|match"):
        ScoreV2Migration(
            score=migration.score,
            render_settings=migration.render_settings,
            performance_facts=MigratedPerformanceFacts(
                score_document_sha256=wrong_hash,
                events=(),
            ),
            receipt=migration.receipt,
        )

    forged_receipt = ScoreV2MigrationReceipt(
        source_document_sha256=migration.receipt.source_document_sha256,
        target_document_sha256=wrong_hash,
        target_render_projection_sha256=(
            migration.receipt.target_render_projection_sha256
        ),
        render_settings_sha256=migration.receipt.render_settings_sha256,
        performance_facts_sha256=(
            migration.receipt.performance_facts_sha256
        ),
        issues=(),
    )
    with pytest.raises(ValueError, match="hash|match"):
        ScoreV2Migration(
            score=migration.score,
            render_settings=migration.render_settings,
            performance_facts=migration.performance_facts,
            receipt=forged_receipt,
        )


def test_receipt_binds_render_settings_and_each_performance_fact() -> None:
    migration = migrate_score_v1_to_v2(_score_v1())

    with pytest.raises(ValueError, match="render settings hash"):
        ScoreV2Migration(
            score=migration.score,
            render_settings=MigratedRenderSettings(
                sample_rate=48_000,
                tail_seconds=migration.render_settings.tail_seconds,
            ),
            performance_facts=migration.performance_facts,
            receipt=migration.receipt,
        )

    changed_facts = MigratedPerformanceFacts(
        score_document_sha256=(
            migration.performance_facts.score_document_sha256
        ),
        events=(
            EventPerformanceFact(
                part_id="violin",
                event_id="event-b-flat-start",
                velocity=Rational(1, 2),
            ),
        ),
    )
    with pytest.raises(ValueError, match="performance facts hash"):
        ScoreV2Migration(
            score=migration.score,
            render_settings=migration.render_settings,
            performance_facts=changed_facts,
            receipt=migration.receipt,
        )


def test_post_construction_slot_tampering_is_detected_before_serialization() -> None:
    migration = migrate_score_v1_to_v2(_score_v1())
    object.__setattr__(migration.render_settings, "sample_rate", 48_000)
    with pytest.raises(ValueError, match="render settings hash"):
        migration.to_dict()
    with pytest.raises(ValueError, match="render settings hash"):
        _ = migration.receipt_sha256

    migration = migrate_score_v1_to_v2(_score_v1())
    fact = migration.performance_facts.events[0]
    object.__setattr__(fact, "velocity", Rational(1, 2))
    with pytest.raises(ValueError, match="performance facts hash"):
        migration.to_dict()

    migration = migrate_score_v1_to_v2(_score_v1())
    object.__setattr__(migration.receipt, "issues", ())
    with pytest.raises(ValueError, match="receipt content changed"):
        migration.to_dict()


def test_nested_frozen_artifacts_are_revalidated_before_they_are_hashed() -> None:
    migration = migrate_score_v1_to_v2(_score_v1())
    object.__setattr__(
        migration.render_settings.tail_seconds,
        "denominator",
        0,
    )
    with pytest.raises(ValueError, match="tail_seconds is not a representable"):
        migration.to_dict()

    migration = migrate_score_v1_to_v2(_score_v1())
    object.__setattr__(
        migration.performance_facts.events[0].velocity,
        "numerator",
        MAX_SAFE_INTEGER + 1,
    )
    with pytest.raises(
        ValueError,
        match="performance fact velocity is not a representable",
    ):
        migration.to_dict()

    migration = migrate_score_v1_to_v2(_score_v1())
    object.__setattr__(migration.receipt.issues[0], "message", "")
    with pytest.raises(ValueError, match="migration issue message"):
        migration.to_dict()

    migration = migrate_score_v1_to_v2(_score_v1())
    object.__setattr__(migration.receipt.issues[0], "message", " " * 100_000)
    with pytest.raises(ValueError, match="migration issue message is too long"):
        migration.to_dict()


def test_cross_generation_snapshot_slots_are_rejected() -> None:
    snapshot = snapshot_score_document(_score_v1())
    object.__setattr__(snapshot, "document_sha256", "0" * 64)
    with pytest.raises(MigrationError) as captured:
        migrate_score_v1_snapshot(snapshot)
    assert captured.value.code == "source.snapshot_binding_invalid"


def test_materialized_v1_defaults_have_bounded_receipt_provenance() -> None:
    source = _score_v1()
    for key in ("title", "sample_rate", "tail_seconds", "tuning"):
        del source[key]
    part = source["parts"][0]
    del part["name"]
    del part["default_dynamic"]
    note = part["notes"][2]
    del note["bar"]
    del note["beat"]
    del note["duration_beats"]

    migration = migrate_score_v1_to_v2(source)
    defaults = [
        issue
        for issue in migration.receipt.issues
        if issue.code == "source.implicit_defaults_materialized"
    ]
    assert len(defaults) == 1
    assert "score.sample_rate=1" in defaults[0].message
    assert "score.parts[].notes[].duration_beats=1" in defaults[0].message
    assert migration.receipt.to_dict()["policies"]["implicit_defaults"] == (
        "materialized-and-summarized-in-receipt"
    )


def test_explicit_v1_null_optional_containers_do_not_escape_as_assertions() -> None:
    source = _score_v1()
    source["tuning"] = None
    source["parts"][0]["phrases"] = None

    migration = migrate_score_v1_to_v2(source)
    assert migration.score.phrases == ()
    assert migration.score.tuning.reference_frequency_hz == Rational(440)
    defaults = next(
        issue
        for issue in migration.receipt.issues
        if issue.code == "source.implicit_defaults_materialized"
    )
    assert "score.tuning(null/default)=1" in defaults.message


def test_pathological_tie_intent_uses_a_bounded_auditable_issue_summary() -> None:
    source = _score_v1()
    source["parts"][0]["phrases"] = []
    source["parts"][0]["notes"] = [
        {
            "event_id": f"event-{index:05d}",
            "bar": 1,
            "beat": 1,
            "duration_beats": 1,
            "pitch": "C4",
            "tie": True,
        }
        for index in range(4_100)
    ]

    migration = migrate_score_v1_to_v2(source)
    assert len(migration.receipt.issues) == 4_096
    summary = migration.receipt.issues[-1]
    assert summary.code == "audit.issue_details_summarized"
    assert "tie.intent_not_contiguous=5" in summary.message
    assert "tie.intent_dangling=1" in summary.message


def test_wrapped_source_diagnostics_are_bounded_and_keep_the_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_source(*args: object, **kwargs: object) -> object:
        raise ValueError("x" * 100_000)

    monkeypatch.setattr(
        "tianlai.score_v2_migration.snapshot_score_document",
        reject_source,
    )
    with pytest.raises(MigrationError) as captured:
        migrate_score_v1_to_v2(_score_v1())
    assert captured.value.code == "source.invalid_score"
    assert len(captured.value.detail) <= 4_096
    assert captured.value.detail.endswith("... [truncated]")


def test_v2_text_limits_fail_before_target_output_materialization() -> None:
    source = _score_v1()
    source["parts"][0]["notes"][0]["event_id"] = "e" * 257
    # A large referenced bar would otherwise materialize a large measure list.
    source["parts"][0]["notes"][0]["bar"] = 100_000

    with pytest.raises(MigrationError) as captured:
        migrate_score_v1_to_v2(source)
    assert captured.value.code == "target.identifier_not_representable"
    assert captured.value.location.endswith("notes[0].event_id")


def _rehash_receipt(bundle: dict) -> None:
    bundle["receipt_sha256"] = hashlib.sha256(
        MIGRATION_RECEIPT_DOMAIN
        + canonical_json_bytes(bundle["receipt"])
    ).hexdigest()


def test_external_bundle_parser_serializer_and_source_verifier_round_trip() -> None:
    source = _score_v1()
    snapshot = snapshot_score_document(source)
    migration = migrate_score_v1_snapshot(snapshot)

    payload = score_v2_migration_json_bytes(migration)
    parsed_from_bytes = parse_score_v2_migration_document(payload)
    parsed_from_dict = parse_score_v2_migration_document(migration.to_dict())

    assert parsed_from_bytes == migration
    assert parsed_from_dict == migration
    assert parsed_from_bytes.to_dict() == migration.to_dict()
    assert score_v2_migration_json_bytes(parsed_from_bytes) == payload
    assert (
        verify_score_v2_migration_document(payload, snapshot)
        == migration
    )


@pytest.mark.parametrize(
    ("payload", "boundary_code"),
    [
        (b'{"kind":"a","kind":"b"}', "bundle.invalid_json"),
        (b'{"value":NaN}', "bundle.invalid_json"),
        (b'{"value":Infinity}', "bundle.invalid_json"),
        (b'{"value":9007199254740992}', "bundle.invalid_json"),
    ],
)
def test_external_bundle_bytes_use_strict_portable_json_boundary(
    payload: bytes,
    boundary_code: str,
) -> None:
    with pytest.raises(MigrationError) as captured:
        parse_score_v2_migration_document(payload)
    assert captured.value.code == boundary_code


@pytest.mark.parametrize(
    "mutation",
    [
        lambda bundle: bundle.update({"unsupported": True}),
        lambda bundle: bundle["render_settings"].update({"unsupported": True}),
        lambda bundle: bundle["performance_facts"].update(
            {"unsupported": True}
        ),
        lambda bundle: bundle["performance_facts"]["events"][0].update(
            {"unsupported": True}
        ),
        lambda bundle: bundle["receipt"].update({"unsupported": True}),
        lambda bundle: bundle["receipt"]["source"].update(
            {"unsupported": True}
        ),
        lambda bundle: bundle["receipt"]["target"].update(
            {"unsupported": True}
        ),
        lambda bundle: bundle["receipt"]["separated_artifacts"].update(
            {"unsupported": True}
        ),
        lambda bundle: bundle["receipt"]["policies"].update(
            {"unsupported": True}
        ),
        lambda bundle: bundle["receipt"]["issues"][0].update(
            {"unsupported": True}
        ),
    ],
)
def test_external_bundle_unknown_fields_fail_closed(mutation: object) -> None:
    bundle = migrate_score_v1_to_v2(_score_v1()).to_dict()
    mutation(bundle)  # type: ignore[operator]
    with pytest.raises(MigrationError) as captured:
        parse_score_v2_migration_document(bundle)
    assert captured.value.code == "bundle.unknown_field"


def test_external_bundle_unknown_field_diagnostic_does_not_reflect_huge_key() -> None:
    bundle = migrate_score_v1_to_v2(_score_v1()).to_dict()
    bundle["x" * 100_000] = True

    with pytest.raises(MigrationError) as captured:
        parse_score_v2_migration_document(bundle)
    assert captured.value.code == "bundle.unknown_field"
    assert len(str(captured.value)) < 256


def test_score_v2_unknown_fields_also_fail_closed_at_the_nested_boundary() -> None:
    bundle = migrate_score_v1_to_v2(_score_v1()).to_dict()
    bundle["score"]["unsupported"] = True

    with pytest.raises(MigrationError) as captured:
        parse_score_v2_migration_document(bundle)
    assert captured.value.code == "bundle.invalid_score"
    assert len(str(captured.value)) <= 4_256


@pytest.mark.parametrize(
    "mutation",
    [
        lambda bundle: bundle["score"].update({"title": "forged"}),
        lambda bundle: bundle["render_settings"].update(
            {"sample_rate": 48_000}
        ),
        lambda bundle: bundle["performance_facts"]["events"][0].update(
            {"velocity": {"numerator": 1, "denominator": 2}}
        ),
    ],
)
def test_external_bundle_recomputes_each_embedded_artifact_hash(
    mutation: object,
) -> None:
    bundle = migrate_score_v1_to_v2(_score_v1()).to_dict()
    mutation(bundle)  # type: ignore[operator]
    with pytest.raises(MigrationError) as captured:
        parse_score_v2_migration_document(bundle)
    assert captured.value.code == "bundle.binding_mismatch"


def test_external_bundle_recomputes_projection_and_receipt_hashes() -> None:
    bundle = migrate_score_v1_to_v2(_score_v1()).to_dict()
    bundle["receipt"]["target"]["render_projection_sha256"] = "0" * 64
    _rehash_receipt(bundle)
    with pytest.raises(MigrationError) as captured:
        parse_score_v2_migration_document(bundle)
    assert captured.value.code == "bundle.binding_mismatch"

    bundle = migrate_score_v1_to_v2(_score_v1()).to_dict()
    bundle["receipt_sha256"] = "0" * 64
    with pytest.raises(MigrationError) as captured:
        parse_score_v2_migration_document(bundle)
    assert captured.value.code == "bundle.receipt_hash_mismatch"


def test_source_verifier_rejects_a_fully_rehashed_but_unrelated_target() -> None:
    source = _score_v1()
    snapshot = snapshot_score_document(source)
    bundle = migrate_score_v1_snapshot(snapshot).to_dict()
    bundle["score"]["title"] = "self-consistent forgery"

    score = parse_score_v2_document(bundle["score"])
    target_hash = hashlib.sha256(
        canonical_json_bytes(score.to_dict())
    ).hexdigest()
    bundle["performance_facts"]["score_document_sha256"] = target_hash
    bundle["receipt"]["target"]["document_sha256"] = target_hash
    bundle["receipt"]["target"]["render_projection_sha256"] = (
        score_render_projection_sha256(score)
    )
    bundle["receipt"]["separated_artifacts"][
        "performance_facts_sha256"
    ] = hashlib.sha256(
        canonical_json_bytes(bundle["performance_facts"])
    ).hexdigest()
    _rehash_receipt(bundle)

    assert parse_score_v2_migration_document(bundle).score.title == (
        "self-consistent forgery"
    )
    with pytest.raises(MigrationError) as captured:
        verify_score_v2_migration_document(bundle, snapshot)
    assert captured.value.code == "bundle.transformation_mismatch"


def test_source_verifier_recomputes_the_claimed_source_identity() -> None:
    snapshot = snapshot_score_document(_score_v1())
    bundle = migrate_score_v1_snapshot(snapshot).to_dict()
    bundle["receipt"]["source"]["document_sha256"] = "0" * 64
    _rehash_receipt(bundle)

    assert parse_score_v2_migration_document(bundle).receipt.source_document_sha256 == (
        "0" * 64
    )
    with pytest.raises(MigrationError) as captured:
        verify_score_v2_migration_document(bundle, snapshot)
    assert captured.value.code == "bundle.source_hash_mismatch"


@pytest.mark.parametrize(
    ("slot", "replacement"),
    [
        ("canonical_bytes", b"{}"),
        ("document_sha256", "0" * 64),
        ("document", {}),
    ],
)
def test_source_verifier_rejects_individually_rebound_snapshot_slots(
    slot: str,
    replacement: object,
) -> None:
    snapshot = snapshot_score_document(_score_v1())
    bundle = migrate_score_v1_snapshot(snapshot).to_dict()
    object.__setattr__(snapshot, slot, replacement)

    with pytest.raises(MigrationError) as captured:
        verify_score_v2_migration_document(bundle, snapshot)
    assert captured.value.code == "bundle.invalid_source_snapshot"


def test_source_verifier_does_not_trust_a_tampered_cached_score() -> None:
    snapshot = snapshot_score_document(_score_v1())
    bundle = migrate_score_v1_snapshot(snapshot).to_dict()
    object.__setattr__(snapshot, "_score", object())

    # The verifier rebinds the retained document generation instead of using
    # the mutable cache slot as authority.
    assert verify_score_v2_migration_document(bundle, snapshot).to_dict() == bundle


def test_bundle_fanout_and_byte_limits_fire_before_score_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = migrate_score_v1_to_v2(_score_v1())
    bundle = migration.to_dict()
    bundle["performance_facts"]["events"] = [
        copy.deepcopy(bundle["performance_facts"]["events"][0])
        for _ in range(5)
    ]

    def must_not_parse_score(*args: object, **kwargs: object) -> object:
        raise AssertionError("typed score parser must not run")

    monkeypatch.setattr(
        "tianlai.score_v2_migration.parse_score_v2_document",
        must_not_parse_score,
    )
    with pytest.raises(MigrationError) as captured:
        parse_score_v2_migration_document(
            bundle,
            limits=ProjectLimits(max_notes=4),
        )
    assert captured.value.code == "bundle.too_many_performance_facts"

    tiny_limits = ProjectLimits(max_score_json_bytes=128)
    with pytest.raises(MigrationError) as captured:
        parse_score_v2_migration_document(
            score_v2_migration_json_bytes(migration),
            limits=tiny_limits,
        )
    assert captured.value.code == "bundle.invalid_json"
    with pytest.raises(MigrationError) as captured:
        score_v2_migration_json_bytes(migration, limits=tiny_limits)
    assert captured.value.code == "bundle.serialization_limit"


@pytest.mark.parametrize(
    ("document", "limits", "code"),
    [
        (
            {"score": {"parts": [{"notes": [None] * 5}]}},
            ProjectLimits(max_notes=4),
            "bundle.too_many_notes",
        ),
        (
            {"performance_facts": {"events": [None] * 5}},
            ProjectLimits(max_notes=4),
            "bundle.too_many_performance_facts",
        ),
        (
            {"receipt": {"issues": [None] * 4_097}},
            ProjectLimits(),
            "bundle.too_many_issues",
        ),
    ],
)
def test_in_memory_fanout_is_rejected_before_detach_encoding(
    document: dict,
    limits: ProjectLimits,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_encode(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("over-limit fan-out reached canonical encoding")

    monkeypatch.setattr(
        "tianlai.score_v2_migration.bounded_canonical_json_bytes",
        must_not_encode,
    )
    with pytest.raises(MigrationError) as captured:
        parse_score_v2_migration_document(document, limits=limits)
    assert captured.value.code == code


def test_detached_generation_rechecks_fanout_after_dict_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = migrate_score_v1_to_v2(_score_v1())
    bundle = migration.to_dict()
    emitted = copy.deepcopy(bundle)
    emitted["performance_facts"]["events"] = [
        copy.deepcopy(emitted["performance_facts"]["events"][0])
        for _ in range(5)
    ]

    def emit_changed_generation(*args: object, **kwargs: object) -> bytes:
        return canonical_json_bytes(emitted)

    def must_not_parse_score(*args: object, **kwargs: object) -> object:
        raise AssertionError("over-limit detached fan-out reached score parsing")

    monkeypatch.setattr(
        "tianlai.score_v2_migration.bounded_canonical_json_bytes",
        emit_changed_generation,
    )
    monkeypatch.setattr(
        "tianlai.score_v2_migration.parse_score_v2_document",
        must_not_parse_score,
    )
    with pytest.raises(MigrationError) as captured:
        parse_score_v2_migration_document(
            bundle,
            limits=ProjectLimits(max_notes=4),
        )
    assert captured.value.code == "bundle.too_many_performance_facts"


def test_bundle_rejects_forged_container_subclasses_before_encoding() -> None:
    class LyingDict(dict):
        def __len__(self) -> int:
            return 0

    with pytest.raises(MigrationError) as captured:
        parse_score_v2_migration_document(LyingDict())  # type: ignore[arg-type]
    assert captured.value.code == "bundle.invalid_input_type"


def _migration_schema_validator() -> Draft202012Validator:
    root = Path(__file__).resolve().parents[1]
    bundle_schema = json.loads(
        (root / "schemas" / "score-v2-migration.schema.json").read_text(
            encoding="utf-8"
        )
    )
    score_schema = json.loads(
        (root / "schemas" / "score-v2.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator.check_schema(bundle_schema)
    registry = Registry().with_resource(
        score_schema["$id"],
        Resource.from_contents(score_schema),
    )
    return Draft202012Validator(bundle_schema, registry=registry)


def test_migration_schema_accepts_generated_bundle_and_matches_unknown_policy() -> None:
    validator = _migration_schema_validator()
    bundle = migrate_score_v1_to_v2(_score_v1()).to_dict()
    assert list(validator.iter_errors(bundle)) == []
    assert parse_score_v2_migration_document(bundle).to_dict() == bundle

    score_unknown = copy.deepcopy(bundle)
    score_unknown["score"]["unsupported"] = True
    assert list(validator.iter_errors(score_unknown))
    with pytest.raises(MigrationError) as captured:
        parse_score_v2_migration_document(score_unknown)
    assert captured.value.code == "bundle.invalid_score"

    locations = (
        bundle,
        bundle["render_settings"],
        bundle["performance_facts"],
        bundle["receipt"],
        bundle["receipt"]["source"],
        bundle["receipt"]["target"],
        bundle["receipt"]["separated_artifacts"],
        bundle["receipt"]["policies"],
        bundle["receipt"]["issues"][0],
    )
    for location in locations:
        mutated = copy.deepcopy(bundle)
        # Locate the corresponding mapping by following its unique kind/key
        # shape after copying, keeping this parity check independent of hash
        # validation that happens later.
        if location is bundle:
            target = mutated
        elif location is bundle["render_settings"]:
            target = mutated["render_settings"]
        elif location is bundle["performance_facts"]:
            target = mutated["performance_facts"]
        elif location is bundle["receipt"]:
            target = mutated["receipt"]
        elif location is bundle["receipt"]["source"]:
            target = mutated["receipt"]["source"]
        elif location is bundle["receipt"]["target"]:
            target = mutated["receipt"]["target"]
        elif location is bundle["receipt"]["separated_artifacts"]:
            target = mutated["receipt"]["separated_artifacts"]
        elif location is bundle["receipt"]["policies"]:
            target = mutated["receipt"]["policies"]
        else:
            target = mutated["receipt"]["issues"][0]
        target["unsupported"] = True
        assert list(validator.iter_errors(mutated))
        with pytest.raises(MigrationError) as captured:
            parse_score_v2_migration_document(mutated)
        assert captured.value.code == "bundle.unknown_field"


def test_migration_schema_and_parser_share_integer_and_rational_normalization() -> None:
    validator = _migration_schema_validator()
    canonical = migrate_score_v1_to_v2(_score_v1()).to_dict()
    bundle = copy.deepcopy(canonical)
    bundle["schema_version"] = 1.0
    bundle["render_settings"]["schema_version"] = 1.0
    bundle["receipt"]["schema_version"] = 1.0
    bundle["receipt"]["source"]["schema_version"] = 1.0
    bundle["receipt"]["target"]["schema_version"] = 2.0
    bundle["score"]["schema_version"] = 2.0

    tail = bundle["render_settings"]["tail_seconds"]
    tail["numerator"] *= 2
    tail["denominator"] *= 2
    velocity = bundle["performance_facts"]["events"][0]["velocity"]
    velocity["numerator"] *= 2
    velocity["denominator"] *= 2
    duration = bundle["score"]["parts"][0]["notes"][0][
        "duration_quarters"
    ]
    duration["numerator"] *= 2
    duration["denominator"] *= 2

    assert list(validator.iter_errors(bundle)) == []
    assert parse_score_v2_migration_document(bundle).to_dict() == canonical


@pytest.mark.parametrize(
    "mutation",
    [
        lambda bundle: bundle.update({"kind": "wrong"}),
        lambda bundle: bundle.update({"schema_version": 2}),
        lambda bundle: bundle["render_settings"].update({"kind": "wrong"}),
        lambda bundle: bundle["render_settings"].update({"sample_rate": 1}),
        lambda bundle: bundle["performance_facts"].update({"kind": "wrong"}),
        lambda bundle: bundle["receipt"].update({"kind": "wrong"}),
        lambda bundle: bundle["receipt"]["source"].update(
            {"identity_contract": "wrong"}
        ),
        lambda bundle: bundle["receipt"]["target"].update(
            {"identity_contract": "wrong"}
        ),
        lambda bundle: bundle["receipt"]["policies"].update(
            {"form": "wrong"}
        ),
        lambda bundle: bundle["receipt"]["issues"][0].update(
            {"audible": True}
        ),
    ],
)
def test_migration_schema_and_parser_match_fixed_contracts(
    mutation: object,
) -> None:
    validator = _migration_schema_validator()
    bundle = migrate_score_v1_to_v2(_score_v1()).to_dict()
    mutation(bundle)  # type: ignore[operator]

    assert list(validator.iter_errors(bundle))
    with pytest.raises(MigrationError):
        parse_score_v2_migration_document(bundle)
