from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from tianlai.canonical_json import canonical_json_bytes
import tianlai.score_v2_execution_profile as profile_module
from tianlai.score_v2_execution_profile import (
    DEFAULT_MAX_EXECUTION_PROFILE_JSON_BYTES,
    HARD_MAX_EXECUTION_PROFILE_JSON_BYTES,
    SCORE_V2_EXECUTION_PROFILE_KIND,
    ScoreV2ArticulationPolicy,
    ScoreV2DynamicLevel,
    ScoreV2ExecutionProfile,
    ScoreV2ExecutionProfileError,
    ScoreV2NoteVelocityPolicy,
    ScoreV2PitchPolicy,
    ScoreV2ProfileRational,
    ScoreV2TuningPolicy,
    parse_score_v2_execution_profile,
)


ROOT = Path(__file__).resolve().parents[1]


def _document() -> dict[str, object]:
    return {
        "kind": "tianlai.score_v2_execution_profile",
        "schema_version": 1,
        "sample_time_policy": "exact",
        "dynamic_profile": {
            "p": {"numerator": 2, "denominator": 8},
            "mf": {"numerator": 3, "denominator": 4},
            "fff": {"numerator": 1, "denominator": 1},
        },
        "note_velocity": {
            "value_policy": "exact",
            "semantic_policy": "exact",
        },
        "tuning": {
            "value_policy": "exact",
            "semantic_policy": "exact",
        },
        "pitch": {
            "value_policy": "adapt",
            "semantic_policy": "approximate",
            "range_policy": "verified_high_quality",
        },
        "articulation": {
            "mapping_policy": "allow_roster_mapping",
            "semantic_policy": "approximate",
        },
        "phrase_policy": "reject",
    }


def _schema() -> dict[str, object]:
    return json.loads(
        (ROOT / "schemas" / "score-v2-execution-profile.schema.json")
        .read_text(encoding="utf-8")
    )


def _error_code(callable_: object) -> str:
    with pytest.raises(ScoreV2ExecutionProfileError) as caught:
        callable_()  # type: ignore[operator]
    assert str(caught.value) == caught.value.code
    return caught.value.code


def test_parse_normalizes_and_seals_canonical_artifact() -> None:
    profile = parse_score_v2_execution_profile(_document())

    assert type(profile) is ScoreV2ExecutionProfile
    assert profile.kind == SCORE_V2_EXECUTION_PROFILE_KIND
    assert profile.schema_version == 1
    assert profile.sample_time_policy == "exact"
    assert profile.dynamic_profile == (
        ScoreV2DynamicLevel("p", ScoreV2ProfileRational(1, 4)),
        ScoreV2DynamicLevel("mf", ScoreV2ProfileRational(3, 4)),
        ScoreV2DynamicLevel("fff", ScoreV2ProfileRational(1, 1)),
    )
    assert profile.note_velocity == ScoreV2NoteVelocityPolicy("exact", "exact")
    assert profile.tuning == ScoreV2TuningPolicy("exact", "exact")
    assert profile.pitch == ScoreV2PitchPolicy(
        "adapt", "approximate", "verified_high_quality"
    )
    assert profile.articulation == ScoreV2ArticulationPolicy(
        "allow_roster_mapping", "approximate"
    )
    assert profile.phrase_policy == "reject"

    expected = _document()
    expected["dynamic_profile"] = {
        "p": {"numerator": 1, "denominator": 4},
        "mf": {"numerator": 3, "denominator": 4},
        "fff": {"numerator": 1, "denominator": 1},
    }
    assert profile.to_dict() == expected
    assert profile.canonical_bytes == canonical_json_bytes(expected)
    assert profile.canonical_json_bytes == profile.canonical_bytes
    assert profile.canonical_json_bytes_size == len(profile.canonical_bytes)
    assert profile.artifact_sha256 == hashlib.sha256(
        profile.canonical_bytes
    ).hexdigest()


def test_bytes_and_dictionary_paths_have_one_normalized_identity() -> None:
    document = _document()
    encoded = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")

    from_dict = parse_score_v2_execution_profile(document)
    from_bytes = parse_score_v2_execution_profile(encoded)

    assert from_bytes.to_dict() == from_dict.to_dict()
    assert from_bytes.canonical_bytes == from_dict.canonical_bytes
    assert from_bytes.artifact_sha256 == from_dict.artifact_sha256


def test_schema_mathematical_integers_are_normalized_to_json_integers() -> None:
    document = _document()
    document["schema_version"] = 1.0
    document["dynamic_profile"] = {
        "mf": {"numerator": 3.0, "denominator": 4.0},
    }

    parsed = parse_score_v2_execution_profile(document)

    assert parsed.to_dict()["schema_version"] == 1
    assert parsed.to_dict()["dynamic_profile"] == {
        "mf": {"numerator": 3, "denominator": 4},
    }
    assert list(Draft202012Validator(_schema()).iter_errors(document)) == []


@pytest.mark.parametrize("sample_policy", ["exact", "adapt"])
@pytest.mark.parametrize("value_policy", ["exact", "adapt"])
@pytest.mark.parametrize("semantic_policy", ["exact", "approximate"])
def test_all_declared_policy_values_are_accepted(
    sample_policy: str,
    value_policy: str,
    semantic_policy: str,
) -> None:
    document = _document()
    document["sample_time_policy"] = sample_policy
    document["note_velocity"] = {
        "value_policy": value_policy,
        "semantic_policy": semantic_policy,
    }
    document["tuning"] = {
        "value_policy": value_policy,
        "semantic_policy": semantic_policy,
    }
    document["pitch"] = {
        "value_policy": value_policy,
        "semantic_policy": semantic_policy,
        "range_policy": "declared_hard",
    }
    document["articulation"] = {
        "mapping_policy": "direct_only",
        "semantic_policy": semantic_policy,
    }

    parsed = parse_score_v2_execution_profile(document)
    assert parsed.sample_time_policy == sample_policy
    assert parsed.note_velocity.value_policy == value_policy
    assert parsed.tuning.semantic_policy == semantic_policy
    assert parsed.pitch.semantic_policy == semantic_policy


def test_phrase_policy_cannot_claim_ignore_or_approximation() -> None:
    for forbidden in ("ignore", "approximate", "adapt", None):
        document = _document()
        document["phrase_policy"] = forbidden
        assert _error_code(
            lambda document=document: parse_score_v2_execution_profile(document)
        ) == "execution_profile.invalid_phrase_policy"


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda value: value.update({"unknown": True}),
            "execution_profile.invalid_document_shape",
        ),
        (
            lambda value: value.pop("pitch"),
            "execution_profile.invalid_document_shape",
        ),
        (
            lambda value: value.update({"kind": "other"}),
            "execution_profile.invalid_kind",
        ),
        (
            lambda value: value.update({"schema_version": 2}),
            "execution_profile.unsupported_schema_version",
        ),
        (
            lambda value: value.update({"sample_time_policy": "round"}),
            "execution_profile.invalid_sample_time_policy",
        ),
        (
            lambda value: value.update({"dynamic_profile": {}}),
            "execution_profile.invalid_dynamic_profile",
        ),
        (
            lambda value: value.update(
                {"dynamic_profile": {"secret-mark": {"numerator": 1, "denominator": 2}}}
            ),
            "execution_profile.invalid_dynamic_profile",
        ),
        (
            lambda value: value.update(
                {"dynamic_profile": {"p": {"numerator": 2, "denominator": 1}}}
            ),
            "execution_profile.invalid_dynamic_value",
        ),
        (
            lambda value: value["note_velocity"].update({"unknown": "x"}),
            "execution_profile.invalid_note_velocity",
        ),
        (
            lambda value: value["tuning"].update({"value_policy": "round"}),
            "execution_profile.invalid_tuning",
        ),
        (
            lambda value: value["pitch"].update({"range_policy": "declared"}),
            "execution_profile.invalid_pitch",
        ),
        (
            lambda value: value["articulation"].update(
                {"mapping_policy": "implicit"}
            ),
            "execution_profile.invalid_articulation",
        ),
    ],
)
def test_semantic_failures_have_stable_non_reflective_codes(
    mutate: object,
    code: str,
) -> None:
    document = _document()
    mutate(document)  # type: ignore[operator]
    assert _error_code(
        lambda: parse_score_v2_execution_profile(document)
    ) == code


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            b'{"kind":"first","kind":"second"}',
            "execution_profile.json.duplicate_object_member",
        ),
        (
            b'{"value":NaN}',
            "execution_profile.json.non_finite_number",
        ),
        (
            b'{"value":Infinity}',
            "execution_profile.json.non_finite_number",
        ),
        (
            b'{"value":9007199254740992}',
            "execution_profile.json.integer_outside_js_safe_range",
        ),
        (
            b'{not-json}',
            "execution_profile.json.invalid_json_syntax",
        ),
    ],
)
def test_byte_parser_is_strict(payload: bytes, code: str) -> None:
    assert _error_code(
        lambda: parse_score_v2_execution_profile(payload)
    ) == code


def test_errors_never_echo_arbitrary_input() -> None:
    secret = "DO-NOT-ECHO-THIS-DYNAMIC-MARK"
    document = _document()
    document["dynamic_profile"] = {
        secret: {"numerator": 1, "denominator": 2}
    }

    with pytest.raises(ScoreV2ExecutionProfileError) as caught:
        parse_score_v2_execution_profile(document)

    assert secret not in str(caught.value)
    assert secret not in caught.value.message_key


def test_plain_builtin_types_are_required_at_the_boundary() -> None:
    class DictionarySubclass(dict[str, object]):
        pass

    class BytesSubclass(bytes):
        pass

    assert _error_code(
        lambda: parse_score_v2_execution_profile(DictionarySubclass(_document()))
    ) == "execution_profile.input_must_be_plain_dict_or_bytes"
    assert _error_code(
        lambda: parse_score_v2_execution_profile(BytesSubclass(b"{}"))
    ) == "execution_profile.input_must_be_plain_dict_or_bytes"

    document = _document()
    document["dynamic_profile"] = DictionarySubclass(
        document["dynamic_profile"]  # type: ignore[arg-type]
    )
    assert _error_code(
        lambda: parse_score_v2_execution_profile(document)
    ) == "execution_profile.json.unsupported_value_type"


def test_caller_mutation_and_capture_time_swap_cannot_change_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document()
    original_materializer = profile_module.bounded_canonical_json_bytes

    def materialize_then_destroy(value: object, **kwargs: object) -> bytes:
        payload = original_materializer(value, **kwargs)
        if value is document:
            document.clear()
            document["kind"] = "destroyed-after-capture"
        return payload

    monkeypatch.setattr(
        profile_module,
        "bounded_canonical_json_bytes",
        materialize_then_destroy,
    )
    parsed = parse_score_v2_execution_profile(document)
    identity = parsed.artifact_sha256

    document.clear()
    returned = parsed.to_dict()
    returned["sample_time_policy"] = "adapt"
    returned["dynamic_profile"] = {}

    assert parsed.sample_time_policy == "exact"
    assert parsed.to_dict()["sample_time_policy"] == "exact"
    assert parsed.artifact_sha256 == identity


def test_capture_time_container_race_has_a_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raced_materializer(*_args: object, **_kwargs: object) -> bytes:
        raise RuntimeError("caller-controlled-race-detail")

    monkeypatch.setattr(
        profile_module,
        "bounded_canonical_json_bytes",
        raced_materializer,
    )
    with pytest.raises(ScoreV2ExecutionProfileError) as caught:
        parse_score_v2_execution_profile(_document())
    assert caught.value.code == "execution_profile.capture_failed"
    assert "caller-controlled" not in str(caught.value)


def test_nested_values_are_tuple_backed_and_resist_object_setattr() -> None:
    parsed = parse_score_v2_execution_profile(_document())

    assert type(parsed.dynamic_profile) is tuple
    assert type(parsed.dynamic_profile[0]) is ScoreV2DynamicLevel
    assert type(parsed.dynamic_profile[0].value) is ScoreV2ProfileRational
    with pytest.raises(AttributeError):
        object.__setattr__(parsed.pitch, "value_policy", "exact")
    with pytest.raises(AttributeError):
        object.__setattr__(parsed.dynamic_profile[0].value, "numerator", 999)

    detached_rational = parsed.dynamic_profile[0].value.as_rational()
    object.__setattr__(detached_rational, "numerator", 999)
    assert parsed.dynamic_profile[0].value.numerator == 1


def test_profile_constructor_subclassing_and_attribute_bypass_fail_closed() -> None:
    with pytest.raises(TypeError, match="must be created"):
        ScoreV2ExecutionProfile()
    with pytest.raises(TypeError, match="cannot be subclassed"):

        class ForgedProfile(ScoreV2ExecutionProfile):
            pass

    parsed = parse_score_v2_execution_profile(_document())
    object.__setattr__(parsed, "sample_time_policy", "adapt")
    assert _error_code(lambda: parsed.to_dict()) == (
        "execution_profile.integrity_mismatch"
    )

    empty = object.__new__(ScoreV2ExecutionProfile)
    assert _error_code(lambda: empty.to_dict()) == (
        "execution_profile.integrity_mismatch"
    )

    class HostileValue:
        def __eq__(self, _other: object) -> bool:
            raise RuntimeError("must not call caller equality")

        def __ne__(self, _other: object) -> bool:
            raise RuntimeError("must not call caller inequality")

    hostile = parse_score_v2_execution_profile(_document())
    object.__setattr__(hostile, "kind", HostileValue())
    assert _error_code(lambda: hostile.to_dict()) == (
        "execution_profile.integrity_mismatch"
    )

    broken_seal = parse_score_v2_execution_profile(_document())
    object.__setattr__(broken_seal, "_identity_seal", ["not", "a", "tuple"])
    assert _error_code(lambda: broken_seal.to_dict()) == (
        "execution_profile.integrity_mismatch"
    )

    # No importable construction token is treated as authority.  Even a
    # manually assembled, internally self-consistent-looking object must pass
    # the same semantic parser before its artifact can be consumed.
    forged = object.__new__(ScoreV2ExecutionProfile)
    valid = parse_score_v2_execution_profile(_document())
    invalid_pitch = ScoreV2PitchPolicy(
        "teleport",
        "exact",
        "declared_hard",
    )
    forged_document = valid.to_dict()
    forged_document["pitch"] = invalid_pitch.to_dict()
    forged_bytes = canonical_json_bytes(forged_document)
    forged_hash = hashlib.sha256(forged_bytes).hexdigest()
    for name, value in (
        ("kind", valid.kind),
        ("schema_version", valid.schema_version),
        ("sample_time_policy", valid.sample_time_policy),
        ("dynamic_profile", valid.dynamic_profile),
        ("note_velocity", valid.note_velocity),
        ("tuning", valid.tuning),
        ("pitch", invalid_pitch),
        ("articulation", valid.articulation),
        ("phrase_policy", valid.phrase_policy),
        ("_canonical_bytes", forged_bytes),
        ("_artifact_sha256", forged_hash),
    ):
        object.__setattr__(forged, name, value)
    object.__setattr__(
        forged,
        "_identity_seal",
        (
            forged.kind,
            forged.schema_version,
            forged.sample_time_policy,
            forged.dynamic_profile,
            forged.note_velocity,
            forged.tuning,
            forged.pitch,
            forged.articulation,
            forged.phrase_policy,
            forged_bytes,
            forged_hash,
        ),
    )
    assert _error_code(lambda: forged.to_dict()) == (
        "execution_profile.integrity_mismatch"
    )


def test_byte_budget_is_enforced_before_json_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_parse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("oversized bytes must not reach JSON parsing")

    monkeypatch.setattr(profile_module, "strict_json_loads", unexpected_parse)
    assert _error_code(
        lambda: parse_score_v2_execution_profile(
            b"x" * 129,
            max_document_bytes=128,
        )
    ) == "execution_profile.json.document_too_large"


def test_default_and_hard_byte_ceilings_are_explicit() -> None:
    assert DEFAULT_MAX_EXECUTION_PROFILE_JSON_BYTES == 1024 * 1024
    assert HARD_MAX_EXECUTION_PROFILE_JSON_BYTES == 4 * 1024 * 1024

    for invalid in (
        0,
        True,
        HARD_MAX_EXECUTION_PROFILE_JSON_BYTES + 1,
    ):
        assert _error_code(
            lambda invalid=invalid: parse_score_v2_execution_profile(
                b"{}", max_document_bytes=invalid
            )
        ) == "execution_profile.invalid_resource_limit"

    assert _error_code(
        lambda: parse_score_v2_execution_profile(
            _document(), max_document_bytes=64
        )
    ) == "execution_profile.json.document_too_large"


def test_schema_is_valid_and_accepts_every_parser_emitted_artifact() -> None:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)

    artifact = parse_score_v2_execution_profile(_document()).to_dict()
    assert validator.is_valid(artifact)


def test_schema_and_parser_reject_the_same_structural_contract_violations() -> None:
    validator = Draft202012Validator(_schema())
    invalid_documents: list[dict[str, object]] = []

    missing = _document()
    missing.pop("note_velocity")
    invalid_documents.append(missing)

    root_unknown = _document()
    root_unknown["unknown"] = 1
    invalid_documents.append(root_unknown)

    wrong_kind = _document()
    wrong_kind["kind"] = "wrong"
    invalid_documents.append(wrong_kind)

    empty_profile = _document()
    empty_profile["dynamic_profile"] = {}
    invalid_documents.append(empty_profile)

    dynamic_unknown = _document()
    dynamic_unknown["dynamic_profile"] = {
        "sfz": {"numerator": 1, "denominator": 2}
    }
    invalid_documents.append(dynamic_unknown)

    rational_unknown = _document()
    rational_unknown["dynamic_profile"] = {
        "p": {"numerator": 1, "denominator": 2, "unit": "ratio"}
    }
    invalid_documents.append(rational_unknown)

    nested_unknown = _document()
    nested_unknown["pitch"]["unknown"] = True  # type: ignore[index]
    invalid_documents.append(nested_unknown)

    wrong_phrase_policy = _document()
    wrong_phrase_policy["phrase_policy"] = "ignore"
    invalid_documents.append(wrong_phrase_policy)

    zero_dynamic = _document()
    zero_dynamic["dynamic_profile"] = {
        "p": {"numerator": 0, "denominator": 1}
    }
    invalid_documents.append(zero_dynamic)

    for document in invalid_documents:
        assert not validator.is_valid(document)
        with pytest.raises(ScoreV2ExecutionProfileError):
            parse_score_v2_execution_profile(document)


def test_unit_interval_cross_field_rule_is_parser_enforced() -> None:
    document = _document()
    document["dynamic_profile"] = {
        "p": {"numerator": 3, "denominator": 2}
    }

    # Portable draft-2020-12 JSON Schema has no keyword that compares sibling
    # integer properties.  The schema documents this one semantic supplement,
    # while the authoritative parser enforces it.
    unit_rational = _schema()["$defs"]["unitRational"]  # type: ignore[index]
    assert "numerator <= denominator" in unit_rational["$comment"]  # type: ignore[index]
    assert _error_code(
        lambda: parse_score_v2_execution_profile(document)
    ) == "execution_profile.invalid_dynamic_value"


def test_to_dict_is_a_fresh_defensive_copy() -> None:
    parsed = parse_score_v2_execution_profile(_document())
    first = parsed.to_dict()
    second = parsed.to_dict()

    assert first == second
    assert first is not second
    assert first["dynamic_profile"] is not second["dynamic_profile"]


def test_input_document_is_not_modified_during_normalization() -> None:
    document = _document()
    original = copy.deepcopy(document)

    parse_score_v2_execution_profile(document)

    assert document == original
