from __future__ import annotations

import copy

import pytest

from tianlai.workflow_binding import validate_workflow_authorization


def _binding() -> dict[str, object]:
    return {
        "workflow_id": "1" * 32,
        "project_id": "2" * 32,
        "reservation_revision": "3" * 64,
        "iteration_number": 1,
        "operation_id": "4" * 32,
        "authoring_revision": "5" * 64,
        "candidate_work_id": "workflow-work",
        "candidate_id": "workflow-candidate",
        "parent_work_id": None,
        "parent_candidate_id": None,
        "parent_manifest_sha256": None,
    }


def test_workflow_authorization_is_exact_and_detached() -> None:
    source = _binding()
    normalized = validate_workflow_authorization(source, allow_none=False)

    assert normalized == source
    assert normalized is not source
    source["candidate_id"] = "mutated"
    assert normalized["candidate_id"] == "workflow-candidate"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workflow_id", "A" * 32),
        ("project_id", "2" * 31),
        ("reservation_revision", "3" * 63),
        ("iteration_number", True),
        ("iteration_number", 0),
        ("iteration_number", 257),
        ("operation_id", "x" * 32),
        ("authoring_revision", "5" * 65),
        ("candidate_work_id", "bad/work"),
        ("candidate_id", "../escape"),
        ("candidate_id", "bad/name"),
        ("candidate_id", "bad\x00name"),
        ("candidate_id", "x" * 129),
        ("parent_candidate_id", "."),
    ],
)
def test_workflow_authorization_rejects_invalid_identity(
    field: str,
    value: object,
) -> None:
    document = _binding()
    document[field] = value

    with pytest.raises(ValueError):
        validate_workflow_authorization(document, allow_none=False)


def test_workflow_authorization_rejects_unknown_or_missing_fields() -> None:
    extra = _binding()
    extra["claim_human_approval"] = True
    missing = copy.deepcopy(_binding())
    del missing["operation_id"]

    with pytest.raises(ValueError, match="invalid shape"):
        validate_workflow_authorization(extra, allow_none=False)
    with pytest.raises(ValueError, match="invalid shape"):
        validate_workflow_authorization(missing, allow_none=False)


def test_none_is_allowed_only_for_explicitly_unmanaged_render() -> None:
    assert validate_workflow_authorization(None) is None
    with pytest.raises(ValueError, match="required"):
        validate_workflow_authorization(None, allow_none=False)


def test_parent_locator_is_atomic() -> None:
    document = _binding()
    document["parent_candidate_id"] = "parent"

    with pytest.raises(ValueError, match="entirely null or complete"):
        validate_workflow_authorization(document, allow_none=False)

    document["parent_work_id"] = "work"
    document["parent_manifest_sha256"] = "6" * 64
    normalized = validate_workflow_authorization(document, allow_none=False)
    assert normalized["parent_candidate_id"] == "parent"


def test_workflow_authorization_rejects_unencodable_candidate_identity() -> None:
    document = _binding()
    document["candidate_id"] = "\ud800"

    with pytest.raises(ValueError, match="candidate_id is invalid"):
        validate_workflow_authorization(document, allow_none=False)
