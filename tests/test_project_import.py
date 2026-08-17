from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from tianlai.capability import InstrumentCapability, load_capabilities
from tianlai.project_import import (
    DRAFT_FORMAT,
    IMPORT_FORMAT,
    build_routing_hints,
    canonical_json_sha256,
    import_midi_project,
    import_musicxml_project,
    import_project,
    promote_roster,
    validate_import_bundle,
    write_import_bundle,
)
from tianlai.roster import parse_roster_document


ROOT = Path(__file__).resolve().parents[1]


def _same_test_path(
    left: str | os.PathLike[str],
    right: str | os.PathLike[str],
) -> bool:
    """Compare test hook paths after expanding ordinary platform aliases."""

    return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)


def _note_mentions_test_path(note: str, path: Path) -> bool:
    """Accept either the caller spelling or its canonical spelling in notes."""

    return str(path) in note or str(path.resolve(strict=False)) in note


def _chunk(identifier: bytes, payload: bytes) -> bytes:
    return identifier + len(payload).to_bytes(4, "big") + payload


def _midi(*, percussion: bool = False) -> bytes:
    channel = 9 if percussion else 0
    name = b"Drums" if percussion else b"Piano"
    note = 36 if percussion else 60
    track = (
        b"\x00\xff\x03"
        + bytes([len(name)])
        + name
        + b"\x00"
        + bytes([0x90 | channel, note, 64])
        + b"\x83\x60"
        + bytes([0x80 | channel, note, 0])
        + b"\x00\xff\x2f\x00"
    )
    header = (
        (0).to_bytes(2, "big")
        + (1).to_bytes(2, "big")
        + (480).to_bytes(2, "big")
    )
    return _chunk(b"MThd", header) + _chunk(b"MTrk", track)


def _musicxml(*, two_parts: bool = False) -> bytes:
    part_list = [
        '<score-part id="P1"><part-name>Flute</part-name></score-part>'
    ]
    parts = [
        """
        <part id="P1"><measure number="1">
          <attributes>
            <divisions>1</divisions>
            <time><beats>4</beats><beat-type>4</beat-type></time>
          </attributes>
          <direction><sound tempo="120"/></direction>
          <note>
            <pitch><step>C</step><octave>4</octave></pitch>
            <duration>1</duration><type>quarter</type>
          </note>
        </measure></part>
        """
    ]
    if two_parts:
        part_list.append(
            '<score-part id="P2"><part-name>Cello</part-name></score-part>'
        )
        parts.append(
            """
            <part id="P2"><measure number="1">
              <attributes>
                <divisions>1</divisions>
                <time><beats>4</beats><beat-type>4</beat-type></time>
              </attributes>
              <note>
                <pitch><step>C</step><octave>3</octave></pitch>
                <duration>2</duration><type>half</type>
              </note>
            </measure></part>
            """
        )
    text = f"""<?xml version="1.0" encoding="UTF-8"?>
    <score-partwise version="3.1">
      <work><work-title>Import Test</work-title></work>
      <part-list>{''.join(part_list)}</part-list>
      {''.join(parts)}
    </score-partwise>
    """
    return text.encode("utf-8")


def _capability(
    relative_path: str,
    *,
    pitched: bool = True,
    license_status: str = "approved",
    implementation_type: str = "synthesizer",
    note_min: float = 0.0,
    note_max: float = 127.0,
) -> InstrumentCapability:
    return InstrumentCapability(
        name=relative_path.rsplit("/", 1)[-1],
        relative_path=relative_path,
        manifest_path=f"/catalog/{relative_path}/乐器.json",
        implementation_type=implementation_type,
        pitched=pitched,
        note_min=note_min,
        note_max=note_max,
        articulations=("normal",),
        default_articulation="normal",
        articulation_source="manifest",
        onset_seconds=0.0,
        quality_tier="formal",
        license_status=license_status,
    )


class ProjectImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_midi(self, *, percussion: bool = False) -> Path:
        path = self.root / ("drums.mid" if percussion else "score.mid")
        path.write_bytes(_midi(percussion=percussion))
        return path

    def _write_musicxml(self, *, two_parts: bool = False) -> Path:
        path = self.root / "score.musicxml"
        path.write_bytes(_musicxml(two_parts=two_parts))
        return path

    @staticmethod
    def _retitled_bundle(bundle: dict, title: str = "New title") -> dict:
        changed = copy.deepcopy(bundle)
        changed["score"]["title"] = title
        digest = canonical_json_sha256(changed["score"])
        changed["import_report"]["score"]["canonical_sha256"] = digest
        changed["import_report"]["score_canonical_sha256"] = digest
        changed["roster_draft"]["source"]["score"][
            "canonical_sha256"
        ] = digest
        changed["roster_draft"]["draft_roster"]["name"] = (
            f"{title} 编制草稿"
        )
        validate_import_bundle(changed)
        return changed

    def test_midi_import_is_v1_hash_bound_and_non_executable(self) -> None:
        path = self._write_midi()
        bundle = import_midi_project(path)

        self.assertEqual(bundle["format"], IMPORT_FORMAT)
        self.assertEqual(bundle["score"]["schema_version"], 1)
        self.assertRegex(
            bundle["score"]["parts"][0]["notes"][0]["event_id"],
            r"^event-\d{6}$",
        )
        report = bundle["import_report"]
        self.assertEqual(
            report["source"]["sha256"],
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        self.assertEqual(report["source"]["byte_length"], path.stat().st_size)
        self.assertEqual(
            report["score"]["canonical_sha256"],
            canonical_json_sha256(bundle["score"]),
        )
        self.assertEqual(bundle["roster_draft"]["format"], DRAFT_FORMAT)
        self.assertIs(bundle["roster_draft"]["executable"], False)
        assignment = bundle["roster_draft"]["draft_roster"]["assignments"][0]
        self.assertIn("instrument", assignment)
        self.assertIsNone(assignment["instrument"])
        validate_import_bundle(bundle)
        with self.assertRaises(ValueError):
            parse_roster_document(
                bundle["roster_draft"]["draft_roster"],
                {},
            )

    def test_musicxml_adds_source_bytes_and_score_hash(self) -> None:
        path = self._write_musicxml()
        bundle = import_musicxml_project(path)
        report = bundle["import_report"]

        self.assertEqual(bundle["score"]["schema_version"], 1)
        self.assertEqual(
            report["source_musicxml_sha256"],
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            report["source_musicxml_byte_length"],
            len(path.read_bytes()),
        )
        self.assertEqual(
            report["score_canonical_sha256"],
            canonical_json_sha256(bundle["score"]),
        )
        self.assertIs(bundle["roster_draft"]["executable"], False)
        self.assertEqual(
            bundle["roster_draft"]["source"]["score"]["canonical_sha256"],
            report["score_canonical_sha256"],
        )
        self.assertIsNone(
            bundle["roster_draft"]["draft_roster"]["assignments"][0][
                "instrument"
            ]
        )
        tampered = copy.deepcopy(bundle)
        tampered["import_report"]["source_musicxml_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source hash is inconsistent"):
            validate_import_bundle(tampered)

    def test_musicxml_playback_evidence_reaches_report_and_roster_draft(self) -> None:
        payload = _musicxml().replace(
            b'<score-part id="P1"><part-name>Flute</part-name></score-part>',
            b'<score-part id="P1"><part-name>Flute</part-name>'
            b'<midi-instrument id="P1-I1"><midi-channel>1</midi-channel>'
            b'<midi-program>74</midi-program><volume>80</volume>'
            b'</midi-instrument></score-part>',
        )
        path = self.root / "playback-evidence.musicxml"
        path.write_bytes(payload)

        bundle = import_musicxml_project(path)
        evidence = [
            {
                "instrument_id": "P1-I1",
                "midi_channel_1based": 1,
                "midi_program_1based": 74,
                "volume_percent": 80.0,
            }
        ]

        self.assertEqual(
            bundle["import_report"]["parts"][0]["midi_playback"],
            evidence,
        )
        self.assertEqual(
            bundle["roster_draft"]["part_evidence"][0]["source"][
                "midi_playback"
            ],
            evidence,
        )
        self.assertTrue(bundle["import_report"]["warnings"])
        validate_import_bundle(bundle)

    def test_mxl_binds_archive_bytes_while_preserving_the_score_hash(self) -> None:
        plain = self._write_musicxml()
        plain_bundle = import_musicxml_project(plain)
        archive = self.root / "score.mxl"
        container = b"""<?xml version="1.0"?>
        <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
          <rootfiles>
            <rootfile full-path="score.xml"
              media-type="application/vnd.recordare.musicxml+xml"/>
          </rootfiles>
        </container>
        """
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("META-INF/container.xml", container)
            package.writestr("score.xml", plain.read_bytes())

        mxl_bundle = import_musicxml_project(archive)
        report = mxl_bundle["import_report"]
        self.assertEqual(
            report["source_musicxml_sha256"],
            hashlib.sha256(archive.read_bytes()).hexdigest(),
        )
        self.assertEqual(report["source_musicxml_byte_length"], archive.stat().st_size)
        self.assertNotEqual(
            report["source_musicxml_sha256"],
            plain_bundle["import_report"]["source_musicxml_sha256"],
        )
        self.assertEqual(
            report["score_canonical_sha256"],
            plain_bundle["import_report"]["score_canonical_sha256"],
        )

    def test_dispatches_by_suffix_and_rejects_unknown_source(self) -> None:
        midi = self._write_midi()
        xml = self._write_musicxml()
        self.assertEqual(import_project(midi)["import_report"]["source_kind"], "midi")
        self.assertEqual(
            import_project(xml)["import_report"]["source_kind"],
            "musicxml",
        )
        unknown = self.root / "score.txt"
        unknown.write_text("not a score", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cannot infer"):
            import_project(unknown)

    def test_descriptor_bound_bytes_are_parsed_after_path_replacement(self) -> None:
        midi_payload = _midi()
        midi_path = self.root / "captured.mid"
        midi_path.write_bytes(b"replacement is not MIDI")
        midi_bundle = import_project(
            midi_path,
            source_bytes=midi_payload,
        )
        self.assertEqual(
            midi_bundle["import_report"]["source"]["sha256"],
            hashlib.sha256(midi_payload).hexdigest(),
        )

        xml_payload = _musicxml()
        xml_path = self.root / "captured.musicxml"
        xml_path.write_bytes(b"replacement is not XML")
        xml_bundle = import_project(
            xml_path,
            source_bytes=xml_payload,
        )
        self.assertEqual(
            xml_bundle["import_report"]["source"]["sha256"],
            hashlib.sha256(xml_payload).hexdigest(),
        )

        archive_path = self.root / "captured.mxl"
        with zipfile.ZipFile(archive_path, "w") as package:
            package.writestr(
                "META-INF/container.xml",
                b"""<?xml version="1.0"?>
                <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
                  <rootfiles><rootfile full-path="score.xml"/></rootfiles>
                </container>""",
            )
            package.writestr("score.xml", xml_payload)
        archive_payload = archive_path.read_bytes()
        archive_path.write_bytes(b"replacement is not an archive")
        mxl_bundle = import_project(
            archive_path,
            source_bytes=archive_payload,
        )
        self.assertEqual(
            mxl_bundle["import_report"]["source"]["sha256"],
            hashlib.sha256(archive_payload).hexdigest(),
        )

    def test_promote_requires_matching_hash_and_returns_formal_roster(self) -> None:
        bundle = import_musicxml_project(self._write_musicxml())
        instrument = "管弦乐/木管组/长笛"
        capabilities = {instrument: _capability(instrument)}
        part_id = bundle["score"]["parts"][0]["id"]
        original_draft = copy.deepcopy(bundle["roster_draft"])
        original_score = copy.deepcopy(bundle["score"])

        roster = promote_roster(
            bundle["roster_draft"],
            bundle["score"],
            [{"part": part_id, "instrument": instrument}],
            capabilities,
            trusted_only=True,
            trusted_instruments={instrument},
        )

        self.assertEqual(roster["assignments"][0]["instrument"], instrument)
        parsed = parse_roster_document(roster, capabilities)
        self.assertEqual(parsed.executors[0].capability.relative_path, instrument)
        self.assertEqual(bundle["roster_draft"], original_draft)
        self.assertEqual(bundle["score"], original_score)

        tampered = copy.deepcopy(bundle["score"])
        tampered["parts"][0]["notes"][0]["pitch"] = "D4"
        with self.assertRaisesRegex(ValueError, "does not match"):
            promote_roster(
                bundle["roster_draft"],
                tampered,
                [{"part": part_id, "instrument": instrument}],
                capabilities,
            )

    def test_promote_requires_every_part_exactly_once(self) -> None:
        bundle = import_musicxml_project(
            self._write_musicxml(two_parts=True)
        )
        instrument = "测试/乐器"
        capabilities = {instrument: _capability(instrument)}
        first, second = [
            part["id"] for part in bundle["score"]["parts"]
        ]

        with self.assertRaisesRegex(ValueError, "missing"):
            promote_roster(
                bundle["roster_draft"],
                bundle["score"],
                [{"part": first, "instrument": instrument}],
                capabilities,
            )
        with self.assertRaisesRegex(ValueError, "more than once"):
            promote_roster(
                bundle["roster_draft"],
                bundle["score"],
                [
                    {"part": first, "instrument": instrument},
                    {"part": first, "instrument": instrument},
                ],
                capabilities,
            )
        with self.assertRaisesRegex(ValueError, "outside the score"):
            promote_roster(
                bundle["roster_draft"],
                bundle["score"],
                [
                    {"part": first, "instrument": instrument},
                    {"part": second, "instrument": instrument},
                    {"part": "phantom", "instrument": instrument},
                ],
                capabilities,
            )

    def test_promote_rejects_unknown_quarantined_and_untrusted_routes(self) -> None:
        bundle = import_midi_project(self._write_midi())
        part = bundle["score"]["parts"][0]["id"]
        approved = "测试/通过"
        quarantined = "测试/隔离"
        capabilities = {
            approved: _capability(approved),
            quarantined: _capability(
                quarantined,
                license_status="quarantined",
            ),
        }
        with self.assertRaisesRegex(ValueError, "不存在"):
            promote_roster(
                bundle["roster_draft"],
                bundle["score"],
                [{"part": part, "instrument": "测试/缺失"}],
                capabilities,
            )
        with self.assertRaisesRegex(ValueError, "不可用"):
            promote_roster(
                bundle["roster_draft"],
                bundle["score"],
                [{"part": part, "instrument": quarantined}],
                capabilities,
            )
        with self.assertRaisesRegex(ValueError, "requires an explicit"):
            promote_roster(
                bundle["roster_draft"],
                bundle["score"],
                [{"part": part, "instrument": approved}],
                capabilities,
                trusted_only=True,
            )
        with self.assertRaisesRegex(
            ValueError,
            "不在当前调用方提供的允许乐器集合",
        ):
            promote_roster(
                bundle["roster_draft"],
                bundle["score"],
                [{"part": part, "instrument": approved}],
                capabilities,
                trusted_only=True,
                trusted_instruments=set(),
            )

    def test_percussion_requires_an_explicit_kit(self) -> None:
        bundle = import_midi_project(self._write_midi(percussion=True))
        part = bundle["score"]["parts"][0]["id"]
        kick = "现代鼓组/底鼓"
        capabilities = {
            kick: _capability(kick, pitched=False),
        }
        with self.assertRaisesRegex(ValueError, "requires explicit kit"):
            promote_roster(
                bundle["roster_draft"],
                bundle["score"],
                [{"part": part, "instrument": kick}],
                capabilities,
            )
        roster = promote_roster(
            bundle["roster_draft"],
            bundle["score"],
            [{"part": part, "kit": {"C2": kick}}],
            capabilities,
        )
        parsed = parse_roster_document(roster, capabilities)
        self.assertEqual(len(parsed.executors), 1)
        self.assertEqual(parsed.executors[0].kit_pitch, 36)

    def test_routing_hints_are_bounded_and_never_fill_the_draft(self) -> None:
        bundle = import_musicxml_project(self._write_musicxml())
        capabilities = {
            f"候选/{index:02d}": _capability(
                f"候选/{index:02d}",
                note_min=24,
                note_max=108,
            )
            for index in range(20)
        }
        hints = build_routing_hints(
            bundle["roster_draft"],
            bundle["score"],
            capabilities,
            limit=3,
        )
        row = hints["parts"][0]
        self.assertEqual(hints["status"], "non_executable_hints")
        self.assertEqual(row["candidate_count_returned"], 3)
        self.assertTrue(row["candidates_truncated"])
        self.assertEqual(len(row["candidates"]), 3)
        self.assertIsNone(
            bundle["roster_draft"]["draft_roster"]["assignments"][0][
                "instrument"
            ]
        )
        with self.assertRaisesRegex(ValueError, "between 1 and"):
            build_routing_hints(
                bundle["roster_draft"],
                bundle["score"],
                capabilities,
                limit=17,
            )

    def test_percussion_hints_use_catalog_routing_not_pitched_guessing(self) -> None:
        bundle = import_midi_project(self._write_midi(percussion=True))
        capabilities = {
            "环境与拟音/海浪": _capability(
                "环境与拟音/海浪",
                pitched=False,
            ),
            "现代鼓组/底鼓": _capability(
                "现代鼓组/底鼓",
                pitched=False,
            ),
            "管弦乐/打击乐组/定音鼓": _capability(
                "管弦乐/打击乐组/定音鼓",
                pitched=True,
                note_min=38,
                note_max=59,
            ),
        }

        hints = build_routing_hints(
            bundle["roster_draft"],
            bundle["score"],
            capabilities,
            limit=8,
        )
        candidates = hints["parts"][0]["candidates"]

        self.assertEqual(
            [item["instrument"] for item in candidates],
            ["现代鼓组/底鼓", "管弦乐/打击乐组/定音鼓"],
        )
        self.assertTrue(all(item["routing_class"] == "percussion" for item in candidates))
        self.assertTrue(candidates[1]["pitched"])
        self.assertNotIn(
            "环境与拟音/海浪",
            {item["instrument"] for item in candidates},
        )

    def test_real_percussion_hints_represent_all_three_routing_families(
        self,
    ) -> None:
        bundle = import_midi_project(self._write_midi(percussion=True))
        capabilities = load_capabilities(ROOT / "乐器")

        for limit in (8, 16):
            with self.subTest(limit=limit):
                hints = build_routing_hints(
                    bundle["roster_draft"],
                    bundle["score"],
                    capabilities,
                    limit=limit,
                )
                candidates = hints["parts"][0]["candidates"]
                self.assertTrue(
                    any(
                        item["instrument"].startswith("现代鼓组/")
                        for item in candidates
                    )
                )
                self.assertTrue(
                    any(
                        item["instrument"].startswith("管弦乐/打击乐组/")
                        and not item["pitched"]
                        for item in candidates
                    )
                )
                self.assertTrue(
                    any(
                        item["instrument"].startswith("管弦乐/打击乐组/")
                        and item["pitched"]
                        for item in candidates
                    )
                )
                self.assertTrue(
                    all(item["routing_class"] == "percussion" for item in candidates)
                )

    def test_write_bundle_is_atomic_and_refuses_overwrite_by_default(self) -> None:
        bundle = import_midi_project(self._write_midi())
        destination = self.root / "imported"
        paths = write_import_bundle(bundle, destination)

        self.assertEqual(
            json.loads(Path(paths["score"]).read_text(encoding="utf-8")),
            bundle["score"],
        )
        self.assertTrue(Path(paths["import_report"]).is_file())
        self.assertTrue(Path(paths["roster_draft"]).is_file())
        before = {
            path.name: path.read_bytes() for path in destination.iterdir()
        }
        with self.assertRaises(FileExistsError):
            write_import_bundle(bundle, destination)
        self.assertEqual(
            {path.name: path.read_bytes() for path in destination.iterdir()},
            before,
        )

    def test_relative_destination_creates_plain_parents_and_preserves_return_spelling(
        self,
    ) -> None:
        bundle = import_midi_project(self._write_midi())
        destination = Path("missing-parent") / "nested" / "imported"
        previous_directory = Path.cwd()
        try:
            os.chdir(self.root)
            paths = write_import_bundle(bundle, destination)
        finally:
            os.chdir(previous_directory)

        self.assertEqual(paths["directory"], str(destination))
        self.assertEqual(paths["score"], str(destination / "score.json"))
        self.assertTrue((self.root / destination / "score.json").is_file())

    def test_first_publish_failure_leaves_no_partial_bundle(self) -> None:
        bundle = import_midi_project(self._write_midi())
        destination = self.root / "imported"

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace

        def fail_publish(source, target):
            if (
                ".import-stage." in Path(source).name
                and _same_test_path(target, destination)
            ):
                raise OSError("simulated first publish failure")
            return real_rename_noreplace(source, target)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=fail_publish,
            ),
            self.assertRaisesRegex(OSError, "simulated first publish failure"),
        ):
            write_import_bundle(bundle, destination)

        self.assertFalse(destination.exists())
        private = [
            path.name
            for path in self.root.iterdir()
            if ".import-stage." in path.name or ".import-backup." in path.name
        ]
        self.assertEqual(private, [])

    def test_partial_stage_write_failure_cleans_only_its_captured_directory(
        self,
    ) -> None:
        bundle = import_midi_project(self._write_midi())
        destination = self.root / "imported"

        real_open = Path.open

        def fail_second_document(path, *args, **kwargs):
            candidate = Path(path)
            if (
                ".import-stage." in candidate.parent.name
                and candidate.name == "import-report.json"
            ):
                raise OSError("simulated document write failure")
            return real_open(candidate, *args, **kwargs)

        with (
            mock.patch.object(Path, "open", new=fail_second_document),
            self.assertRaisesRegex(OSError, "document write failure"),
        ):
            write_import_bundle(bundle, destination)

        self.assertFalse(destination.exists())
        self.assertEqual(
            [
                path
                for path in self.root.iterdir()
                if ".import-stage." in path.name
                or ".tianlai-import-cleanup." in path.name
            ],
            [],
        )

    def test_replaced_stage_before_snapshot_is_never_cleaned_as_owned(self) -> None:
        bundle = import_midi_project(self._write_midi())
        destination = self.root / "imported"
        parked_owned_stage = self.root / "parked-owned-stage-before-snapshot"
        replacement_stage: Path | None = None

        real_open = Path.open
        replaced = False

        def replace_stage_before_first_write(path, *args, **kwargs):
            nonlocal replaced, replacement_stage
            candidate = Path(path)
            if not replaced and ".import-stage." in candidate.parent.name:
                replaced = True
                replacement_stage = candidate.parent
                os.replace(candidate.parent, parked_owned_stage)
                candidate.parent.mkdir()
            return real_open(candidate, *args, **kwargs)

        with (
            mock.patch.object(
                Path,
                "open",
                new=replace_stage_before_first_write,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "staging directory identity changed",
            ) as caught,
        ):
            write_import_bundle(bundle, destination)

        self.assertTrue(replaced)
        self.assertIsNotNone(replacement_stage)
        assert replacement_stage is not None
        self.assertTrue((replacement_stage / "score.json").is_file())
        self.assertTrue(parked_owned_stage.is_dir())
        self.assertFalse(destination.exists())
        self.assertTrue(
            any(
                "cleanup was not completed" in note
                for note in getattr(caught.exception, "__notes__", ())
            )
        )

    def test_overwrite_refuses_an_unrelated_existing_directory(self) -> None:
        bundle = import_midi_project(self._write_midi())
        destination = self.root / "not-an-import-generation"
        destination.mkdir()
        sentinel = destination / "keep.txt"
        sentinel.write_text("user data", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "refusing overwrite"):
            write_import_bundle(
                bundle,
                destination,
                overwrite=True,
            )

        self.assertEqual(
            sentinel.read_text(encoding="utf-8"),
            "user data",
        )

    def test_overwrite_failure_restores_the_previous_complete_bundle(self) -> None:
        first = import_midi_project(self._write_midi())
        destination = self.root / "imported"
        write_import_bundle(first, destination)
        before = {
            path.name: path.read_bytes() for path in destination.iterdir()
        }
        second = self._retitled_bundle(first)

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        publish_attempts = 0

        def fail_new_generation(source, target):
            nonlocal publish_attempts
            source_path = Path(source)
            if ".import-stage." in source_path.name:
                publish_attempts += 1
                if publish_attempts == 1:
                    raise OSError("simulated publish failure")
            return real_rename_noreplace(source, target)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=fail_new_generation,
            ),
            self.assertRaisesRegex(OSError, "simulated publish failure"),
        ):
            write_import_bundle(second, destination, overwrite=True)

        self.assertEqual(
            {path.name: path.read_bytes() for path in destination.iterdir()},
            before,
        )
        private = [
            path.name
            for path in self.root.iterdir()
            if ".import-stage." in path.name or ".import-backup." in path.name
        ]
        self.assertEqual(private, [])

    def test_overwrite_swap_race_preserves_the_racer_and_fails_closed(
        self,
    ) -> None:
        first = import_midi_project(self._write_midi())
        second = self._retitled_bundle(first)
        destination = self.root / "imported"
        write_import_bundle(first, destination)
        parked = self.root / "parked-verified-generation"

        import tianlai.project_import as project_import

        real_replace = os.replace
        real_rename_noreplace = project_import._rename_noreplace
        raced = False

        def replace_after_race(source, target):
            nonlocal raced
            source_path = Path(source)
            target_path = Path(target)
            if (
                not raced
                and _same_test_path(source_path, destination)
                and ".import-backup." in target_path.name
            ):
                raced = True
                real_replace(destination, parked)
                destination.mkdir()
                (destination / "user-data.txt").write_text(
                    "must survive",
                    encoding="utf-8",
                )
            return real_rename_noreplace(source, target)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=replace_after_race,
            ),
            self.assertRaisesRegex(RuntimeError, "replaced concurrently"),
        ):
            write_import_bundle(second, destination, overwrite=True)

        backups = list(self.root.glob(".imported.import-backup.*"))
        self.assertTrue(raced)
        self.assertEqual(backups, [])
        self.assertEqual(
            (destination / "user-data.txt").read_text(encoding="utf-8"),
            "must survive",
        )
        self.assertTrue((parked / "score.json").is_file())

    def test_overwrite_backup_move_then_error_restores_old_and_rethrows(
        self,
    ) -> None:
        first = import_midi_project(self._write_midi())
        second = self._retitled_bundle(first)
        destination = self.root / "imported"
        write_import_bundle(first, destination)
        before = {
            path.name: path.read_bytes() for path in destination.iterdir()
        }

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        injected = False

        def move_backup_then_fail(source, target):
            nonlocal injected
            source_path = Path(source)
            target_path = Path(target)
            real_rename_noreplace(source_path, target_path)
            if (
                not injected
                and _same_test_path(source_path, destination)
                and ".import-backup." in target_path.name
            ):
                injected = True
                raise PermissionError("PRIMARY backup move reported failure")

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=move_backup_then_fail,
            ),
            self.assertRaisesRegex(
                PermissionError,
                "PRIMARY backup move reported failure",
            ) as caught,
        ):
            write_import_bundle(second, destination, overwrite=True)

        self.assertTrue(injected)
        self.assertEqual(
            {path.name: path.read_bytes() for path in destination.iterdir()},
            before,
        )
        self.assertEqual(list(self.root.glob(".imported.import-backup.*")), [])
        self.assertIsNone(caught.exception.__cause__)

    def test_overwrite_source_swap_restores_a_moved_file_racer(self) -> None:
        first = import_midi_project(self._write_midi())
        second = self._retitled_bundle(first)
        destination = self.root / "imported"
        write_import_bundle(first, destination)
        parked = self.root / "parked-old-for-file-racer"

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        real_replace = os.replace
        raced = False

        def replace_with_file_before_backup(source, target):
            nonlocal raced
            source_path = Path(source)
            target_path = Path(target)
            if (
                not raced
                and _same_test_path(source_path, destination)
                and ".import-backup." in target_path.name
            ):
                raced = True
                real_replace(destination, parked)
                destination.write_bytes(b"file racer must remain public")
            real_rename_noreplace(source_path, target_path)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=replace_with_file_before_backup,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "replaced concurrently during backup",
            ),
        ):
            write_import_bundle(second, destination, overwrite=True)

        self.assertTrue(raced)
        self.assertEqual(destination.read_bytes(), b"file racer must remain public")
        self.assertTrue((parked / "score.json").is_file())
        self.assertEqual(list(self.root.glob(".imported.import-backup.*")), [])

    def test_overwrite_in_place_change_during_backup_is_restored_publicly(
        self,
    ) -> None:
        first = import_midi_project(self._write_midi())
        second = self._retitled_bundle(first)
        destination = self.root / "imported"
        write_import_bundle(first, destination)

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        mutated = False

        def mutate_after_backup_move(source, target):
            nonlocal mutated
            source_path = Path(source)
            target_path = Path(target)
            real_rename_noreplace(source_path, target_path)
            if (
                not mutated
                and _same_test_path(source_path, destination)
                and ".import-backup." in target_path.name
            ):
                mutated = True
                (target_path / "score.json").write_bytes(b"{}\n")

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=mutate_after_backup_move,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "generation changed during backup",
            ),
        ):
            write_import_bundle(second, destination, overwrite=True)

        self.assertTrue(mutated)
        self.assertEqual((destination / "score.json").read_bytes(), b"{}\n")
        self.assertEqual(list(self.root.glob(".imported.import-backup.*")), [])

    def test_backup_move_then_error_and_inspection_failure_restores_public_entry(
        self,
    ) -> None:
        first = import_midi_project(self._write_midi())
        second = self._retitled_bundle(first)
        destination = self.root / "imported"
        write_import_bundle(first, destination)
        before = {
            path.name: path.read_bytes() for path in destination.iterdir()
        }

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        real_entry_identity = project_import._import_entry_identity
        backup_path: Path | None = None
        inspect_failed = False

        def move_then_fail(source, target):
            nonlocal backup_path
            source_path = Path(source)
            target_path = Path(target)
            real_rename_noreplace(source_path, target_path)
            if _same_test_path(
                source_path,
                destination,
            ) and ".import-backup." in target_path.name:
                backup_path = target_path
                raise PermissionError("PRIMARY backup move failure")

        def fail_backup_identity(path):
            nonlocal inspect_failed
            candidate = Path(path)
            if (
                backup_path is not None
                and _same_test_path(candidate, backup_path)
                and not inspect_failed
            ):
                inspect_failed = True
                raise OSError("transient backup identity failure")
            return real_entry_identity(candidate)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=move_then_fail,
            ),
            mock.patch.object(
                project_import,
                "_import_entry_identity",
                side_effect=fail_backup_identity,
            ),
            self.assertRaisesRegex(
                PermissionError,
                "PRIMARY backup move failure",
            ) as caught,
        ):
            write_import_bundle(second, destination, overwrite=True)

        self.assertTrue(inspect_failed)
        self.assertEqual(
            {path.name: path.read_bytes() for path in destination.iterdir()},
            before,
        )
        self.assertEqual(list(self.root.glob(".imported.import-backup.*")), [])
        self.assertTrue(
            any(
                "conservatively restored" in note
                for note in getattr(caught.exception, "__notes__", ())
            )
        )

    def test_overwrite_source_swap_with_reoccupied_target_preserves_both_racers(
        self,
    ) -> None:
        first = import_midi_project(self._write_midi())
        second = self._retitled_bundle(first)
        destination = self.root / "imported"
        write_import_bundle(first, destination)
        parked = self.root / "parked-expected-old-generation"

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        real_replace = os.replace
        raced_backup: Path | None = None

        def swap_move_and_reoccupy(source, target):
            nonlocal raced_backup
            source_path = Path(source)
            target_path = Path(target)
            if (
                raced_backup is None
                and _same_test_path(source_path, destination)
                and ".import-backup." in target_path.name
            ):
                raced_backup = target_path
                real_replace(destination, parked)
                destination.mkdir()
                (destination / "first-racer.txt").write_text(
                    "first racer",
                    encoding="utf-8",
                )
                real_rename_noreplace(destination, target_path)
                destination.mkdir()
                (destination / "second-racer.txt").write_text(
                    "second racer",
                    encoding="utf-8",
                )
                return
            real_rename_noreplace(source_path, target_path)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=swap_move_and_reoccupy,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "replaced concurrently during backup",
            ) as caught,
        ):
            write_import_bundle(second, destination, overwrite=True)

        self.assertIsNotNone(raced_backup)
        assert raced_backup is not None
        self.assertEqual(
            (destination / "second-racer.txt").read_text(encoding="utf-8"),
            "second racer",
        )
        self.assertEqual(
            (raced_backup / "first-racer.txt").read_text(encoding="utf-8"),
            "first racer",
        )
        self.assertTrue((parked / "score.json").is_file())
        self.assertTrue(
            any(
                _note_mentions_test_path(note, raced_backup)
                for note in getattr(caught.exception, "__notes__", ())
            )
        )

    def test_overwrite_backup_name_race_never_clobbers_the_racer(self) -> None:
        first = import_midi_project(self._write_midi())
        second = self._retitled_bundle(first)
        destination = self.root / "imported"
        write_import_bundle(first, destination)
        before = {
            path.name: path.read_bytes() for path in destination.iterdir()
        }

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        raced_backup: Path | None = None

        def occupy_backup(source, target):
            nonlocal raced_backup
            source_path = Path(source)
            target_path = Path(target)
            if (
                raced_backup is None
                and _same_test_path(source_path, destination)
                and ".import-backup." in target_path.name
            ):
                raced_backup = target_path
                target_path.mkdir()
                (target_path / "user-data.txt").write_text(
                    "backup name racer",
                    encoding="utf-8",
                )
            return real_rename_noreplace(source, target)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=occupy_backup,
            ),
            self.assertRaises(FileExistsError),
        ):
            write_import_bundle(second, destination, overwrite=True)

        self.assertIsNotNone(raced_backup)
        assert raced_backup is not None
        self.assertEqual(
            (raced_backup / "user-data.txt").read_text(encoding="utf-8"),
            "backup name racer",
        )
        self.assertEqual(
            {path.name: path.read_bytes() for path in destination.iterdir()},
            before,
        )

    def test_first_publish_target_race_never_clobbers_the_racer(self) -> None:
        bundle = import_midi_project(self._write_midi())
        destination = self.root / "imported"

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        raced = False

        def occupy_target(source, target):
            nonlocal raced
            source_path = Path(source)
            target_path = Path(target)
            if (
                not raced
                and ".import-stage." in source_path.name
                and _same_test_path(target_path, destination)
            ):
                raced = True
                destination.mkdir()
                (destination / "user-data.txt").write_text(
                    "target racer",
                    encoding="utf-8",
                )
            return real_rename_noreplace(source, target)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=occupy_target,
            ),
            self.assertRaises(FileExistsError),
        ):
            write_import_bundle(bundle, destination)

        self.assertTrue(raced)
        self.assertEqual(
            (destination / "user-data.txt").read_text(encoding="utf-8"),
            "target racer",
        )
        self.assertEqual(
            [path for path in self.root.iterdir() if ".import-stage." in path.name],
            [],
        )

    def test_rollback_target_move_never_clobbers_a_stage_path_racer(self) -> None:
        first = import_midi_project(self._write_midi())
        second = self._retitled_bundle(first)
        destination = self.root / "imported"
        write_import_bundle(first, destination)
        old_generation = {
            path.name: path.read_bytes() for path in destination.iterdir()
        }

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        published_then_failed = False
        raced_stage: Path | None = None

        def fail_after_publish_then_occupy_stage(source, target):
            nonlocal published_then_failed, raced_stage
            source_path = Path(source)
            target_path = Path(target)
            if (
                not published_then_failed
                and ".import-stage." in source_path.name
                and _same_test_path(target_path, destination)
            ):
                real_rename_noreplace(source, target)
                published_then_failed = True
                raise OSError("primary post-publish failure")
            if (
                published_then_failed
                and _same_test_path(source_path, destination)
                and ".import-stage." in target_path.name
            ):
                raced_stage = target_path
                target_path.mkdir()
                (target_path / "user-data.txt").write_text(
                    "rollback stage racer",
                    encoding="utf-8",
                )
            return real_rename_noreplace(source, target)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=fail_after_publish_then_occupy_stage,
            ),
            self.assertRaisesRegex(
                OSError,
                "primary post-publish failure",
            ) as caught,
        ):
            write_import_bundle(second, destination, overwrite=True)

        self.assertTrue(published_then_failed)
        self.assertIsNotNone(raced_stage)
        assert raced_stage is not None
        self.assertEqual(
            (raced_stage / "user-data.txt").read_text(encoding="utf-8"),
            "rollback stage racer",
        )
        self.assertEqual(
            {path.name: path.read_bytes() for path in destination.iterdir()},
            old_generation,
        )
        self.assertEqual(len(list(self.root.glob(".imported.import-backup.*"))), 0)
        recoveries = list(self.root.glob(".imported.import-recovery.*"))
        self.assertEqual(len(recoveries), 1)
        self.assertTrue((recoveries[0] / "score.json").is_file())
        self.assertTrue(
            any(
                "retained for recovery" in note
                and _note_mentions_test_path(note, recoveries[0])
                for note in getattr(caught.exception, "__notes__", ())
            )
        )

    def test_rollback_restore_never_clobbers_a_target_racer(self) -> None:
        first = import_midi_project(self._write_midi())
        second = self._retitled_bundle(first)
        destination = self.root / "imported"
        write_import_bundle(first, destination)

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        publish_failed = False
        restore_raced = False

        def fail_publish_then_occupy_target(source, target):
            nonlocal publish_failed, restore_raced
            source_path = Path(source)
            target_path = Path(target)
            if (
                not publish_failed
                and ".import-stage." in source_path.name
                and _same_test_path(target_path, destination)
            ):
                publish_failed = True
                raise OSError("primary pre-publish failure")
            if (
                publish_failed
                and ".import-backup." in source_path.name
                and _same_test_path(target_path, destination)
            ):
                restore_raced = True
                destination.mkdir()
                (destination / "user-data.txt").write_text(
                    "rollback target racer",
                    encoding="utf-8",
                )
            return real_rename_noreplace(source, target)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=fail_publish_then_occupy_target,
            ),
            self.assertRaisesRegex(
                OSError,
                "primary pre-publish failure",
            ) as caught,
        ):
            write_import_bundle(second, destination, overwrite=True)

        self.assertTrue(publish_failed)
        self.assertTrue(restore_raced)
        self.assertEqual(
            (destination / "user-data.txt").read_text(encoding="utf-8"),
            "rollback target racer",
        )
        backups = list(self.root.glob(".imported.import-backup.*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "score.json").is_file())
        self.assertTrue(
            any(
                "automatic rollback was incomplete" in note
                and _note_mentions_test_path(note, backups[0])
                for note in getattr(caught.exception, "__notes__", ())
            )
        )

    def test_rollback_restore_move_then_error_is_not_reported_incomplete(
        self,
    ) -> None:
        first = import_midi_project(self._write_midi())
        second = self._retitled_bundle(first)
        destination = self.root / "imported"
        write_import_bundle(first, destination)
        before = {
            path.name: path.read_bytes() for path in destination.iterdir()
        }

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        publish_failed = False
        restore_reported_error = False

        def fail_publish_and_report_after_restore(source, target):
            nonlocal publish_failed, restore_reported_error
            source_path = Path(source)
            target_path = Path(target)
            if (
                not publish_failed
                and ".import-stage." in source_path.name
                and _same_test_path(target_path, destination)
            ):
                publish_failed = True
                raise OSError("PRIMARY staged publication failure")
            if (
                publish_failed
                and not restore_reported_error
                and ".import-backup." in source_path.name
                and _same_test_path(target_path, destination)
            ):
                real_rename_noreplace(source_path, target_path)
                restore_reported_error = True
                raise PermissionError("rollback restore reported failure")
            real_rename_noreplace(source_path, target_path)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=fail_publish_and_report_after_restore,
            ),
            self.assertRaisesRegex(
                OSError,
                "PRIMARY staged publication failure",
            ) as caught,
        ):
            write_import_bundle(second, destination, overwrite=True)

        self.assertTrue(publish_failed and restore_reported_error)
        self.assertEqual(
            {path.name: path.read_bytes() for path in destination.iterdir()},
            before,
        )
        self.assertEqual(list(self.root.glob(".imported.import-backup.*")), [])
        self.assertFalse(
            any(
                "automatic rollback was incomplete" in note
                for note in getattr(caught.exception, "__notes__", ())
            )
        )

    def test_rollback_withdraw_source_swap_restores_racer_before_old_backup(
        self,
    ) -> None:
        first = import_midi_project(self._write_midi())
        second = self._retitled_bundle(first)
        destination = self.root / "imported"
        write_import_bundle(first, destination)
        parked_new = self.root / "parked-expected-new-generation"

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        real_replace = os.replace
        published_then_failed = False
        raced = False

        def fail_publish_then_swap_withdraw_source(source, target):
            nonlocal published_then_failed, raced
            source_path = Path(source)
            target_path = Path(target)
            if (
                not published_then_failed
                and ".import-stage." in source_path.name
                and _same_test_path(target_path, destination)
            ):
                real_rename_noreplace(source_path, target_path)
                published_then_failed = True
                raise OSError("PRIMARY post-publish failure")
            if (
                published_then_failed
                and not raced
                and _same_test_path(source_path, destination)
                and ".import-stage." in target_path.name
            ):
                raced = True
                real_replace(destination, parked_new)
                destination.mkdir()
                (destination / "racer.txt").write_text(
                    "rollback withdraw racer",
                    encoding="utf-8",
                )
            real_rename_noreplace(source_path, target_path)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=fail_publish_then_swap_withdraw_source,
            ),
            self.assertRaisesRegex(
                OSError,
                "PRIMARY post-publish failure",
            ) as caught,
        ):
            write_import_bundle(second, destination, overwrite=True)

        self.assertTrue(published_then_failed and raced)
        self.assertEqual(
            (destination / "racer.txt").read_text(encoding="utf-8"),
            "rollback withdraw racer",
        )
        self.assertTrue((parked_new / "score.json").is_file())
        backups = list(self.root.glob(".imported.import-backup.*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "score.json").is_file())
        self.assertTrue(
            any(
                "automatic rollback was incomplete" in note
                for note in getattr(caught.exception, "__notes__", ())
            )
        )

    def test_published_stage_identity_race_is_detected_and_preserved(self) -> None:
        bundle = import_midi_project(self._write_midi())
        destination = self.root / "imported"
        parked_stage = self.root / "parked-published-stage"

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        raced = False

        def replace_after_stage_move(source, target):
            nonlocal raced
            source_path = Path(source)
            target_path = Path(target)
            result = real_rename_noreplace(source, target)
            if (
                not raced
                and ".import-stage." in source_path.name
                and _same_test_path(target_path, destination)
            ):
                raced = True
                os.replace(destination, parked_stage)
                destination.mkdir()
                (destination / "user-data.txt").write_text(
                    "post-move target racer",
                    encoding="utf-8",
                )
            return result

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=replace_after_stage_move,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "staging generation was replaced concurrently",
            ),
        ):
            write_import_bundle(bundle, destination)

        self.assertTrue(raced)
        self.assertEqual(
            (destination / "user-data.txt").read_text(encoding="utf-8"),
            "post-move target racer",
        )
        self.assertTrue((parked_stage / "score.json").is_file())

    def test_first_publish_in_place_mutation_is_withdrawn_from_public_path(
        self,
    ) -> None:
        bundle = import_midi_project(self._write_midi())
        destination = self.root / "imported"

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        mutated = False

        def mutate_after_stage_move(source, target):
            nonlocal mutated
            source_path = Path(source)
            target_path = Path(target)
            result = real_rename_noreplace(source, target)
            if (
                not mutated
                and ".import-stage." in source_path.name
                and _same_test_path(target_path, destination)
            ):
                mutated = True
                (destination / "score.json").write_bytes(b"{}\n")
            return result

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=mutate_after_stage_move,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "staging generation was replaced concurrently",
            ) as caught,
        ):
            write_import_bundle(bundle, destination)

        recoveries = list(self.root.glob(".imported.import-stage.*"))
        self.assertTrue(mutated)
        self.assertFalse(destination.exists())
        self.assertEqual(len(recoveries), 1)
        self.assertEqual((recoveries[0] / "score.json").read_bytes(), b"{}\n")
        self.assertTrue(
            any(
                "retained for recovery" in note
                and _note_mentions_test_path(note, recoveries[0])
                for note in getattr(caught.exception, "__notes__", ())
            )
        )

    def test_overwrite_in_place_mutation_restores_the_old_generation(self) -> None:
        first = import_midi_project(self._write_midi())
        second = self._retitled_bundle(first)
        destination = self.root / "imported"
        write_import_bundle(first, destination)
        old_generation = {
            path.name: path.read_bytes() for path in destination.iterdir()
        }

        import tianlai.project_import as project_import

        real_rename_noreplace = project_import._rename_noreplace
        mutated = False

        def mutate_after_stage_move(source, target):
            nonlocal mutated
            source_path = Path(source)
            target_path = Path(target)
            result = real_rename_noreplace(source, target)
            if (
                not mutated
                and ".import-stage." in source_path.name
                and _same_test_path(target_path, destination)
            ):
                mutated = True
                (destination / "score.json").write_bytes(b"{}\n")
            return result

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=mutate_after_stage_move,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "staging generation was replaced concurrently",
            ) as caught,
        ):
            write_import_bundle(second, destination, overwrite=True)

        recoveries = list(self.root.glob(".imported.import-stage.*"))
        self.assertTrue(mutated)
        self.assertEqual(
            {path.name: path.read_bytes() for path in destination.iterdir()},
            old_generation,
        )
        self.assertEqual(len(list(self.root.glob(".imported.import-backup.*"))), 0)
        self.assertEqual(len(recoveries), 1)
        self.assertEqual((recoveries[0] / "score.json").read_bytes(), b"{}\n")
        self.assertTrue(
            any(
                "retained for recovery" in note
                and _note_mentions_test_path(note, recoveries[0])
                for note in getattr(caught.exception, "__notes__", ())
            )
        )

    def test_overwrite_rejects_a_hard_linked_generation_document(self) -> None:
        first = import_midi_project(self._write_midi())
        destination = self.root / "imported"
        write_import_bundle(first, destination)
        alias = self.root / "score-alias.json"
        try:
            os.link(destination / "score.json", alias)
        except OSError as exc:  # pragma: no cover - unusual test filesystem
            self.skipTest(f"hard links unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "verified Tianlai"):
            write_import_bundle(
                self._retitled_bundle(first),
                destination,
                overwrite=True,
            )

        self.assertTrue(destination.is_dir())
        self.assertEqual(
            (destination / "score.json").read_bytes(),
            alias.read_bytes(),
        )

    def test_stage_cleanup_preserves_a_last_moment_replacement_and_primary_error(
        self,
    ) -> None:
        bundle = import_midi_project(self._write_midi())
        destination = self.root / "imported"
        parked = self.root / "parked-owned-stage"

        import tianlai.project_import as project_import

        real_replace = os.replace
        real_rename_noreplace = project_import._rename_noreplace
        replaced_stage: Path | None = None

        def fail_after_replacing_stage(source, target):
            nonlocal replaced_stage
            source_path = Path(source)
            if ".import-stage." in source_path.name:
                replaced_stage = source_path
                real_replace(source_path, parked)
                source_path.mkdir()
                (source_path / "user-data.txt").write_text(
                    "cleanup must not delete me",
                    encoding="utf-8",
                )
                raise OSError("primary publish failure")
            return real_rename_noreplace(source, target)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=fail_after_replacing_stage,
            ),
            self.assertRaisesRegex(OSError, "primary publish failure") as caught,
        ):
            write_import_bundle(bundle, destination)

        self.assertIsNotNone(replaced_stage)
        assert replaced_stage is not None
        self.assertEqual(
            (replaced_stage / "user-data.txt").read_text(encoding="utf-8"),
            "cleanup must not delete me",
        )
        self.assertTrue((parked / "score.json").is_file())
        self.assertTrue(
            any(
                "cleanup was not completed" in note
                for note in getattr(caught.exception, "__notes__", ())
            )
        )
        self.assertFalse(destination.exists())

    def test_backup_cleanup_quarantines_but_does_not_delete_a_racer(
        self,
    ) -> None:
        first = import_midi_project(self._write_midi())
        second = self._retitled_bundle(first)
        destination = self.root / "imported"
        write_import_bundle(first, destination)
        parked = self.root / "parked-old-backup"

        import tianlai.project_import as project_import

        real_replace = os.replace
        real_rename_noreplace = project_import._rename_noreplace
        raced = False

        def race_cleanup(source, target):
            nonlocal raced
            source_path = Path(source)
            target_path = Path(target)
            if (
                not raced
                and ".import-backup." in source_path.name
                and target_path.name == "generation"
                and target_path.parent.name.startswith(
                    ".tianlai-import-cleanup."
                )
            ):
                raced = True
                real_replace(source_path, parked)
                source_path.mkdir()
                (source_path / "user-data.txt").write_text(
                    "preserved cleanup racer",
                    encoding="utf-8",
                )
            return real_rename_noreplace(source, target)

        with (
            mock.patch.object(
                project_import,
                "_rename_noreplace",
                side_effect=race_cleanup,
            ),
            self.assertWarnsRegex(RuntimeWarning, "cleanup was not completed"),
        ):
            write_import_bundle(second, destination, overwrite=True)

        preserved = [
            path
            for path in self.root.iterdir()
            if path.name.startswith(".tianlai-import-cleanup.")
        ]
        self.assertTrue(raced)
        self.assertEqual(len(preserved), 1)
        self.assertEqual(
            (preserved[0] / "generation" / "user-data.txt").read_text(
                encoding="utf-8"
            ),
            "preserved cleanup racer",
        )
        self.assertTrue((parked / "score.json").is_file())
        self.assertEqual(
            json.loads((destination / "score.json").read_text(encoding="utf-8"))[
                "title"
            ],
            "New title",
        )


class CanonicalHashTests(unittest.TestCase):
    def test_hash_is_key_order_independent_and_rejects_nonfinite_values(self) -> None:
        self.assertEqual(
            canonical_json_sha256({"b": 2, "a": 1}),
            canonical_json_sha256({"a": 1, "b": 2}),
        )
        self.assertNotEqual(
            canonical_json_sha256({"value": 1}),
            canonical_json_sha256({"value": 1.0}),
        )
        with self.assertRaises(ValueError):
            canonical_json_sha256({"value": float("inf")})


if __name__ == "__main__":
    unittest.main()
