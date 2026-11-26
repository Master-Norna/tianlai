from pathlib import Path
import tempfile
import unittest

from tianlai.sfz import note_number, parse_sfz, regions_to_manifest


class SfzParserTests(unittest.TestCase):
    def test_symbolic_notes_follow_midi_octaves(self) -> None:
        self.assertEqual(note_number("C4"), 60)
        self.assertEqual(note_number("G3"), 55)
        self.assertEqual(note_number("Db4"), 61)

    def test_group_values_are_inherited_and_region_values_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sfz"
            path.write_text(
                "<group> ampeg_release=1.2 tune=-5\n"
                "<region> sample=a.wav key=C4\n"
                "volume=3 tune=2\n"
                "<region> sample=b.wav lokey=D4 hikey=E4 pitch_keycenter=D4\n",
                encoding="utf-8",
            )
            regions = parse_sfz(path)
            self.assertEqual(len(regions), 2)
            self.assertEqual(regions[0].values["tune"], "2")
            self.assertEqual(regions[0].values["ampeg_release"], "1.2")
            self.assertEqual(regions[1].values["tune"], "-5")

    def test_master_inheritance_and_global_reset_match_dedicated_parser(self) -> None:
        from tianlai.dedicated_sfz import parse_dedicated_sfz

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hierarchy.sfz"
            path.write_text(
                "<global> tune=2\n"
                "<master> volume=3\n"
                "<group> ampeg_release=0.5\n"
                "<region> sample=a.wav key=C4\n"
                "<group> ampeg_attack=0.1\n"
                "<region> sample=b.wav key=D4\n"
                "<master> volume=4\n"
                "<region> sample=c.wav key=E4\n"
                "<global> tune=-1\n"
                "<region> sample=d.wav key=F4\n",
                encoding="utf-8",
            )
            simple = [item.values for item in parse_sfz(path)]
            dedicated = [
                item.values for item in parse_dedicated_sfz(
                    path, asset_root=Path(directory)
                ).regions
            ]
            self.assertEqual(simple, dedicated)
            self.assertEqual(simple[0]["volume"], "3")
            self.assertEqual(simple[1]["volume"], "3")
            self.assertNotIn("ampeg_release", simple[1])
            self.assertNotIn("ampeg_attack", simple[2])
            self.assertNotIn("volume", simple[3])
            self.assertEqual(simple[3]["tune"], "-1")

    def test_attack_and_release_regions_can_be_separated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trigger.sfz"
            path.write_text(
                "<region> sample=attack.wav key=60\n"
                "<group> trigger=release\n"
                "<region> sample=release.wav key=60\n",
                encoding="utf-8",
            )
            attacks = regions_to_manifest(path, use_embedded_loops=False)
            releases = regions_to_manifest(
                path,
                use_embedded_loops=False,
                trigger="release",
            )
            self.assertEqual(Path(attacks[0]["sample"]).name, "attack.wav")
            self.assertEqual(Path(releases[0]["sample"]).name, "release.wav")


if __name__ == "__main__":
    unittest.main()
