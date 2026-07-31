"""渲染手风琴固定试听，并核验核心/有限扩展边界与 WAV 信号。"""

import json
import math
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.audio import read_audio_float
from tianlai.dedicated_candidates import (
    dedicated_manifest_sources,
    generate_dedicated_audition_verification,
)


def _segment_metrics(frames, sample_rate: int, start: float, end: float) -> dict:
    begin = round(start * sample_rate)
    finish = min(len(frames), round(end * sample_rate))
    if finish <= begin:
        raise ValueError(f"空试听区间：{start}-{end}")
    segment = frames[begin:finish]
    values = [float(value) for frame in segment for value in frame]
    peak = max(abs(value) for value in values)
    rms = math.sqrt(sum(value * value for value in values) / len(values))
    return {
        "start_seconds": start,
        "end_seconds": end,
        "peak": round(peak, 7),
        "rms": round(rms, 8),
    }


def main() -> None:
    here = Path(__file__).resolve().parent
    manifest_path = here / "乐器.json"
    events_path = ROOT / "examples" / "手风琴_奏法.events.json"
    wav_path = ROOT / "output" / "手风琴_有限高音_candidate.wav"
    report_path = here / "试听核验.json"
    report = generate_dedicated_audition_verification(
        manifest_path,
        events_path,
        wav_path,
        output_path=report_path,
        coverage=[
            "D3-G5 核心音域与最高真实根音 MIDI 79",
            "G#5-A#5 有限扩展及顶端 MIDI 82 长音循环",
            "同一录音层的弱/中/强三档播放响度",
            "奏法:sustain；note-off 配对释放与尾音",
        ],
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    event_document = json.loads(events_path.read_text(encoding="utf-8"))
    audition_notes = [
        int(event["midi_note"])
        for event in event_document["events"]
        if event["type"] == "note_on"
    ]
    if 79 not in audition_notes or 82 not in audition_notes:
        raise ValueError("固定试听必须同时包含最高真实根音 MIDI 79 和扩展顶端 82")
    if max(audition_notes) != int(manifest["note_max"]):
        raise ValueError("固定试听最高音必须等于 manifest.note_max")
    inventory = dedicated_manifest_sources(manifest_path)
    attack_regions = inventory["articulations"]["sustain"]["attack_regions"]
    highest_real_root = max(int(region["root_midi"]) for region in attack_regions)
    core_range = [int(manifest["note_min"]), highest_real_root]
    extension_range = [highest_real_root + 1, int(manifest["note_max"])]
    if core_range != [50, 79] or extension_range != [80, 82]:
        raise ValueError("运行音域策略发生变化，需要重新人工审查")

    sample_rate, frames = read_audio_float(wav_path)
    core_top = _segment_metrics(frames, sample_rate, 4.65, 5.35)
    extension_top = _segment_metrics(frames, sample_rate, 5.5, 7.65)
    tail = _segment_metrics(
        frames,
        sample_rate,
        max(0.0, len(frames) / sample_rate - 0.05),
        len(frames) / sample_rate,
    )
    failures: list[str] = []
    if int(report["clipped_samples"]) != 0 or float(report["peak"]) >= 0.98:
        failures.append("全局试听存在削波或安全余量不足")
    if core_top["rms"] < 0.005:
        failures.append("最高真实根音试听能量过低")
    if extension_top["rms"] < 0.005:
        failures.append("有限扩展顶端试听能量过低")
    if tail["rms"] > 0.00001 or tail["peak"] > 0.00005:
        failures.append("试听尾部未归零")
    if failures:
        raise ValueError("；".join(failures))

    report.update(
        {
            "range_policy": {
                "core_playable_range": core_range,
                "bounded_extension_range": extension_range,
                "legacy_rejected_range": [83, 91],
                "audition_note_ons": audition_notes,
                "highest_real_root_audited": highest_real_root,
                "bounded_extension_top_audited": extension_range[1],
                "extension_top_hold_seconds": 2.15,
            },
            "signal_gates": {
                "global_peak_limit": 0.98,
                "core_top": core_top,
                "bounded_extension_top": extension_top,
                "final_50ms": tail,
            },
            "failures": [],
        }
    )
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(report_path)
    print(
        f"峰值 {report['peak']:.6f}，削波 {report['clipped_samples']}，"
        f"MIDI 82 RMS {extension_top['rms']:.6f}：{report_path}"
    )


if __name__ == "__main__":
    main()
