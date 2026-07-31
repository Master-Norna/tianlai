"""渲染纯 CC0 VSCO2 中提琴声部的固定单项试听。"""

import json
import math
from pathlib import Path
import sys

import numpy as np
import soundfile as sf


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.dedicated_candidates import (  # noqa: E402
    generate_dedicated_audition_verification,
)


def main() -> None:
    here = Path(__file__).resolve().parent
    wav_path = ROOT / "output" / "中提琴_VSCO2_CC0_candidate.wav"
    report_path = here / "试听核验.json"
    report = generate_dedicated_audition_verification(
        here / "乐器.json",
        ROOT / "examples" / "中提琴_奏法.events.json",
        wav_path,
        output_path=report_path,
        coverage=[
            "playable MIDI 48 low, 59 exact root, 76 high and 93 mapped upper extension",
            "sustain is real susvib and crosses one embedded-loop boundary",
            "spiccato only; two consecutive MIDI 62 notes exercise true RR1/RR2",
            "weak/strong velocity demonstrates amplitude response only, not extra layers",
            "per-sample pitch calibration",
            "expression smoothing and release tail",
        ],
    )
    if report["clipped_samples"] != 0 or not 0.01 < report["peak"] < 0.98:
        raise RuntimeError(
            "中提琴试听未通过电平门："
            f"peak={report['peak']}, clipped={report['clipped_samples']}"
        )
    audio, sample_rate = sf.read(
        str(wav_path),
        dtype="float64",
        always_2d=True,
    )
    differences = np.max(np.abs(np.diff(audio, axis=0)), axis=1)
    # The MIDI 59 sustain starts at 1.55 s and crosses the source's first
    # embedded-loop boundary shortly before its 6.0 s release.
    loop_audit = differences[
        round(5.5 * sample_rate) : round(6.05 * sample_rate)
    ]
    tail_frames = max(1, round(0.05 * sample_rate))
    tail = audio[-tail_frames:]
    signal_gates = {
        "maximum_frame_discontinuity": round(
            float(np.max(differences)),
            7,
        ),
        "maximum_loop_audit_frame_discontinuity": round(
            float(np.max(loop_audit)),
            7,
        ),
        "loop_audit_interval_seconds": [5.5, 6.05],
        "tail_50ms_rms": round(
            float(math.sqrt(np.mean(tail * tail))),
            8,
        ),
        "final_frame_peak": round(
            float(np.max(np.abs(audio[-1]))),
            8,
        ),
        "limits": {
            "maximum_frame_discontinuity": 0.1,
            "maximum_loop_audit_frame_discontinuity": 0.05,
            "tail_50ms_rms": 1e-5,
            "final_frame_peak": 1e-5,
        },
    }
    report["signal_gates"] = signal_gates
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    for name, limit in signal_gates["limits"].items():
        if signal_gates[name] > limit:
            raise RuntimeError(
                f"中提琴试听未通过 {name} 门："
                f"{signal_gates[name]} > {limit}"
            )
    print(
        f"峰值 {report['peak']:.6f}，RMS {report['rms']:.6f}，"
        f"削波 {report['clipped_samples']}，"
        "循环审计最大帧差 "
        f"{signal_gates['maximum_loop_audit_frame_discontinuity']:.6f}"
    )


if __name__ == "__main__":
    main()
