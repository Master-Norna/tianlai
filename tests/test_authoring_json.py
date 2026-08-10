from __future__ import annotations

import math

import pytest

from tianlai.authoring_json import (
    AuthoringJsonError,
    AuthoringJsonLimits,
    JS_SAFE_INTEGER,
    json_document_bytes,
    strict_json_loads,
    validate_json_value,
    validate_request_size,
)


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
