from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows 必须先把项目本地 DLL 目录暴露给加载器，再间接导入 pyfluidsynth。
from tianlai.soundfont import prepare_fluidsynth_runtime

prepare_fluidsynth_runtime(str(ROOT))

from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament


def main() -> int:
    parser = argparse.ArgumentParser(description="通过天籁事件接口巡检一件乐器")
    parser.add_argument("manifest")
    parser.add_argument("--sample-rate", type=int, default=24000)
    parser.add_argument("--seconds", type=float, default=0.9)
    args = parser.parse_args()

    path = Path(args.manifest).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    instrument = create_instrument(manifest, args.sample_rate, base_directory=str(path.parent))
    try:
        if "fixed_midi_note" in manifest:
            midi = float(manifest["fixed_midi_note"])
        else:
            note_min = float(manifest.get("note_min", 48))
            note_max = float(manifest.get("note_max", 84))
            midi = (note_min + note_max) / 2.0

        tuning = EqualTemperament()
        instrument.handle_event(
            PerformanceEvent(0, 0, "note_on", {"note_id": 1, "midi_note": midi, "velocity": 0.86}),
            tuning,
        )
        note_off_frame = max(1, round(args.sample_rate * args.seconds * 0.62))
        total = max(1, round(args.sample_rate * args.seconds))
        samples: list[tuple[float, float]] = []
        for frame in range(total):
            if frame == note_off_frame and "fixed_midi_note" not in manifest:
                instrument.handle_event(
                    PerformanceEvent(frame, 1, "note_off", {"note_id": 1, "release_velocity": 0.5}),
                    tuning,
                )
            samples.append(instrument.render_frame())

        audio = np.asarray(samples, dtype=np.float64)
        finite = bool(np.isfinite(audio).all())
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        rms = float(np.sqrt(np.mean(audio * audio))) if audio.size else 0.0
        silent = peak <= 1e-6 or rms <= 1e-8
        clipped = peak > 1.000001
        status = "pass" if finite and not silent and not clipped else "fail"
        result = {
            "name": path.parent.name,
            "manifest": str(path.relative_to(ROOT)),
            "type": manifest.get("type"),
            "program": manifest.get("program"),
            "midi_note": midi,
            "sample_rate": args.sample_rate,
            "finite": finite,
            "silent": silent,
            "clipped": clipped,
            "peak": peak,
            "rms": rms,
            "status": status,
        }
        if not finite:
            result["error"] = "输出包含 NaN 或无穷值"
        elif silent:
            result["error"] = "静音或映射缺失"
        elif clipped:
            result["error"] = "浮点输出越过 [-1, 1]"
        print(json.dumps(result, ensure_ascii=False))
        return 0 if status == "pass" else 3
    finally:
        close = getattr(instrument, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    raise SystemExit(main())
