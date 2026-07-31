"""纯 CC0 的 VSCO2-CE 中提琴声部 candidate 入口。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import sys
from typing import Any

from tianlai.events import PerformanceEvent, event_pitch_hz
from tianlai.instrument import Instrument, StereoFrame
from tianlai.sampler import SampleInstrument
from tianlai.tuning import EqualTemperament


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from VSCO2中提琴映射 import (  # noqa: E402
    SOURCE_SUBDIRECTORY,
    build_region_sets,
    expected_sample_paths,
)


_ARTICULATIONS = frozenset(("sustain", "spiccato"))


@dataclass(frozen=True, slots=True)
class _NoteRoute:
    articulation: str
    engine_note_id: int | None


class Vsco2ViolaSectionInstrument(Instrument):
    """Two-articulation viola-section instrument with honest source semantics."""

    def __init__(
        self,
        sample_rate: int,
        manifest: dict[str, Any],
        base_directory: str,
    ) -> None:
        super().__init__(sample_rate)
        self.instrument_name = str(
            manifest.get("instrument_name", "VSCO2-CE viola section")
        )
        self.note_min = float(manifest["note_min"])
        self.note_max = float(manifest["note_max"])
        self.sampled_range = str(manifest["sampled_range"])
        allowed = tuple(str(item) for item in manifest["allowed_articulations"])
        if allowed != ("sustain", "spiccato"):
            raise ValueError(
                "VSCO2 中提琴只允许按顺序声明 sustain、spiccato 两种真实奏法"
            )
        default = str(manifest.get("default_articulation", "sustain"))
        if default not in _ARTICULATIONS:
            raise ValueError("default_articulation must be sustain or spiccato")
        self.articulation = default

        base = Path(base_directory).resolve()
        self.asset_root = (base / str(manifest["asset_root"])).resolve()
        self.source_root = (self.asset_root / SOURCE_SUBDIRECTORY).resolve()
        if not self.source_root.is_dir():
            raise ValueError(
                f"VSCO2-CE 中提琴子集不存在：{self.source_root}。"
                "请按来源.md 安装 Virtual Playing Orchestra wave files。"
            )

        calibration_path = (
            base / str(manifest.get("pitch_calibration", "音准校准.json"))
        ).resolve()
        if not calibration_path.is_file():
            raise ValueError(f"VSCO2 中提琴缺少音准表：{calibration_path}")
        document = json.loads(calibration_path.read_text(encoding="utf-8"))
        if (
            document.get("schema_version") != 1
            or document.get("status") != "passed"
        ):
            raise ValueError("VSCO2 中提琴只接受 schema 1 且 passed 的音准表")
        calibration = document.get("samples")
        if not isinstance(calibration, dict):
            raise ValueError("VSCO2 中提琴音准表 samples 必须是对象")
        expected = set(expected_sample_paths())
        actual = set(calibration)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(
                "VSCO2 中提琴音准表与36采样映射不一致："
                f"missing={missing[:3]}, extra={extra[:3]}"
            )

        regions = build_region_sets(self.asset_root, calibration, manifest)
        for articulation_regions in regions.values():
            for region in articulation_regions:
                path = Path(region["sample"]).resolve()
                try:
                    path.relative_to(self.source_root)
                except ValueError as error:
                    raise ValueError(
                        f"中提琴采样越出纯 CC0 子树：{path}"
                    ) from error

        gain = float(manifest.get("gain", 0.24))
        velocity_exponent = float(manifest.get("velocity_exponent", 0.72))
        articulation_gain = manifest.get("articulation_gain", {})
        if not isinstance(articulation_gain, dict):
            raise ValueError("articulation_gain must be an object")
        shared_cache: dict[Path, Any] = {}
        self.engines = {
            name: SampleInstrument.from_manifest(
                {
                    "regions": articulation_regions,
                    "reference_a4_hz": float(
                        manifest.get("reference_a4_hz", 440.0)
                    ),
                    "gain": gain * float(articulation_gain.get(name, 1.0)),
                    "velocity_exponent": velocity_exponent,
                    "release_seconds": float(
                        manifest.get("sustain_release_seconds", 0.8)
                    ),
                },
                sample_rate,
                base_directory=str(base),
                sample_cache=shared_cache,
            )
            for name, articulation_regions in regions.items()
        }
        self.note_routes: dict[int, _NoteRoute] = {}
        self._auxiliary_note_id = int(
            manifest.get("auxiliary_note_id_base", 1_360_000_000)
        )
        self.expression = 1.0
        self.expression_target = 1.0
        smoothing_seconds = max(
            0.001,
            float(manifest.get("expression_smoothing_seconds", 0.014)),
        )
        self._expression_coefficient = 1.0 - math.exp(
            -1.0 / (smoothing_seconds * sample_rate)
        )

        # Public facts used by audits and collaboration code.  They state what
        # is actually recorded, not what velocity-amplitude scaling simulates.
        self.recorded_velocity_layers = {"sustain": 1, "spiccato": 1}
        self.recorded_round_robins = {"sustain": 1, "spiccato": 2}

    def _next_auxiliary_id(self) -> int:
        self._auxiliary_note_id += 1
        return self._auxiliary_note_id

    def _event_midi(
        self,
        event: PerformanceEvent,
        tuning: EqualTemperament,
    ) -> float:
        if "midi_note" in event.payload:
            return float(event.payload["midi_note"])
        return 69.0 + 12.0 * math.log2(
            event_pitch_hz(event, tuning) / 440.0
        )

    def _check_range(
        self,
        event: PerformanceEvent,
        tuning: EqualTemperament,
    ) -> None:
        note = self._event_midi(event, tuning)
        if not self.note_min <= note <= self.note_max:
            raise ValueError(
                f"{self.instrument_name} note {note:.3f} is outside the sampled "
                f"{self.sampled_range} playable map"
            )

    @staticmethod
    def _with_note_id(
        event: PerformanceEvent,
        note_id: int,
    ) -> PerformanceEvent:
        return PerformanceEvent(
            sample=event.sample,
            sequence=event.sequence,
            type=event.type,
            payload={**event.payload, "note_id": note_id},
        )

    def handle_event(
        self,
        event: PerformanceEvent,
        tuning: EqualTemperament,
    ) -> None:
        if event.type == "articulation":
            name = str(event.payload["name"])
            if name not in _ARTICULATIONS:
                raise ValueError(
                    f"unsupported VSCO2 viola articulation {name!r}; "
                    "choose sustain or spiccato"
                )
            self.articulation = name
            return

        if event.type == "control":
            name = str(event.payload["name"])
            if name == "expression":
                self.expression_target = float(event.payload["value"]) ** 1.35
            elif name == "sustain_pedal":
                self.engines["sustain"].handle_event(event, tuning)
            return

        if event.type == "note_on":
            self._check_range(event, tuning)
            public_note_id = int(event.payload["note_id"])
            if self.articulation == "spiccato":
                engine_note_id = self._next_auxiliary_id()
                self.engines["spiccato"].handle_event(
                    self._with_note_id(event, engine_note_id),
                    tuning,
                )
                # A spiccato is a recorded one-shot: note_off must not turn it
                # into a sustained patch or truncate its natural decay.
                self.note_routes[public_note_id] = _NoteRoute(
                    "spiccato",
                    None,
                )
            else:
                self.engines["sustain"].handle_event(event, tuning)
                self.note_routes[public_note_id] = _NoteRoute(
                    "sustain",
                    public_note_id,
                )
            return

        if event.type == "note_off":
            public_note_id = int(event.payload["note_id"])
            route = self.note_routes.pop(public_note_id, None)
            if route is not None and route.engine_note_id is not None:
                self.engines[route.articulation].handle_event(
                    self._with_note_id(event, route.engine_note_id),
                    tuning,
                )

    def render_frame(self) -> StereoFrame:
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
    return Vsco2ViolaSectionInstrument(
        sample_rate,
        manifest,
        base_directory,
    )
