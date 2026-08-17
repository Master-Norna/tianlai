from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from tianlai.canonical_json import canonical_json_bytes
from tianlai.cli import main as cli_main
from tianlai.score_v2_migration import (
    MigrationError,
    migrate_score_v1_to_v2,
    parse_score_v2_migration_document,
)


def _score_v1() -> dict[str, object]:
    return {
        "schema_version": 1,
        "title": "CLI migration",
        "sample_rate": 48_000,
        "tail_seconds": 1,
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1,
                "bpm": 120,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "part-1",
                "name": "Part",
                "notes": [
                    {
                        "event_id": "note-1",
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "C4",
                        "velocity": 0.75,
                    }
                ],
            }
        ],
    }


def _write_score(path: Path, document: dict[str, object]) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )


def _arguments(source: Path, output: Path, *extra: str) -> list[str]:
    return [
        "migrate-score-v2",
        "--score",
        str(source),
        "--output",
        str(output),
        *extra,
    ]


def test_migrate_score_v2_help_is_explicit_about_the_non_rendering_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as captured:
        cli_main(["migrate-score-v2", "--help"])

    assert captured.value.code == 0
    help_text = capsys.readouterr().out
    assert "score-v1" in help_text
    assert "migration bundle" in help_text
    assert "does not render score-v2" in help_text


def test_migrate_score_v2_writes_a_parseable_complete_bundle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_document = _score_v1()
    source = tmp_path / "source.score.json"
    output = tmp_path / "migration.json"
    _write_score(source, source_document)

    assert cli_main(_arguments(source, output)) == 0

    stdout = capsys.readouterr().out
    assert str(output.resolve()) in stdout
    raw = json.loads(output.read_bytes())
    assert raw["kind"] == "tianlai.score_v2_migration"
    assert raw["score"]["schema_version"] == 2
    assert raw["render_settings"]["kind"] == "tianlai.render_settings"
    assert (
        raw["performance_facts"]["kind"]
        == "tianlai.score_performance_facts"
    )
    assert raw["receipt"]["kind"] == "tianlai.score_v2_migration_receipt"

    parsed = parse_score_v2_migration_document(output.read_bytes())
    assert parsed.score.title == "CLI migration"
    assert parsed.receipt.source_document_sha256 == hashlib.sha256(
        canonical_json_bytes(source_document)
    ).hexdigest()


def test_one_seventh_failure_does_not_create_or_overwrite_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source_document = _score_v1()
    source_document["parts"][0]["notes"][0]["duration_beats"] = 1 / 7  # type: ignore[index]
    source = tmp_path / "source.score.json"
    output = tmp_path / "migration.json"
    _write_score(source, source_document)
    output.write_bytes(b"existing generation")

    assert cli_main(_arguments(source, output, "--overwrite")) == 2

    assert output.read_bytes() == b"existing generation"
    stderr = capsys.readouterr().err
    assert "numeric.denominator_exceeds_v2_limit" in stderr
    assert "will not approximate" in stderr


@pytest.mark.parametrize("source_kind", ["legacy", "v2", "unknown"])
def test_non_v1_sources_fail_closed_before_output_creation(
    source_kind: str,
    tmp_path: Path,
) -> None:
    source_document = _score_v1()
    if source_kind == "legacy":
        del source_document["schema_version"]
    elif source_kind == "v2":
        source_document = migrate_score_v1_to_v2(
            source_document
        ).score.to_dict()
    else:
        source_document["schema_version"] = 3
    source = tmp_path / f"{source_kind}.score.json"
    output = tmp_path / f"{source_kind}.migration.json"
    _write_score(source, source_document)

    assert cli_main(_arguments(source, output)) == 2
    assert not output.exists()


def test_existing_output_requires_overwrite_and_explicit_overwrite_succeeds(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.score.json"
    output = tmp_path / "migration.json"
    _write_score(source, _score_v1())
    output.write_bytes(b"existing generation")

    assert cli_main(_arguments(source, output)) == 2
    assert output.read_bytes() == b"existing generation"

    assert cli_main(_arguments(source, output, "--overwrite")) == 0
    assert parse_score_v2_migration_document(output.read_bytes()).score.title == (
        "CLI migration"
    )


def test_migration_cannot_replace_the_source_even_with_overwrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.score.json"
    original = _score_v1()
    _write_score(source, original)
    previous = source.read_bytes()

    assert cli_main(_arguments(source, source, "--overwrite")) == 2
    assert source.read_bytes() == previous


def test_atomic_overwrite_failure_restores_the_previous_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tianlai.atomic_publish as atomic_publish

    source = tmp_path / "source.score.json"
    output = tmp_path / "migration.json"
    _write_score(source, _score_v1())
    previous = b"existing generation"
    output.write_bytes(previous)

    original_rename = atomic_publish._rename_noreplace
    failed_install = False

    def fail_first_install(source_path: object, destination_path: object) -> None:
        nonlocal failed_install
        if Path(destination_path) == output and not failed_install:
            failed_install = True
            raise OSError("injected migration publication failure")
        original_rename(source_path, destination_path)

    monkeypatch.setattr(
        atomic_publish,
        "_rename_noreplace",
        fail_first_install,
    )

    assert cli_main(_arguments(source, output, "--overwrite")) == 2
    assert failed_install
    assert output.read_bytes() == previous


def test_input_path_replacement_blocks_publication_of_an_unreplayable_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tianlai.score_source as score_source

    original_document = _score_v1()
    replacement_document = copy.deepcopy(original_document)
    replacement_document["title"] = "replacement generation"
    source = tmp_path / "source.score.json"
    output = tmp_path / "migration.json"
    _write_score(source, original_document)

    original_reader = score_source.read_plain_file_bytes

    def replace_after_descriptor_read(
        path: str | Path,
        *,
        maximum_bytes: int,
    ) -> tuple[object, bytes]:
        identity, payload = original_reader(path, maximum_bytes=maximum_bytes)
        _write_score(source, replacement_document)
        return identity, payload

    monkeypatch.setattr(
        score_source,
        "read_plain_file_bytes",
        replace_after_descriptor_read,
    )

    assert cli_main(_arguments(source, output)) == 2
    assert not output.exists()
    assert json.loads(source.read_bytes())["title"] == "replacement generation"


def test_source_moved_to_output_after_serialization_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tianlai.score_v2_migration as migration_module

    source = tmp_path / "source.score.json"
    output = tmp_path / "migration.json"
    _write_score(source, _score_v1())
    source_generation = source.read_bytes()
    original_serializer = migration_module.score_v2_migration_json_bytes

    def move_source_after_serialization(
        *args: object,
        **kwargs: object,
    ) -> bytes:
        payload = original_serializer(*args, **kwargs)
        source.replace(output)
        return payload

    monkeypatch.setattr(
        migration_module,
        "score_v2_migration_json_bytes",
        move_source_after_serialization,
    )

    assert cli_main(_arguments(source, output, "--overwrite")) == 2
    assert not source.exists()
    assert output.read_bytes() == source_generation


def test_hard_link_alias_introduced_after_read_blocks_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tianlai.score_v2_migration as migration_module

    source = tmp_path / "source.score.json"
    output = tmp_path / "migration.json"
    _write_score(source, _score_v1())
    source_generation = source.read_bytes()
    original_serializer = migration_module.score_v2_migration_json_bytes

    def link_source_after_serialization(
        *args: object,
        **kwargs: object,
    ) -> bytes:
        payload = original_serializer(*args, **kwargs)
        os.link(source, output)
        return payload

    monkeypatch.setattr(
        migration_module,
        "score_v2_migration_json_bytes",
        link_source_after_serialization,
    )

    assert cli_main(_arguments(source, output, "--overwrite")) == 2
    assert source.read_bytes() == source_generation
    assert output.read_bytes() == source_generation


def test_bounded_serialization_failure_happens_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tianlai.cli as cli
    import tianlai.score_v2_migration as migration_module

    source = tmp_path / "source.score.json"
    output = tmp_path / "migration.json"
    _write_score(source, _score_v1())
    publication_called = False

    def reject_bundle(*args: object, **kwargs: object) -> bytes:
        raise MigrationError(
            "bundle.document_too_large",
            "migration",
            "injected whole-bundle byte limit",
        )

    def publish(*args: object, **kwargs: object) -> None:
        nonlocal publication_called
        publication_called = True
        raise AssertionError("publication must follow bounded serialization")

    monkeypatch.setattr(
        migration_module,
        "score_v2_migration_json_bytes",
        reject_bundle,
    )
    monkeypatch.setattr(cli, "_publish_bytes_atomic", publish)

    assert cli_main(_arguments(source, output)) == 2
    assert not publication_called
    assert not output.exists()
