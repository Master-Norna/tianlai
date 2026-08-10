from pathlib import Path
import unittest

import pytest

from tianlai.analysis import analyze_file_pitch
from tianlai.events import PerformanceEvent, parse_performance_document
from tianlai.instrument import create_instrument
from tianlai.renderer import load_json_object
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "乐器/键盘乐器/钢琴/乐器.json"
ASSET_PATH = ROOT / "音源/钢琴/SalamanderGrandPiano/Samples"
SAMPLE_RATE = 48000
pytestmark = pytest.mark.external_assets


@unittest.skipUnless(ASSET_PATH.is_dir(), "Salamander piano assets are not installed")
class PianoInstrumentTests(unittest.TestCase):
    def _create_piano(self):
        return create_instrument(
            load_json_object(MANIFEST_PATH),
            SAMPLE_RATE,
            base_directory=str(MANIFEST_PATH.parent),
        )

    def test_recorded_a4_is_within_two_cents(self) -> None:
        measurement = analyze_file_pitch(ASSET_PATH / "A4v8.flac", 440.0)
        self.assertLess(abs(measurement.detune_cents), 2.0)

    def test_a4_middle_velocity_selects_original_layer_8_lazily(self) -> None:
        piano = self._create_piano()
        self.assertEqual(sum(region.sample.frames is not None for region in piano.main.regions), 0)

        piano.handle_event(
            PerformanceEvent(
                sample=0,
                sequence=0,
                type="note_on",
                payload={"note_id": 1, "midi_note": 69.0, "velocity": 0.5},
            ),
            EqualTemperament(440.0),
        )
        loaded = [region.sample.path.name for region in piano.main.regions if region.sample.frames is not None]
        self.assertEqual(loaded, ["A4v8.flac"])

    def test_b7_and_c8_use_the_corrected_c8_source_zone(self) -> None:
        piano = self._create_piano()
        tuning = EqualTemperament(440.0)

        for note_id, midi_note in enumerate((107.0, 108.0), start=1):
            piano.handle_event(
                PerformanceEvent(
                    sample=note_id - 1,
                    sequence=note_id - 1,
                    type="note_on",
                    payload={
                        "note_id": note_id,
                        "midi_note": midi_note,
                        "velocity": 0.5,
                    },
                ),
                tuning,
            )
            voice = piano.main.voices[note_id]
            self.assertEqual(voice.region.path.name, "C8v8.flac")

    def test_every_piano_sample_layer_uses_bandlimited_resampling(self) -> None:
        piano = self._create_piano()
        for name in (
            "main",
            "hammer",
            "resonance",
            "resonance_v3",
            "pedal_down",
            "pedal_up",
        ):
            with self.subTest(layer=name):
                self.assertEqual(
                    getattr(piano, name).resampling_quality,
                    "bandlimited",
                )

    def test_release_and_pedal_layers_are_triggered(self) -> None:
        piano = self._create_piano()
        tuning = EqualTemperament(440.0)
        piano.handle_event(
            PerformanceEvent(
                sample=0,
                sequence=0,
                type="note_on",
                payload={"note_id": 1, "midi_note": 60.0, "velocity": 0.6},
            ),
            tuning,
        )
        piano.handle_event(
            PerformanceEvent(
                sample=1,
                sequence=1,
                type="note_off",
                payload={"note_id": 1, "release_velocity": 0.4},
            ),
            tuning,
        )
        self.assertEqual(piano.hammer.active_voice_count, 1)
        self.assertEqual(piano.resonance.active_voice_count, 1)
        self.assertEqual(piano.resonance_v3.active_voice_count, 1)

        piano.handle_event(
            PerformanceEvent(
                sample=2,
                sequence=2,
                type="control",
                payload={"name": "sustain_pedal", "value": 1.0},
            ),
            tuning,
        )
        self.assertEqual(piano.pedal_down.active_voice_count, 1)

    def test_release_noise_scales_steeply_with_note_velocity(self) -> None:
        """松键机械噪声必须随音符力度陡峭缩放:弱奏时几乎无声,强奏才明显。

        否则弱奏曲每次松键都发出与强奏同响的“咚”,在稀疏乐句的间隙里成为
        非均匀的“秒针”背景声。这里必须经过正式事件解析器，并确认没有显式
        release_velocity 的 note_off 保持“未知”；钢琴的 SFZ release 层应
        使用对应 note_on 的力度。
        """

        def release_noise_peak(velocity: float) -> float:
            performance = parse_performance_document(
                {
                    "sample_rate": SAMPLE_RATE,
                    "channels": 2,
                    "tail_seconds": 0.5,
                    "events": [
                        {
                            "time": 0.0,
                            "type": "note_on",
                            "note_id": 1,
                            "midi_note": 60.0,
                            "velocity": velocity,
                        },
                        {
                            "time": 0.1,
                            "type": "note_off",
                            "note_id": 1,
                        },
                    ],
                }
            )
            self.assertNotIn("release_velocity", performance.events[1].payload)
            piano = self._create_piano()
            tuning = EqualTemperament(440.0)
            for event in performance.events:
                piano.handle_event(event, tuning)
            # 只测 hammer 引擎的输出,隔离释放噪声本身。
            peak = 0.0
            for _ in range(int(SAMPLE_RATE * 0.5)):
                left, right = piano.hammer.render_frame()
                peak = max(peak, abs(left), abs(right))
            return peak

        soft = release_noise_peak(0.2)
        loud = release_noise_peak(0.8)
        # 上游 amp_veltrack=82；即使采用近似曲线，强弱两次 release 也应
        # 相差约一个数量级，绝不能被解析器默认的 0.5 压成完全相同。
        self.assertGreater(loud, soft * 8.0)

    def test_release_layers_wait_for_sustain_pedal_and_trigger_once_on_pedal_up(
        self,
    ) -> None:
        piano = self._create_piano()
        tuning = EqualTemperament(440.0)
        piano.handle_event(
            PerformanceEvent(
                0,
                0,
                "note_on",
                {"note_id": 1, "midi_note": 60.0, "velocity": 0.6},
            ),
            tuning,
        )
        piano.handle_event(
            PerformanceEvent(
                100,
                1,
                "control",
                {"name": "sustain_pedal", "value": 1.0},
            ),
            tuning,
        )
        piano.handle_event(
            PerformanceEvent(
                200,
                2,
                "note_off",
                {"note_id": 1, "release_velocity": 0.5},
            ),
            tuning,
        )
        self.assertEqual(piano.hammer.active_voice_count, 0)
        self.assertEqual(piano.resonance.active_voice_count, 0)
        self.assertEqual(piano.resonance_v3.active_voice_count, 0)

        piano.handle_event(
            PerformanceEvent(
                300,
                3,
                "control",
                {"name": "sustain_pedal", "value": 0.0},
            ),
            tuning,
        )
        self.assertEqual(piano.hammer.active_voice_count, 1)
        self.assertEqual(piano.resonance.active_voice_count, 1)
        self.assertEqual(piano.resonance_v3.active_voice_count, 1)

        # 重复发送同一踏板状态不应再次释放同一个音。
        piano.handle_event(
            PerformanceEvent(
                400,
                4,
                "control",
                {"name": "sustain_pedal", "value": 0.0},
            ),
            tuning,
        )
        self.assertEqual(piano.hammer.active_voice_count, 1)
        self.assertEqual(piano.resonance.active_voice_count, 1)
        self.assertEqual(piano.resonance_v3.active_voice_count, 1)

    def test_sustain_same_key_restrike_does_not_emit_a_stale_release(self) -> None:
        piano = self._create_piano()
        tuning = EqualTemperament(440.0)

        piano.handle_event(
            PerformanceEvent(
                0,
                0,
                "note_on",
                {"note_id": 1, "midi_note": 60.0, "velocity": 0.6},
            ),
            tuning,
        )
        piano.handle_event(
            PerformanceEvent(
                100,
                1,
                "control",
                {"name": "sustain_pedal", "value": 1.0},
            ),
            tuning,
        )
        piano.handle_event(
            PerformanceEvent(
                200,
                2,
                "note_off",
                {"note_id": 1, "release_velocity": 0.5},
            ),
            tuning,
        )
        self.assertIn(1, piano.deferred_releases)

        piano.handle_event(
            PerformanceEvent(
                300,
                3,
                "note_on",
                {"note_id": 2, "midi_note": 60.0, "velocity": 0.7},
            ),
            tuning,
        )
        self.assertNotIn(1, piano.deferred_releases)

        # Releasing the pedal while the replacement strike is still held
        # must not play the old key cycle's hammer/string release layers.
        piano.handle_event(
            PerformanceEvent(
                400,
                4,
                "control",
                {"name": "sustain_pedal", "value": 0.0},
            ),
            tuning,
        )
        self.assertEqual(piano.hammer.active_voice_count, 0)
        self.assertEqual(piano.resonance.active_voice_count, 0)
        self.assertEqual(piano.resonance_v3.active_voice_count, 0)

        piano.handle_event(
            PerformanceEvent(
                500,
                5,
                "note_off",
                {"note_id": 2, "release_velocity": 0.5},
            ),
            tuning,
        )
        self.assertEqual(piano.hammer.active_voice_count, 1)
        self.assertEqual(piano.resonance.active_voice_count, 1)
        self.assertEqual(piano.resonance_v3.active_voice_count, 1)
        release_voices = (
            next(iter(piano.hammer.voices.values())),
            next(iter(piano.resonance.voices.values())),
            next(iter(piano.resonance_v3.voices.values())),
        )

        # A duplicate note_off no longer owns a held note and therefore must
        # not restart any one-shot release sample.
        piano.handle_event(
            PerformanceEvent(
                600,
                6,
                "note_off",
                {"note_id": 2, "release_velocity": 0.5},
            ),
            tuning,
        )
        self.assertIs(next(iter(piano.hammer.voices.values())), release_voices[0])
        self.assertIs(next(iter(piano.resonance.voices.values())), release_voices[1])
        self.assertIs(
            next(iter(piano.resonance_v3.voices.values())),
            release_voices[2],
        )

    def test_notes_above_resonance_mapping_only_trigger_hammer_release(self) -> None:
        piano = self._create_piano()
        tuning = EqualTemperament(440.0)
        piano.handle_event(
            PerformanceEvent(
                0,
                0,
                "note_on",
                {"note_id": 1, "midi_note": 89.0, "velocity": 0.7},
            ),
            tuning,
        )
        piano.handle_event(
            PerformanceEvent(
                4800,
                1,
                "note_off",
                {"note_id": 1, "release_velocity": 0.5},
            ),
            tuning,
        )
        self.assertEqual(piano.hammer.active_voice_count, 1)
        self.assertEqual(piano.resonance.active_voice_count, 0)
        self.assertEqual(piano.resonance_v3.active_voice_count, 0)

    def test_release_layers_limit_same_pitch_polyphony_but_keep_other_pitches(
        self,
    ) -> None:
        piano = self._create_piano()
        tuning = EqualTemperament(440.0)

        def release(note_id: int, midi_note: float, sample: int) -> None:
            piano.handle_event(
                PerformanceEvent(
                    sample,
                    note_id * 2,
                    "note_on",
                    {
                        "note_id": note_id,
                        "midi_note": midi_note,
                        "velocity": 0.7,
                    },
                ),
                tuning,
            )
            piano.handle_event(
                PerformanceEvent(
                    sample + 240,
                    note_id * 2 + 1,
                    "note_off",
                    {"note_id": note_id, "release_velocity": 0.5},
                ),
                tuning,
            )

        release(1, 60.0, 0)
        release(2, 60.0, 480)
        self.assertEqual(piano.hammer.active_voice_count, 1)
        self.assertEqual(piano.resonance.active_voice_count, 1)
        self.assertEqual(piano.resonance_v3.active_voice_count, 1)

        release(3, 62.0, 960)
        self.assertEqual(piano.hammer.active_voice_count, 2)
        self.assertEqual(piano.resonance.active_voice_count, 2)
        self.assertEqual(piano.resonance_v3.active_voice_count, 2)

    def test_same_key_retrigger_damps_the_previous_main_voice_only(self) -> None:
        piano = self._create_piano()
        tuning = EqualTemperament(440.0)
        piano.handle_event(
            PerformanceEvent(
                0,
                0,
                "note_on",
                {"note_id": 1, "midi_note": 60.0, "velocity": 0.6},
            ),
            tuning,
        )
        for _ in range(480):
            piano.main.render_frame()

        piano.handle_event(
            PerformanceEvent(
                480,
                1,
                "note_on",
                {"note_id": 2, "midi_note": 60.0, "velocity": 0.65},
            ),
            tuning,
        )
        # A late note_off for the superseded source note must not release the
        # new strike that now owns this physical key.
        piano.handle_event(
            PerformanceEvent(
                500,
                2,
                "note_off",
                {"note_id": 1, "release_velocity": 0.5},
            ),
            tuning,
        )
        self.assertIn(2, piano.main.voices)
        for _ in range(round(SAMPLE_RATE * 0.05)):
            piano.main.render_frame()
        self.assertEqual(set(piano.main.voices), {2})

    def test_hammer_release_applies_two_db_per_second_hold_decay(self) -> None:
        tuning = EqualTemperament(440.0)

        def hammer_amplitude(hold_seconds: float) -> float:
            piano = self._create_piano()
            piano.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "midi_note": 60.0, "velocity": 0.7},
                ),
                tuning,
            )
            piano.handle_event(
                PerformanceEvent(
                    round(hold_seconds * SAMPLE_RATE),
                    1,
                    "note_off",
                    {"note_id": 1, "release_velocity": 0.5},
                ),
                tuning,
            )
            self.assertEqual(piano.hammer.active_voice_count, 1)
            return next(iter(piano.hammer.voices.values())).amplitude

        short = hammer_amplitude(0.1)
        long = hammer_amplitude(2.0)
        expected_ratio = 10.0 ** (-(2.0 - 0.1) * 2.0 / 20.0)
        self.assertLess(long, short)
        self.assertAlmostEqual(long / short, expected_ratio, delta=0.03)

    def test_v3_release_uses_upstream_velocity_curve_and_hold_decay(self) -> None:
        tuning = EqualTemperament(440.0)

        def v3_amplitude(velocity: float, hold_seconds: float) -> float:
            piano = self._create_piano()
            self.assertAlmostEqual(piano.resonance_v3.velocity_exponent, 1.92)
            piano.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "midi_note": 60.0, "velocity": velocity},
                ),
                tuning,
            )
            piano.handle_event(
                PerformanceEvent(
                    round(hold_seconds * SAMPLE_RATE),
                    1,
                    "note_off",
                    {"note_id": 1, "release_velocity": 0.5},
                ),
                tuning,
            )
            self.assertEqual(piano.resonance_v3.active_voice_count, 1)
            return next(iter(piano.resonance_v3.voices.values())).amplitude

        soft = v3_amplitude(0.4, 0.1)
        loud = v3_amplitude(0.8, 0.1)
        self.assertAlmostEqual(loud / soft, (0.8 / 0.4) ** 1.92, delta=0.03)

        short = v3_amplitude(0.7, 0.1)
        long = v3_amplitude(0.7, 2.0)
        expected_ratio = 10.0 ** (-(2.0 - 0.1) * 2.0 / 20.0)
        self.assertLess(long, short)
        self.assertAlmostEqual(long / short, expected_ratio, delta=0.03)

    def test_performance_without_controls_never_triggers_pedal_noise(self) -> None:
        performance = parse_performance_document(
            {
                "sample_rate": SAMPLE_RATE,
                "channels": 2,
                "tail_seconds": 0.5,
                "events": [
                    {
                        "time": 0.0,
                        "type": "note_on",
                        "note_id": 1,
                        "midi_note": 60.0,
                        "velocity": 0.6,
                    },
                    {
                        "time": 0.2,
                        "type": "note_off",
                        "note_id": 1,
                    },
                ],
            }
        )
        piano = self._create_piano()
        tuning = EqualTemperament(440.0)
        for event in performance.events:
            piano.handle_event(event, tuning)
        self.assertEqual(piano.pedal_down.active_voice_count, 0)
        self.assertEqual(piano.pedal_up.active_voice_count, 0)


if __name__ == "__main__":
    unittest.main()
