from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock
import unittest
import zipfile

from tianlai.cli import main as cli_main
from tianlai.conductor import _merge_ties
from tianlai.musicxml_import import read_musicxml
from tianlai.score import parse_score_document


PARTWISE_COMPLEX = """\
<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <work><work-title>MusicXML 回归曲</work-title></work>
  <part-list>
    <score-part id="P1"><part-name>B-flat Clarinet</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>4</divisions>
        <time><beats>6</beats><beat-type>8</beat-type></time>
        <transpose><chromatic>-2</chromatic></transpose>
      </attributes>
      <direction>
        <direction-type><dynamics><p/></dynamics></direction-type>
        <sound tempo="90"/>
      </direction>
      <note>
        <pitch><step>C</step><octave>4</octave></pitch>
        <duration>2</duration><voice>1</voice><type>eighth</type>
        <tie type="start"/>
        <notations>
          <tied type="start"/>
          <articulations><staccato/></articulations>
        </notations>
      </note>
      <note>
        <chord/>
        <pitch><step>E</step><octave>4</octave></pitch>
        <duration>2</duration><voice>1</voice><type>eighth</type>
      </note>
      <note>
        <pitch><step>C</step><octave>4</octave></pitch>
        <duration>2</duration><voice>1</voice><type>eighth</type>
        <tie type="stop"/>
      </note>
      <direction>
        <direction-type><dynamics><ff/></dynamics></direction-type>
      </direction>
      <note>
        <rest/><duration>2</duration><voice>1</voice><type>eighth</type>
      </note>
      <note>
        <pitch><step>D</step><octave>4</octave></pitch>
        <duration>4</duration><voice>1</voice><type>quarter</type>
        <notations><articulations><accent/></articulations></notations>
      </note>
    </measure>
  </part>
</score-partwise>
"""


MULTIVOICE = """\
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>2</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice></note>
      <note><pitch><step>D</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice></note>
      <backup><duration>8</duration></backup>
      <note><pitch><step>E</step><octave>3</octave></pitch><duration>2</duration><voice>2</voice></note>
      <note><pitch><step>F</step><octave>3</octave></pitch><duration>2</duration><voice>2</voice></note>
      <note><pitch><step>G</step><octave>3</octave></pitch><duration>4</duration><voice>2</voice></note>
    </measure>
  </part>
</score-partwise>
"""


MULTIVOICE_SAME_PITCH_TIE = """\
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
      <note>
        <pitch><step>C</step><octave>4</octave></pitch>
        <duration>1</duration><voice>1</voice><staff>1</staff>
        <tie type="start"/>
      </note>
      <backup><duration>1</duration></backup>
      <forward><duration>1</duration></forward>
      <note dynamics="100">
        <pitch><step>C</step><octave>4</octave></pitch>
        <duration>1</duration><voice>2</voice><staff>1</staff>
        <notations><articulations><accent/></articulations></notations>
      </note>
      <backup><duration>1</duration></backup>
      <note>
        <pitch><step>C</step><octave>4</octave></pitch>
        <duration>1</duration><voice>1</voice><staff>1</staff>
        <tie type="stop"/>
      </note>
    </measure>
  </part>
</score-partwise>
"""


MINIMAL = """\
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>
  </measure></part>
</score-partwise>
"""


PICKUP = """\
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1">
    <measure number="0" implicit="yes">
      <attributes><divisions>4</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <note><pitch><step>G</step><octave>4</octave></pitch><duration>4</duration></note>
    </measure>
    <measure number="1">
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>16</duration></note>
    </measure>
  </part>
</score-partwise>
"""


PERCUSSION = """\
<score-partwise version="4.0">
  <part-list>
    <score-part id="P1">
      <part-name>Drumset</part-name>
      <score-instrument id="P1-I1"><instrument-name>Acoustic Bass Drum</instrument-name></score-instrument>
      <midi-instrument id="P1-I1"><midi-channel>10</midi-channel><midi-unpitched>36</midi-unpitched></midi-instrument>
    </score-part>
  </part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <note>
      <unpitched><display-step>C</display-step><display-octave>5</display-octave></unpitched>
      <instrument id="P1-I1"/><duration>1</duration>
    </note>
  </measure></part>
</score-partwise>
"""


class MusicXMLImportTest(unittest.TestCase):
    def _write(self, directory: str, name: str, content: str) -> Path:
        path = Path(directory) / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_imports_concert_pitch_meter_dynamics_chord_tie_and_articulation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "clarinet.musicxml", PARTWISE_COMPLEX)
            document, report = read_musicxml(path)

        score = parse_score_document(document)
        self.assertEqual(score.schema_version, 1)
        self.assertEqual(score.title, "MusicXML 回归曲")
        self.assertEqual(len(score.parts), 1)
        self.assertEqual(score.tempo_map.entries[0].bpm, 90.0)
        self.assertEqual(
            (
                score.tempo_map.entries[0].beats_per_bar,
                score.tempo_map.entries[0].beat_unit,
            ),
            (6, 8),
        )
        notes = score.parts[0].notes
        self.assertEqual(
            len({note.source_event_id for note in notes}),
            len(notes),
        )
        self.assertEqual(
            [(note.beat, note.midi, note.duration_beats) for note in notes],
            [(1.0, 58.0, 1.0), (1.0, 62.0, 1.0), (2.0, 58.0, 1.0), (4.0, 60.0, 2.0)],
        )
        self.assertEqual(notes[0].dynamic, "p")
        self.assertEqual(notes[0].articulation, "staccato")
        self.assertTrue(notes[0].tie)
        self.assertEqual((notes[0].staff, notes[0].voice), (1, "1"))
        self.assertFalse(notes[2].tie)
        self.assertEqual(notes[-1].dynamic, "ff")
        self.assertEqual(notes[-1].articulation, "accent")
        self.assertEqual(report.parts[0]["range"], "A#3~D4")

    def test_backup_flattens_voices_without_changing_onsets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "voices.musicxml", MULTIVOICE)
            document, _ = read_musicxml(path)

        notes = parse_score_document(document).parts[0].notes
        self.assertEqual(
            [(note.beat, note.midi, note.duration_beats) for note in notes],
            [
                (1.0, 52.0, 1.0),
                (1.0, 60.0, 2.0),
                (2.0, 53.0, 1.0),
                (3.0, 55.0, 2.0),
                (3.0, 62.0, 2.0),
            ],
        )

    def test_staff_and_voice_keep_equal_pitch_ties_in_their_own_voice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(
                directory,
                "same-pitch-voices.musicxml",
                MULTIVOICE_SAME_PITCH_TIE,
            )
            document, _ = read_musicxml(path)

        score = parse_score_document(document)
        notes = score.parts[0].notes
        self.assertEqual(
            [(note.staff, note.voice) for note in notes],
            [(1, "1"), (1, "2"), (1, "1")],
        )
        resolved = _merge_ties(notes, score)
        self.assertEqual(len(resolved), 2)
        self.assertEqual(
            (resolved[0].start_quarter, resolved[0].duration_quarters),
            (0.0, 2.0),
        )
        self.assertEqual(
            (resolved[1].start_quarter, resolved[1].duration_quarters),
            (1.0, 1.0),
        )
        self.assertEqual(resolved[1].articulation, "accent")

    def test_reads_compressed_mxl_container_without_extracting(self):
        container = """\
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="scores/回归.musicxml"
              media-type="application/vnd.recordare.musicxml+xml"/>
  </rootfiles>
</container>
"""
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "score.mxl"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("META-INF/container.xml", container)
                package.writestr("scores/回归.musicxml", MINIMAL)
            document, _ = read_musicxml(archive)
            self.assertFalse((Path(directory) / "scores").exists())

        note = parse_score_document(document).parts[0].notes[0]
        self.assertEqual((note.bar, note.beat, note.midi, note.duration_beats), (1, 1.0, 60.0, 1.0))

    def test_rejects_mxl_rootfile_path_traversal(self):
        container = """\
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="../outside.musicxml"/></rootfiles>
</container>
"""
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "hostile.mxl"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("META-INF/container.xml", container)
                package.writestr("../outside.musicxml", MINIMAL)
            with self.assertRaises(ValueError):
                read_musicxml(archive)

    def test_rejects_doctype_and_score_timewise(self):
        with tempfile.TemporaryDirectory() as directory:
            doctype = self._write(
                directory,
                "entity.musicxml",
                '<!DOCTYPE score-partwise [<!ENTITY x "boom">]><score-partwise>&x;</score-partwise>',
            )
            timewise = self._write(
                directory,
                "timewise.musicxml",
                '<score-timewise version="4.0"><part-list/></score-timewise>',
            )
            with self.assertRaises(ValueError):
                read_musicxml(doctype)
            with self.assertRaises(ValueError):
                read_musicxml(timewise)

    def test_maps_midi_unpitched_to_zero_based_midi(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "drums.musicxml", PERCUSSION)
            document, report = read_musicxml(path)

        note = parse_score_document(document).parts[0].notes[0]
        self.assertEqual(note.midi, 35.0)
        self.assertTrue(report.parts[0]["percussion"])

    def test_encodes_anacrusis_as_a_short_first_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "pickup.musicxml", PICKUP)
            document, _ = read_musicxml(path)

        score = parse_score_document(document)
        self.assertEqual(
            (
                score.tempo_map.entries[0].beats_per_bar,
                score.tempo_map.entries[0].beat_unit,
            ),
            (1, 4),
        )
        self.assertEqual(
            (
                score.tempo_map.entries[1].bar,
                score.tempo_map.entries[1].beats_per_bar,
                score.tempo_map.entries[1].beat_unit,
            ),
            (2, 4, 4),
        )
        self.assertEqual([(n.bar, n.beat) for n in score.parts[0].notes], [(1, 1.0), (2, 1.0)])

    def test_skips_grace_and_warns_when_repeat_is_not_expanded(self):
        xml = MINIMAL.replace(
            "<note><pitch><step>C</step>",
            "<barline location=\"right\"><repeat direction=\"backward\"/></barline>"
            "<note><grace/><pitch><step>D</step><octave>4</octave></pitch></note>"
            "<note><pitch><step>C</step>",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "warnings.musicxml", xml)
            document, report = read_musicxml(path)

        self.assertEqual(len(parse_score_document(document).parts[0].notes), 1)
        warnings = "\n".join(report.warnings).lower()
        self.assertIn("grace", warnings)
        self.assertTrue("repeat" in warnings or "重复" in warnings)

    def test_accepts_namespace_and_standard_external_doctype(self):
        xml = """\
<?xml version="1.0"?>
<!DOCTYPE score-partwise PUBLIC
  "-//Recordare//DTD MusicXML 4.0 Partwise//EN"
  "http://www.musicxml.org/dtds/partwise.dtd">
<score-partwise xmlns="http://www.musicxml.org/ns/musicxml" version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "doctype.musicxml", xml)
            document, _ = read_musicxml(path)
        self.assertEqual(parse_score_document(document).parts[0].notes[0].midi, 60.0)

    def test_cli_writes_a_score_document(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._write(directory, "source.musicxml", MINIMAL)
            destination = Path(directory) / "nested" / "source.score.json"
            with mock.patch("sys.stdout"):
                status = cli_main(
                    [
                        "import-musicxml",
                        "--musicxml",
                        str(source),
                        "--output",
                        str(destination),
                    ]
            )
            self.assertEqual(status, 0)
            self.assertTrue(destination.is_file())
            parsed = parse_score_document(
                json.loads(destination.read_text(encoding="utf-8"))
            )
            self.assertEqual(parsed.parts[0].notes[0].midi, 60.0)

    def test_mcp_tool_returns_score_report_and_is_registered(self):
        try:
            from tianlai import mcp_server
        except ModuleNotFoundError:
            self.skipTest("未安装可选 MCP 组件")

        with tempfile.TemporaryDirectory() as directory:
            source = self._write(directory, "source.musicxml", MINIMAL)
            with mock.patch.dict(
                "os.environ",
                {"TIANLAI_INPUT_ROOTS": str(source.parent)},
            ):
                result = mcp_server.import_musicxml(str(source))

        self.assertNotIn("error", result)
        self.assertEqual(result["parts"][0]["range"], "C4~C4")
        self.assertEqual(result["report"]["source_format"], "musicxml")
        names = {
            tool.name for tool in mcp_server.mcp._tool_manager.list_tools()
        }
        self.assertIn("import_musicxml", names)

    def test_staff_specific_dynamics_do_not_rewrite_an_earlier_voice(self):
        xml = """\
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <direction><direction-type><dynamics><p/></dynamics></direction-type><staff>1</staff></direction>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>1</duration><voice>1</voice><staff>1</staff></note>
    <backup><duration>1</duration></backup>
    <direction><direction-type><dynamics><ff/></dynamics></direction-type><staff>2</staff></direction>
    <note><pitch><step>E</step><octave>3</octave></pitch><duration>1</duration><voice>2</voice><staff>2</staff></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "staff-dynamics.musicxml", xml)
            document, _ = read_musicxml(path)

        by_pitch = {
            note.midi: note.dynamic
            for note in parse_score_document(document).parts[0].notes
        }
        self.assertEqual(by_pitch, {52.0: "ff", 60.0: "p"})

    def test_direct_sound_note_velocity_and_performance_offsets_are_preserved(self):
        xml = """\
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>4</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <sound tempo="77" dynamics="40"/>
    <note dynamics="71" attack="1" release="1">
      <pitch><step>C</step><octave>4</octave></pitch><duration>4</duration>
    </note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "performance.musicxml", xml)
            document, _ = read_musicxml(path)

        score = parse_score_document(document)
        note = score.parts[0].notes[0]
        self.assertEqual(score.tempo_map.entries[0].bpm, 77.0)
        self.assertEqual((note.beat, note.duration_beats), (1.25, 1.0))
        self.assertAlmostEqual(note.velocity, 63.9 / 127.0, places=6)

    def test_meter_change_and_mid_bar_tempo_use_the_new_beat_unit(self):
        xml = """\
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Flute</part-name></score-part></part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>4</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
      <note><rest/><duration>16</duration></note>
    </measure>
    <measure number="2">
      <attributes><time><beats>3</beats><beat-type>8</beat-type></time></attributes>
      <note><pitch><step>C</step><octave>5</octave></pitch><duration>2</duration></note>
      <direction><sound tempo="105"/></direction>
      <note><pitch><step>D</step><octave>5</octave></pitch><duration>4</duration></note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "meter.musicxml", xml)
            document, _ = read_musicxml(path)

        score = parse_score_document(document)
        self.assertEqual(
            [
                (
                    entry.bar,
                    entry.beat,
                    entry.bpm,
                    entry.beats_per_bar,
                    entry.beat_unit,
                )
                for entry in score.tempo_map.entries
            ],
            [(1, 1.0, 120.0, 4, 4), (2, 1.0, 120.0, 3, 8), (2, 2.0, 105.0, 3, 8)],
        )
        self.assertEqual(
            [(note.beat, note.duration_beats) for note in score.parts[0].notes],
            [(1.0, 1.0), (2.0, 2.0)],
        )

    def test_cli_does_not_create_output_for_invalid_xml(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self._write(directory, "broken.musicxml", "<score-partwise>")
            destination = Path(directory) / "never" / "broken.score.json"
            with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                status = cli_main(
                    [
                        "import-musicxml",
                        "--musicxml",
                        str(source),
                        "--output",
                        str(destination),
                    ]
                )
            self.assertEqual(status, 2)
            self.assertFalse(destination.exists())

    def test_direction_offset_moves_playback_only_when_sound_is_yes(self):
        template = """\
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <direction>
      <direction-type><metronome><beat-unit>quarter</beat-unit><per-minute>60</per-minute></metronome></direction-type>
      <offset{sound}>2</offset><sound tempo="60"/>
    </direction>
    <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as directory:
            visual = self._write(
                directory, "visual.musicxml", template.format(sound="")
            )
            audible = self._write(
                directory,
                "audible.musicxml",
                template.format(sound=' sound="yes"'),
            )
            visual_score = parse_score_document(read_musicxml(visual)[0])
            audible_score = parse_score_document(read_musicxml(audible)[0])

        self.assertEqual(
            [(entry.beat, entry.bpm) for entry in visual_score.tempo_map.entries],
            [(1.0, 60.0)],
        )
        self.assertEqual(
            [(entry.beat, entry.bpm) for entry in audible_score.tempo_map.entries],
            [(1.0, 120.0), (3.0, 60.0)],
        )

    def test_mixed_denominator_meter_is_collapsed_without_losing_time(self):
        xml = """\
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1">
    <measure number="1">
      <attributes><divisions>4</divisions><time>
        <beats>2</beats><beat-type>4</beat-type>
        <beats>3</beats><beat-type>8</beat-type>
      </time></attributes>
      <note><rest/><duration>14</duration></note>
    </measure>
    <measure number="2">
      <note><pitch><step>C</step><octave>4</octave></pitch><duration>2</duration></note>
    </measure>
  </part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "mixed-meter.musicxml", xml)
            document, report = read_musicxml(path)

        score = parse_score_document(document)
        self.assertEqual(
            (
                score.tempo_map.entries[0].beats_per_bar,
                score.tempo_map.entries[0].beat_unit,
            ),
            (7, 8),
        )
        self.assertEqual(score.tempo_map.quarter_at(2, 1.0), 3.5)
        self.assertIn("2/4 + 3/8", "\n".join(report.warnings))

    def test_utf16_internal_entity_is_rejected(self):
        xml = """\
<?xml version="1.0" encoding="UTF-16"?>
<!DOCTYPE score-partwise [<!ENTITY injected "boom">]>
<score-partwise><part-list>&injected;</part-list></score-partwise>
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "entity.musicxml"
            path.write_bytes(xml.encode("utf-16"))
            with self.assertRaises(ValueError):
                read_musicxml(path)

    def test_note_attached_steady_dynamic_persists_to_following_notes(self):
        xml = """\
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Violin</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <note>
      <pitch><step>C</step><octave>5</octave></pitch><duration>1</duration>
      <notations><dynamics><p/></dynamics></notations>
    </note>
    <note><pitch><step>D</step><octave>5</octave></pitch><duration>1</duration></note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "note-dynamic.musicxml", xml)
            document, _ = read_musicxml(path)

        notes = parse_score_document(document).parts[0].notes
        self.assertEqual([note.dynamic for note in notes], ["p", "p"])

    def test_octave_shift_is_not_double_applied_to_sounding_pitch_data(self):
        xml = """\
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Flute</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
    <direction><direction-type><octave-shift type="down" size="8"/></direction-type></direction>
    <note><pitch><step>C</step><octave>5</octave></pitch><duration>1</duration></note>
    <direction><direction-type><octave-shift type="stop" size="8"/></direction-type></direction>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "octave.musicxml", xml)
            document, report = read_musicxml(path)

        note = parse_score_document(document).parts[0].notes[0]
        self.assertEqual(note.midi, 72.0)
        self.assertNotIn("octave-shift", "\n".join(report.warnings))

    def test_chord_tones_inherit_omitted_voice_staff_and_staff_transpose(self):
        xml = """\
<score-partwise version="4.0">
  <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
  <part id="P1"><measure number="1">
    <attributes>
      <divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time>
      <staves>2</staves>
      <transpose number="2"><chromatic>-12</chromatic></transpose>
    </attributes>
    <direction>
      <direction-type><dynamics><p/></dynamics></direction-type>
      <voice>2</voice><staff>2</staff>
    </direction>
    <note>
      <pitch><step>E</step><octave>4</octave></pitch><duration>1</duration>
      <voice>2</voice><staff>2</staff>
    </note>
    <note>
      <chord/><pitch><step>G</step><octave>4</octave></pitch><duration>1</duration>
    </note>
  </measure></part>
</score-partwise>
"""
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, "chord-context.musicxml", xml)
            document, _ = read_musicxml(path)

        notes = parse_score_document(document).parts[0].notes
        self.assertEqual(
            [(note.midi, note.dynamic) for note in notes],
            [(52.0, "p"), (55.0, "p")],
        )


if __name__ == "__main__":
    unittest.main()
