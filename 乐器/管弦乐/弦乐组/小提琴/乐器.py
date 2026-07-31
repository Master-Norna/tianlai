"""Virtual Playing Orchestra 独奏小提琴的无界面演奏层。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

from tianlai.events import PerformanceEvent, event_pitch_hz
from tianlai.instrument import Instrument, StereoFrame
from tianlai.sampler import SampleInstrument
from tianlai.sfz import regions_to_manifest
from tianlai.tuning import EqualTemperament
from tianlai.vpo_strings import vpo_regions_to_manifest


# 一把小提琴,两种编制。SOLO 是一位独奏者(NoBudgetOrch 独奏采样),SEC 是第一
# 小提琴声部齐奏(SSO 1st Violins 采样)。二者是同一件乐器的两种用法——独奏细而
# 有个性,齐奏厚而稳——所以合成一个入口,由编制表选,而不是摆成两件乐器让人分辨。
#
# 两个变体的奏法映射**不能简单套同一个文件名模板**:上游对 SOLO 与 SEC 的做法
# 不同。SOLO 的 normal-mod-wheel 是 30 个纯独奏采样,直接可用;SEC 的
# normal-mod-wheel 却是"28 个断奏 + 14 个持续"由 mod wheel(CC1)交叉淡入的复合
# patch——本采样器不读 CC1,会把两层同时播出,断奏音头压在持续音上(音色发浑),
# 两层各自的 tune 又不同而产生拍频(听感像跑调)。故 SEC 的持续音直接用纯持续
# 映射 1st-violin-SEC-sustain.sfz。
_SFZ_BY_VARIANT = {
    "SOLO": {
        "sustain": "1st-violin-SOLO-normal-mod-wheel.sfz",
        "slow_sustain": "1st-violin-SOLO-sustain.sfz",
        "staccato": "1st-violin-SOLO-staccato.sfz",
        "pizzicato": "1st-violin-SOLO-pizzicato.sfz",
        "tremolo": "1st-violin-SOLO-tremolo.sfz",
    },
    "SEC": {
        # SSO 声部只有一套持续采样,故 sustain 与 slow_sustain 同源(与合并前的
        # 第一小提琴组一致);差别交给指挥层的起音/时值处理。
        "sustain": "1st-violin-SEC-sustain.sfz",
        "slow_sustain": "1st-violin-SEC-sustain.sfz",
        "staccato": "1st-violin-SEC-staccato.sfz",
        "pizzicato": "1st-violin-SEC-pizzicato.sfz",
        "tremolo": "1st-violin-SEC-tremolo.sfz",
        # 声部有上游自带的复合 accent patch；适配器会把其中 RR 断奏音头和
        # 延迟持续体拆成两个引擎后逐音同时触发。独奏没有可用的等价物，才需要
        # 下面那套自合成 accent。
        "accent": "1st-violin-SEC-accent.sfz",
    },
}
_VARIANTS = tuple(_SFZ_BY_VARIANT)
_ONE_SHOTS = frozenset(("staccato", "pizzicato"))
_PUBLIC_ARTICULATIONS = frozenset((*_SFZ_BY_VARIANT["SOLO"], "accent"))


def _resolve_variant(manifest: dict[str, Any]) -> str:
    """编制表可用 overrides 把这一次的演奏切到 SEC(声部齐奏)。"""

    variant = str(manifest.get("sample_variant", "SOLO")).upper()
    if variant not in _VARIANTS:
        raise ValueError(
            f"小提琴 sample_variant 只能是 {' / '.join(_VARIANTS)},收到 {variant!r}"
        )
    return variant


def _with_note_id(event: PerformanceEvent, note_id: int) -> PerformanceEvent:
    return PerformanceEvent(
        sample=event.sample,
        sequence=event.sequence,
        type=event.type,
        payload={**event.payload, "note_id": note_id},
    )


@dataclass(frozen=True, slots=True)
class _NoteRoute:
    articulation: str
    sustained_note_id: int | None = None


@dataclass(slots=True)
class _ScheduledRelease:
    engine_name: str
    note_id: int
    remaining_samples: int
    release_seconds: float


class ViolinInstrument(Instrument):
    """A deterministic solo violin assembled from the upstream SFZ mappings."""

    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        sfz_root = asset_root / "Strings"
        if not sfz_root.is_dir():
            raise ValueError(
                f"小提琴 SFZ 不存在：{sfz_root}。请按 来源.md 获取 Virtual Playing Orchestra。"
            )

        shared_cache: dict[Path, Any] = {}
        calibration_path = Path(base_directory) / str(
            manifest.get("pitch_calibration", "音准校准.json")
        )
        calibration: dict[str, Any] = {}
        if calibration_path.is_file():
            calibration_document = json.loads(calibration_path.read_text(encoding="utf-8"))
            calibration = calibration_document.get("samples", {})
            if not isinstance(calibration, dict):
                raise ValueError("violin pitch calibration samples must be an object")
        articulation_gain = manifest.get("articulation_gain", {})
        if not isinstance(articulation_gain, dict):
            raise ValueError("articulation_gain must be an object")

        variant = _resolve_variant(manifest)
        # 两套采样各奏法之间的电平关系不同,需各自配平(SEC 尤其是 accent:上游
        # 声部 accent patch 自带音头,不压会冲出来)。有变体专属表就整表替换。
        variant_articulation_gain = manifest.get("variant_articulation_gain") or {}
        if not isinstance(variant_articulation_gain, dict):
            raise ValueError("variant_articulation_gain must be an object")
        per_variant = variant_articulation_gain.get(variant)
        if isinstance(per_variant, dict):
            articulation_gain = per_variant
        sfz_files = dict(_SFZ_BY_VARIANT[variant])
        self.sample_variant = variant
        # 两套采样的原始电平不同,各自标定到同一峰值;由乐器吸收这个差异,
        # 换变体时编制表不必重新配平(否则同一份 gain_db 会突然过载或变小声)。
        variant_gain = manifest.get("variant_gain") or {}
        if not isinstance(variant_gain, dict):
            raise ValueError("variant_gain must be an object")
        default_gain = float(variant_gain.get(variant, manifest.get("gain", 0.58)))
        effective_release_seconds = float(manifest.get("release_seconds", 0.7))
        if (
            not math.isfinite(effective_release_seconds)
            or effective_release_seconds < 0.0
        ):
            raise ValueError("violin release_seconds must be finite and non-negative")

        self.engines: dict[str, SampleInstrument] = {}
        for name, sfz_name in sfz_files.items():
            sfz_path = sfz_root / sfz_name
            if not sfz_path.is_file():
                raise ValueError(f"小提琴奏法映射不存在：{sfz_path}")
            if variant == "SEC" and name == "accent":
                # VPO's SEC accent SFZ is not one round-robin patch.  It is
                # two RR staccato attacks plus one delayed sustained body per
                # key zone, and both components must sound on every note.
                # Feeding all 42 regions into one SampleInstrument randomly
                # chose just one of the three; short notes that chose the
                # 200 ms-delayed body could finish before making any sound.
                for component, looped in (
                    ("accent_attack", False),
                    ("accent_sustain", True),
                ):
                    regions = vpo_regions_to_manifest(
                        sfz_path,
                        use_embedded_loops=looped,
                        component=component,
                    )
                    if component == "accent_sustain":
                        for region in regions:
                            relative = (
                                Path(region["sample"])
                                .relative_to(asset_root)
                                .as_posix()
                            )
                            measured = calibration.get(relative)
                            if (
                                isinstance(measured, dict)
                                and "detune_cents" in measured
                            ):
                                region["measured_tuning_cents"] = float(
                                    measured["detune_cents"]
                                )
                            region["release_seconds"] = (
                                effective_release_seconds
                            )
                    self.engines[component] = SampleInstrument.from_manifest(
                        {
                            "regions": regions,
                            "reference_a4_hz": 440.0,
                            "gain": default_gain
                            * float(articulation_gain.get("accent", 1.0)),
                            "velocity_exponent": float(
                                manifest.get("velocity_exponent", 0.72)
                            ),
                            "release_seconds": effective_release_seconds,
                        },
                        sample_rate,
                        base_directory=base_directory,
                        sample_cache=shared_cache,
                    )
                continue
            regions = regions_to_manifest(
                sfz_path,
                use_embedded_loops=name not in _ONE_SHOTS,
            )
            if name in ("sustain", "slow_sustain"):
                for region in regions:
                    relative = Path(region["sample"]).relative_to(asset_root).as_posix()
                    measured = calibration.get(relative)
                    if isinstance(measured, dict) and "detune_cents" in measured:
                        region["measured_tuning_cents"] = float(measured["detune_cents"])
            if name not in _ONE_SHOTS:
                for region in regions:
                    region["release_seconds"] = effective_release_seconds
            engine_manifest = {
                "regions": regions,
                "reference_a4_hz": 440.0,
                "gain": default_gain * float(articulation_gain.get(name, 1.0)),
                "velocity_exponent": float(manifest.get("velocity_exponent", 0.72)),
                "release_seconds": effective_release_seconds,
            }
            self.engines[name] = SampleInstrument.from_manifest(
                engine_manifest,
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            )

        # 独奏没有可用的 accent patch,需自合成:accent 早先叠放 staccato 一击 +
        # 完整的慢起弓 sustain,而 VPO 这套 solo 采样起音很慢(高音区更像 swell),
        # sustain 会在 staccato 之后才缓缓升起,形成第二次起音——听感就是"回声/
        # 空灵",高音尤甚。故为 accent 单独造一个 sustain 引擎:从采样中段起播
        # (offset 跳过慢起弓)、用短起音包络,让 body 与 staccato 的瞬态对齐。
        # SEC 的作者复合 accent 已在上面显式拆成 attack+sustain 两层；SOLO
        # 没有等价 patch，才使用 staccato + 快速持续体的项目合成方案。
        self.has_native_accent = (
            "accent_attack" in self.engines
            and "accent_sustain" in self.engines
        )
        if not self.has_native_accent:
            accent_offset_frames = int(
                float(manifest.get("accent_sustain_offset_seconds", 0.28)) * sample_rate
            )
            accent_attack = float(manifest.get("accent_sustain_attack_seconds", 0.02))
            accent_regions = regions_to_manifest(
                sfz_root / sfz_files["sustain"], use_embedded_loops=True
            )
            for region in accent_regions:
                relative = Path(region["sample"]).relative_to(asset_root).as_posix()
                measured = calibration.get(relative)
                if isinstance(measured, dict) and "detune_cents" in measured:
                    region["measured_tuning_cents"] = float(measured["detune_cents"])
                # offset 不能越过采样长度,加载时会再校验;这里给一个保守值。
                region["offset_frames"] = accent_offset_frames
                region["attack_seconds"] = accent_attack
                region["release_seconds"] = effective_release_seconds
            self.engines["accent_sustain"] = SampleInstrument.from_manifest(
                {
                    "regions": accent_regions,
                    "reference_a4_hz": 440.0,
                    "gain": default_gain * float(articulation_gain.get("sustain", 1.0)),
                    "velocity_exponent": float(manifest.get("velocity_exponent", 0.72)),
                    "release_seconds": effective_release_seconds,
                },
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            )

        default_articulation = str(manifest.get("default_articulation", "sustain"))
        if default_articulation not in _PUBLIC_ARTICULATIONS:
            raise ValueError(f"unsupported default violin articulation: {default_articulation!r}")
        self.articulation = default_articulation
        self.note_routes: dict[int, _NoteRoute] = {}
        self._auxiliary_note_id = 1_100_000_000
        self._scheduled_accent_releases: list[_ScheduledRelease] = []
        self._accent_attack_gate_samples = max(
            1,
            round(
                float(manifest.get("accent_attack_gate_seconds", 0.18))
                * sample_rate
            ),
        )
        self._accent_attack_release_seconds = max(
            0.001,
            float(manifest.get("accent_attack_release_seconds", 0.08)),
        )
        self.expression = 1.0
        self.expression_target = 1.0
        smoothing_seconds = max(0.001, float(manifest.get("expression_smoothing_seconds", 0.012)))
        self._expression_coefficient = 1.0 - math.exp(-1.0 / (smoothing_seconds * sample_rate))

    def _next_auxiliary_id(self) -> int:
        self._auxiliary_note_id += 1
        return self._auxiliary_note_id

    def _check_range(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if "midi_note" in event.payload:
            note = float(event.payload["midi_note"])
        else:
            note = 69.0 + 12.0 * math.log2(event_pitch_hz(event, tuning) / 440.0)
        if not 55.0 <= note <= 105.0:
            raise ValueError(f"violin note {note:.3f} is outside the sampled G3-A7 range")

    def _trigger_one_shot(
        self,
        name: str,
        event: PerformanceEvent,
        tuning: EqualTemperament,
        *,
        velocity_scale: float = 1.0,
        schedule_release: bool = False,
    ) -> int:
        auxiliary_id = self._next_auxiliary_id()
        velocity = min(1.0, float(event.payload["velocity"]) * velocity_scale)
        forwarded = PerformanceEvent(
            sample=event.sample,
            sequence=event.sequence,
            type="note_on",
            payload={**event.payload, "note_id": auxiliary_id, "velocity": velocity},
        )
        self.engines[name].handle_event(forwarded, tuning)
        if schedule_release:
            self._scheduled_accent_releases.append(
                _ScheduledRelease(
                    engine_name=name,
                    note_id=auxiliary_id,
                    remaining_samples=self._accent_attack_gate_samples,
                    release_seconds=self._accent_attack_release_seconds,
                )
            )
        return auxiliary_id

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            name = str(event.payload["name"])
            if name not in _PUBLIC_ARTICULATIONS:
                choices = ", ".join(sorted(_PUBLIC_ARTICULATIONS))
                raise ValueError(f"unsupported violin articulation {name!r}; choose from {choices}")
            self.articulation = name
            return

        if event.type == "control":
            if event.payload["name"] == "expression":
                self.expression_target = float(event.payload["value"]) ** 1.35
            return

        if event.type == "note_on":
            self._check_range(event, tuning)
            note_id = int(event.payload["note_id"])
            name = self.articulation
            if name in _ONE_SHOTS:
                self._trigger_one_shot(name, event, tuning)
                self.note_routes[note_id] = _NoteRoute(name)
                return
            if name == "accent" and self.has_native_accent:
                self._trigger_one_shot(
                    "accent_attack",
                    event,
                    tuning,
                    schedule_release=True,
                )
                sustained_id = self._next_auxiliary_id()
                self.engines["accent_sustain"].handle_event(
                    _with_note_id(event, sustained_id),
                    tuning,
                )
                self.note_routes[note_id] = _NoteRoute(name, sustained_id)
                return
            if name == "accent":
                self._trigger_one_shot(
                    "staccato",
                    event,
                    tuning,
                    velocity_scale=1.12,
                    schedule_release=True,
                )
                sustained_id = self._next_auxiliary_id()
                # 用 offset 的 accent_sustain 而非慢起弓 sustain,body 立即到位,
                # 不再形成第二次起音。
                self.engines["accent_sustain"].handle_event(
                    _with_note_id(event, sustained_id), tuning
                )
                self.note_routes[note_id] = _NoteRoute(name, sustained_id)
                return
            self.engines[name].handle_event(event, tuning)
            self.note_routes[note_id] = _NoteRoute(name, note_id)
            return

        if event.type == "note_off":
            note_id = int(event.payload["note_id"])
            route = self.note_routes.pop(note_id, None)
            if route is None or route.sustained_note_id is None:
                return
            if route.articulation == "accent":
                engine_name = "accent_sustain"
            else:
                engine_name = route.articulation
            self.engines[engine_name].handle_event(
                _with_note_id(event, route.sustained_note_id), tuning
            )

    def render_frame(self) -> StereoFrame:
        pending: list[_ScheduledRelease] = []
        for scheduled in self._scheduled_accent_releases:
            scheduled.remaining_samples -= 1
            if scheduled.remaining_samples <= 0:
                self.engines[scheduled.engine_name].release_note(
                    scheduled.note_id,
                    release_seconds=scheduled.release_seconds,
                )
            else:
                pending.append(scheduled)
        self._scheduled_accent_releases = pending
        self.expression += (
            self.expression_target - self.expression
        ) * self._expression_coefficient
        left = 0.0
        right = 0.0
        for engine in self.engines.values():
            engine_left, engine_right = engine.render_frame()
            left += engine_left
            right += engine_right
        return left * self.expression, right * self.expression

    @property
    def active_voice_count(self) -> int:
        return sum(engine.active_voice_count for engine in self.engines.values())


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return ViolinInstrument(sample_rate, manifest, base_directory)
