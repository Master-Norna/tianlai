from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from .events import PerformanceEvent
from .instrument import Instrument, StereoFrame
from .tuning import EqualTemperament


ENGINE_VERSION = "1.1.0"
_UINT32_MASK = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class SfxProfile:
    attack_seconds: float
    release_seconds: float
    gain: float
    one_shot_seconds: float | None = None


SFX_PROFILES: dict[str, SfxProfile] = {
    "breath": SfxProfile(0.08, 0.22, 0.42),
    "applause": SfxProfile(0.45, 1.2, 0.34),
    "gunshot": SfxProfile(0.0001, 0.65, 0.58, 2.4),
    "ocean": SfxProfile(1.4, 2.6, 0.40),
    "telephone_bell": SfxProfile(0.012, 0.35, 0.32),
    "helicopter": SfxProfile(0.7, 1.4, 0.38),
    "rain_atmosphere": SfxProfile(1.2, 2.2, 0.36),
    "bird_chorus": SfxProfile(0.35, 1.1, 0.32),
}


@dataclass(slots=True)
class _FilterState:
    low: float = 0.0
    band: float = 0.0


@dataclass(slots=True)
class _Ladder:
    """Four cascaded one-pole sections, i.e. 24 dB per octave."""

    a: float = 0.0
    b: float = 0.0
    c: float = 0.0
    d: float = 0.0


@dataclass(slots=True)
class _Burst:
    kind: str
    age: int
    duration: int
    amplitude: float
    frequency: float
    frequency_end: float
    phase: float
    pan: float
    filter: _FilterState = field(default_factory=_FilterState)


@dataclass(slots=True)
class _Voice:
    note_id: int
    velocity: float
    rng_state: int
    envelope: float = 0.0
    stage: str = "attack"
    release_step: float = 0.0
    pending_release: bool = False
    age: int = 0
    phase_a: float = 0.0
    phase_b: float = 0.0
    phase_c: float = 0.0
    low_left: float = 0.0
    low_right: float = 0.0
    slow_left: float = 0.0
    slow_right: float = 0.0
    band_left: _FilterState = field(default_factory=_FilterState)
    band_right: _FilterState = field(default_factory=_FilterState)
    ladder_left: _Ladder = field(default_factory=_Ladder)
    ladder_right: _Ladder = field(default_factory=_Ladder)
    ladder_extra: _Ladder = field(default_factory=_Ladder)
    bursts: list[_Burst] = field(default_factory=list)


def _finite_float(value: object, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _next_random(voice: _Voice) -> float:
    state = voice.rng_state or 0xA341316C
    state ^= (state << 13) & _UINT32_MASK
    state ^= state >> 17
    state ^= (state << 5) & _UINT32_MASK
    voice.rng_state = state & _UINT32_MASK
    return voice.rng_state / 4294967296.0


def _noise(voice: _Voice) -> float:
    return _next_random(voice) * 2.0 - 1.0


def _one_pole(previous: float, value: float, cutoff_hz: float, sample_rate: int) -> float:
    coefficient = 1.0 - math.exp(-math.tau * min(cutoff_hz, sample_rate * 0.42) / sample_rate)
    return previous + coefficient * (value - previous)


def _ladder_lowpass(
    state: _Ladder, value: float, cutoff_hz: float, sample_rate: int, poles: int = 4
) -> float:
    """Cascade one-pole sections to get a slope that actually shapes noise.

    A single pole rolls off at 6 dB per octave, which barely darkens white
    noise: a one-pole at 4 kHz still leaves a spectral centroid up around
    8 kHz, so every noise-based scene ends up sounding like the same hiss.
    Four sections give 24 dB per octave, which is what separates breath from
    surf from rain.
    """

    coefficient = 1.0 - math.exp(
        -math.tau * min(cutoff_hz, sample_rate * 0.45) / sample_rate
    )
    state.a += coefficient * (value - state.a)
    state.b += coefficient * (state.a - state.b)
    if poles <= 2:
        return state.b
    state.c += coefficient * (state.b - state.c)
    state.d += coefficient * (state.c - state.d)
    return state.d


def _state_variable_bandpass(
    state: _FilterState,
    value: float,
    cutoff_hz: float,
    damping: float,
    sample_rate: int,
) -> float:
    cutoff = min(sample_rate * 0.19, max(20.0, cutoff_hz))
    coefficient = 2.0 * math.sin(math.pi * cutoff / sample_rate)
    state.low += coefficient * state.band
    high = value - state.low - damping * state.band
    state.band += coefficient * high
    return state.band


def _equal_power_pan(value: float, pan: float) -> StereoFrame:
    angle = (min(1.0, max(-1.0, pan)) + 1.0) * math.pi / 4.0
    return value * math.cos(angle) * math.sqrt(2.0), value * math.sin(angle) * math.sqrt(2.0)


class ProceduralSfxInstrument(Instrument):
    """Eight deterministic, versioned environmental/acoustic scene models.

    These are not generic oscillators renamed as effects.  Every profile has a
    separate source model (turbulent breath, crowd micro-impulses, blast and
    reflection taps, surf envelopes, a dual-gong telephone ringer, rotor blade
    pulses, rain droplets, or scheduled FM bird calls).  A fixed seed keeps
    offline renders byte-repeatable while note ids still produce independent
    voices.
    """

    def __init__(
        self,
        sample_rate: int,
        *,
        profile_name: str,
        profile: SfxProfile,
        seed: int,
        expression_smoothing_seconds: float,
    ) -> None:
        super().__init__(sample_rate)
        self.profile_name = profile_name
        self.profile = profile
        self.seed = seed & _UINT32_MASK
        self.attack_samples = max(1, round(profile.attack_seconds * sample_rate))
        self.release_samples = max(1, round(profile.release_seconds * sample_rate))
        self.expression_smoothing_samples = max(
            1, round(expression_smoothing_seconds * sample_rate)
        )
        self.expression = 1.0
        self.expression_target = 1.0
        self.modulation = 0.5
        self.modulation_target = 0.5
        self.distance = 0.2
        self.distance_target = 0.2
        self.sustain_pedal = 0.0
        self.voices: dict[int, _Voice] = {}

    @classmethod
    def from_manifest(
        cls, manifest: dict[str, Any], sample_rate: int
    ) -> "ProceduralSfxInstrument":
        version = str(manifest.get("engine_version", ""))
        if version != ENGINE_VERSION:
            raise ValueError(
                f"procedural SFX engine_version must be {ENGINE_VERSION!r}, got {version!r}"
            )
        name = str(manifest.get("profile", ""))
        try:
            base = SFX_PROFILES[name]
        except KeyError as error:
            choices = ", ".join(sorted(SFX_PROFILES))
            raise ValueError(f"unknown procedural SFX profile {name!r}; choose from {choices}") from error
        parameters = manifest.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError("procedural SFX parameters must be an object")
        allowed = {"attack_seconds", "release_seconds", "gain", "one_shot_seconds"}
        unknown = set(parameters) - allowed
        if unknown:
            raise ValueError("unknown procedural SFX parameter(s): " + ", ".join(sorted(unknown)))
        values = {
            "attack_seconds": parameters.get("attack_seconds", base.attack_seconds),
            "release_seconds": parameters.get("release_seconds", base.release_seconds),
            "gain": parameters.get("gain", base.gain),
            "one_shot_seconds": parameters.get("one_shot_seconds", base.one_shot_seconds),
        }
        one_shot = values["one_shot_seconds"]
        profile = SfxProfile(
            attack_seconds=_finite_float(values["attack_seconds"], "attack_seconds"),
            release_seconds=_finite_float(values["release_seconds"], "release_seconds"),
            gain=_finite_float(values["gain"], "gain"),
            one_shot_seconds=(
                None if one_shot is None else _finite_float(one_shot, "one_shot_seconds")
            ),
        )
        if profile.attack_seconds <= 0.0 or profile.release_seconds <= 0.0:
            raise ValueError("procedural SFX attack and release must be positive")
        if profile.gain <= 0.0:
            raise ValueError("procedural SFX gain must be positive")
        if profile.one_shot_seconds is not None and profile.one_shot_seconds <= 0.0:
            raise ValueError("procedural SFX one_shot_seconds must be positive")
        seed = int(manifest.get("seed", 0))
        if not 0 <= seed <= _UINT32_MASK:
            raise ValueError("procedural SFX seed must be between 0 and 4294967295")
        smoothing = _finite_float(
            manifest.get("expression_smoothing_seconds", 0.025),
            "expression_smoothing_seconds",
        )
        if smoothing <= 0.0:
            raise ValueError("expression_smoothing_seconds must be positive")
        return cls(
            sample_rate,
            profile_name=name,
            profile=profile,
            seed=seed,
            expression_smoothing_seconds=smoothing,
        )

    def _new_voice(self, note_id: int, velocity: float) -> _Voice:
        seed = (
            self.seed
            ^ ((note_id * 0x9E3779B1) & _UINT32_MASK)
            ^ (round(velocity * 65535.0) & _UINT32_MASK)
        ) or 0xA341316C
        voice = _Voice(note_id=note_id, velocity=velocity, rng_state=seed)
        voice.phase_a = _next_random(voice)
        voice.phase_b = _next_random(voice)
        voice.phase_c = _next_random(voice)
        return voice

    def _begin_release(self, voice: _Voice) -> None:
        if voice.stage != "release":
            voice.stage = "release"
            voice.pending_release = False
            voice.release_step = max(voice.envelope, 1.0e-9) / self.release_samples

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        del tuning
        if event.type == "note_on":
            note_id = int(event.payload["note_id"])
            if note_id in self.voices:
                raise ValueError(f"procedural SFX note_id {note_id} is already active")
            velocity = _finite_float(event.payload.get("velocity", 0.8), "velocity")
            if not 0.0 <= velocity <= 1.0:
                raise ValueError("velocity must be between 0 and 1")
            self.voices[note_id] = self._new_voice(note_id, velocity)
            return
        if event.type == "note_off":
            voice = self.voices.get(int(event.payload["note_id"]))
            if voice is not None and self.profile.one_shot_seconds is None:
                if self.sustain_pedal >= 0.5:
                    voice.pending_release = True
                else:
                    self._begin_release(voice)
            return
        if event.type != "control":
            return
        name = str(event.payload["name"])
        value = _finite_float(event.payload["value"], f"control {name}")
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"control {name} must be between 0 and 1")
        if name == "expression":
            self.expression_target = value
        elif name == "modulation":
            self.modulation_target = value
        elif name == "distance":
            self.distance_target = value
        elif name == "sustain_pedal":
            previous = self.sustain_pedal
            self.sustain_pedal = value
            if previous >= 0.5 and value < 0.5:
                for voice in self.voices.values():
                    if voice.pending_release:
                        self._begin_release(voice)

    def _advance_envelope(self, voice: _Voice) -> float:
        if voice.stage == "attack":
            voice.envelope = min(1.0, voice.envelope + 1.0 / self.attack_samples)
            if voice.envelope >= 1.0:
                voice.stage = "sustain"
        elif voice.stage == "release":
            voice.envelope = max(0.0, voice.envelope - voice.release_step)
        if self.profile.one_shot_seconds is not None:
            release_at = max(1, round(self.profile.one_shot_seconds * self.sample_rate))
            if voice.age >= release_at and voice.stage != "release":
                self._begin_release(voice)
        return voice.envelope

    def _spawn_burst(
        self,
        voice: _Voice,
        *,
        kind: str,
        duration_seconds: tuple[float, float],
        amplitude: tuple[float, float],
        frequency: tuple[float, float],
        frequency_end_ratio: tuple[float, float] = (1.0, 1.0),
    ) -> None:
        duration = round(
            (duration_seconds[0] + (duration_seconds[1] - duration_seconds[0]) * _next_random(voice))
            * self.sample_rate
        )
        start_frequency = frequency[0] + (frequency[1] - frequency[0]) * _next_random(voice)
        ratio = frequency_end_ratio[0] + (
            frequency_end_ratio[1] - frequency_end_ratio[0]
        ) * _next_random(voice)
        voice.bursts.append(
            _Burst(
                kind=kind,
                age=0,
                duration=max(2, duration),
                amplitude=amplitude[0] + (amplitude[1] - amplitude[0]) * _next_random(voice),
                frequency=start_frequency,
                frequency_end=start_frequency * ratio,
                phase=_next_random(voice),
                pan=_next_random(voice) * 2.0 - 1.0,
            )
        )

    def _render_bursts(self, voice: _Voice) -> StereoFrame:
        left = 0.0
        right = 0.0
        remaining: list[_Burst] = []
        nyquist_frequency = self.sample_rate * 0.42
        for burst in voice.bursts:
            progress = burst.age / burst.duration
            if progress >= 1.0:
                continue
            frequency = min(
                nyquist_frequency,
                burst.frequency + (burst.frequency_end - burst.frequency) * progress,
            )
            if burst.kind == "clap":
                envelope = math.exp(-progress * 15.0)
                # 拍手是宽带噪声冲击,没有音高。先前用 72% 正弦合成,结果是一串
                # 带调的小音符而不是掌声;这里改成用宽带通给噪声着色,保留冲击
                # 本身的宽带质地。
                # 只用带通着色后的噪声。掺原始白噪声看似"更宽带",实际 48 kHz
                # 白噪声大半能量在 10 kHz 以上,只会让掌声变成嘶声。
                value = 2.1 * _state_variable_bandpass(
                    burst.filter, _noise(voice), frequency, 0.85, self.sample_rate
                )
            elif burst.kind == "drop":
                envelope = math.sin(math.pi * min(1.0, progress * 3.0)) * math.exp(-progress * 6.5)
                value = math.sin(math.tau * burst.phase)
            else:  # bird call
                envelope = math.sin(math.pi * progress) ** 1.5
                vibrato = 1.0 + 0.018 * math.sin(math.tau * progress * 8.0)
                frequency *= vibrato
                value = math.sin(math.tau * burst.phase + 1.1 * math.sin(math.tau * burst.phase * 1.73))
            burst.phase = (burst.phase + frequency / self.sample_rate) % 1.0
            burst.age += 1
            burst_left, burst_right = _equal_power_pan(value * envelope * burst.amplitude, burst.pan)
            left += burst_left
            right += burst_right
            remaining.append(burst)
        voice.bursts = remaining
        return left, right

    def _breath(self, voice: _Voice) -> StereoFrame:
        left_noise = _noise(voice)
        right_noise = _noise(voice)
        # 呼吸声的能量集中在 2 kHz 以下。先前用单极点 4.1 kHz 低通,只有
        # 6 dB/倍频程,几乎没把白噪声压暗;四级 24 dB/倍频程才真正塑形。
        voice.low_left = _ladder_lowpass(
            voice.ladder_left, left_noise, 1900.0, self.sample_rate
        )
        voice.low_right = _ladder_lowpass(
            voice.ladder_right, right_noise, 2100.0, self.sample_rate
        )
        # 共振峰直接取自原始噪声:低通之后再带通会把这一层也一并压掉。
        band_left = _state_variable_bandpass(
            voice.band_left, left_noise, 780.0 + 220.0 * self.modulation, 0.55, self.sample_rate
        )
        band_right = _state_variable_bandpass(
            voice.band_right, right_noise, 900.0 + 180.0 * self.modulation, 0.58, self.sample_rate
        )
        pulse = 0.82 + 0.18 * math.sin(math.tau * voice.age / (self.sample_rate * 3.2))
        # 梯形滤波逐级衰减,这里把电平补回来。
        return (3.4 * voice.low_left + 0.55 * band_left) * pulse, (
            3.4 * voice.low_right + 0.55 * band_right
        ) * pulse

    def _applause(self, voice: _Voice) -> StereoFrame:
        # 一场掌声是几十上百双手,每秒几十次拍击才成"片";先前 18/秒 听起来
        # 只有寥寥几个人。拍击本身也远比原来短:真实一次拍手约 10~45 毫秒。
        density = 45.0 + 220.0 * self.modulation
        if _next_random(voice) < density / self.sample_rate:
            self._spawn_burst(
                voice,
                kind="clap",
                duration_seconds=(0.012, 0.045),
                amplitude=(0.12, 0.38),
                frequency=(420.0, 2400.0),
                frequency_end_ratio=(0.70, 0.98),
            )
        # 人群底噪偏暗:大厅里的嗡声不是白噪声。
        voice.low_left = _ladder_lowpass(
            voice.ladder_left, _noise(voice), 1400.0, self.sample_rate
        )
        crowd = 0.11 * voice.low_left
        burst_left, burst_right = self._render_bursts(voice)
        return burst_left + crowd, burst_right - crowd * 0.7

    def _gunshot(self, voice: _Voice) -> StereoFrame:
        time = voice.age / self.sample_rate
        blast = 0.0
        if time < 0.11:
            pressure = math.exp(-time * 38.0)
            # 枪口爆音由低频主导。先前直接用 72% 原始白噪声,谱心落在 11.5 kHz,
            # 听起来是"嘶"而不是"砰";这里让噪声先过一道陡低通。
            shaped = _ladder_lowpass(
                voice.ladder_left, _noise(voice), 900.0, self.sample_rate
            )
            blast = pressure * (
                3.6 * shaped + 0.28 * math.sin(math.tau * 86.0 * time)
            )
            if voice.age < 4:
                blast += (1.0 - voice.age / 4.0) * 2.2
        reflection = 0.0
        # 回声来自远处墙面,传播路径本身就是低通;原先用原始白噪声,把枪声
        # 的尾巴变成了嘶声。
        echo_noise = _ladder_lowpass(
            voice.ladder_right, _noise(voice), 1400.0, self.sample_rate, poles=2
        )
        for delay, gain, frequency in ((0.083, 0.34, 127.0), (0.171, 0.21, 93.0), (0.319, 0.13, 61.0)):
            local = time - delay
            if 0.0 <= local < 0.16:
                reflection += gain * math.exp(-local * 20.0) * (
                    1.9 * echo_noise + 0.38 * math.sin(math.tau * frequency * local)
                )
        mechanical = 0.0
        if 0.045 <= time < 0.065:
            # 枪机动作是金属碰撞,保留高频,但压低比重免得盖过爆音。
            mechanical = 0.10 * math.exp(-(time - 0.045) * 90.0) * _noise(voice)
        value = blast + reflection + mechanical
        return value, value * 0.91 + reflection * 0.13

    def _ocean(self, voice: _Voice) -> StereoFrame:
        left_noise = _noise(voice)
        right_noise = _noise(voice)
        # 海浪的主体是低频轰鸣,浪花只是浮在上面的一层。先前浪花用单极点
        # 4800 Hz,斜率太浅,盖过了轰鸣,整体听起来只剩白噪声。
        voice.slow_left = _ladder_lowpass(
            voice.ladder_left, left_noise, 220.0, self.sample_rate
        )
        voice.slow_right = _ladder_lowpass(
            voice.ladder_right, right_noise, 260.0, self.sample_rate
        )
        voice.low_left = _one_pole(voice.low_left, left_noise, 2400.0, self.sample_rate)
        voice.low_right = _one_pole(voice.low_right, right_noise, 2700.0, self.sample_rate)
        time = voice.age / self.sample_rate
        swell_left = max(0.0, math.sin(math.tau * (time / 6.7 + voice.phase_a))) ** 1.7
        swell_right = max(0.0, math.sin(math.tau * (time / 7.9 + voice.phase_b))) ** 1.7
        foam_left = voice.low_left * swell_left
        foam_right = voice.low_right * swell_right
        # 轰鸣与浪花的配比:全给轰鸣会只剩闷响,听不出"浪";实测能量应当在
        # 低频主导的同时,于 500 Hz~2 kHz 仍留下可辨的浪花层。
        return (
            3.4 * voice.slow_left + 1.15 * foam_left,
            3.4 * voice.slow_right + 1.15 * foam_right,
        )

    def _telephone_bell(self, voice: _Voice) -> StereoFrame:
        time = voice.age / self.sample_rate
        cadence = time % 6.0
        active = 1.0 if cadence < 2.0 else 0.0
        hammer = max(0.0, math.sin(math.tau * 20.0 * time)) ** 10
        ring_envelope = active * (0.18 + 0.82 * hammer)
        frequency_a = 820.0 + 18.0 * self.modulation
        frequency_b = 1040.0 - 22.0 * self.modulation
        voice.phase_a = (voice.phase_a + frequency_a / self.sample_rate) % 1.0
        voice.phase_b = (voice.phase_b + frequency_b / self.sample_rate) % 1.0
        voice.phase_c = (voice.phase_c + 31.0 / self.sample_rate) % 1.0
        decay_motion = 0.88 + 0.12 * math.sin(math.tau * voice.phase_c)
        value = ring_envelope * decay_motion * (
            0.62 * math.sin(math.tau * voice.phase_a)
            + 0.38 * math.sin(math.tau * voice.phase_b + 0.25)
        )
        return value, value * 0.94

    def _helicopter(self, voice: _Voice) -> StereoFrame:
        time = voice.age / self.sample_rate
        rotor_hz = 5.2 + 1.8 * self.modulation
        blade_phase = (time * rotor_hz) % 1.0
        # 桨叶拍击比原先的 sin^5 更有棱角,但不能一味加尖:闸门本身会产生宽带
        # 瞬态,推得太狠反而把整机声音变亮,失去直升机低频主导的特征。
        slap = max(0.0, math.sin(math.tau * blade_phase * 4.0)) ** 9
        sub = math.sin(math.tau * blade_phase)
        engine_hz = 92.0 + 34.0 * self.modulation
        voice.phase_a = (voice.phase_a + engine_hz / self.sample_rate) % 1.0
        voice.phase_b = (voice.phase_b + engine_hz * 2.03 / self.sample_rate) % 1.0
        turbulence = _noise(voice)
        # 直升机整体是低频主导的声音,滤波必须够陡;这里用四级 24 dB/倍频程。
        voice.low_left = _ladder_lowpass(
            voice.ladder_left, turbulence, 620.0, self.sample_rate
        )
        # 涡轮是宽带啸叫,不是两个纯正弦;保留少量谐波音高,主体交给带通噪声。
        whine = _state_variable_bandpass(
            voice.band_left, turbulence, engine_hz * 8.0, 0.30, self.sample_rate
        )
        # 拍击只用低通后的噪声。掺原始白噪声会把整机声音变成嘶嘶的高频。
        slap_noise = slap * 2.6 * voice.low_left
        # 保留一部分确定性的音调脉冲(原实现全靠它,谱形本来就是对的),
        # 再叠上暗的噪声成分,补出"拍打"的质地。
        # sub 走的是旋翼转速本身(5~7 Hz),完全在听阈以下:给它太大权重只会
        # 白占动态余量,把可听的部分挤小。这里压到辅助地位。
        rotor = 0.52 * slap + 0.46 * slap_noise + 0.10 * sub
        engine = (
            0.15 * math.sin(math.tau * voice.phase_a)
            + 0.07 * math.sin(math.tau * voice.phase_b)
            + 0.30 * whine
        )
        left = rotor + engine + 1.10 * voice.low_left
        right = 0.91 * rotor + 0.93 * engine - 0.88 * voice.low_left
        return left, right

    def _rain(self, voice: _Voice) -> StereoFrame:
        left_noise = _noise(voice)
        right_noise = _noise(voice)
        # 雨的嘶声本身有明确的高频滚降;6800 Hz 的单极点等于没滤。
        voice.low_left = _ladder_lowpass(
            voice.ladder_left, left_noise, 3200.0, self.sample_rate, poles=2
        )
        voice.low_right = _ladder_lowpass(
            voice.ladder_right, right_noise, 3500.0, self.sample_rate, poles=2
        )
        voice.slow_left = _one_pole(voice.slow_left, left_noise, 420.0, self.sample_rate)
        voice.slow_right = _one_pole(voice.slow_right, right_noise, 390.0, self.sample_rate)
        density = 12.0 + 95.0 * self.modulation
        if _next_random(voice) < density / self.sample_rate:
            self._spawn_burst(
                voice,
                kind="drop",
                duration_seconds=(0.018, 0.085),
                amplitude=(0.025, 0.16),
                frequency=(900.0, min(5400.0, self.sample_rate * 0.38)),
                frequency_end_ratio=(0.68, 1.12),
            )
        drops_left, drops_right = self._render_bursts(voice)
        hiss_left = 0.30 * voice.low_left + 0.19 * voice.slow_left
        hiss_right = 0.30 * voice.low_right + 0.19 * voice.slow_right
        return hiss_left + drops_left, hiss_right + drops_right

    def _birds(self, voice: _Voice) -> StereoFrame:
        rate = 0.7 + 2.6 * self.modulation
        if _next_random(voice) < rate / self.sample_rate:
            self._spawn_burst(
                voice,
                kind="bird",
                duration_seconds=(0.10, 0.42),
                amplitude=(0.10, 0.34),
                frequency=(1200.0, min(4400.0, self.sample_rate * 0.32)),
                frequency_end_ratio=(0.68, 1.55),
            )
        calls_left, calls_right = self._render_bursts(voice)
        forest = 0.015 * _noise(voice)
        return calls_left + forest, calls_right - forest * 0.6

    def _render_source(self, voice: _Voice) -> StereoFrame:
        if self.profile_name == "breath":
            return self._breath(voice)
        if self.profile_name == "applause":
            return self._applause(voice)
        if self.profile_name == "gunshot":
            return self._gunshot(voice)
        if self.profile_name == "ocean":
            return self._ocean(voice)
        if self.profile_name == "telephone_bell":
            return self._telephone_bell(voice)
        if self.profile_name == "helicopter":
            return self._helicopter(voice)
        if self.profile_name == "rain_atmosphere":
            return self._rain(voice)
        if self.profile_name == "bird_chorus":
            return self._birds(voice)
        raise RuntimeError(f"unsupported procedural SFX profile: {self.profile_name}")

    def render_frame(self) -> StereoFrame:
        smoothing = self.expression_smoothing_samples
        self.expression += (self.expression_target - self.expression) / smoothing
        self.modulation += (self.modulation_target - self.modulation) / smoothing
        self.distance += (self.distance_target - self.distance) / smoothing
        distance_gain = 1.0 / (1.0 + 2.6 * self.distance)

        left = 0.0
        right = 0.0
        finished: list[int] = []
        for note_id, voice in self.voices.items():
            envelope = self._advance_envelope(voice)
            if voice.stage == "release" and envelope <= 0.0:
                finished.append(note_id)
                continue
            source_left, source_right = self._render_source(voice)
            amplitude = (
                self.profile.gain
                * voice.velocity**0.72
                * envelope
                * self.expression
                * distance_gain
            )
            left += source_left * amplitude
            right += source_right * amplitude
            voice.age += 1

        for note_id in finished:
            del self.voices[note_id]
        return 0.97 * math.tanh(left), 0.97 * math.tanh(right)

    @property
    def active_voice_count(self) -> int:
        return len(self.voices)


def create_procedural_sfx(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    del base_directory
    return ProceduralSfxInstrument.from_manifest(manifest, sample_rate)


__all__ = [
    "ENGINE_VERSION",
    "SFX_PROFILES",
    "ProceduralSfxInstrument",
    "SfxProfile",
    "create_procedural_sfx",
]
