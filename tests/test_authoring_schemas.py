from __future__ import annotations

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from tianlai.authoring_core import (
    READINESS_ISSUE_SOURCES,
    build_authoring_snapshot,
)
from tianlai.authoring_project import create_authoring_project


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
AUTHORING_SCHEMAS = (
    "score.schema.json",
    "render-profile.schema.json",
    "authoring-roster.schema.json",
    "authoring-project-storage.schema.json",
    "authoring-project-snapshot.schema.json",
)


def _schema(name: str) -> dict[str, object]:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


def _registry() -> Registry:
    resources = []
    for name in AUTHORING_SCHEMAS:
        document = _schema(name)
        resources.append((document["$id"], Resource.from_contents(document)))
    return Registry().with_resources(resources)


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(
        _schema(name),
        registry=_registry(),
        format_checker=FormatChecker(),
    )


def test_authoring_schemas_are_valid_draft_2020_12() -> None:
    for name in AUTHORING_SCHEMAS:
        Draft202012Validator.check_schema(_schema(name))
    issue_source = _schema("authoring-project-snapshot.schema.json")["$defs"][
        "issue"
    ]["properties"]["source"]["enum"]
    assert set(issue_source) == set(READINESS_ISSUE_SOURCES)


def test_blank_snapshot_validates_and_duration_sample_rate_are_never_null(
    tmp_path: Path,
) -> None:
    root = tmp_path / "Unicode 工程"
    state = create_authoring_project(root, title="空白作品")
    snapshot = build_authoring_snapshot(state, project_root=root)
    validator = _validator("authoring-project-snapshot.schema.json")

    validator.validate(snapshot)
    assert snapshot["readiness"]["summary"]["duration_seconds"] == 0.0
    assert snapshot["readiness"]["summary"]["sample_rate"] == 48_000
    for key in ("duration_seconds", "sample_rate"):
        invalid = copy.deepcopy(snapshot)
        invalid["readiness"]["summary"][key] = None
        assert list(validator.iter_errors(invalid))


def test_durable_project_revision_and_save_event_metadata_validate_exactly(
    tmp_path: Path,
) -> None:
    root = tmp_path / "durable metadata"
    state = create_authoring_project(root, title="Metadata")
    validator = _validator("authoring-project-storage.schema.json")
    project_manifest = json.loads(
        (root / "tianlai-project.json").read_text(encoding="utf-8")
    )
    revision_manifest = json.loads(
        (
            root
            / ".tianlai"
            / "revisions"
            / state.revision
            / "revision.json"
        ).read_text(encoding="utf-8")
    )
    save_event = json.loads(
        (
            root
            / ".tianlai"
            / "save-events"
            / f"{state.save_event_sha256}.json"
        ).read_text(encoding="utf-8")
    )

    validator.validate(project_manifest)
    validator.validate(revision_manifest)
    validator.validate(save_event)
    for document in (project_manifest, revision_manifest):
        invalid = copy.deepcopy(document)
        invalid["created_at_utc"] = "2026-08-09T12:34:56+00:00"
        assert list(validator.iter_errors(invalid))


def test_snapshot_contract_rejects_paths_unknown_fields_and_status_drift(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    state = create_authoring_project(root, title="Score")
    snapshot = build_authoring_snapshot(state, project_root=root)
    validator = _validator("authoring-project-snapshot.schema.json")

    with_path = copy.deepcopy(snapshot)
    with_path["project"]["project_root"] = str(root)
    assert list(validator.iter_errors(with_path))
    contradictory = copy.deepcopy(snapshot)
    contradictory["readiness"]["status"] = "ready"
    contradictory["readiness"]["render_allowed"] = False
    assert list(validator.iter_errors(contradictory))
