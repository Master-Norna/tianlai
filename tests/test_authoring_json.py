from __future__ import annotations

import math

import pytest

import tianlai.authoring_json as authoring_json_module
from tianlai.authoring_json import (
    AuthoringJsonError,
    AuthoringJsonLimits,
    JS_SAFE_INTEGER,
    bounded_canonical_json_bytes,
    json_document_bytes,
    strict_json_loads,
    validate_json_value,
    validate_request_size,
)
from tianlai.canonical_json import canonical_json_bytes


def _error_code(call) -> str:
    with pytest.raises(AuthoringJsonError) as captured:
        call()
    return captured.value.code


def test_strict_loader_accepts_unicode_spaces_and_utf8_bom() -> None:
    document = strict_json_loads(
        b"\xef\xbb\xbf"
        + '{"title":"天籁 工程","parts":[]}'.encode("utf-8")
    )
    assert document == {"title": "天籁 工程", "parts": []}


def test_strict_loader_rejects_duplicate_members_and_constants() -> None:
    assert _error_code(lambda: strict_json_loads('{"a":1,"a":2}')) == (
        "duplicate_object_member"
    )
    assert _error_code(lambda: strict_json_loads('{"a":NaN}')) == (
        "non_finite_number"
    )
    assert _error_code(lambda: strict_json_loads('{"a":Infinity}')) == (
        "non_finite_number"
    )


def test_strict_loader_rejects_text_and_bytes_subclasses() -> None:
    class DerivedText(str):
        pass

    class DerivedBytes(bytes):
        pass

    assert _error_code(lambda: strict_json_loads(DerivedText("{}"))) == (
        "text_or_bytes_required"
    )
    assert _error_code(lambda: strict_json_loads(DerivedBytes(b"{}"))) == (
        "text_or_bytes_required"
    )


def test_value_gate_rejects_nonfinite_and_non_json_values() -> None:
    assert _error_code(lambda: validate_json_value({"x": math.inf})) == (
        "non_finite_number"
    )
    assert _error_code(lambda: validate_json_value({"x": (1, 2)})) == (
        "unsupported_value_type"
    )
    assert _error_code(lambda: validate_json_value({1: "x"})) == (
        "non_string_object_key"
    )

    class LyingList(list):
        def __len__(self) -> int:
            return 0

    class LyingDict(dict):
        def __len__(self) -> int:
            return 0

    assert _error_code(
        lambda: validate_json_value({"x": LyingList(["x"] * 100)})
    ) == "unsupported_value_type"
    assert _error_code(
        lambda: validate_json_value({"x": LyingDict({"y": "z"})})
    ) == "unsupported_value_type"


def test_integer_gate_uses_javascript_safe_range_without_treating_bool_as_int() -> None:
    assert validate_json_value({"enabled": True}) == {"enabled": True}
    failure = None
    try:
        validate_json_value({"value": JS_SAFE_INTEGER + 1})
    except AuthoringJsonError as exc:
        failure = exc
    assert failure is not None
    assert failure.code == "integer_outside_js_safe_range"
    assert failure.location_segments == ("value",)


def test_local_document_reader_can_opt_out_of_javascript_integer_policy() -> None:
    value = JS_SAFE_INTEGER + 1
    assert strict_json_loads(
        f'{{"value":{value}}}',
        require_js_safe_integers=False,
    ) == {"value": value}
    assert validate_json_value(
        {"value": value},
        require_js_safe_integers=False,
    ) == {"value": value}


def test_depth_node_string_and_container_limits_are_enforced() -> None:
    shallow_limits = AuthoringJsonLimits(
        max_document_bytes=1024,
        max_depth=3,
        max_nodes=100,
        max_string_bytes=4,
        max_array_items=2,
        max_object_members=2,
    )
    assert _error_code(
        lambda: validate_json_value(
            {"a": {"b": {"c": 1}}},
            limits=shallow_limits,
        )
    ) == "too_deep"
    assert _error_code(
        lambda: validate_json_value(
            {"a": "12345"},
            limits=shallow_limits,
        )
    ) == "string_too_large"
    assert _error_code(
        lambda: validate_json_value(
            {"a": [1, 2, 3]},
            limits=shallow_limits,
        )
    ) == "array_too_large"
    assert _error_code(
        lambda: validate_json_value(
            {"a": 1, "b": 2, "c": 3},
            limits=shallow_limits,
        )
    ) == "object_too_large"

    node_limits = AuthoringJsonLimits(
        max_document_bytes=1024,
        max_depth=10,
        max_nodes=4,
        max_string_bytes=32,
        max_array_items=10,
        max_object_members=10,
    )
    assert _error_code(
        lambda: validate_json_value({"a": 1, "b": 2}, limits=node_limits)
    ) == "too_many_nodes"


def test_serialization_is_deterministic_finite_and_size_bounded() -> None:
    assert json_document_bytes({"曲名": "夜 风", "a": 1}) == (
        '{\n  "a": 1,\n  "曲名": "夜 风"\n}\n'.encode("utf-8")
    )
    limits = AuthoringJsonLimits(max_document_bytes=8)
    assert _error_code(
        lambda: json_document_bytes({"value": "long"}, limits=limits)
    ) == "document_too_large"


def test_value_gate_counts_repeated_references_before_materialization() -> None:
    shared = "x" * 1_024
    value = {"repeated": [shared] * 100}
    limits = AuthoringJsonLimits(
        max_document_bytes=4_096,
        max_string_bytes=2_048,
        max_array_items=200,
    )

    with pytest.raises(AuthoringJsonError) as caught:
        validate_json_value(value, limits=limits)
    assert caught.value.code == "document_too_large"
    assert caught.value.actual is not None
    assert caught.value.actual > limits.max_document_bytes


def test_value_gate_canonical_size_matches_the_project_encoder() -> None:
    value = {
        "quoted": '"\\\n\x00天',
        "values": [True, False, None, 1, -0.0],
    }
    exact_size = len(canonical_json_bytes(value))
    exact_limits = AuthoringJsonLimits(max_document_bytes=exact_size)
    assert validate_json_value(value, limits=exact_limits) == value

    with pytest.raises(AuthoringJsonError) as caught:
        validate_json_value(
            value,
            limits=AuthoringJsonLimits(
                max_document_bytes=exact_size - 1,
            ),
        )
    assert caught.value.code == "document_too_large"


def test_bounded_canonical_encoder_matches_project_encoder() -> None:
    value = {
        "quoted": '"\\\n\x00天籁',
        "values": [True, False, None, 1, -0.0, 1.25],
    }
    assert bounded_canonical_json_bytes(value) == canonical_json_bytes(value)


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        {"z": 1, "a": 2, "中": 3},
        [0, -1, 1.25, -0.0, 1e-300, 1e300],
        {"controls": "\b\f\n\r\t\x00\x1f"},
        {"astral": "𝄞", "slash": "/", "quote": '"\\'},
        {"nested": [{"empty": []}, {}, [True, False, None]]},
    ],
)
def test_bounded_canonical_encoder_matches_all_canonical_token_classes(
    value: object,
) -> None:
    assert bounded_canonical_json_bytes(
        value,
        require_object=False,
    ) == canonical_json_bytes(value)


def test_bounded_canonical_encoder_rechecks_growth_during_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value: dict[str, object] = {"repeated": []}
    shared = "x" * 1_024
    real_validate = authoring_json_module.validate_json_value

    def validate_then_mutate(
        candidate: object,
        **kwargs: object,
    ) -> dict[str, object] | object:
        result = real_validate(candidate, **kwargs)
        assert isinstance(candidate, dict)
        candidate["repeated"] = [shared] * 100
        return result

    monkeypatch.setattr(
        authoring_json_module,
        "validate_json_value",
        validate_then_mutate,
    )
    with pytest.raises(AuthoringJsonError) as caught:
        bounded_canonical_json_bytes(
            value,
            limits=AuthoringJsonLimits(
                max_document_bytes=4_096,
                max_string_bytes=2_048,
                max_array_items=200,
            ),
        )
    assert caught.value.code == "document_too_large"
    assert caught.value.actual is not None
    assert caught.value.actual > 4_096


def test_bounded_canonical_encoder_rejects_swapped_giant_string_before_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value: dict[str, object] = {"text": "small"}
    giant = "x" * 100_000
    real_validate = authoring_json_module.validate_json_value
    real_dumps = authoring_json_module.json.dumps

    def validate_then_mutate(
        candidate: object,
        **kwargs: object,
    ) -> dict[str, object] | object:
        result = real_validate(candidate, **kwargs)
        assert isinstance(candidate, dict)
        candidate["text"] = giant
        return result

    def guarded_dumps(candidate: object, *args: object, **kwargs: object) -> str:
        if candidate is giant:
            raise AssertionError("oversized string reached JSON escaping")
        return real_dumps(candidate, *args, **kwargs)

    monkeypatch.setattr(
        authoring_json_module,
        "validate_json_value",
        validate_then_mutate,
    )
    monkeypatch.setattr(authoring_json_module.json, "dumps", guarded_dumps)
    with pytest.raises(AuthoringJsonError) as caught:
        bounded_canonical_json_bytes(
            value,
            limits=AuthoringJsonLimits(
                max_document_bytes=4_096,
                max_string_bytes=1_024,
            ),
        )
    assert caught.value.code == "string_too_large"


@pytest.mark.parametrize("as_key", [False, True])
def test_encoder_rejects_escape_expansion_before_allocating_the_chunk(
    as_key: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value: dict[str, object] = {"text": "small"}
    expanded = "\x00" * 1_000
    real_validate = authoring_json_module.validate_json_value
    real_dumps = authoring_json_module.json.dumps

    def validate_then_mutate(
        candidate: object,
        **kwargs: object,
    ) -> dict[str, object] | object:
        result = real_validate(candidate, **kwargs)
        assert isinstance(candidate, dict)
        candidate.clear()
        if as_key:
            candidate[expanded] = 1
        else:
            candidate["text"] = expanded
        return result

    def guarded_dumps(candidate: object, *args: object, **kwargs: object) -> str:
        if candidate is expanded:
            raise AssertionError("expanded string reached JSON escaping")
        return real_dumps(candidate, *args, **kwargs)

    monkeypatch.setattr(
        authoring_json_module,
        "validate_json_value",
        validate_then_mutate,
    )
    monkeypatch.setattr(authoring_json_module.json, "dumps", guarded_dumps)
    with pytest.raises(AuthoringJsonError) as caught:
        bounded_canonical_json_bytes(
            value,
            limits=AuthoringJsonLimits(
                max_document_bytes=128,
                max_string_bytes=2_048,
            ),
        )
    assert caught.value.code == "document_too_large"


def test_authoritative_encoder_rechecks_container_and_node_limits_after_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value: dict[str, object] = {"items": []}
    real_validate = authoring_json_module.validate_json_value

    def validate_then_mutate(
        candidate: object,
        **kwargs: object,
    ) -> dict[str, object] | object:
        result = real_validate(candidate, **kwargs)
        assert isinstance(candidate, dict)
        candidate["items"] = [None] * 20
        return result

    monkeypatch.setattr(
        authoring_json_module,
        "validate_json_value",
        validate_then_mutate,
    )
    with pytest.raises(AuthoringJsonError) as caught:
        bounded_canonical_json_bytes(
            value,
            limits=AuthoringJsonLimits(
                max_document_bytes=1_024,
                max_nodes=10,
                max_array_items=10,
            ),
        )
    assert caught.value.code == "array_too_large"


def test_human_readable_encoder_rechecks_growth_during_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value: dict[str, object] = {"repeated": []}
    shared = "x" * 1_024
    real_validate = authoring_json_module.validate_json_value

    def validate_then_mutate(
        candidate: object,
        **kwargs: object,
    ) -> dict[str, object] | object:
        result = real_validate(candidate, **kwargs)
        assert isinstance(candidate, dict)
        candidate["repeated"] = [shared] * 100
        return result

    monkeypatch.setattr(
        authoring_json_module,
        "validate_json_value",
        validate_then_mutate,
    )
    with pytest.raises(AuthoringJsonError) as caught:
        json_document_bytes(
            value,
            limits=AuthoringJsonLimits(
                max_document_bytes=4_096,
                max_string_bytes=2_048,
                max_array_items=200,
            ),
        )
    assert caught.value.code == "document_too_large"


def test_loader_rejects_oversize_before_parsing() -> None:
    limits = AuthoringJsonLimits(max_document_bytes=8)
    assert _error_code(
        lambda: strict_json_loads('{"value":1}', limits=limits)
    ) == "document_too_large"


def test_combined_save_request_has_an_independent_budget() -> None:
    documents = {
        "score": {"a": "一"},
        "roster": {"b": "二"},
    }
    total = validate_request_size(documents, maximum_bytes=1024)
    assert total > 0
    assert _error_code(
        lambda: validate_request_size(documents, maximum_bytes=total - 1)
    ) == "request_too_large"


def test_error_projection_never_contains_values_or_paths() -> None:
    try:
        validate_json_value({"secret": JS_SAFE_INTEGER + 1})
    except AuthoringJsonError as exc:
        issue = exc.to_issue(source="score")
    else:  # pragma: no cover - defensive
        raise AssertionError("expected AuthoringJsonError")
    assert issue == {
        "code": "integer_outside_js_safe_range",
        "message_key": "authoringJson.integer_outside_js_safe_range",
        "source": "score",
        "severity": "error",
        "decision": "block",
        "location": {"segments": ["secret"]},
    }
    assert "C:\\" not in repr(issue)
