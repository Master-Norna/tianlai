"""合奏空间层:一个共享的算法厅堂。

架构里"合奏与空间层"原本只做了声像(左右)与距离增益,没有真正的空间——
所有乐器都是消声室般的干声硬拼。干声本身没错(分轨要可复算),但成品少了
厅堂:近距离录音的弓毛/贴弦质感裸露("沙哑"),音与音之间硬切、没有余韵
("不悠扬")。这里补上厅堂:早反射把原始质感揉开,尾混填满换音间隙。

**为什么是算法混响而不是卷积。** 卷积混响要一条厅堂脉冲响应(IR)采样,而
高质量 IR 大多带许可限制;本项目对音源坚持"明确公开许可",算法混响是纯数学、
无任何采样、无许可负担,正合此原则。

**为什么放在合奏而不动分轨。** 混响是全体共处一厅的整体效果,只作用于合奏
总线;分轨仍是全干、逐轨可复算——"改一轨只重渲一轨、绝不惊动别轨"的性质
不受影响。混响本身是确定性 DSP,同输入必同输出。

实现是 Freeverb 式 Schroeder 混响:并联反馈梳状滤波器给密度,串联全通滤波器
做扩散,右声道梳状延时错开做立体声去相关。阻尼(尾部变暖)与高通(不让低频
糊底)在 FFT 域一次成型。没有 scipy,梳状/全通的一阶反馈用"残差类 reshape"
向量化:y[n]=x[n]+g·y[n-D] 里,下标同余 D 的子序列各自是一阶递归,把信号
reshape 成 (行, D) 后每一列独立,沿行推进即可,避免 1600 万样本的逐样本循环。
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Integral, Real
from typing import Any

import numpy as np


# Freeverb 的经典延时(采样点 @44.1kHz)。换采样率时按比例缩放并就近取整。
_COMB_44K = (1116, 1188, 1277, 1356, 1422, 1491, 1557, 1617)
_ALLPASS_44K = (556, 441, 341, 225)
_STEREO_SPREAD_44K = 23

# 配置边界同时是稳定性契约。它们不是“推荐预设”：边界内仍允许从极干的小房间
# 到很湿的特效空间；边界外则可能令反馈失稳、指数溢出，或在无意中申请巨量延时。
_MIN_WET_DB = -120.0
_MAX_WET_DB = 12.0
_MAX_PREDELAY_MS = 2_000.0
_MAX_FILTER_HZ = 192_000.0       # 对应项目允许的最高 384 kHz 采样率
_MAX_REFERENCE_DISTANCE_M = 1_000.0
_MAX_DISTANCE_EXPONENT = 4.0
_MAX_SEND = 8.0

# 低采样率下不拒绝原本合法的厅堂预设，而是在真正渲染时按 Nyquist 做确定性保护。
# 高通留到 90%、低通留到 98%，即使两者的声明值都高于 Nyquist 也仍保留有限通带。
_HIGHPASS_NYQUIST_RATIO = 0.90
_DAMPING_NYQUIST_RATIO = 0.98


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"space {label} 必须是有限数值")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"space {label} 必须是有限数值")
    return number


def _validated_sample_rate(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError("space sample_rate 必须是 8000..384000 的整数")
    sample_rate = int(value)
    if not 8_000 <= sample_rate <= 384_000:
        raise ValueError("space sample_rate 必须是 8000..384000 的整数")
    return sample_rate


@dataclass(frozen=True)
class SpaceConfig:
    """一个厅堂的参数。默认是"小厅堂",微湿,只添真实感、不冲淡。"""

    name: str = "小厅堂"
    wet_db: float = -15.0          # 湿声整体电平(相对送入的干声),越高越"湿"
    room_size: float = 0.5         # 0..1,映射到梳状反馈量,决定混响时长(RT60)
    predelay_ms: float = 18.0      # 前置延时:湿声比干声晚到,拉开直达声与厅堂
    damping_hz: float = 6500.0     # 尾部低通:压掉高频让混响变暖,不刺
    highpass_hz: float = 150.0     # 湿声高通:低频不进混响,底不糊
    reference_distance_m: float = 3.0   # 该距离处送出量为标称 1.0
    distance_exponent: float = 0.5      # 送出量随距离 (d/ref)^e,越远越湿(纵深)
    min_send: float = 0.5
    max_send: float = 1.8

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("space name 必须是非空字符串")

        numeric_fields = (
            "wet_db",
            "room_size",
            "predelay_ms",
            "damping_hz",
            "highpass_hz",
            "reference_distance_m",
            "distance_exponent",
            "min_send",
            "max_send",
        )
        for field_name in numeric_fields:
            value = _finite_number(getattr(self, field_name), field_name)
            # 统一成 float，让 JSON 审计结果不因调用方传 1 还是 1.0 而漂移。
            object.__setattr__(self, field_name, value)

        if not _MIN_WET_DB <= self.wet_db <= _MAX_WET_DB:
            raise ValueError(
                f"space wet_db 必须在 {_MIN_WET_DB:g}..{_MAX_WET_DB:g} 之间"
            )
        if not 0.0 <= self.room_size <= 1.0:
            raise ValueError("space room_size 必须在 0..1 之间")
        if not 0.0 <= self.predelay_ms <= _MAX_PREDELAY_MS:
            raise ValueError(
                f"space predelay_ms 必须在 0..{_MAX_PREDELAY_MS:g} 之间"
            )
        for field_name in ("damping_hz", "highpass_hz"):
            value = getattr(self, field_name)
            if not 0.0 <= value <= _MAX_FILTER_HZ:
                raise ValueError(
                    f"space {field_name} 必须在 0..{_MAX_FILTER_HZ:g} 之间"
                )
        if (
            self.highpass_hz > 0.0
            and self.damping_hz > 0.0
            and self.highpass_hz >= self.damping_hz
        ):
            raise ValueError("space highpass_hz 必须低于 damping_hz")
        if not 0.0 < self.reference_distance_m <= _MAX_REFERENCE_DISTANCE_M:
            raise ValueError(
                "space reference_distance_m 必须大于 0"
                f"且不超过 {_MAX_REFERENCE_DISTANCE_M:g}"
            )
        if not 0.0 <= self.distance_exponent <= _MAX_DISTANCE_EXPONENT:
            raise ValueError(
                f"space distance_exponent 必须在 0..{_MAX_DISTANCE_EXPONENT:g} 之间"
            )
        if not 0.0 <= self.min_send <= _MAX_SEND:
            raise ValueError(f"space min_send 必须在 0..{_MAX_SEND:g} 之间")
        if not 0.0 <= self.max_send <= _MAX_SEND:
            raise ValueError(f"space max_send 必须在 0..{_MAX_SEND:g} 之间")
        if self.min_send > self.max_send:
            raise ValueError("space min_send 不得大于 max_send")

    @classmethod
    def from_dict(cls, raw: Any) -> "SpaceConfig | None":
        """从作品配置构造。``None`` 或 ``{"enabled": false}`` 表示不加厅堂。"""
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ValueError("space 配置必须是对象、null 或 {\"enabled\": false}")
        if raw.get("enabled") is False:
            return None
        data = dict(raw)
        data.pop("enabled", None)
        data.pop("说明", None)   # 允许清单里按项目惯例写中文注释,不当作参数
        allowed = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"未知的 space 参数:{sorted(unknown)}")
        return cls(**data)

    @property
    def feedback(self) -> float:
        # Freeverb 的映射:roomsize 0..1 → 0.70..0.98。0.5 → 0.84,RT≈1.2s。
        return self.room_size * 0.28 + 0.70

    def send_scale(self, distance_m: float) -> float:
        """按座位距离决定送入厅堂的量:越远越湿,制造纵深。"""
        distance = _finite_number(distance_m, "distance_m")
        if distance <= 0.0:
            raise ValueError("space distance_m 必须大于 0")
        ratio = max(distance, 0.1) / self.reference_distance_m
        scale = ratio ** self.distance_exponent
        return float(min(self.max_send, max(self.min_send, scale)))

    def effective_filter_frequencies(self, sample_rate: int) -> tuple[float, float]:
        """返回该采样率下实际使用的 ``(highpass, damping)`` 截止频率。

        清单保存的是与采样率无关的声明值。渲染时按固定 Nyquist 比例截断，使
        默认 6500 Hz 阻尼在 8 kHz 这类合法低采样率下仍安全可用，而不是突然
        拒绝整份作品。公开这个换算入口，审计工具无需复制隐含规则即可复算。
        """

        sr = _validated_sample_rate(sample_rate)
        nyquist = sr * 0.5
        highpass = (
            min(self.highpass_hz, nyquist * _HIGHPASS_NYQUIST_RATIO)
            if self.highpass_hz > 0.0
            else 0.0
        )
        damping = (
            min(self.damping_hz, nyquist * _DAMPING_NYQUIST_RATIO)
            if self.damping_hz > 0.0
            else 0.0
        )
        return float(highpass), float(damping)

    def tail_seconds(self, sample_rate: int) -> float:
        """Conservative time needed for the algorithmic hall to decay by 60 dB.

        ``render_reverb`` intentionally returns exactly as many frames as it
        receives.  The ensemble bus therefore has to append silence before
        calling it; otherwise the feedback combs are cut off at the score's
        final frame.  The longest comb controls the late decay.  Predelay and
        the serial all-pass diffusion delays are added explicitly so the
        estimate remains safe at every supported sample rate.
        """

        sr = _validated_sample_rate(sample_rate)
        scale = sr / 44100.0
        longest_comb = max(
            max(1, round(delay * scale)) + round(_STEREO_SPREAD_44K * scale)
            for delay in _COMB_44K
        )
        diffusion = sum(max(1, round(delay * scale)) for delay in _ALLPASS_44K)
        # One feedback traversal multiplies the late amplitude by ``feedback``.
        # 0.001 is -60 dB in amplitude.  ``feedback`` is strictly inside (0, 1)
        # by construction, so the logarithmic estimate is finite.
        traversals = math.ceil(math.log(0.001) / math.log(self.feedback))
        frames = (
            round(self.predelay_ms * 1.0e-3 * sr)
            + traversals * longest_comb
            + diffusion
        )
        return float(frames / sr)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "wet_db": self.wet_db,
            "room_size": self.room_size,
            "predelay_ms": self.predelay_ms,
            "damping_hz": self.damping_hz,
            "highpass_hz": self.highpass_hz,
            "reference_distance_m": self.reference_distance_m,
            "distance_exponent": self.distance_exponent,
            "min_send": self.min_send,
            "max_send": self.max_send,
        }


def _feedback_along_rows(a: np.ndarray, g: float) -> np.ndarray:
    """就地计算 a[k] += g·a[k-1](沿 0 轴)。

    a 是 (行, 列) 的二维数组,每一列是一条独立的一阶反馈递归。行数 = N/D,
    对梳状(D≈1200~1800)只有约一万次迭代,每次是整行的向量加法。
    """
    for k in range(1, a.shape[0]):
        a[k] += g * a[k - 1]
    return a


def _reshape_columns(x: np.ndarray, delay: int) -> tuple[np.ndarray, int]:
    """把一维信号补零成 (行, delay) 的二维;返回数组与补零前长度。"""
    n = x.size
    rows = (n + delay - 1) // delay
    padded = np.zeros(rows * delay, dtype=np.float64)
    padded[:n] = x
    return padded.reshape(rows, delay), n


def _comb(x: np.ndarray, delay: int, g: float) -> np.ndarray:
    """反馈梳状:y[n] = x[n] + g·y[n-D]。"""
    grid, n = _reshape_columns(x, delay)
    _feedback_along_rows(grid, g)
    return grid.reshape(-1)[:n]


def _allpass(x: np.ndarray, delay: int, g: float) -> np.ndarray:
    """Schroeder 全通:y[n] = -g·x[n] + x[n-D] + g·y[n-D],做扩散不改幅频。"""
    grid, n = _reshape_columns(x, delay)
    x_prev = np.zeros_like(grid)
    x_prev[1:] = grid[:-1]              # x[n-D]:上移一行(同列即差 D 个样本)
    u = -g * grid + x_prev
    _feedback_along_rows(u, g)
    return u.reshape(-1)[:n]


def _spectral_shape(x: np.ndarray, sr: int, highpass_hz: float, lowpass_hz: float) -> np.ndarray:
    """FFT 域的高通+低通成型(零相位,只改幅度)。给混响尾去底糊、变暖。"""
    n = x.size
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    response = np.ones_like(freqs)
    if highpass_hz > 0.0:
        # 二阶巴特沃斯幅频:|H| = 1/sqrt(1+(fc/f)^4)
        with np.errstate(divide="ignore"):
            response *= 1.0 / np.sqrt(1.0 + (highpass_hz / np.maximum(freqs, 1e-9)) ** 4)
    if lowpass_hz > 0.0:
        response *= 1.0 / np.sqrt(1.0 + (freqs / lowpass_hz) ** 4)
    return np.fft.irfft(spectrum * response, n)


def _reverb_bank(send: np.ndarray, sr: int, cfg: SpaceConfig, offset: int) -> np.ndarray:
    """一条声道:并联梳状求和,再串联全通扩散。offset 用于左右去相关。"""
    scale = sr / 44100.0
    g = cfg.feedback
    acc = np.zeros(send.size, dtype=np.float64)
    for d in _COMB_44K:
        delay = max(1, round(d * scale) + offset)
        acc += _comb(send, delay, g)
    acc /= len(_COMB_44K)
    for d in _ALLPASS_44K:
        delay = max(1, round(d * scale) + offset)
        acc = _allpass(acc, delay, 0.5)
    return acc


def _render_reverb_pair(
    send: np.ndarray,
    sr: int,
    cfg: SpaceConfig,
    left_offset: int,
    right_offset: int,
) -> tuple[np.ndarray, np.ndarray]:
    """用两套延时银行渲染一条信号，并完成公共的滤波、前置延时和增益。"""

    highpass_hz, damping_hz = cfg.effective_filter_frequencies(sr)
    wet_l = _reverb_bank(send, sr, cfg, left_offset)
    wet_r = _reverb_bank(send, sr, cfg, right_offset)
    wet_l = _spectral_shape(wet_l, sr, highpass_hz, damping_hz)
    wet_r = _spectral_shape(wet_r, sr, highpass_hz, damping_hz)

    predelay = max(0, round(cfg.predelay_ms * 1e-3 * sr))
    if predelay:
        wet_l = np.concatenate([np.zeros(predelay), wet_l])[: send.size]
        wet_r = np.concatenate([np.zeros(predelay), wet_r])[: send.size]

    wet_gain = 10.0 ** (cfg.wet_db / 20.0)
    return wet_l * wet_gain, wet_r * wet_gain


def render_reverb(send: np.ndarray, sr: int, cfg: SpaceConfig) -> tuple[np.ndarray, np.ndarray]:
    """把单声道送出总线渲成立体声湿声(左、右)。

    这是原有兼容入口。它的延时、滤波、前置延时和增益顺序保持不变；已有调用方
    无需迁移。真正的立体声总线应改用 :func:`render_reverb_stereo`，否则在调用
    前把左右相加会令纯反相信号消失。
    """

    sr = _validated_sample_rate(sr)
    spread = round(_STEREO_SPREAD_44K * sr / 44100.0)
    return _render_reverb_pair(send, sr, cfg, 0, spread)


def _validated_audio_channel(value: Any, label: str) -> np.ndarray:
    """把一个只读音频通道校验为一维 float64 视图或副本。"""

    raw = np.asarray(value)
    if np.iscomplexobj(raw):
        raise ValueError(f"space {label} 必须是一维实数音频")
    try:
        channel = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"space {label} 必须是一维实数音频") from exc
    if channel.ndim != 1:
        raise ValueError(f"space {label} 必须是一维实数音频")
    if not np.isfinite(channel).all():
        raise ValueError(f"space {label} 不得包含 NaN 或无穷值")
    return channel


def render_reverb_stereo(
    left: np.ndarray,
    right: np.ndarray,
    sr: int,
    cfg: SpaceConfig,
) -> tuple[np.ndarray, np.ndarray]:
    """把真正的立体声送出总线渲染为相位安全的立体声湿声。

    输入先线性分解成 ``mid=(L+R)/2`` 与 ``side=(L-R)/2``。mid 沿用原单声道
    厅堂的两套延时银行；side 使用位于同一最大延时边界内的另一对偏移银行。
    解码时左声道加 side、右声道减 side。这样同相信号与原入口完全兼容，而
    ``L=x, R=-x`` 的纯反相信号也会进入 side 厅堂，不会在送入前被相消。

    函数不修改输入，返回长度始终与输入相同。算法只含线性加减、稳定滤波与
    常数增益；没有 ``abs``、限幅或依据信号内容改变增益的非线性步骤。
    """

    sr = _validated_sample_rate(sr)
    left_channel = _validated_audio_channel(left, "left")
    right_channel = _validated_audio_channel(right, "right")
    if left_channel.shape != right_channel.shape:
        raise ValueError("space stereo 左右通道长度必须一致")
    if left_channel.size == 0:
        empty = np.zeros(0, dtype=np.float64)
        return empty.copy(), empty

    # 先乘 0.5 再相加，避免两个很大的有限输入在 L+R 中间步骤溢出。
    mid = 0.5 * left_channel + 0.5 * right_channel
    side = 0.5 * left_channel - 0.5 * right_channel

    spread = round(_STEREO_SPREAD_44K * sr / 44100.0)
    wet_mid_l, wet_mid_r = _render_reverb_pair(mid, sr, cfg, 0, spread)

    # 另取 spread 内的两个确定性偏移；最大延时不超过原右声道银行，因此
    # SpaceConfig.tail_seconds() 的既有保守估计仍然成立。即使 side 恰为零也
    # 仍走同一线性路径，不依据信号内容切换算法。
    side_left_offset = max(1, round(spread / 3.0))
    side_right_offset = min(
        spread - 1,
        max(side_left_offset + 1, round(2.0 * spread / 3.0)),
    )
    wet_side_l, wet_side_r = _render_reverb_pair(
        side,
        sr,
        cfg,
        side_left_offset,
        side_right_offset,
    )
    wet_l = wet_mid_l + wet_side_l
    wet_r = wet_mid_r - wet_side_r

    if not np.isfinite(wet_l).all() or not np.isfinite(wet_r).all():
        raise ValueError("space stereo 渲染结果超出有限数值范围")
    return wet_l, wet_r
