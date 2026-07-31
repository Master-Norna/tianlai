"""确定性物理/信号建模乐器。

这些入口是"找不到合法采样时的诚实退路"或"本就该合成的音色":每个
profile 是针对该乐器发声机理的专用模型,不是通用振荡器换名。全部
参数、种子和引擎版本纳入版本控制;同一事件序列必得同一输出。

- ``plucked_string``:扩展 Karplus-Strong(分数延迟精确音高、阻尼、
  非线性琴桥嗡振、共鸣体、同情弦),用于三味线 / 日本筝 / 西塔琴;
- ``blown_pipe``:气鸣管模型(谐波簇 + 气息噪声 + 起音吹口噪),用于
  尺八 / 排箫;
- ``double_reed``:双簧亮音模型(富谐波 + 鼻音共振峰),用于唢呐近似;
- ``membrane_drum``:圆膜模态模型(贝塞尔模态比 + 鼓皮拍击噪声),太鼓;
- ``steelpan``:钢鼓模态模型(失谐分音对拍频 + ping 起音);
- ``music_box``:音乐盒钢梳齿模型(非谐分音 + 机械触发咔嗒);
- ``synth_drum``:模拟鼓机合成鼓(指数降频正弦 + 噪声瞬态)。

音准由构造保证(振荡器直接跑目标频率;弦模型用分数延迟),并由
``generate_modeled_pitch_calibration`` 渲染测试音、FFT 实测后写入校准
文件作为机器证据。
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .events import PerformanceEvent, event_pitch_hz
from .instrument import Instrument, StereoFrame
from .provenance import project_authored_dsp_provenance
from .tuning import EqualTemperament


ENGINE_VERSION = "1.1.0"


def _finite(value: object, name: str) -> float:
    number = float(value)  # type: ignore[arg-type]
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _smoothstep(value: float) -> float:
    """Return a C1-continuous 0..1 transition."""

    value = min(1.0, max(0.0, value))
    return value * value * (3.0 - 2.0 * value)


def _bandlimit_gain(
    frequency_hz: float,
    sample_rate: int,
    *,
    start_ratio: float,
    stop_ratio: float,
) -> float:
    """Raised-cosine attenuation before a modal partial reaches Nyquist.

    A hard ``frequency >= limit`` branch made a whole music-box partial
    disappear between adjacent chromatic keys.  Keeping a guard band and
    approaching zero continuously removes that timbre step without ever
    synthesizing a partial close to Nyquist.
    """

    if not 0.0 < start_ratio <= stop_ratio < 0.5:
        raise ValueError(
            "modal bandlimit ratios must satisfy "
            "0 < start_ratio <= stop_ratio < 0.5"
        )
    start_hz = sample_rate * start_ratio
    stop_hz = sample_rate * stop_ratio
    if start_hz == stop_hz:
        return 1.0 if frequency_hz < stop_hz else 0.0
    if frequency_hz <= start_hz:
        return 1.0
    if frequency_hz >= stop_hz:
        return 0.0
    progress = (frequency_hz - start_hz) / (stop_hz - start_hz)
    return 0.5 * (1.0 + math.cos(math.pi * progress))


class _Deterministic:
    """xorshift64* 伪随机;显式种子,与平台无关。"""

    __slots__ = ("state",)

    def __init__(self, seed: int) -> None:
        self.state = (seed & 0xFFFFFFFFFFFFFFFF) or 0x9E3779B97F4A7C15

    def next_float(self) -> float:
        state = self.state
        state ^= (state >> 12) & 0xFFFFFFFFFFFFFFFF
        state ^= (state << 25) & 0xFFFFFFFFFFFFFFFF
        state ^= (state >> 27) & 0xFFFFFFFFFFFFFFFF
        self.state = state & 0xFFFFFFFFFFFFFFFF
        return ((state * 0x2545F4914F6CDD1D) & 0xFFFFFFFFFFFFFFFF) / 2.0 ** 64

    def noise(self) -> float:
        return 2.0 * self.next_float() - 1.0


class _VoiceBase:
    __slots__ = ("finished", "released", "release_step", "release_level")

    def __init__(self) -> None:
        self.finished = False
        self.released = False
        self.release_level = 1.0
        self.release_step = 0.0

    def release(self, release_samples: int) -> None:
        if not self.released:
            self.released = True
            self.release_step = 1.0 / max(1, release_samples)

    def _release_gain(self) -> float:
        if not self.released:
            return 1.0
        self.release_level -= self.release_step
        if self.release_level <= 0.0:
            self.release_level = 0.0
            self.finished = True
        return self.release_level


class _PluckedStringVoice(_VoiceBase):
    """分数延迟 Karplus-Strong + 非线性琴桥 + 共鸣体。"""

    __slots__ = (
        "sample_rate", "buffer", "length", "write", "delay", "damping",
        "brightness_state", "buzz_amount", "buzz_threshold", "body_state1",
        "body_state2", "body_mix", "body_cutoff1", "body_cutoff2", "amplitude",
        "age", "attack_noise", "attack_noise_samples", "attack_samples",
        "random", "pan", "sympathetic", "dc_input_state", "dc_output_state",
        "dc_coefficient", "level_envelope", "level_release_coefficient",
        "silent_samples", "silence_window_samples", "silence_threshold",
    )

    def __init__(
        self,
        sample_rate: int,
        frequency_hz: float,
        velocity: float,
        params: dict[str, Any],
        random: _Deterministic,
        *,
        with_sympathetic: bool = True,
    ) -> None:
        super().__init__()
        self.sample_rate = sample_rate
        self.random = random
        # 反馈路径里的两点平均滤波贡献 0.5 样本群延迟,这里从延迟线里扣掉,
        # 否则弦模型整体偏低(高音区可达 -10 音分)。
        self.delay = max(2.0, sample_rate / frequency_hz - 0.5)
        self.length = max(4, int(self.delay) + 2)
        pluck_position = _finite(params.get("pluck_position", 0.28), "pluck_position")
        excitation_noise = _finite(params.get("excitation_noise", 0.55), "excitation_noise")
        buffer: list[float] = []
        for index in range(self.length):
            shape = index / self.length
            comb = math.sin(math.pi * shape)
            position = 1.0 - abs(shape - pluck_position) / max(
                pluck_position, 1.0 - pluck_position
            )
            deterministic_noise = random.noise()
            buffer.append(
                (comb * position + excitation_noise * deterministic_noise)
                * (0.4 + 0.6 * velocity)
            )
        # A plucked displacement must oscillate around equilibrium.  The old
        # half-sine displacement was strictly positive and therefore stored a
        # very large DC component in the feedback loop (most visible on the
        # short high-register koto delays).  Centre the complete deterministic
        # excitation, including its finite random realization.
        buffer_mean = sum(buffer) / len(buffer)
        self.buffer = [sample - buffer_mean for sample in buffer]
        self.write = 0
        base_damping = _finite(params.get("damping", 0.996), "damping")
        velocity_brightness = _finite(params.get("velocity_brightness", 0.002), "velocity_brightness")
        self.damping = min(0.9999, base_damping + velocity_brightness * velocity)
        self.brightness_state = 0.0
        self.buzz_amount = _finite(params.get("bridge_buzz", 0.0), "bridge_buzz")
        self.buzz_threshold = _finite(params.get("buzz_threshold", 0.12), "buzz_threshold")
        self.body_cutoff1 = _finite(params.get("body_low_hz", 180.0), "body_low_hz")
        self.body_cutoff2 = _finite(params.get("body_mid_hz", 900.0), "body_mid_hz")
        self.body_mix = _finite(params.get("body_mix", 0.35), "body_mix")
        self.body_state1 = 0.0
        self.body_state2 = 0.0
        self.amplitude = velocity
        self.age = 0
        self.attack_noise = _finite(params.get("attack_noise", 0.12), "attack_noise")
        self.attack_noise_samples = max(
            1, round(_finite(params.get("attack_noise_seconds", 0.006), "attack_noise_seconds") * sample_rate)
        )
        attack_seconds = _finite(
            params.get("attack_seconds", 0.002), "attack_seconds"
        )
        if not 0.001 <= attack_seconds <= 0.003:
            raise ValueError(
                "plucked_string attack_seconds must be between 0.001 and 0.003"
            )
        self.attack_samples = max(1, round(attack_seconds * sample_rate))
        dc_block_hz = _finite(params.get("dc_block_hz", 18.0), "dc_block_hz")
        if not 0.0 < dc_block_hz < sample_rate * 0.25:
            raise ValueError("dc_block_hz must be between 0 and sample_rate / 4")
        self.dc_coefficient = math.exp(
            -2.0 * math.pi * dc_block_hz / sample_rate
        )
        self.dc_input_state = 0.0
        self.dc_output_state = 0.0
        level_release_seconds = _finite(
            params.get("silence_envelope_release_seconds", 0.04),
            "silence_envelope_release_seconds",
        )
        if level_release_seconds <= 0.0:
            raise ValueError("silence_envelope_release_seconds must be positive")
        self.level_envelope = 0.0
        self.level_release_coefficient = math.exp(
            -1.0 / (level_release_seconds * sample_rate)
        )
        silence_window_seconds = _finite(
            params.get("silence_window_seconds", 0.05),
            "silence_window_seconds",
        )
        if silence_window_seconds <= 0.0:
            raise ValueError("silence_window_seconds must be positive")
        self.silence_window_samples = max(
            1, round(silence_window_seconds * sample_rate)
        )
        self.silence_threshold = _finite(
            params.get("silence_threshold", 1.0e-5),
            "silence_threshold",
        )
        if not 0.0 < self.silence_threshold < 1.0:
            raise ValueError("silence_threshold must be between 0 and 1")
        self.silent_samples = 0
        self.pan = _finite(params.get("pan", 0.0), "pan")
        self.sympathetic: list[tuple[list[float], int, float]] = []
        if with_sympathetic:
            for ratio in params.get("sympathetic_ratios", []):
                frequency = frequency_hz * _finite(ratio, "sympathetic ratio")
                if frequency >= sample_rate * 0.45:
                    continue
                length = max(4, int(sample_rate / frequency) + 1)
                self.sympathetic.append(([0.0] * length, 0, 0.9985))

    def step(self) -> StereoFrame:
        read_position = self.write - self.delay
        index = int(math.floor(read_position)) % self.length
        next_index = (index + 1) % self.length
        fraction = read_position - math.floor(read_position)
        sample = self.buffer[index] + (self.buffer[next_index] - self.buffer[index]) * fraction

        averaged = 0.5 * (sample + self.brightness_state)
        self.brightness_state = sample
        feedback = averaged * self.damping
        if self.buzz_amount > 0.0 and abs(feedback) > self.buzz_threshold:
            overshoot = abs(feedback) - self.buzz_threshold
            feedback -= math.copysign(
                overshoot * self.buzz_amount * 0.6, feedback
            )
        self.buffer[self.write % self.length] = feedback
        self.write += 1

        output = sample
        if self.age < self.attack_noise_samples:
            fade = 1.0 - self.age / self.attack_noise_samples
            output += self.attack_noise * fade * self.random.noise() * self.amplitude
        for entry_index, (line, position, damping) in enumerate(self.sympathetic):
            length = len(line)
            excitation = output * 0.012
            value = line[position % length]
            line[position % length] = 0.5 * (value + line[(position + 1) % length]) * damping + excitation
            self.sympathetic[entry_index] = (line, position + 1, damping)
            output += value * 0.35

        coefficient1 = 1.0 - math.exp(-2.0 * math.pi * self.body_cutoff1 / self.sample_rate)
        self.body_state1 += coefficient1 * (output - self.body_state1)
        coefficient2 = 1.0 - math.exp(-2.0 * math.pi * self.body_cutoff2 / self.sample_rate)
        self.body_state2 += coefficient2 * (output - self.body_state2)
        body = 0.6 * self.body_state1 + 0.4 * (self.body_state2 - self.body_state1)
        mixed = output * (1.0 - self.body_mix) + body * self.body_mix

        attack_progress = self.age / self.attack_samples
        mixed *= _smoothstep(attack_progress)
        blocked = (
            mixed
            - self.dc_input_state
            + self.dc_coefficient * self.dc_output_state
        )
        self.dc_input_state = mixed
        self.dc_output_state = blocked
        self.age += 1

        gain = self._release_gain() * self.amplitude
        output_level = abs(blocked * gain)
        self.level_envelope = max(
            output_level,
            self.level_envelope * self.level_release_coefficient,
        )
        # A periodic signal crosses zero every half-cycle.  Treating one such
        # sample as silence used to kill an otherwise audible koto tail.  End
        # the natural decay only after a peak-following envelope has remained
        # below the silence floor for a complete contiguous window.
        if self.age > self.sample_rate and self.level_envelope < self.silence_threshold:
            self.silent_samples += 1
            if self.silent_samples >= self.silence_window_samples:
                self.finished = True
        else:
            self.silent_samples = 0
        left = blocked * gain * (1.0 - max(0.0, self.pan))
        right = blocked * gain * (1.0 + min(0.0, self.pan))
        return left, right


class _BlownPipeVoice(_VoiceBase):
    """气鸣管:谐波簇 + 带通气息噪声 + 吹口起音。"""

    __slots__ = (
        "sample_rate", "frequency", "phase", "harmonics", "attack_samples",
        "age", "breath_gain", "breath_state", "breath_center", "amplitude",
        "random", "vibrato_depth", "vibrato_rate", "vibrato_phase",
        "attack_bend_cents", "attack_bend_samples", "chiff_gain",
        "chiff_samples", "odd_bias",
    )

    def __init__(
        self,
        sample_rate: int,
        frequency_hz: float,
        velocity: float,
        params: dict[str, Any],
        random: _Deterministic,
    ) -> None:
        super().__init__()
        self.sample_rate = sample_rate
        self.frequency = frequency_hz
        self.phase = 0.0
        rolloff = _finite(params.get("harmonic_rolloff", 1.7), "harmonic_rolloff")
        self.odd_bias = _finite(params.get("odd_harmonic_bias", 1.0), "odd_harmonic_bias")
        count = int(params.get("harmonic_count", 8))
        harmonics: list[float] = []
        for order in range(1, count + 1):
            amplitude = 1.0 / (order ** rolloff)
            if order % 2 == 0:
                amplitude /= self.odd_bias
            if frequency_hz * order >= sample_rate * 0.45:
                amplitude = 0.0
            harmonics.append(amplitude)
        total = sum(harmonics) or 1.0
        self.harmonics = [amplitude / total for amplitude in harmonics]
        self.attack_samples = max(
            1, round(_finite(params.get("attack_seconds", 0.05), "attack_seconds") * sample_rate)
        )
        self.age = 0
        self.breath_gain = _finite(params.get("breath_noise", 0.16), "breath_noise")
        self.breath_state = 0.0
        self.breath_center = _finite(params.get("breath_center_ratio", 2.6), "breath_center_ratio")
        self.amplitude = 0.35 + 0.65 * velocity
        self.random = random
        self.vibrato_depth = _finite(params.get("vibrato_cents", 22.0), "vibrato_cents")
        self.vibrato_rate = _finite(params.get("vibrato_rate_hz", 4.6), "vibrato_rate_hz")
        self.vibrato_phase = random.next_float() * 2.0 * math.pi
        self.attack_bend_cents = _finite(params.get("attack_bend_cents", -28.0), "attack_bend_cents")
        self.attack_bend_samples = max(
            1, round(_finite(params.get("attack_bend_seconds", 0.045), "attack_bend_seconds") * sample_rate)
        )
        self.chiff_gain = _finite(params.get("chiff_noise", 0.3), "chiff_noise")
        self.chiff_samples = max(
            1, round(_finite(params.get("chiff_seconds", 0.03), "chiff_seconds") * sample_rate)
        )

    def step(self, modulation: float) -> StereoFrame:
        envelope = min(1.0, self.age / self.attack_samples)
        cents = 0.0
        if self.age < self.attack_bend_samples:
            cents += self.attack_bend_cents * (1.0 - self.age / self.attack_bend_samples)
        vibrato_fade = min(1.0, self.age / (self.sample_rate * 0.5))
        self.vibrato_phase += 2.0 * math.pi * self.vibrato_rate / self.sample_rate
        cents += self.vibrato_depth * modulation * vibrato_fade * math.sin(self.vibrato_phase)
        frequency = self.frequency * (2.0 ** (cents / 1200.0))
        self.phase += 2.0 * math.pi * frequency / self.sample_rate
        if self.phase > 2.0 * math.pi:
            self.phase -= 2.0 * math.pi
        tone = 0.0
        for order, amplitude in enumerate(self.harmonics, start=1):
            if amplitude > 0.0:
                tone += amplitude * math.sin(self.phase * order)

        noise = self.random.noise()
        center = min(self.frequency * self.breath_center, self.sample_rate * 0.4)
        coefficient = 1.0 - math.exp(-2.0 * math.pi * center / self.sample_rate)
        self.breath_state += coefficient * (noise - self.breath_state)
        breath = (noise - self.breath_state) * self.breath_gain
        if self.age < self.chiff_samples:
            breath += noise * self.chiff_gain * (1.0 - self.age / self.chiff_samples)

        self.age += 1
        value = (tone + breath * envelope) * envelope * self.amplitude * self._release_gain()
        return value, value


class _DoubleReedVoice(_BlownPipeVoice):
    """双簧亮音:在气鸣模型上叠加共振峰。"""

    __slots__ = ("formant_states", "formants")

    def __init__(self, sample_rate, frequency_hz, velocity, params, random) -> None:
        super().__init__(sample_rate, frequency_hz, velocity, params, random)
        self.formants = [
            (_finite(center, "formant center"), _finite(gain, "formant gain"))
            for center, gain in params.get("formants", [(1250.0, 1.8), (3100.0, 1.1)])
        ]
        self.formant_states = [[0.0, 0.0] for _ in self.formants]

    def step(self, modulation: float) -> StereoFrame:
        left, _ = super().step(modulation)
        boosted = left
        for index, (center, gain) in enumerate(self.formants):
            state = self.formant_states[index]
            coefficient = 1.0 - math.exp(-2.0 * math.pi * center / self.sample_rate)
            state[0] += coefficient * (left - state[0])
            state[1] += coefficient * (state[0] - state[1])
            boosted += (state[0] - state[1]) * gain
        boosted *= 0.6
        return boosted, boosted


class _ModalVoice(_VoiceBase):
    """模态合成:一组指数衰减的正弦模态 + 起音噪声。"""

    __slots__ = (
        "sample_rate", "modes", "age", "attack_noise", "attack_noise_samples",
        "noise_rise_samples", "noise_center", "noise_state", "amplitude",
        "random", "pan",
    )

    def __init__(
        self,
        sample_rate: int,
        frequency_hz: float,
        velocity: float,
        params: dict[str, Any],
        random: _Deterministic,
    ) -> None:
        super().__init__()
        self.sample_rate = sample_rate
        self.random = random
        self.modes: list[list[float]] = []
        bandlimit_stop = _finite(
            params.get("bandlimit_stop_ratio", 0.45),
            "bandlimit_stop_ratio",
        )
        bandlimit_start = _finite(
            params.get("bandlimit_start_ratio", bandlimit_stop),
            "bandlimit_start_ratio",
        )

        def append_mode(
            frequency: float,
            amplitude: object,
            decay_seconds: object,
            attack_seconds: object,
        ) -> None:
            bandlimit = _bandlimit_gain(
                frequency,
                sample_rate,
                start_ratio=bandlimit_start,
                stop_ratio=bandlimit_stop,
            )
            if bandlimit <= 0.0:
                return
            phase_step = 2.0 * math.pi * frequency / sample_rate
            decay = math.exp(-1.0 / (max(0.005, _finite(decay_seconds, "mode decay")) * sample_rate))
            attack_samples = max(1, round(_finite(attack_seconds, "mode attack") * sample_rate))
            self.modes.append([
                0.0, phase_step,
                _finite(amplitude, "mode amplitude")
                * bandlimit
                * (0.35 + 0.65 * velocity),
                1.0, decay, float(attack_samples),
            ])

        for ratio, amplitude, decay_seconds, attack_seconds in params["modes"]:
            append_mode(
                frequency_hz * _finite(ratio, "mode ratio"),
                amplitude,
                decay_seconds,
                attack_seconds,
            )
        fixed_mode_gain = _finite(
            params.get("fixed_mode_gain", 1.0), "fixed_mode_gain"
        )
        for absolute_hz, amplitude, decay_seconds, attack_seconds in params.get(
            "fixed_modes_hz", []
        ):
            append_mode(
                _finite(absolute_hz, "fixed mode frequency"),
                _finite(amplitude, "fixed mode amplitude") * fixed_mode_gain,
                decay_seconds,
                attack_seconds,
            )
        self.age = 0
        self.attack_noise = _finite(params.get("attack_noise", 0.4), "attack_noise") * velocity
        self.attack_noise_samples = max(
            1, round(_finite(params.get("attack_noise_seconds", 0.008), "attack_noise_seconds") * sample_rate)
        )
        default_rise_seconds = min(
            0.00035,
            0.25 * self.attack_noise_samples / sample_rate,
        )
        rise_seconds = _finite(
            params.get("attack_noise_rise_seconds", default_rise_seconds),
            "attack_noise_rise_seconds",
        )
        if rise_seconds < 0.0:
            raise ValueError("attack_noise_rise_seconds must not be negative")
        self.noise_rise_samples = (
            1
            if self.attack_noise_samples <= 1
            else min(
                self.attack_noise_samples - 1,
                max(1, round(rise_seconds * sample_rate)),
            )
        )
        self.noise_center = _finite(params.get("attack_noise_center_hz", 3200.0), "attack_noise_center_hz")
        self.noise_state = 0.0
        self.amplitude = _finite(params.get("output_gain", 1.0), "output_gain")
        self.pan = _finite(params.get("pan", 0.0), "pan")

    def step(self) -> StereoFrame:
        value = 0.0
        alive = False
        for mode in self.modes:
            mode[0] += mode[1]
            if mode[0] > 2.0 * math.pi:
                mode[0] -= 2.0 * math.pi
            envelope = min(1.0, self.age / mode[5])
            mode[3] *= mode[4]
            if mode[3] > 1.0e-5:
                alive = True
            value += math.sin(mode[0]) * mode[2] * mode[3] * envelope
        if self.age < self.attack_noise_samples:
            noise = self.random.noise()
            coefficient = 1.0 - math.exp(-2.0 * math.pi * self.noise_center / self.sample_rate)
            self.noise_state += coefficient * (noise - self.noise_state)
            if self.age < self.noise_rise_samples:
                noise_envelope = _smoothstep(
                    self.age / self.noise_rise_samples
                )
            else:
                fall_samples = max(
                    1,
                    self.attack_noise_samples - 1 - self.noise_rise_samples,
                )
                fall_progress = (
                    self.age - self.noise_rise_samples
                ) / fall_samples
                noise_envelope = (1.0 - min(1.0, fall_progress)) ** 2
            value += (
                (noise - self.noise_state)
                * self.attack_noise
                * noise_envelope
            )
        self.age += 1
        gain = self._release_gain()
        if not alive and self.age > self.attack_noise_samples:
            self.finished = True
        left = value * gain * self.amplitude * (1.0 - max(0.0, self.pan))
        right = value * gain * self.amplitude * (1.0 + min(0.0, self.pan))
        return left, right


class _SynthDrumVoice(_VoiceBase):
    """模拟鼓机:指数降频正弦 + 咔嗒瞬态 + 噪声。"""

    __slots__ = (
        "sample_rate", "phase", "frequency", "target", "sweep_coefficient",
        "level", "decay", "noise_gain", "noise_decay", "noise_level",
        "amplitude", "random", "age",
    )

    def __init__(self, sample_rate, frequency_hz, velocity, params, random) -> None:
        super().__init__()
        self.sample_rate = sample_rate
        self.phase = 0.0
        sweep_ratio = _finite(params.get("sweep_ratio", 2.6), "sweep_ratio")
        sweep_seconds = _finite(params.get("sweep_seconds", 0.055), "sweep_seconds")
        self.frequency = frequency_hz * (1.0 + (sweep_ratio - 1.0) * (0.5 + 0.5 * velocity))
        self.target = frequency_hz
        self.sweep_coefficient = math.exp(-1.0 / (sweep_seconds * sample_rate))
        self.level = 1.0
        self.decay = math.exp(-1.0 / (_finite(params.get("decay_seconds", 0.32), "decay_seconds") * sample_rate))
        self.noise_gain = _finite(params.get("noise_gain", 0.25), "noise_gain") * velocity
        self.noise_decay = math.exp(-1.0 / (_finite(params.get("noise_decay_seconds", 0.05), "noise_decay_seconds") * sample_rate))
        self.noise_level = 1.0
        self.amplitude = 0.3 + 0.7 * velocity
        self.random = random
        self.age = 0

    def step(self) -> StereoFrame:
        self.frequency = self.target + (self.frequency - self.target) * self.sweep_coefficient
        self.phase += 2.0 * math.pi * self.frequency / self.sample_rate
        if self.phase > 2.0 * math.pi:
            self.phase -= 2.0 * math.pi
        self.level *= self.decay
        self.noise_level *= self.noise_decay
        value = math.sin(self.phase) * self.level
        value += self.random.noise() * self.noise_gain * self.noise_level
        if self.age < 32:
            value += (1.0 - self.age / 32.0) * 0.5
        self.age += 1
        if self.level < 1.0e-5 and self.noise_level < 1.0e-5:
            self.finished = True
        value *= self.amplitude * self._release_gain()
        return value, value


PROFILES: dict[str, dict[str, Any]] = {
    "shamisen": {
        "voice": "plucked_string",
        "params": {
            "damping": 0.9935, "pluck_position": 0.18, "excitation_noise": 0.7,
            "bridge_buzz": 0.55, "buzz_threshold": 0.09,
            "body_low_hz": 240.0, "body_mid_hz": 1100.0, "body_mix": 0.42,
            "attack_noise": 0.35, "attack_noise_seconds": 0.005,
            "attack_seconds": 0.0015, "dc_block_hz": 18.0,
            "velocity_brightness": 0.004,
        },
        "release_seconds": 0.09,
    },
    "koto": {
        "voice": "plucked_string",
        "params": {
            "damping": 0.9975, "pluck_position": 0.3, "excitation_noise": 0.45,
            "bridge_buzz": 0.0, "body_low_hz": 170.0, "body_mid_hz": 800.0,
            "body_mix": 0.3, "attack_noise": 0.1, "attack_noise_seconds": 0.004,
            "attack_seconds": 0.002, "dc_block_hz": 18.0,
            "velocity_brightness": 0.002,
        },
        "release_seconds": 0.25,
    },
    "sitar": {
        "voice": "plucked_string",
        "params": {
            "damping": 0.9982, "pluck_position": 0.12, "excitation_noise": 0.6,
            "bridge_buzz": 0.85, "buzz_threshold": 0.05,
            "body_low_hz": 210.0, "body_mid_hz": 1400.0, "body_mix": 0.38,
            "attack_noise": 0.12, "attack_noise_seconds": 0.004,
            "attack_seconds": 0.0015, "dc_block_hz": 18.0,
            "velocity_brightness": 0.003,
            "sympathetic_ratios": [1.5, 2.0, 2.667, 4.0],
        },
        "release_seconds": 0.4,
    },
    "shakuhachi": {
        "voice": "blown_pipe",
        "params": {
            "harmonic_rolloff": 1.55, "harmonic_count": 9, "odd_harmonic_bias": 1.25,
            "attack_seconds": 0.075, "breath_noise": 0.22, "breath_center_ratio": 2.4,
            "vibrato_cents": 34.0, "vibrato_rate_hz": 4.2,
            "attack_bend_cents": -38.0, "attack_bend_seconds": 0.06,
            "chiff_noise": 0.18, "chiff_seconds": 0.05,
        },
        "release_seconds": 0.16,
    },
    "pan_flute": {
        "voice": "blown_pipe",
        "params": {
            "harmonic_rolloff": 2.1, "harmonic_count": 7, "odd_harmonic_bias": 1.9,
            "attack_seconds": 0.028, "breath_noise": 0.3, "breath_center_ratio": 2.1,
            "vibrato_cents": 16.0, "vibrato_rate_hz": 5.1,
            "attack_bend_cents": -14.0, "attack_bend_seconds": 0.02,
            "chiff_noise": 0.42, "chiff_seconds": 0.024,
        },
        "release_seconds": 0.12,
    },
    "suona": {
        "voice": "double_reed",
        "params": {
            "harmonic_rolloff": 1.12, "harmonic_count": 14, "odd_harmonic_bias": 0.95,
            "attack_seconds": 0.02, "breath_noise": 0.05, "breath_center_ratio": 3.2,
            "vibrato_cents": 26.0, "vibrato_rate_hz": 5.4,
            "attack_bend_cents": 24.0, "attack_bend_seconds": 0.03,
            "chiff_noise": 0.08, "chiff_seconds": 0.012,
            "formants": [[1250.0, 1.8], [3150.0, 1.2]],
        },
        "release_seconds": 0.1,
    },
    "taiko": {
        "voice": "modal",
        "pitch_mode": "keymap",
        # All strokes excite the same low cavity and wooden-shell modes.
        # Strike position changes membrane weights and transient brightness,
        # not the physical identity of the drum.
        "params": {
            "fixed_modes_hz": [
                [68.0, 0.24, 0.62, 0.0015],
                [214.0, 0.12, 0.3, 0.001],
                [410.0, 0.055, 0.16, 0.0008],
            ],
            "attack_noise_rise_seconds": 0.0003,
        },
        "keymap": {
            60: {"frequency_hz": 82.0, "label": "don 中心击", "params": {
                "modes": [
                    [1.0, 0.92, 0.82, 0.0012],
                    [1.59, 0.42, 0.4, 0.001],
                    [2.14, 0.27, 0.23, 0.0009],
                    [2.65, 0.14, 0.14, 0.0008],
                    [2.92, 0.085, 0.1, 0.0008],
                ],
                "attack_noise": 0.42, "attack_noise_seconds": 0.008,
                "attack_noise_center_hz": 2300.0,
                "fixed_mode_gain": 1.0, "output_gain": 0.9,
            }},
            61: {"frequency_hz": 82.0, "label": "边缘击", "params": {
                "modes": [
                    [1.0, 0.38, 0.52, 0.001],
                    [1.59, 0.62, 0.35, 0.0009],
                    [2.14, 0.46, 0.22, 0.0008],
                    [2.65, 0.28, 0.14, 0.0007],
                    [2.92, 0.17, 0.1, 0.0007],
                ],
                "attack_noise": 0.5, "attack_noise_seconds": 0.006,
                "attack_noise_center_hz": 3400.0,
                "fixed_mode_gain": 0.82, "output_gain": 1.05,
            }},
            62: {"frequency_hz": 940.0, "label": "ka 鼓边木击", "params": {
                "modes": [
                    [1.0, 0.45, 0.055, 0.0005],
                    [2.62, 0.28, 0.03, 0.0004],
                    [4.9, 0.12, 0.018, 0.0004],
                ],
                "attack_noise": 0.58, "attack_noise_seconds": 0.0035,
                "attack_noise_center_hz": 4600.0,
                "fixed_mode_gain": 0.34, "output_gain": 2.0,
            }},
        },
        "release_seconds": 0.3,
    },
    "steelpan": {
        "voice": "modal",
        "params": {
            "modes": [
                [1.0, 1.0, 0.78, 0.0015],
                [2.003, 0.64, 0.46, 0.0025],
                # A weak, short-lived coupled partner keeps an acoustic
                # shimmer without the old deep, sustained electrical beat.
                [2.044, 0.1, 0.14, 0.0012],
                [3.01, 0.32, 0.3, 0.002],
                [4.18, 0.16, 0.18, 0.0015],
                [5.55, 0.07, 0.1, 0.001],
            ],
            "attack_noise": 0.14, "attack_noise_seconds": 0.0035,
            "attack_noise_rise_seconds": 0.0004,
            "attack_noise_center_hz": 3000.0,
        },
        "release_seconds": 0.35,
    },
    "music_box": {
        "voice": "modal",
        "params": {
            "modes": [
                [1.0, 1.0, 1.6, 0.0006], [3.42, 0.34, 0.5, 0.0006],
                [8.93, 0.105, 0.16, 0.0006],
                [16.2, 0.032, 0.06, 0.0006],
            ],
            "attack_noise": 0.055, "attack_noise_seconds": 0.0025,
            "attack_noise_rise_seconds": 0.0003,
            "attack_noise_center_hz": 5200.0,
            "bandlimit_start_ratio": 0.3,
            "bandlimit_stop_ratio": 0.45,
        },
        "release_seconds": 0.5,
    },
    "synth_drum": {
        "voice": "synth_drum",
        "params": {
            "sweep_ratio": 2.6, "sweep_seconds": 0.055, "decay_seconds": 0.34,
            "noise_gain": 0.22, "noise_decay_seconds": 0.045,
        },
        "release_seconds": 0.12,
    },
}


class ModeledInstrument(Instrument):
    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        del base_directory
        declared_version = str(manifest.get("engine_version", "")).strip()
        if declared_version != ENGINE_VERSION:
            raise ValueError(
                "modeled_instrument engine_version "
                f"{declared_version!r} does not match runtime {ENGINE_VERSION!r}"
            )
        profile_name = str(manifest.get("profile", ""))
        if profile_name not in PROFILES:
            raise ValueError(
                f"unknown modeled_instrument profile {profile_name!r}; "
                f"choose from {sorted(PROFILES)}"
            )
        self.profile_name = profile_name
        self.profile = PROFILES[profile_name]
        self.pitch_keymap = self.profile.get("pitch_mode") == "keymap"
        overrides = manifest.get("model_params", {})
        if not isinstance(overrides, dict):
            raise ValueError("model_params must be an object")
        self.params = {**self.profile.get("params", {}), **overrides}
        self.seed = int(manifest.get("seed", 8888))
        self.note_min = float(manifest.get("note_min", 0))
        self.note_max = float(manifest.get("note_max", 127))
        self.gain = _finite(manifest.get("gain", 0.5), "gain")
        self.velocity_exponent = _finite(manifest.get("velocity_exponent", 0.72), "velocity_exponent")
        self.release_samples = max(
            1,
            round(
                _finite(
                    manifest.get("release_seconds", self.profile.get("release_seconds", 0.2)),
                    "release_seconds",
                )
                * sample_rate
            ),
        )
        self.modulation = 0.0
        self.expression = 1.0
        self._voices: dict[int, Any] = {}
        self._trigger_count = 0

    def _make_voice(self, frequency_hz: float, velocity: float, midi: float) -> Any:
        self._trigger_count += 1
        random = _Deterministic(
            (self.seed * 1_000_003 + self._trigger_count * 7919 + int(midi * 128)) & 0xFFFFFFFFFFFFFFFF
        )
        kind = str(self.profile["voice"])
        if kind == "plucked_string":
            return _PluckedStringVoice(self.sample_rate, frequency_hz, velocity, self.params, random)
        if kind == "blown_pipe":
            return _BlownPipeVoice(self.sample_rate, frequency_hz, velocity, self.params, random)
        if kind == "double_reed":
            return _DoubleReedVoice(self.sample_rate, frequency_hz, velocity, self.params, random)
        if kind == "modal":
            if self.pitch_keymap:
                key = int(round(midi))
                keymap = self.profile["keymap"]
                if key not in keymap:
                    raise ValueError(
                        f"modeled_instrument {self.profile_name!r} has no key {key}; "
                        f"valid keys {sorted(keymap)}"
                    )
                spec = keymap[key]
                return _ModalVoice(
                    self.sample_rate,
                    _finite(spec["frequency_hz"], "keymap frequency"),
                    velocity,
                    {**spec["params"], **{k: v for k, v in self.params.items() if k != "modes"}},
                    random,
                )
            return _ModalVoice(self.sample_rate, frequency_hz, velocity, self.params, random)
        if kind == "synth_drum":
            return _SynthDrumVoice(self.sample_rate, frequency_hz, velocity, self.params, random)
        raise ValueError(f"unknown voice kind {kind!r}")

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "note_on":
            note_id = int(event.payload["note_id"])
            if note_id in self._voices:
                raise ValueError(f"modeled_instrument note_id {note_id} is already active")
            if "midi_note" in event.payload:
                midi = float(event.payload["midi_note"])
            else:
                midi = 69.0 + 12.0 * math.log2(event_pitch_hz(event, tuning) / tuning.a4_hz)
            if not self.note_min <= midi <= self.note_max:
                raise ValueError(
                    f"modeled_instrument note {midi:g} is outside declared range "
                    f"{self.note_min:g}..{self.note_max:g}"
                )
            frequency = event_pitch_hz(event, tuning)
            velocity = min(1.0, max(0.0, float(event.payload.get("velocity", 0.8))))
            velocity = velocity ** self.velocity_exponent
            self._voices[note_id] = self._make_voice(frequency, velocity, midi)
        elif event.type == "note_off":
            note_id = int(event.payload["note_id"])
            voice = self._voices.get(note_id)
            if voice is not None:
                voice.release(self.release_samples)
        elif event.type == "control":
            name = str(event.payload["name"])
            value = float(event.payload["value"])
            if name == "modulation":
                self.modulation = min(1.0, max(0.0, value))
            elif name == "expression":
                self.expression = min(1.0, max(0.0, value))

    def render_frame(self) -> StereoFrame:
        left = right = 0.0
        finished: list[int] = []
        for note_id, voice in self._voices.items():
            if isinstance(voice, (_BlownPipeVoice, _DoubleReedVoice)):
                voice_left, voice_right = voice.step(self.modulation)
            else:
                voice_left, voice_right = voice.step()
            left += voice_left
            right += voice_right
            if voice.finished:
                finished.append(note_id)
        for note_id in finished:
            self._voices.pop(note_id, None)
        scale = self.gain * self.expression
        return left * scale, right * scale

    @property
    def active_voice_count(self) -> int:
        return len(self._voices)


def create_modeled_instrument(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> ModeledInstrument:
    return ModeledInstrument(sample_rate, manifest, base_directory)


def _engine_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def generate_modeled_resource_verification(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    report = {
        "implementation": "Tianlai self-authored deterministic physical/signal model",
        "engine": "tianlai/modeled_instruments.py",
        "engine_version": ENGINE_VERSION,
        "profile": str(manifest.get("profile", "")),
        "seed": int(manifest.get("seed", 0)),
        "model_params_override": manifest.get("model_params", {}),
        "engine_sha256": _engine_sha256(),
        "external_assets": [],
        **project_authored_dsp_provenance(),
        "generated_at": _datetime.date.today().isoformat(),
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent
        / str(manifest.get("resource_verification", "资源核验.json"))
    )
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def generate_modeled_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """渲染测试音并用 FFT 实测,把模型音准写成机器证据。"""

    import numpy as np

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent / str(manifest.get("pitch_calibration", "音准校准.json"))
    )
    profile = PROFILES[str(manifest["profile"])]
    if profile.get("pitch_mode") == "keymap":
        reason = str(manifest.get("calibration_not_applicable_reason", "")).strip()
        if not reason:
            raise ValueError(
                f"keymap profiles must record calibration_not_applicable_reason: {source_manifest}"
            )
        document: dict[str, Any] = {
            "applicable": False,
            "pitch_mode": "keymap",
            "engine_version": ENGINE_VERSION,
            "reason": reason,
            "samples": {},
            "generated_at": _datetime.date.today().isoformat(),
        }
        destination.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return document

    sample_rate = 48000
    note_min = int(manifest["note_min"])
    note_max = int(manifest["note_max"])
    test_notes = sorted({note_min, (note_min + note_max) // 2, note_max})
    tuning = EqualTemperament(440.0)
    results: dict[str, dict[str, float]] = {}
    errors: list[float] = []
    for midi in test_notes:
        instrument = ModeledInstrument(sample_rate, manifest, str(source_manifest.parent))
        expected_hz = 440.0 * 2.0 ** ((midi - 69.0) / 12.0)
        instrument.handle_event(
            PerformanceEvent(0, 0, "note_on", {"note_id": 1, "midi_note": midi, "velocity": 0.8}),
            tuning,
        )
        frames = int(sample_rate * 1.2)
        buffer = np.empty(frames, dtype=np.float64)
        for index in range(frames):
            left, _ = instrument.render_frame()
            buffer[index] = left
        start = int(sample_rate * 0.25)
        segment = buffer[start:]
        segment = segment - np.mean(segment)
        spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment))))
        frequencies = np.fft.rfftfreq(len(segment), 1.0 / sample_rate)
        ratio = 2.0 ** (120.0 / 1200.0)
        mask = (frequencies >= expected_hz / ratio) & (frequencies <= expected_hz * ratio)
        bins = np.flatnonzero(mask)
        if len(bins) == 0:
            raise ValueError(f"no FFT bins near {expected_hz} Hz")
        peak = int(bins[np.argmax(spectrum[mask])])
        delta = 0.0
        if 0 < peak < len(spectrum) - 1:
            left_bin, center, right_bin = np.log(spectrum[peak - 1 : peak + 2] + 1e-20)
            denominator = left_bin - 2.0 * center + right_bin
            if denominator != 0.0:
                delta = float(0.5 * (left_bin - right_bin) / denominator)
        measured_hz = (peak + delta) * sample_rate / len(segment)
        error_cents = 1200.0 * math.log2(measured_hz / expected_hz)
        results[str(midi)] = {
            "expected_hz": round(expected_hz, 6),
            "measured_hz": round(measured_hz, 6),
            "error_cents": round(error_cents, 6),
        }
        errors.append(error_cents)

    document = {
        "applicable": True,
        "method": (
            "self-test: render each probe note with the released engine, FFT the "
            "steady segment, compare against the equal-temperament target"
        ),
        "engine_version": ENGINE_VERSION,
        "reference_a4_hz": 440.0,
        "summary": {
            "probe_count": len(errors),
            "maximum_absolute_error_cents": round(max(abs(error) for error in errors), 6),
        },
        "probes": results,
        "generated_at": _datetime.date.today().isoformat(),
    }
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return document
