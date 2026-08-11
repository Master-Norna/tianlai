"""Salamander Yamaha C5 钢琴的受信内置状态机与采样映射。"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

from tianlai._event_free_blocks import audited_event_free_blocks
from tianlai.events import PerformanceEvent, event_pitch_hz
from tianlai.instrument import Instrument, StereoFrame
from tianlai.sampler import SampleInstrument
from tianlai.tuning import EqualTemperament


# 上游 SFZ 的 Data/region.txt：每小三度一个主采样根音。
_MAIN_ROOTS = (
    ("A0", 21), ("C1", 24), ("D#1", 27), ("F#1", 30),
    ("A1", 33), ("C2", 36), ("D#2", 39), ("F#2", 42),
    ("A2", 45), ("C3", 48), ("D#3", 51), ("F#3", 54),
    ("A3", 57), ("C4", 60), ("D#4", 63), ("F#4", 66),
    ("A4", 69), ("C5", 72), ("D#5", 75), ("F#5", 78),
    ("A5", 81), ("C6", 84), ("D#6", 87), ("F#6", 90),
    ("A6", 93), ("C7", 96), ("D#7", 99), ("F#7", 102),
    ("A7", 105), ("C8", 108),
)

# 上游 SFZ 的 Data/notes.txt：保留原始的非均匀 16 层力度边界。
_VELOCITY_RANGES = (
    (1, 26), (27, 34), (35, 36), (37, 43),
    (44, 46), (47, 50), (51, 56), (57, 64),
    (65, 72), (73, 80), (81, 88), (89, 96),
    (97, 104), (105, 112), (113, 120), (121, 127),
)

# 交感共鸣采样只覆盖到 D#6，与上游 str_res.txt 一致。
_RESONANCE_ROOTS = _MAIN_ROOTS[:23]
_RESONANCE_MIN_MIDI = 21.0
_RESONANCE_MAX_MIDI = 88.0
_HAMMER_VOICE_BASE = 1_000_000_000
_RESONANCE_VOICE_BASE = 1_200_000_000
_RESONANCE_V3_VOICE_BASE = 1_300_000_000
_PEDAL_VOICE_ID = 1_400_000_000


def _finite_non_negative(value: object, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return number


def _finite_positive(value: object, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{label} must be a positive finite number")
    return number


def _event(
    source: PerformanceEvent,
    *,
    event_type: str,
    note_id: int,
    pitch_hz: float,
    velocity: float,
) -> PerformanceEvent:
    return PerformanceEvent(
        sample=source.sample,
        sequence=source.sequence,
        type=event_type,
        payload={
            "note_id": note_id,
            "pitch_hz": pitch_hz,
            "velocity": max(0.0, min(1.0, velocity)),
        },
    )


# 上游素材修正:C8 组采样实测基频 4433 Hz,即 MIDI 109.00(C#8),
# 比文件名标称的 C8 高整整一个半音。其余全部根音落在平滑的 Railsback
# 伸展曲线上（A0 -18、C4 -1、A7 +38 音分），唯独此处断崖式跳 +99,
# 因此判定为上游标注/素材错误而非伸展调律。这里如实声明该组采样的
# 真实音高，引擎据此重采样，使 B7/C8 键回到正确音高；此表之外不做
# 任何"修正"，以免抹掉真实的伸展调律。详见 README 的已知限制。
_ROOT_TUNING_CENTS = {"C8": 100.0}


def _main_regions(samples: Path) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    for layer, ((low, high)) in enumerate(_VELOCITY_RANGES, start=1):
        velocity_min = 0.0 if layer == 1 else (low - 0.5) / 127.0
        velocity_max = 1.0 if layer == 16 else (high + 0.5) / 127.0
        for name, midi_note in _MAIN_ROOTS:
            region: dict[str, Any] = {
                "sample": str(samples / f"{name}v{layer}.flac"),
                "root_midi": midi_note,
                # Data/region.txt 的显式键区。尤其 C8 素材按真实音高
                # C#8 声明后，仍必须只服务上游规定的 B7/C8 两键，不能
                # 让“最近根音”算法重新推导边界。
                "key_min": max(21, midi_note - 1),
                "key_max": min(108, midi_note + 1),
                "velocity_min": velocity_min,
                "velocity_max": velocity_max,
                "release_seconds": 4.0 if midi_note >= 95 else (3.0 if midi_note >= 89 else 0.48),
            }
            correction = _ROOT_TUNING_CENTS.get(name)
            if correction is not None:
                region["measured_tuning_cents"] = correction
            regions.append(region)
    return regions


def _hammer_regions(samples: Path) -> list[dict[str, Any]]:
    return [
        {
            "sample": str(samples / f"rel{index + 1}.flac"),
            "root_midi": midi_note,
            "key_min": midi_note,
            "key_max": midi_note,
        }
        for index, midi_note in enumerate(range(21, 109))
    ]


def _resonance_regions(samples: Path) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    for prefix, velocity_min, velocity_max, gain_db in (
        ("harmS", 0.0, 44.5 / 127.0, 0.0),
        ("harmL", 44.5 / 127.0, 1.0, -4.0),
    ):
        for name, midi_note in _RESONANCE_ROOTS:
            regions.append(
                {
                    "sample": str(samples / f"{prefix}{name}.flac"),
                    "root_midi": midi_note,
                    "key_min": 21 if midi_note == 21 else midi_note - 1,
                    "key_max": midi_note + 1,
                    "velocity_min": velocity_min,
                    "velocity_max": velocity_max,
                    "gain_db": gain_db,
                }
            )
    return regions


def _resonance_v3_regions(samples: Path) -> list[dict[str, Any]]:
    """上游第三层释放共鸣；它与 soft/large 层并行而非三选一。"""

    return [
        {
            "sample": str(samples / f"harmV3{name}.flac"),
            "root_midi": midi_note,
            "key_min": max(21, midi_note - 1),
            "key_max": min(88, midi_note + 1),
        }
        for name, midi_note in _RESONANCE_ROOTS
    ]


def _pedal_regions(samples: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    down = [
        {"sample": str(samples / f"pedalD{index}.flac"), "root_pitch_hz": 440.0}
        for index in (1, 2)
    ]
    up = [
        {"sample": str(samples / f"pedalU{index}.flac"), "root_pitch_hz": 440.0}
        for index in (1, 2)
    ]
    return down, up


@dataclass(slots=True)
class _HeldNote:
    pitch_hz: float
    midi_note: float
    velocity: float
    started_sample: int

    @property
    def pitch_key(self) -> int:
        # The public score permits fractional MIDI notes.  A millitone bucket
        # is far finer than any playable distinction here while giving the
        # upstream note_polyphony=1 groups a deterministic integer key.
        return round(self.midi_note * 1000.0)


@audited_event_free_blocks(silence_safe=False)
class PianoInstrument(Instrument):
    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        samples = asset_root / "Samples"
        if not samples.is_dir():
            raise ValueError(
                f"钢琴采样不存在：{samples}。请按 来源.md 下载 Salamander Grand Piano。"
            )

        main_manifest = {
            "regions": _main_regions(samples),
            "reference_a4_hz": 440.0,
            "release_seconds": float(manifest.get("release_seconds", 0.48)),
            "velocity_exponent": float(manifest.get("velocity_exponent", 0.82)),
            "gain": float(manifest.get("main_gain", 0.72)),
        }
        self.hammer_velocity_exponent = _finite_positive(
            manifest.get("release_noise_exponent", 1.64),
            "release_noise_exponent",
        )
        self.hammer_rt_decay_db_per_second = _finite_non_negative(
            manifest.get("hammer_rt_decay_db_per_second", 2.0),
            "hammer_rt_decay_db_per_second",
        )
        self.resonance_velocity_exponent = _finite_positive(
            manifest.get("resonance_velocity_exponent", 1.84),
            "resonance_velocity_exponent",
        )
        self.resonance_v3_velocity_exponent = _finite_positive(
            manifest.get("resonance_v3_velocity_exponent", 1.92),
            "resonance_v3_velocity_exponent",
        )
        self.resonance_v3_rt_decay_db_per_second = _finite_non_negative(
            manifest.get("resonance_v3_rt_decay_db_per_second", 2.0),
            "resonance_v3_rt_decay_db_per_second",
        )
        self.main_retrigger_release_seconds = _finite_non_negative(
            manifest.get("main_retrigger_release_seconds", 0.04),
            "main_retrigger_release_seconds",
        )
        resampling_quality = str(manifest.get("resampling_quality", "linear"))
        main_manifest["resampling_quality"] = resampling_quality
        hammer_manifest = {
            "regions": _hammer_regions(samples),
            # 上游 hammer.txt 明确是 volume=-37。旧适配器的 0.16
            # （约 -16 dB）把机械层抬高了约 21 dB，密集作品会把它听成
            # 一条独立的底声。
            "gain": _finite_non_negative(
                manifest.get("hammer_noise", 10.0 ** (-37.0 / 20.0)),
                "hammer_noise",
            ),
            "velocity_exponent": self.hammer_velocity_exponent,
            "release_seconds": 0.05,
            "resampling_quality": resampling_quality,
        }
        resonance_manifest = {
            "regions": _resonance_regions(samples),
            "gain": float(manifest.get("string_resonance", 0.18)),
            # 上游 soft/large 两组的 amp_veltrack 是 90/94。当前采样器
            # 只有一个全局曲线，取两者中点作幂函数近似。
            "velocity_exponent": self.resonance_velocity_exponent,
            "release_seconds": 0.2,
            "resampling_quality": resampling_quality,
        }
        resonance_v3_manifest = {
            "regions": _resonance_v3_regions(samples),
            "gain": float(manifest.get("string_resonance", 0.18)),
            # str_res.txt: harmV3 是独立 group=5、amp_veltrack=96，
            # 每次释键与 harmS/harmL 之一同时出现。
            "velocity_exponent": self.resonance_v3_velocity_exponent,
            "release_seconds": 0.2,
            "resampling_quality": resampling_quality,
        }
        pedal_down, pedal_up = _pedal_regions(samples)
        pedal_gain = float(manifest.get("pedal_noise", 0.12))
        self.main = SampleInstrument.from_manifest(main_manifest, sample_rate, base_directory=base_directory)
        self.hammer = SampleInstrument.from_manifest(hammer_manifest, sample_rate, base_directory=base_directory)
        self.resonance = SampleInstrument.from_manifest(
            resonance_manifest, sample_rate, base_directory=base_directory
        )
        self.resonance_v3 = SampleInstrument.from_manifest(
            resonance_v3_manifest, sample_rate, base_directory=base_directory
        )
        self.pedal_down = SampleInstrument.from_manifest(
            {
                "regions": pedal_down,
                "gain": pedal_gain,
                "resampling_quality": resampling_quality,
            },
            sample_rate,
            base_directory=base_directory,
        )
        self.pedal_up = SampleInstrument.from_manifest(
            {
                "regions": pedal_up,
                "gain": pedal_gain,
                "resampling_quality": resampling_quality,
            },
            sample_rate,
            base_directory=base_directory,
        )
        self.held_notes: dict[int, _HeldNote] = {}
        self.deferred_releases: dict[int, _HeldNote] = {}
        self._active_main_by_pitch: dict[int, int] = {}
        self.sustain = 0.0
        self.una_corda = 0.0
        self.sustain_threshold = float(manifest.get("sustain_threshold", 0.5))

    def _trigger_one_shot(
        self,
        instrument: SampleInstrument,
        source: PerformanceEvent,
        tuning: EqualTemperament,
        *,
        note_id: int,
        pitch_hz: float,
        velocity: float,
    ) -> None:
        if velocity <= 0.0:
            return
        instrument.handle_event(
            _event(
                source,
                event_type="note_on",
                note_id=note_id,
                pitch_hz=pitch_hz,
                velocity=velocity,
            ),
            tuning,
        )

    @staticmethod
    def _midi_note(pitch_hz: float, tuning: EqualTemperament) -> float:
        return 69.0 + 12.0 * math.log2(pitch_hz / tuning.a4_hz)

    @staticmethod
    def _decayed_velocity(
        velocity: float,
        *,
        exponent: float,
        decay_db: float,
    ) -> float:
        # SampleInstrument later raises velocity to ``exponent``.  Applying
        # the inverse exponent here makes the requested rt_decay operate in
        # amplitude dB, exactly once.
        amplitude_gain = 10.0 ** (-max(0.0, decay_db) / 20.0)
        return velocity * (amplitude_gain ** (1.0 / exponent))

    @staticmethod
    def _resonance_rt_decay_db_per_second(held: _HeldNote) -> float:
        # str_res.txt: the soft layer is 7 dB/s throughout.  The loud layer
        # rises from 6 to 9 dB/s across its four keyboard zones.
        if held.velocity <= 44.5 / 127.0:
            return 7.0
        if held.midi_note <= 28.0:
            return 6.0
        if held.midi_note <= 37.0:
            return 7.0
        if held.midi_note <= 49.0:
            return 8.0
        return 9.0

    def _trigger_release_layers(
        self,
        source: PerformanceEvent,
        tuning: EqualTemperament,
        held: _HeldNote,
    ) -> None:
        held_seconds = max(
            0.0,
            (source.sample - held.started_sample) / self.sample_rate,
        )
        hammer_velocity = self._decayed_velocity(
            held.velocity,
            exponent=self.hammer_velocity_exponent,
            decay_db=self.hammer_rt_decay_db_per_second * held_seconds,
        )
        self._trigger_one_shot(
            self.hammer,
            source,
            tuning,
            note_id=_HAMMER_VOICE_BASE + held.pitch_key,
            pitch_hz=held.pitch_hz,
            velocity=hammer_velocity,
        )
        if not _RESONANCE_MIN_MIDI <= held.midi_note <= _RESONANCE_MAX_MIDI:
            return
        resonance_velocity = self._decayed_velocity(
            held.velocity,
            exponent=self.resonance_velocity_exponent,
            decay_db=(
                self._resonance_rt_decay_db_per_second(held) * held_seconds
            ),
        )
        self._trigger_one_shot(
            self.resonance,
            source,
            tuning,
            note_id=_RESONANCE_VOICE_BASE + held.pitch_key,
            pitch_hz=held.pitch_hz,
            velocity=resonance_velocity,
        )
        resonance_v3_velocity = self._decayed_velocity(
            held.velocity,
            exponent=self.resonance_v3_velocity_exponent,
            decay_db=(
                self.resonance_v3_rt_decay_db_per_second * held_seconds
            ),
        )
        self._trigger_one_shot(
            self.resonance_v3,
            source,
            tuning,
            note_id=_RESONANCE_V3_VOICE_BASE + held.pitch_key,
            pitch_hz=held.pitch_hz,
            velocity=resonance_v3_velocity,
        )

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "note_on":
            pitch_hz = event_pitch_hz(event, tuning)
            midi_note = float(
                event.payload.get(
                    "midi_note",
                    self._midi_note(pitch_hz, tuning),
                )
            )
            velocity = float(event.payload["velocity"])
            note_id = int(event.payload["note_id"])
            # 首版柔音踏板近似为力度与亮度的下降；未来将换成独立 una-corda 采样或模型。
            softened_velocity = velocity * (1.0 - 0.16 * self.una_corda)
            forwarded = PerformanceEvent(
                sample=event.sample,
                sequence=event.sequence,
                type=event.type,
                payload={**event.payload, "velocity": softened_velocity},
            )
            held = _HeldNote(
                pitch_hz=pitch_hz,
                midi_note=midi_note,
                velocity=velocity,
                started_sample=event.sample,
            )
            previous_note_id = self._active_main_by_pitch.get(held.pitch_key)
            if previous_note_id is not None and previous_note_id != note_id:
                # Salamander declares note_polyphony=1 for the main keyboard.
                # Give the superseded sample a short click-safe damper instead
                # of letting every rapid same-key strike keep a 0.48 s tail.
                self.main.release_note(
                    previous_note_id,
                    release_seconds=self.main_retrigger_release_seconds,
                )
            # 若旧一次击键已在延音踏板下等待释放，同键新击键会继续抬起
            # 同一组制音器。旧周期不应在随后抬踏板时、趁新键仍按住而
            # 额外触发击槌/琴弦释放声。
            for deferred_note_id, deferred in tuple(
                self.deferred_releases.items()
            ):
                if deferred.pitch_key == held.pitch_key:
                    self.deferred_releases.pop(deferred_note_id)
            self._active_main_by_pitch[held.pitch_key] = note_id
            self.held_notes[note_id] = held
            self.main.handle_event(forwarded, tuning)
            return

        if event.type == "note_off":
            note_id = int(event.payload["note_id"])
            held = self.held_notes.pop(note_id, None)
            self.main.handle_event(event, tuning)
            if held is not None:
                if (
                    self.sustain < self.sustain_threshold
                    and self._active_main_by_pitch.get(held.pitch_key) == note_id
                ):
                    self._active_main_by_pitch.pop(held.pitch_key, None)
                # SFZ trigger=release follows the originating note_on
                # velocity.  An optional MIDI note-off release_velocity
                # describes key-release speed and must not flatten every
                # piano release to the same mechanical knock.
                another_key_down = any(
                    other.pitch_key == held.pitch_key
                    for other in self.held_notes.values()
                )
                if not another_key_down:
                    if self.sustain >= self.sustain_threshold:
                        self.deferred_releases[note_id] = held
                    else:
                        self._trigger_release_layers(event, tuning, held)
            return

        if event.type == "control":
            name = str(event.payload["name"])
            value = float(event.payload["value"])
            if name == "una_corda":
                self.una_corda = value
                return
            if name == "sustain_pedal":
                was_down = self.sustain >= self.sustain_threshold
                is_down = value >= self.sustain_threshold
                self.sustain = value
                self.main.handle_event(event, tuning)
                if was_down != is_down:
                    if was_down and not is_down:
                        for note_id in sorted(self.deferred_releases):
                            held = self.deferred_releases[note_id]
                            self._trigger_release_layers(
                                event,
                                tuning,
                                held,
                            )
                            if (
                                self._active_main_by_pitch.get(held.pitch_key)
                                == note_id
                            ):
                                self._active_main_by_pitch.pop(
                                    held.pitch_key,
                                    None,
                                )
                        self.deferred_releases.clear()
                    self._trigger_one_shot(
                        self.pedal_down if is_down else self.pedal_up,
                        event,
                        tuning,
                        note_id=_PEDAL_VOICE_ID,
                        pitch_hz=440.0,
                        velocity=max(0.25, abs(value - (1.0 if was_down else 0.0))),
                    )

    def render_frame(self) -> StereoFrame:
        engines = (
            self.main,
            self.hammer,
            self.resonance,
            self.resonance_v3,
            self.pedal_down,
            self.pedal_up,
        )
        left = 0.0
        right = 0.0
        for engine in engines:
            engine_left, engine_right = engine.render_frame()
            left += engine_left
            right += engine_right
        return left, right

    @property
    def active_voice_count(self) -> int:
        return sum(
            engine.active_voice_count
            for engine in (
                self.main,
                self.hammer,
                self.resonance,
                self.resonance_v3,
                self.pedal_down,
                self.pedal_up,
            )
        )


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return PianoInstrument(sample_rate, manifest, base_directory)
