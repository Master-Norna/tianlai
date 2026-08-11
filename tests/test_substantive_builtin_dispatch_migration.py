from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from jsonschema import Draft202012Validator

from tianlai.capability import read_capability
from tianlai.onset_evidence import _render_python_closure
from tools import reverify_substantive_builtin_dispatch_migration as migration


ROOT = Path(__file__).resolve().parents[1]


def _repository_has_frozen_baseline(root: Path) -> bool:
    """Return whether *this* project root contains the historical commit."""

    if not (root / ".git").exists():
        return False
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "cat-file",
                "-e",
                f"{migration.BASELINE_REVISION}^{{commit}}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


class SubstantiveBuiltinDispatchMigrationTests(unittest.TestCase):
    def test_frozen_baseline_and_exact_six_targets_are_bound(self) -> None:
        self.assertEqual(
            migration.BASELINE_REVISION,
            "4b3e3aa5b19a587ccc0e766212165a43a739ee12",
        )
        self.assertEqual(len(migration.TARGETS), 6)
        self.assertEqual(len(set(migration.TARGETS)), 6)

    def test_each_proposed_manifest_only_removes_implementation(self) -> None:
        if not _repository_has_frozen_baseline(ROOT):
            self.skipTest(
                "repository-only migration history requires the exact frozen "
                "Git baseline object"
            )
        for relative in migration.TARGETS:
            manifest_path = (
                migration.CATALOG / Path(relative) / migration.MANIFEST_NAME
            )
            old, new = migration._manifest_pair(manifest_path)
            with self.subTest(instrument=relative):
                self.assertEqual(old["implementation"], "乐器.py")
                self.assertNotIn("implementation", new)
                self.assertEqual(
                    {key: value for key, value in old.items() if key != "implementation"},
                    new,
                )
                current = json.loads(manifest_path.read_text(encoding="utf-8"))
                self.assertIn(current, (old, new))

    def test_schema_accepts_only_the_builtin_state_for_all_six_types(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "instrument.schema.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(schema)
        for relative in migration.TARGETS:
            manifest_path = (
                migration.CATALOG / Path(relative) / migration.MANIFEST_NAME
            )
            proposed = migration._current_object(manifest_path)
            with self.subTest(instrument=relative):
                self.assertNotIn("implementation", proposed)
                self.assertFalse(list(validator.iter_errors(proposed)))
                local = dict(proposed, implementation="乐器.py")
                self.assertTrue(list(validator.iter_errors(local)))

                # Removing local dispatch must not also narrow the three
                # pre-existing optional engine identity fields.
                metadata = dict(proposed)
                metadata.setdefault("instrument_name", "compatibility name")
                metadata["display_name"] = "Compatibility display name"
                metadata.setdefault("engine_version", "1.2.3")
                self.assertFalse(list(validator.iter_errors(metadata)))

    def test_builtin_string_backends_retain_their_full_articulation_vocabularies(
        self,
    ) -> None:
        expected = {
            "violin": (
                "accent",
                "pizzicato",
                "slow_sustain",
                "staccato",
                "sustain",
                "tremolo",
            ),
            "cello": (
                "accent",
                "pizzicato",
                "slow_sustain",
                "staccato",
                "sustain",
            ),
            "flute": (
                "accent",
                "legato",
                "slow_sustain",
                "staccato",
                "sustain",
            ),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            for relative in migration.TARGETS:
                manifest_path = (
                    migration.CATALOG / Path(relative) / migration.MANIFEST_NAME
                )
                proposed = migration._current_object(manifest_path)
                instrument_type = proposed["type"]
                if instrument_type not in expected:
                    continue
                proposed_path = (
                    temporary_root / instrument_type / migration.MANIFEST_NAME
                )
                proposed_path.parent.mkdir()
                proposed_path.write_text(
                    json.dumps(proposed, ensure_ascii=False),
                    encoding="utf-8",
                )
                capability = read_capability(
                    proposed_path,
                    root=temporary_root,
                    defer_onset_evidence=True,
                )
                with self.subTest(instrument=relative):
                    self.assertNotIn("implementation", proposed)
                    self.assertEqual(
                        capability.articulations,
                        expected[instrument_type],
                    )
                    self.assertEqual(
                        capability.articulation_source,
                        "backend:tianlai."
                        f"{instrument_type}._PUBLIC_ARTICULATIONS",
                    )
                    if instrument_type == "violin":
                        self.assertEqual(
                            tuple(
                                rule.target_articulation
                                for rule in capability.duration_articulation_rules
                            ),
                            ("accent",),
                        )

    def test_every_target_retains_direct_import_compatibility_files(self) -> None:
        for relative in migration.TARGETS:
            directory = migration.CATALOG / Path(relative)
            with self.subTest(instrument=relative):
                self.assertTrue(
                    (directory / migration.IMPLEMENTATION_NAME).is_file()
                )
        viola = migration.CATALOG / Path(migration.TARGETS[1])
        self.assertTrue((viola / migration.VIOLA_MAPPING_NAME).is_file())

    def test_builtin_render_closures_include_each_relocated_dependency(self) -> None:
        expected = {
            "modeled_bianzhong": {
                "tianlai/_event_free_blocks.py",
                "tianlai/bianzhong.py",
            },
            "vsco2_viola_section": {
                "tianlai/sampler.py",
                "tianlai/vsco2_viola.py",
                "tianlai/vsco2_viola_mapping.py",
            },
            "cello": {
                "tianlai/cello.py",
                "tianlai/sampler.py",
                "tianlai/sfz.py",
            },
            "violin": {
                "tianlai/sampler.py",
                "tianlai/sfz.py",
                "tianlai/violin.py",
                "tianlai/vpo_strings.py",
            },
            "flute": {
                "tianlai/flute.py",
                "tianlai/sampler.py",
                "tianlai/sfz.py",
            },
            "piano": {
                "tianlai/piano.py",
                "tianlai/sampler.py",
            },
        }
        for relative in migration.TARGETS:
            directory = migration.CATALOG / Path(relative)
            manifest_path = directory / migration.MANIFEST_NAME
            proposed = migration._current_object(manifest_path)
            closure = _render_python_closure(
                migration.ROOT,
                manifest_path,
                proposed,
            )
            paths = {entry["path"] for entry in closure["files"]}
            with self.subTest(instrument=relative):
                self.assertNotIn("implementation", proposed)
                self.assertTrue(expected[proposed["type"]] <= paths)
                self.assertNotIn(
                    (directory / migration.IMPLEMENTATION_NAME)
                    .relative_to(migration.ROOT)
                    .as_posix(),
                    paths,
                )
                if proposed["type"] == "vsco2_viola_section":
                    self.assertNotIn(
                        (directory / migration.VIOLA_MAPPING_NAME)
                        .relative_to(migration.ROOT)
                        .as_posix(),
                        paths,
                    )

    def test_migration_record_names_the_full_byte_exact_proof(self) -> None:
        previous = "0" * 64
        record = migration._migration_record(previous)
        self.assertEqual(
            record["status"],
            "implementation_relocated_to_builtin_no_audio_change",
        )
        self.assertEqual(record["previous_manifest_canonical_sha256"], previous)
        self.assertEqual(record["changed_fields"], ["implementation"])
        self.assertIs(record["audio_rerendered"], False)
        self.assertEqual(record["baseline_revision"], migration.BASELINE_REVISION)
        self.assertEqual(
            set(record["byte_exact_fields"]),
            {
                "float64_stream",
                "float32_stream",
                "pcm24_wav",
                "frame_count",
                "peak_active_voices",
            },
        )

    def test_repository_history_guard_requires_own_git_and_exact_baseline(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with mock.patch.object(subprocess, "run") as run:
                self.assertFalse(_repository_has_frozen_baseline(root))
            run.assert_not_called()

            (root / ".git").mkdir()
            missing = subprocess.CompletedProcess([], returncode=128)
            with mock.patch.object(subprocess, "run", return_value=missing) as run:
                self.assertFalse(_repository_has_frozen_baseline(root))
            run.assert_called_once_with(
                [
                    "git",
                    "-C",
                    str(root),
                    "cat-file",
                    "-e",
                    f"{migration.BASELINE_REVISION}^{{commit}}",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

            available = subprocess.CompletedProcess([], returncode=0)
            with mock.patch.object(
                subprocess,
                "run",
                return_value=available,
            ):
                self.assertTrue(_repository_has_frozen_baseline(root))


if __name__ == "__main__":
    unittest.main()
