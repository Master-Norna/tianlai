"""生成编钟标准全音域试听、机器报告及正鼓/侧鼓对照试听。"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Iterator


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.audio import write_wav_pcm24
from tianlai.audition_protocol import (
    PROTOCOL_ID,
    build_full_range_audition,
)
from tianlai.dedicated_candidates import (
    generate_dedicated_audition_verification,
)
from tianlai.events import PerformanceEvent
from tianlai.instrument import StereoFrame, create_instrument
from tianlai.tuning import EqualTemperament


HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "乐器.json"
EVENTS = (
    ROOT
    / "examples"
    / "全音域上行"
    / "世界乐器"
    / "编钟_全音域上行.events.json"
)
OUTPUT = ROOT / "output" / "编钟候选"
FULL_RANGE_WAV = OUTPUT / "01_编钟_正鼓全音域上行.wav"
AB_WAV = OUTPUT / "02_编钟_正鼓与侧鼓对照.wav"
REPORT = HERE / "试听核验.json"
SAMPLE_RATE = 48_000
AB_SEQUENCE = (
    ("zhenggu", 48, 5.0),
    ("cegu", 48, 5.0),
    ("zhenggu", 67, 3.8),
    ("cegu", 67, 3.8),
    ("zhenggu", 86, 2.8),
    ("cegu", 86, 2.8),
)


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _ab_frames(manifest: dict) -> Iterator[StereoFrame]:
    """Render independent clips so an earlier long bell cannot mask its pair."""

    tuning = EqualTemperament(440.0)
    # low, middle, high; each pair is 正鼓 then 侧鼓.
    fade_frames = round(0.45 * SAMPLE_RATE)
    silence_frames = round(0.65 * SAMPLE_RATE)

    for articulation, midi_note, duration_seconds in AB_SEQUENCE:
        instrument = create_instrument(
            manifest,
            SAMPLE_RATE,
            base_directory=str(HERE),
        )
        try:
            instrument.handle_event(
                PerformanceEvent(
                    sample=0,
                    sequence=0,
                    type="articulation",
                    payload={"name": articulation},
                ),
                tuning,
            )
            instrument.handle_event(
                PerformanceEvent(
                    sample=0,
                    sequence=1,
                    type="note_on",
                    payload={
                        "note_id": 1,
                        "midi_note": midi_note,
                        "velocity": 0.72,
                    },
                ),
                tuning,
            )
            total_frames = round(duration_seconds * SAMPLE_RATE)
            fade_start = total_frames - fade_frames
            for frame_index in range(total_frames):
                left, right = instrument.render_frame()
                if frame_index >= fade_start:
                    phase = (frame_index - fade_start) / max(1, fade_frames - 1)
                    fade = 0.5 * (1.0 + math.cos(math.pi * phase))
                    left *= fade
                    right *= fade
                yield left, right
        finally:
            close = getattr(instrument, "close", None)
            if callable(close):
                close()
        for _ in range(silence_frames):
            yield 0.0, 0.0


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    plan = build_full_range_audition(
        MANIFEST,
        instrument_root=ROOT / "乐器",
    )
    _write_json(EVENTS, plan.document)
    OUTPUT.mkdir(parents=True, exist_ok=True)

    report = generate_dedicated_audition_verification(
        MANIFEST,
        EVENTS,
        FULL_RANGE_WAV,
        output_path=REPORT,
        coverage=plan.coverage,
    )
    report["audition_profile"] = "ascending-scale"
    report["audition_protocol"] = PROTOCOL_ID
    report["review_evidence_status"] = (
        "pending_new_review_old_hash_bound_reviews_do_not_apply"
    )

    ab_frames = write_wav_pcm24(AB_WAV, _ab_frames(manifest), SAMPLE_RATE)
    report["auxiliary_auditions"] = [
        {
            "wav": AB_WAV.relative_to(ROOT).as_posix(),
            "wav_persistence": "temporary",
            "wav_sha256": hashlib.sha256(AB_WAV.read_bytes()).hexdigest(),
            "frame_count": ab_frames,
            "duration_seconds": round(ab_frames / SAMPLE_RATE, 6),
            "sequence": [
                {
                    "articulation": articulation,
                    "midi_note": midi_note,
                    "isolated_clip_seconds": duration,
                }
                for articulation, midi_note, duration in AB_SEQUENCE
            ],
            "construction": (
                "每次敲击使用独立引擎实例；片尾 0.45s 仅为对照文件做淡出，"
                "避免前一枚钟的长尾掩蔽下一枚，不代表正式渲染的 note_off 行为"
            ),
        }
    ]
    _write_json(REPORT, report)
    print(
        f"全音域：{FULL_RANGE_WAV}，"
        f"peak={report['peak']:.6f}，clips={report['clipped_samples']}"
    )
    print(
        f"击位对照：{AB_WAV}，"
        f"{ab_frames / SAMPLE_RATE:.2f}s；"
        "顺序为低/中/高音区，每区正鼓后侧鼓"
    )


if __name__ == "__main__":
    main()
