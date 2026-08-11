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

    def test_first_publish_failure_leaves_no_partial_bundle(self) -> None:
        bundle = import_midi_project(self._write_midi())
        destination = self.root / "imported"

        import tianlai.project_import as project_import

        real_replace = os.replace

        def fail_publish(source, target):
            if ".import-stage." in Path(source).name:
                raise OSError("simulated first publish failure")
            return real_replace(source, target)

        with (
            mock.patch.object(
                project_import.os,
                "replace",
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
        second = copy.deepcopy(first)
        second["score"]["title"] = "New title"
        digest = canonical_json_sha256(second["score"])
        second["import_report"]["score"]["canonical_sha256"] = digest
        second["import_report"]["score_canonical_sha256"] = digest
        second["roster_draft"]["source"]["score"]["canonical_sha256"] = digest
        second["roster_draft"]["draft_roster"]["name"] = "New title 编制草稿"
        validate_import_bundle(second)

        import tianlai.project_import as project_import

        real_replace = os.replace
        publish_attempts = 0

        def fail_new_generation(source, target):
            nonlocal publish_attempts
            source_path = Path(source)
            if ".import-stage." in source_path.name:
                publish_attempts += 1
                if publish_attempts == 1:
                    raise OSError("simulated publish failure")
            return real_replace(source, target)

        with (
            mock.patch.object(
                project_import.os,
                "replace",
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
