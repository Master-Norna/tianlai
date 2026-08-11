"""dedicated_sfz 采样核心 + 确定性后处理链。

用于“同一件真实采样乐器经过明确信号链”的专用入口，例如
DI 电吉他经过音色滤波 / 过载 / 失真,或 FM 电钢琴经过立体声合唱。
全部效果参数写进 ``乐器.json``,无随机源,同一输入必得同一输出;
不引入任何通用 SoundFont 回退。

支持的效果(按 ``effects`` 数组顺序逐帧串联):

- ``highpass``:一阶高通,``cutoff_hz``;
- ``lowpass``:一阶低通,``cutoff_hz``;
- ``waveshaper``:``pre_gain`` 后接软/硬削波(``curve``: soft|hard),
  ``post_gain`` 收平,模拟过载/失真前级;
- ``chorus``:双声道 LFO 调制延迟线,``rate_hz``/``depth_seconds``/
  ``base_delay_seconds``/``mix``/``stereo_phase``,右声道相位偏移。
"""

from __future__ import annotations

import math
from typing import Any

from .dedicated_sfz import DedicatedSfzInstrument
from .events import PerformanceEvent
from ._event_free_blocks import audited_event_free_blocks
from .instrument import Instrument, StereoFrame
from .tuning import EqualTemperament


class _OnePole:
    __slots__ = ("coefficient", "state")

    def __init__(self, cutoff_hz: float, sample_rate: int) -> None:
        if cutoff_hz <= 0.0:
            raise ValueError("cutoff_hz must be positive")
        self.coefficient = 1.0 - math.exp(-2.0 * math.pi * cutoff_hz / sample_rate)
        self.state = 0.0

    def lowpass(self, value: float) -> float:
        self.state += self.coefficient * (value - self.state)
        return self.state

    def highpass(self, value: float) -> float:
        self.state += self.coefficient * (value - self.state)
        return value - self.state


class _Effect:
    def process(self, left: float, right: float) -> tuple[float, float]:
        raise NotImplementedError


class _HighpassEffect(_Effect):
    def __init__(self, params: dict[str, Any], sample_rate: int) -> None:
        cutoff = float(params["cutoff_hz"])
        self._left = _OnePole(cutoff, sample_rate)
        self._right = _OnePole(cutoff, sample_rate)

    def process(self, left: float, right: float) -> tuple[float, float]:
        return self._left.highpass(left), self._right.highpass(right)


class _LowpassEffect(_Effect):
    def __init__(self, params: dict[str, Any], sample_rate: int) -> None:
        cutoff = float(params["cutoff_hz"])
        self._left = _OnePole(cutoff, sample_rate)
        self._right = _OnePole(cutoff, sample_rate)

    def process(self, left: float, right: float) -> tuple[float, float]:
        return self._left.lowpass(left), self._right.lowpass(right)


def _soft_clip(value: float) -> float:
    return value / (1.0 + abs(value))


def _hard_clip(value: float) -> float:
    if value >= 1.0:
        return 2.0 / 3.0
    if value <= -1.0:
        return -2.0 / 3.0
    return value - (value ** 3) / 3.0


class _WaveshaperEffect(_Effect):
    def __init__(self, params: dict[str, Any], sample_rate: int) -> None:
        del sample_rate
        self.pre_gain = float(params["pre_gain"])
        self.post_gain = float(params["post_gain"])
        curve = str(params.get("curve", "soft"))
        if curve == "soft":
            self._shape = _soft_clip
        elif curve == "hard":
            self._shape = _hard_clip
        else:
            raise ValueError(f"unsupported waveshaper curve: {curve!r}")

    def process(self, left: float, right: float) -> tuple[float, float]:
        return (
            self._shape(left * self.pre_gain) * self.post_gain,
            self._shape(right * self.pre_gain) * self.post_gain,
        )


class _ChorusEffect(_Effect):
    def __init__(self, params: dict[str, Any], sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.rate_hz = float(params.get("rate_hz", 0.8))
        self.depth = float(params.get("depth_seconds", 0.004)) * sample_rate
        self.base_delay = float(params.get("base_delay_seconds", 0.014)) * sample_rate
        self.mix = float(params.get("mix", 0.5))
        self.stereo_phase = float(params.get("stereo_phase", math.pi / 2.0))
        capacity = int(self.base_delay + self.depth) + 8
        self._buffer_left = [0.0] * capacity
        self._buffer_right = [0.0] * capacity
        self._capacity = capacity
        self._write = 0
        self._phase = 0.0
        self._phase_step = 2.0 * math.pi * self.rate_hz / sample_rate

    def _read(self, buffer: list[float], delay: float) -> float:
        position = self._write - delay
        index = int(math.floor(position))
        fraction = position - index
        first = buffer[index % self._capacity]
        second = buffer[(index + 1) % self._capacity]
        return first + (second - first) * fraction

    def process(self, left: float, right: float) -> tuple[float, float]:
        self._buffer_left[self._write % self._capacity] = left
        self._buffer_right[self._write % self._capacity] = right
        delay_left = self.base_delay + self.depth * 0.5 * (
            1.0 + math.sin(self._phase)
        )
        delay_right = self.base_delay + self.depth * 0.5 * (
            1.0 + math.sin(self._phase + self.stereo_phase)
        )
        wet_left = self._read(self._buffer_left, delay_left)
        wet_right = self._read(self._buffer_right, delay_right)
        self._write += 1
        self._phase += self._phase_step
        if self._phase > 2.0 * math.pi:
            self._phase -= 2.0 * math.pi
        return (
            left + (wet_left - left) * self.mix,
            right + (wet_right - right) * self.mix,
        )


class _PeakEffect(_Effect):
    """RBJ peaking EQ biquad — deterministic resonant boost/cut."""

    def __init__(self, params: dict[str, Any], sample_rate: int) -> None:
        center_hz = float(params["center_hz"])
        q = float(params.get("q", 1.0))
        gain_db = float(params.get("gain_db", 0.0))
        if center_hz <= 0.0 or center_hz >= sample_rate / 2.0:
            raise ValueError("peak center_hz must be inside the Nyquist band")
        if q <= 0.0:
            raise ValueError("peak q must be positive")
        amplitude = 10.0 ** (gain_db / 40.0)
        omega = 2.0 * math.pi * center_hz / sample_rate
        alpha = math.sin(omega) / (2.0 * q)
        b0 = 1.0 + alpha * amplitude
        b1 = -2.0 * math.cos(omega)
        b2 = 1.0 - alpha * amplitude
        a0 = 1.0 + alpha / amplitude
        a1 = b1
        a2 = 1.0 - alpha / amplitude
        self.b0 = b0 / a0
        self.b1 = b1 / a0
        self.b2 = b2 / a0
        self.a1 = a1 / a0
        self.a2 = a2 / a0
        self._state = [[0.0, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, 0.0]]

    def _biquad(self, value: float, state: list[float]) -> float:
        output = (
            self.b0 * value
            + self.b1 * state[0]
            + self.b2 * state[1]
            - self.a1 * state[2]
            - self.a2 * state[3]
        )
        state[1] = state[0]
        state[0] = value
        state[3] = state[2]
        state[2] = output
        return output

    def process(self, left: float, right: float) -> tuple[float, float]:
        return (
            self._biquad(left, self._state[0]),
            self._biquad(right, self._state[1]),
        )


_EFFECTS = {
    "highpass": _HighpassEffect,
    "lowpass": _LowpassEffect,
    "waveshaper": _WaveshaperEffect,
    "chorus": _ChorusEffect,
    "peak": _PeakEffect,
}


@audited_event_free_blocks(silence_safe=False)
class DedicatedFxInstrument(Instrument):
    """DedicatedSfzInstrument 输出经过固定效果链的包装。"""

    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        effects_spec = manifest.get("effects")
        if not isinstance(effects_spec, list) or not effects_spec:
            raise ValueError("dedicated_fx manifest requires a non-empty effects array")
        core_manifest = dict(manifest)
        core_manifest["type"] = "dedicated_sfz"
        self.core = DedicatedSfzInstrument(sample_rate, core_manifest, base_directory)
        self.effects: list[_Effect] = []
        for item in effects_spec:
            if not isinstance(item, dict) or "type" not in item:
                raise ValueError("each effect must be an object with a type")
            kind = str(item["type"])
            if kind not in _EFFECTS:
                raise ValueError(f"unsupported effect type: {kind!r}")
            self.effects.append(_EFFECTS[kind](item, sample_rate))
        self.output_gain = float(manifest.get("output_gain", 1.0))

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        self.core.handle_event(event, tuning)

    def render_frame(self) -> StereoFrame:
        left, right = self.core.render_frame()
        for effect in self.effects:
            left, right = effect.process(float(left), float(right))
        return left * self.output_gain, right * self.output_gain

    @property
    def active_voice_count(self) -> int:
        return self.core.active_voice_count


def create_dedicated_fx(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> DedicatedFxInstrument:
    return DedicatedFxInstrument(sample_rate, manifest, base_directory)
