"""Built-in v0.5 ensemble-calibration fixture documents.

The catalogue is intentionally pure data: importing or querying it performs
no filesystem access, capability discovery, rendering, or audio-source
inspection.  Every public document is a fresh tree containing only strict JSON
values.

The relative-level relations in the rosters are calibration hypotheses.  They
run in ``suggest`` mode and are candidates for listening review, never hard
acceptance assertions.
"""

from __future__ import annotations

import copy
from typing import Any


JsonObject = dict[str, Any]

_VARIANTS = ("typical", "stress")
_FAMILY_IDS = (
    "01-cello-background",
    "02-sitar-visibility",
    "03-orchestral-depth",
    "04-modern-rhythm",
    "05-atmosphere-tail",
    "06-lead-handoff",
)
_FIXTURE_IDS = tuple(
    f"{family}-{variant}-v1"
    for family in _FAMILY_IDS
    for variant in _VARIANTS
)

_FAMILY_SEEDS = {
    "01-cello-background": 500_101,
    "02-sitar-visibility": 500_202,
    "03-orchestral-depth": 500_303,
    "04-modern-rhythm": 500_404,
    "05-atmosphere-tail": 500_505,
    "06-lead-handoff": 500_606,
}

_HUMAN_QUESTIONS = {
    "01-cello-background": (
        "大提琴是否始终是背景氛围，而没有抢走钢琴主奏？",
        "大提琴尾音与原声贝斯叠加时，低中频是否变得浑浊？",
        "原声贝斯的音高与脉冲是否仍清楚可辨？",
    ),
    "02-sitar-visibility": (
        "西塔琴是否无需夸张增益就能稳定站在前景？",
        "抬高西塔琴后，jawari 与共鸣尾音是否变得刺耳？",
        "太鼓是否只强调句尾，而没有遮住旋律？",
    ),
    "03-orchestral-depth": (
        "能否持续跟住长笛主奏，并听清小提琴的独立对位？",
        "弦乐合奏与圆号是否保有后景深度，而没有压平舞台？",
        "织体最密处是否出现刺耳、遮蔽或糊成一层？",
    ),
    "04-modern-rhythm": (
        "底鼓与指弹电贝斯是否彼此分开且都可辨认？",
        "边击军鼓是否清楚，闭合踩镲是否过亮或过近？",
        "强音镲是否只形成强调，而没有瞬间抹掉清音电吉他？",
        "单声道折叠后，主奏与节奏重心是否仍稳定？",
    ),
    "05-atmosphere-tail": (
        "雨声是否被听成环境，而不是电流声或底噪？",
        "电钢琴在铺底、雨境与长尾之间是否仍然清楚？",
        "反向镲是否完整涌起、无截尾或爆点，并自然抵达强调点？",
        "共享厅堂与素材长尾是否洗掉后续乐句？",
    ),
    "06-lead-handoff": (
        "同一旋律换到下一件主奏乐器时，远近或大小是否突然跳变？",
        "西塔琴在接力中是否明显后退，或因补偿而变得过亮？",
        "压力版交接重叠时是否发生遮蔽、声像突变或重心漂移？",
    ),
}


def _note(
    bar: int,
    beat: float,
    duration_beats: float,
    pitch: str,
    *,
    dynamic: str,
    articulation: str | None = None,
) -> JsonObject:
    data: JsonObject = {
        "bar": bar,
        "beat": beat,
        "duration_beats": duration_beats,
        "pitch": pitch,
        "dynamic": dynamic,
    }
    if articulation is not None:
        data["articulation"] = articulation
    return data


def _part(
    part_id: str,
    name: str,
    notes: list[JsonObject],
    *,
    default_dynamic: str,
    default_articulation: str | None = None,
) -> JsonObject:
    data: JsonObject = {
        "id": part_id,
        "name": name,
        "default_dynamic": default_dynamic,
        "notes": notes,
    }
    if default_articulation is not None:
        data["default_articulation"] = default_articulation
    return data


def _score(
    title: str,
    parts: list[JsonObject],
    *,
    tail_seconds: float = 2.5,
) -> JsonObject:
    return {
        "title": title,
        "sample_rate": 48_000,
        "tail_seconds": tail_seconds,
        "tuning": {"temperament": "equal", "a4_hz": 440.0},
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1.0,
                "bpm": 96.0,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": parts,
    }


def _assignment(
    part: str,
    instrument: str,
    function: str,
    prominence: str,
    *,
    gain_db: float,
    azimuth_deg: float,
    distance_m: float,
) -> JsonObject:
    return {
        "part": part,
        "instrument": instrument,
        "gain_db": gain_db,
        "seat": {
            "azimuth_deg": azimuth_deg,
            "distance_m": distance_m,
        },
        "role": {
            "function": function,
            "prominence": prominence,
        },
    }


def _relation(
    subject: str,
    reference: str,
    target_offset_db: float,
    tolerance_db: float,
    *,
    max_suggestion_db: float = 4.0,
) -> JsonObject:
    return {
        "subject": subject,
        "reference": reference,
        "target_offset_db": target_offset_db,
        "tolerance_db": tolerance_db,
        "max_suggestion_db": max_suggestion_db,
    }


def _roster(
    name: str,
    assignments: list[JsonObject],
    relations: list[JsonObject],
) -> JsonObject:
    return {
        "name": name,
        "assignments": assignments,
        "collaboration": {
            "mode": "suggest",
            "analysis": {
                "metric": "overlap_active_rms",
                "window_ms": 400.0,
                "hop_ms": 100.0,
                "gate_dbfs": -60.0,
            },
            "balance_relations": relations,
        },
    }


def _targets(assignments: list[JsonObject]) -> list[JsonObject]:
    return [
        {
            "part_id": assignment["part"],
            "instrument_path": assignment["instrument"],
            "role": copy.deepcopy(assignment["role"]),
        }
        for assignment in assignments
    ]


def _space() -> JsonObject:
    return {
        "name": "协奏校准小厅堂",
        "wet_db": -15.0,
        "room_size": 0.5,
        "predelay_ms": 18.0,
        "damping_hz": 6500.0,
        "highpass_hz": 150.0,
        "reference_distance_m": 3.0,
        "distance_exponent": 0.5,
        "min_send": 0.5,
        "max_send": 1.8,
    }


def _family_01_roster() -> JsonObject:
    assignments = [
        _assignment(
            "piano_lead",
            "键盘乐器/钢琴",
            "lead",
            "foreground",
            gain_db=-2.0,
            azimuth_deg=-12.0,
            distance_m=2.5,
        ),
        _assignment(
            "cello_pad",
            "管弦乐/弦乐组/大提琴",
            "pad",
            "background",
            # 首轮共同活动筛查为 +1.75/+3.15 dB，和 background
            # -10 dB 候选意图方向相反。这里只校准本 fixture 的编制推子；
            # 不修改大提琴全局音量，也不声称长音段落漂移已经解决。
            gain_db=-20.450481,
            azimuth_deg=18.0,
            distance_m=5.0,
        ),
        _assignment(
            "acoustic_bass",
            "低音乐器/原声贝斯",
            "bass",
            "midground",
            gain_db=-4.61295,
            azimuth_deg=2.0,
            distance_m=3.5,
        ),
    ]
    return _roster(
        "协奏校准 01：大提琴背景层",
        assignments,
        [
            _relation("cello_pad", "piano_lead", -10.0, 3.0),
            _relation("acoustic_bass", "piano_lead", -6.0, 3.0),
        ],
    )


def _family_01_score(variant: str) -> JsonObject:
    melody = (
        (
            ("G4", "A4", "D5"),
            ("F4", "C5", "A4"),
            ("E4", "G4", "B4"),
            ("D4", "F4", "A4"),
            ("A4", "B4", "E5"),
            ("G4", "D5", "B4"),
            ("F4", "A4", "C5"),
            ("E4", "G4", "D5"),
        )
        if variant == "typical"
        else (
            ("E4", "G4", "A4", "D5"),
            ("F4", "A4", "C5", "E5"),
            ("G4", "B4", "D5", "F5"),
            ("F4", "A4", "C5", "D5"),
            ("A4", "C5", "E5", "G5"),
            ("G4", "B4", "D5", "E5"),
            ("F4", "A4", "C5", "E5"),
            ("E4", "G4", "B4", "D5"),
        )
    )
    if variant == "typical":
        piano_notes = [
            _note(
                bar,
                beat,
                duration,
                pitch,
                dynamic="mf" if bar < 7 else "f",
            )
            for bar, pitches in enumerate(melody, 1)
            for beat, duration, pitch in zip(
                (1.0, 2.5, 3.0),
                (1.5, 0.5, 2.0),
                pitches,
                strict=True,
            )
        ]
        cello_notes = [
            _note(1, 1.0, 6.0, "C3", dynamic="p", articulation="sustain"),
            _note(4, 1.0, 6.0, "A2", dynamic="p", articulation="sustain"),
            _note(7, 1.0, 6.0, "G2", dynamic="p", articulation="sustain"),
        ]
        bass_notes = [
            _note(bar, 1.0, 2.0, pitch, dynamic="mp", articulation="pizzicato")
            for bar, pitch in ((1, "C2"), (3, "G1"), (5, "A1"), (7, "F1"))
        ]
    else:
        piano_notes = [
            _note(
                bar,
                float(beat),
                1.0,
                pitch,
                dynamic="f" if bar >= 5 else "mf",
            )
            for bar, pitches in enumerate(melody, 1)
            for beat, pitch in enumerate(pitches, 1)
        ]
        piano_notes.extend(
            _note(bar, 1.0, 4.0, pitch, dynamic="mp")
            for bar, pitch in enumerate(
                ("C3", "A2", "F3", "G2", "A2", "E3", "F3", "G2"),
                1,
            )
        )
        cello_notes = [
            _note(bar, 1.0, 9.0, pitch, dynamic="mp", articulation="sustain")
            for bar, pitch in ((1, "C3"), (3, "B2"), (5, "A2"), (7, "G2"))
        ]
        bass_notes = [
            _note(
                bar,
                beat,
                duration,
                pitch,
                dynamic="mf",
                articulation="pizzicato",
            )
            for bar, pitches in enumerate(
                (
                    ("C2", "G1"),
                    ("A1", "E2"),
                    ("F1", "C2"),
                    ("G1", "D2"),
                    ("A1", "E2"),
                    ("E2", "B1"),
                    ("F1", "C2"),
                    ("G1", "D2"),
                ),
                1,
            )
            for beat, duration, pitch in zip(
                (1.0, 3.0),
                (1.75, 1.5),
                pitches,
                strict=True,
            )
        ]
    return _score(
        f"协奏校准 01：大提琴背景层 / {variant}（原创）",
        [
            _part(
                "piano_lead",
                "钢琴主奏",
                piano_notes,
                default_dynamic="mf",
            ),
            _part(
                "cello_pad",
                "大提琴背景铺底",
                cello_notes,
                default_dynamic="p",
                default_articulation="sustain",
            ),
            _part(
                "acoustic_bass",
                "原声贝斯",
                bass_notes,
                default_dynamic="mp",
                default_articulation="pizzicato",
            ),
        ],
    )


def _family_02_roster() -> JsonObject:
    assignments = [
        _assignment(
            "sitar_lead",
            "世界乐器/西塔琴",
            "lead",
            "foreground",
            # 首轮两版仍分别低于尼龙和声约 12/18 dB。先采用 roster
            # 允许的 +12 dB 上限让主奏可听；不通过压低和声 15 dB 来
            # 机械追逐尚未人工批准的 +6 dB 候选目标。
            gain_db=12.0,
            azimuth_deg=-10.0,
            distance_m=2.4,
        ),
        _assignment(
            "nylon_harmony",
            "弹拨乐器/尼龙弦吉他",
            "harmony",
            "midground",
            gain_db=-2.0,
            azimuth_deg=15.0,
            distance_m=3.5,
        ),
        _assignment(
            "taiko_accent",
            "管弦乐/打击乐组/太鼓",
            "accent",
            "midground",
            gain_db=-7.0,
            azimuth_deg=3.0,
            distance_m=5.0,
        ),
    ]
    return _roster(
        "协奏校准 02：西塔琴可见度",
        assignments,
        [
            _relation(
                "sitar_lead",
                "nylon_harmony",
                6.0,
                3.0,
                max_suggestion_db=6.0,
            )
        ],
    )


def _family_02_score(variant: str) -> JsonObject:
    scale = ("G4", "A4", "C5", "D5", "E5", "D5", "C5", "A4")
    if variant == "typical":
        sitar_notes = [
            _note(
                bar,
                beat,
                duration,
                scale[(bar * 2 + index) % len(scale)],
                dynamic="mf",
            )
            for bar in range(1, 9)
            for index, (beat, duration) in enumerate(
                ((1.0, 1.5), (2.5, 0.5), (3.0, 2.0))
            )
        ]
        guitar_notes = [
            _note(
                bar,
                1.0,
                4.0,
                pitch,
                dynamic="p",
                articulation="normal",
            )
            for bar, dyad in (
                (1, ("E3", "B3")),
                (3, ("D3", "A3")),
                (5, ("C3", "G3")),
                (7, ("D3", "A3")),
            )
            for pitch in dyad
        ]
        taiko_notes = [
            _note(bar, 4.0, 0.5, pitch, dynamic="mf")
            for bar, pitch in ((2, "C4"), (4, "D4"), (6, "C4"), (8, "D4"))
        ]
    else:
        sitar_notes = [
            _note(
                bar,
                1.0 + index * 0.5,
                0.5,
                scale[(bar + index) % len(scale)],
                dynamic="f" if index in (0, 6) else "mf",
            )
            for bar in range(1, 9)
            for index in range(8)
        ]
        harmony_pattern = ("E3", "G3", "B3", "D4")
        guitar_notes = [
            _note(
                bar,
                float(beat),
                1.0,
                harmony_pattern[(bar + beat) % len(harmony_pattern)],
                dynamic="mf",
                articulation="normal",
            )
            for bar in range(1, 9)
            for beat in range(1, 5)
        ]
        taiko_notes = [
            _note(
                bar,
                beat,
                0.5,
                pitch,
                dynamic="f" if pitch == "C4" else "mf",
            )
            for bar in range(1, 9)
            for beat, pitch in ((1.0, "C4"), (3.0, "C#4"), (4.5, "D4"))
        ]
    return _score(
        f"协奏校准 02：西塔琴可见度 / {variant}（原创）",
        [
            _part(
                "sitar_lead",
                "西塔琴主奏",
                sitar_notes,
                default_dynamic="mf",
            ),
            _part(
                "nylon_harmony",
                "尼龙弦吉他和声",
                guitar_notes,
                default_dynamic="p",
                default_articulation="normal",
            ),
            _part(
                "taiko_accent",
                "太鼓强调",
                taiko_notes,
                default_dynamic="mf",
            ),
        ],
    )


def _family_03_roster() -> JsonObject:
    assignments = [
        _assignment(
            "flute_lead",
            "管弦乐/木管组/长笛",
            "lead",
            "foreground",
            gain_db=-1.0,
            azimuth_deg=-18.0,
            distance_m=2.5,
        ),
        _assignment(
            "violin_counter",
            "管弦乐/弦乐组/小提琴",
            "countermelody",
            "midground",
            gain_db=-3.157131,
            azimuth_deg=20.0,
            distance_m=3.2,
        ),
        _assignment(
            "strings_pad",
            "管弦乐/弦乐组/弦乐合奏",
            "pad",
            "background",
            # 算术交点约需 +8.33 dB，但它极易改变 -60 dB gate 的
            # 活动窗集合。第二轮只走一次报告允许的 +4 dB 有界小步。
            gain_db=-6.0,
            azimuth_deg=-26.0,
            distance_m=6.0,
        ),
        _assignment(
            "horn_harmony",
            "管弦乐/铜管组/圆号",
            "harmony",
            "background",
            # 典型/压力版相差约 11 dB，静态推子没有共同解；这里只走
            # +4 dB 有界小步，保留力度层漂移给后续 A/B 与动态处理。
            gain_db=-4.0,
            azimuth_deg=28.0,
            distance_m=6.5,
        ),
    ]
    return _roster(
        "协奏校准 03：管弦深度",
        assignments,
        [
            _relation("violin_counter", "flute_lead", -4.0, 3.0),
            _relation("strings_pad", "flute_lead", -10.0, 3.0),
            _relation("horn_harmony", "flute_lead", -8.0, 3.0),
        ],
    )


def _family_03_score(variant: str) -> JsonObject:
    flute_line = (
        ("C5", "E5"),
        ("D5", "G5"),
        ("E5", "A5"),
        ("D5", "F5"),
        ("G5", "E5"),
        ("A5", "F5"),
        ("G5", "D5"),
        ("E5", "C5"),
    )
    if variant == "typical":
        flute_notes = [
            _note(
                bar,
                beat,
                2.0,
                pitch,
                dynamic="mf" if bar < 7 else "f",
                articulation="sustain",
            )
            for bar, pitches in enumerate(flute_line, 1)
            for beat, pitch in zip((1.0, 3.0), pitches, strict=True)
        ]
        violin_notes = [
            _note(
                bar,
                2.0,
                4.0,
                pitch,
                dynamic="mp",
                articulation="sustain",
            )
            for bar, pitch in ((1, "G3"), (3, "A3"), (5, "B3"), (7, "D4"))
        ]
        strings_notes = [
            _note(
                bar,
                1.0,
                6.0,
                pitch,
                dynamic="p",
                articulation="sustain",
            )
            for bar, chord in (
                (1, ("C3", "G3")),
                (3, ("A2", "E3")),
                (5, ("F3", "C4")),
                (7, ("G2", "D3")),
            )
            for pitch in chord
        ]
        horn_notes = [
            _note(
                bar,
                1.0,
                4.0,
                pitch,
                dynamic="p",
                articulation="sustain",
            )
            for bar, pitch in ((1, "G2"), (3, "A2"), (5, "C3"), (7, "B2"))
        ]
    else:
        flute_notes = [
            _note(
                bar,
                float(beat),
                1.0,
                pitches[(beat - 1) % 2],
                dynamic="f" if beat == 1 else "mf",
                articulation="legato",
            )
            for bar, pitches in enumerate(flute_line, 1)
            for beat in range(1, 5)
        ]
        violin_echo = ("C5", "D5", "E5", "G5")
        violin_notes = [
            _note(
                bar,
                1.5 + index,
                1.25,
                violin_echo[(bar + index) % len(violin_echo)],
                dynamic="mf",
                articulation="sustain",
            )
            for bar in range(1, 9)
            for index in range(3)
        ]
        strings_notes = [
            _note(
                bar,
                1.0,
                9.0,
                pitch,
                dynamic="mp",
                articulation="sustain",
            )
            for bar, chord in (
                (1, ("C4", "E4", "G4")),
                (3, ("A3", "C4", "E4")),
                (5, ("F3", "A3", "C4")),
                (7, ("G3", "B3", "D4")),
            )
            for pitch in chord
        ]
        horn_notes = [
            _note(
                bar,
                1.0,
                9.0,
                pitch,
                dynamic="mp",
                articulation="sustain",
            )
            for bar, pitch in ((1, "A3"), (3, "B3"), (5, "C4"), (7, "G3"))
        ]
    return _score(
        f"协奏校准 03：管弦深度 / {variant}（原创）",
        [
            _part(
                "flute_lead",
                "长笛主奏",
                flute_notes,
                default_dynamic="mf",
                default_articulation="sustain",
            ),
            _part(
                "violin_counter",
                "小提琴对位",
                violin_notes,
                default_dynamic="mp",
                default_articulation="sustain",
            ),
            _part(
                "strings_pad",
                "弦乐合奏铺底",
                strings_notes,
                default_dynamic="p",
                default_articulation="sustain",
            ),
            _part(
                "horn_harmony",
                "圆号和声",
                horn_notes,
                default_dynamic="p",
                default_articulation="sustain",
            ),
        ],
    )


def _family_04_roster() -> JsonObject:
    assignments = [
        _assignment(
            "guitar_lead",
            "弹拨乐器/清音电吉他",
            "lead",
            "foreground",
            gain_db=-1.0,
            azimuth_deg=-18.0,
            distance_m=2.4,
        ),
        _assignment(
            "finger_bass",
            "低音乐器/指弹电贝斯",
            "bass",
            "midground",
            gain_db=-9.11787,
            azimuth_deg=8.0,
            distance_m=3.3,
        ),
        _assignment(
            "warm_pad",
            "电子乐器/温暖铺底",
            "pad",
            "background",
            gain_db=2.040791,
            azimuth_deg=24.0,
            distance_m=6.0,
        ),
        _assignment(
            "kick_rhythm",
            "现代鼓组/底鼓",
            "rhythm",
            "midground",
            gain_db=-8.0,
            azimuth_deg=0.0,
            distance_m=4.0,
        ),
        _assignment(
            "rim_snare_rhythm",
            "现代鼓组/边击军鼓",
            "rhythm",
            "midground",
            gain_db=-9.0,
            azimuth_deg=5.0,
            distance_m=4.2,
        ),
        _assignment(
            "closed_hihat_rhythm",
            "现代鼓组/闭合踩镲",
            "rhythm",
            "midground",
            gain_db=-13.0,
            azimuth_deg=15.0,
            distance_m=4.0,
        ),
        _assignment(
            "crash_accent",
            "现代鼓组/强音镲",
            "accent",
            "midground",
            gain_db=-13.0,
            azimuth_deg=-25.0,
            distance_m=5.5,
        ),
    ]
    return _roster(
        "协奏校准 04：现代节奏与强调",
        assignments,
        [
            _relation("warm_pad", "guitar_lead", -10.0, 3.0),
            _relation("finger_bass", "guitar_lead", -5.0, 3.0),
        ],
    )


def _family_04_score(variant: str) -> JsonObject:
    guitar_pairs = (
        ("G3", "B3"),
        ("A3", "D4"),
        ("B3", "E4"),
        ("A3", "C4"),
        ("D4", "F#4"),
        ("C4", "E4"),
        ("B3", "D4"),
        ("G3", "G4"),
    )
    if variant == "typical":
        guitar_notes = [
            _note(
                bar,
                beat,
                2.0,
                pitch,
                dynamic="mf",
                articulation="normal",
            )
            for bar, pitches in enumerate(guitar_pairs, 1)
            for beat, pitch in zip((1.0, 3.0), pitches, strict=True)
        ]
        bass_notes = [
            _note(
                bar,
                1.0,
                2.5,
                pitch,
                dynamic="mp",
                articulation="normal",
            )
            for bar, pitch in enumerate(
                ("G1", "D2", "E2", "C2", "D2", "C2", "B1", "G1"),
                1,
            )
        ]
        pad_notes = [
            _note(bar, 1.0, 8.0, pitch, dynamic="p")
            for bar, chord in (
                (1, ("G3", "B3", "D4")),
                (5, ("C4", "E4", "G4")),
            )
            for pitch in chord
        ]
        kick_beats = (1.0, 3.0)
        rim_beats = (2.0, 4.0)
        hat_step = 0.5
        crash_locations = ((1, 1.0), (5, 1.0))
    else:
        guitar_notes = [
            _note(
                bar,
                float(beat),
                1.0,
                pitches[(beat - 1) % 2],
                dynamic="f" if beat in (1, 4) else "mf",
                articulation="normal",
            )
            for bar, pitches in enumerate(guitar_pairs, 1)
            for beat in range(1, 5)
        ]
        bass_notes = [
            _note(
                bar,
                beat,
                1.5,
                pitch,
                dynamic="mf",
                articulation="normal",
            )
            for bar, pitches in enumerate(
                (
                    ("G1", "D2"),
                    ("D2", "A1"),
                    ("E2", "B1"),
                    ("C2", "G1"),
                    ("D2", "A1"),
                    ("C2", "G1"),
                    ("B1", "F#2"),
                    ("G1", "D2"),
                ),
                1,
            )
            for beat, pitch in zip((1.0, 3.0), pitches, strict=True)
        ]
        pad_notes = [
            _note(bar, 1.0, 9.0, pitch, dynamic="mp")
            for bar, chord in (
                (1, ("G3", "B3", "D4")),
                (3, ("E3", "G3", "B3")),
                (5, ("C4", "E4", "G4")),
                (7, ("D4", "F#4", "A4")),
            )
            for pitch in chord
        ]
        kick_beats = (1.0, 2.5, 3.0, 4.5)
        rim_beats = (2.0, 3.5, 4.0)
        hat_step = 0.25
        crash_locations = (
            (1, 1.0),
            (3, 1.0),
            (5, 1.0),
            (7, 1.0),
            (8, 3.0),
        )
    kick_notes = [
        _note(bar, beat, 0.25, "C4", dynamic="f", articulation="hit")
        for bar in range(1, 9)
        for beat in kick_beats
    ]
    rim_notes = [
        _note(bar, beat, 0.25, "D4", dynamic="mf", articulation="hit")
        for bar in range(1, 9)
        for beat in rim_beats
    ]
    hat_count = round(4.0 / hat_step)
    hat_notes = [
        _note(
            bar,
            1.0 + index * hat_step,
            hat_step,
            "F#2",
            dynamic="mp" if index % 2 == 0 else "p",
            articulation="hit",
        )
        for bar in range(1, 9)
        for index in range(hat_count)
    ]
    crash_notes = [
        _note(bar, beat, 0.5, "F#4", dynamic="f", articulation="hit")
        for bar, beat in crash_locations
    ]
    return _score(
        f"协奏校准 04：现代节奏与强调 / {variant}（原创）",
        [
            _part(
                "guitar_lead",
                "清音电吉他主奏",
                guitar_notes,
                default_dynamic="mf",
                default_articulation="normal",
            ),
            _part(
                "finger_bass",
                "指弹电贝斯",
                bass_notes,
                default_dynamic="mp",
                default_articulation="normal",
            ),
            _part(
                "warm_pad",
                "温暖铺底",
                pad_notes,
                default_dynamic="p",
            ),
            _part(
                "kick_rhythm",
                "底鼓节奏",
                kick_notes,
                default_dynamic="mf",
                default_articulation="hit",
            ),
            _part(
                "rim_snare_rhythm",
                "边击军鼓节奏",
                rim_notes,
                default_dynamic="mf",
                default_articulation="hit",
            ),
            _part(
                "closed_hihat_rhythm",
                "闭合踩镲节奏",
                hat_notes,
                default_dynamic="mp",
                default_articulation="hit",
            ),
            _part(
                "crash_accent",
                "强音镲强调",
                crash_notes,
                default_dynamic="f",
                default_articulation="hit",
            ),
        ],
        # 温暖铺底的实测 release 是 2.9 s；额外 0.2 s 避免收尾被截断。
        tail_seconds=3.1,
    )


def _family_05_roster() -> JsonObject:
    assignments = [
        _assignment(
            "electric_piano_lead",
            "键盘乐器/电钢琴",
            "lead",
            "foreground",
            gain_db=-2.0,
            azimuth_deg=-12.0,
            distance_m=2.5,
        ),
        _assignment(
            "warm_pad",
            "电子乐器/温暖铺底",
            "pad",
            "background",
            # 该程序铺底首轮在两版中低于电钢琴约 28/33 dB。这里把
            # fixture 推子移到两版候选区间的中点，是否过近仍由听审裁决。
            gain_db=9.598508,
            azimuth_deg=18.0,
            distance_m=5.5,
        ),
        _assignment(
            "rain_ambience",
            "环境与拟音/雨境合成氛围",
            "ambience",
            "background",
            gain_db=-3.0,
            azimuth_deg=0.0,
            distance_m=7.0,
        ),
        _assignment(
            "reverse_cymbal_effect",
            "管弦乐/打击乐组/反向镲",
            "effect",
            "midground",
            gain_db=-12.0,
            azimuth_deg=-25.0,
            distance_m=4.5,
        ),
    ]
    return _roster(
        "协奏校准 05：氛围与长尾",
        assignments,
        [
            _relation("warm_pad", "electric_piano_lead", -10.0, 3.0),
            _relation("rain_ambience", "electric_piano_lead", -18.0, 5.0),
        ],
    )


def _family_05_score(variant: str) -> JsonObject:
    if variant == "typical":
        piano_notes = [
            _note(bar, beat, duration, pitch, dynamic="mf", articulation="normal")
            for bar, beat, duration, pitch in (
                (1, 1.0, 2.0, "E4"),
                (1, 3.0, 2.0, "B4"),
                (3, 1.0, 2.0, "D4"),
                (3, 3.0, 2.0, "A4"),
                (5, 1.0, 2.0, "G4"),
                (5, 3.0, 2.0, "D5"),
                (7, 1.0, 2.0, "F4"),
                (8, 3.0, 2.0, "E4"),
            )
        ]
        pad_chords = (
            (1, ("E3", "B3")),
            (5, ("C4", "G4")),
        )
        pad_dynamic = "p"
        rain_dynamic = "mp"
        reverse_pitch = "C4"
        reverse_duration = 28.0
        emphasis_location = (7, 2.28)
    else:
        piano_notes = [
            _note(
                bar,
                float(beat),
                1.0,
                pitch,
                dynamic="f" if beat == 1 else "mf",
                articulation="normal",
            )
            for bar, pitches in enumerate(
                (
                    ("E4", "G4", "B4", "D5"),
                    ("D4", "F4", "A4", "C5"),
                    ("C4", "E4", "G4", "B4"),
                    ("D4", "F4", "A4", "E5"),
                    ("G4", "B4", "D5", "F5"),
                    ("F4", "A4", "C5", "E5"),
                    ("E4", "G4", "B4", "D5"),
                    ("D4", "F4", "A4", "E5"),
                ),
                1,
            )
            for beat, pitch in enumerate(pitches, 1)
        ]
        pad_chords = (
            (1, ("E3", "G3", "B3")),
            (3, ("D3", "F3", "A3")),
            (5, ("C4", "E4", "G4")),
            (7, ("D4", "F4", "A4")),
        )
        pad_dynamic = "mp"
        rain_dynamic = "mf"
        reverse_pitch = "D4"
        reverse_duration = 37.0
        emphasis_location = (9, 2.44)
    # ``ff`` is deliberately unique inside this part.  The timing contract
    # locates this notated emphasis and compares its fixed-seed planned onset
    # with the reverse-cymbal sample's natural (not note_off) endpoint.
    piano_notes.append(
        _note(
            emphasis_location[0],
            emphasis_location[1],
            0.75,
            "A4",
            dynamic="ff",
            articulation="normal",
        )
    )
    pad_notes = [
        _note(bar, 1.0, 9.0 if variant == "stress" else 8.0, pitch, dynamic=pad_dynamic)
        for bar, chord in pad_chords
        for pitch in chord
    ]
    rain_notes = [
        # 34 beats remains active for 20.1875 s after the conductor's 0.95
        # default duration shape, then the 3.1 s score tail covers release.
        _note(1, 1.0, 34.0, "C4", dynamic=rain_dynamic)
    ]
    reverse_notes = [
        # 28/37 beats leave >0.8 s / >1.0 s safety after the conductor's
        # default 0.95 duration shape for the 15.807710/20.906757 s swells.
        _note(
            1,
            1.0,
            reverse_duration,
            reverse_pitch,
            dynamic="mf" if variant == "typical" else "f",
        )
    ]
    return _score(
        f"协奏校准 05：氛围与长尾 / {variant}（原创）",
        [
            _part(
                "electric_piano_lead",
                "电钢琴主奏",
                piano_notes,
                default_dynamic="mf",
                default_articulation="normal",
            ),
            _part(
                "warm_pad",
                "温暖铺底",
                pad_notes,
                default_dynamic="p",
            ),
            _part(
                "rain_ambience",
                "雨境",
                rain_notes,
                default_dynamic="pp",
            ),
            _part(
                "reverse_cymbal_effect",
                "反向镲效果",
                reverse_notes,
                default_dynamic="mf",
            ),
        ],
        tail_seconds=3.1,
    )


def _family_06_roster() -> JsonObject:
    assignments = [
        _assignment(
            "piano_lead",
            "键盘乐器/钢琴",
            "lead",
            "foreground",
            gain_db=-2.0,
            azimuth_deg=0.0,
            distance_m=2.5,
        ),
        _assignment(
            "sitar_lead",
            "世界乐器/西塔琴",
            "lead",
            "foreground",
            gain_db=4.0,
            azimuth_deg=0.0,
            distance_m=2.5,
        ),
        _assignment(
            "flute_lead",
            "管弦乐/木管组/长笛",
            "lead",
            "foreground",
            gain_db=-2.0,
            azimuth_deg=0.0,
            distance_m=2.5,
        ),
        _assignment(
            "guitar_lead",
            "弹拨乐器/清音电吉他",
            "lead",
            "foreground",
            gain_db=-1.0,
            azimuth_deg=0.0,
            distance_m=2.5,
        ),
    ]
    return _roster(
        "协奏校准 06：前景交接标尺",
        assignments,
        [
            _relation("sitar_lead", "piano_lead", 0.0, 3.0),
            _relation("flute_lead", "sitar_lead", 0.0, 3.0),
            _relation("guitar_lead", "flute_lead", 0.0, 3.0),
        ],
    )


def _handoff_notes(start_bar: int, *, overlap: bool, is_last: bool) -> list[JsonObject]:
    pitches = ("D4", "F4", "A4", "E4", "G4", "F4", "E4", "D4")
    if overlap:
        locations = (
            (start_bar, 1.0),
            (start_bar, 2.0),
            (start_bar, 3.0),
            (start_bar, 4.0),
            (start_bar + 1, 1.0),
            (start_bar + 1, 2.0),
            (start_bar + 1, 3.0),
            (start_bar + 1, 4.0),
        )
        durations = (
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0 if is_last else 4.0,
        )
    else:
        # Fit the complete motif into the first half of each bar.  The final
        # note ends around beat 3, leaving two notated beats before the next
        # player enters; release tails cannot accidentally satisfy the
        # analyzer's 0.5 s handoff-overlap threshold.
        locations = (
            (start_bar, 1.0),
            (start_bar, 1.5),
            (start_bar, 2.0),
            (start_bar, 2.5),
            (start_bar + 1, 1.0),
            (start_bar + 1, 1.5),
            (start_bar + 1, 2.0),
            (start_bar + 1, 2.5),
        )
        durations = (0.5,) * len(locations)
    return [
        _note(
            bar,
            beat,
            durations[index],
            pitch,
            dynamic="mf",
        )
        for index, ((bar, beat), pitch) in enumerate(
            zip(locations, pitches, strict=True)
        )
    ]


def _family_06_score(variant: str) -> JsonObject:
    overlap = variant == "stress"
    specifications = (
        ("piano_lead", "钢琴主奏接力", 1, None),
        ("sitar_lead", "西塔琴主奏接力", 3, None),
        ("flute_lead", "长笛主奏接力", 5, "sustain"),
        ("guitar_lead", "清音电吉他主奏接力", 7, "normal"),
    )
    parts = []
    for index, (part_id, name, start_bar, articulation) in enumerate(
        specifications
    ):
        notes = _handoff_notes(
            start_bar,
            overlap=overlap,
            is_last=index == len(specifications) - 1,
        )
        if articulation is not None:
            for note in notes:
                note["articulation"] = articulation
        parts.append(
            _part(
                part_id,
                name,
                notes,
                default_dynamic="mf",
                default_articulation=articulation,
            )
        )
    return _score(
        f"协奏校准 06：前景交接标尺 / {variant}（原创）",
        parts,
    )


def _documents_for(family: str, variant: str) -> tuple[JsonObject, JsonObject]:
    if family == "01-cello-background":
        return _family_01_score(variant), _family_01_roster()
    if family == "02-sitar-visibility":
        return _family_02_score(variant), _family_02_roster()
    if family == "03-orchestral-depth":
        return _family_03_score(variant), _family_03_roster()
    if family == "04-modern-rhythm":
        return _family_04_score(variant), _family_04_roster()
    if family == "05-atmosphere-tail":
        return _family_05_score(variant), _family_05_roster()
    if family == "06-lead-handoff":
        return _family_06_score(variant), _family_06_roster()
    raise AssertionError(f"unhandled fixture family: {family}")


def fixture_ids() -> tuple[str, ...]:
    """Return the stable family-major, typical-before-stress fixture order."""

    return _FIXTURE_IDS


def build_fixture_documents(fixture_id: str) -> JsonObject:
    """Build one independent strict-JSON fixture document.

    ``KeyError`` is raised for an unknown ID so a misspelled calibration target
    can never silently select another fixture.
    """

    try:
        position = _FIXTURE_IDS.index(fixture_id)
    except ValueError as error:
        raise KeyError(f"unknown collaboration fixture: {fixture_id!r}") from error
    family = _FAMILY_IDS[position // len(_VARIANTS)]
    variant = _VARIANTS[position % len(_VARIANTS)]
    score, roster = _documents_for(family, variant)
    assignments = roster["assignments"]
    document: JsonObject = {
        "fixture_id": fixture_id,
        "family": family,
        "variant": variant,
        "profile_version": 1,
        "seed": _FAMILY_SEEDS[family],
        "space": _space(),
        "master_gain_db": -3.0,
        "normalize_peak_db": None,
        "balance_target_status": "candidate",
        "balance_calibration": {
            "round": 2,
            "status": "machine_candidate_pending_human",
            "scope": "fixture_roster_only",
            "metric": "overlap_active_rms",
            "analyzer_modified_audio": False,
        },
        "score": score,
        "roster": roster,
        "targets": _targets(assignments),
        "human_questions": list(_HUMAN_QUESTIONS[family]),
    }
    if family == "05-atmosphere-tail":
        document["timing_contracts"] = [
            {
                "contract_id": "reverse-cymbal-natural-end-v1",
                "kind": "natural_sample_end_to_marked_note_on",
                "source_part_id": "reverse_cymbal_effect",
                "source_anchor": "natural_sample_end",
                "source_duration_seconds_by_midi_note": {
                    "60": 15.807710,
                    "62": 20.906757,
                },
                "target_part_id": "electric_piano_lead",
                "target_anchor": "planned_note_on",
                "target_marker": {"dynamic": "ff"},
                "tolerance_ms": 50.0,
            }
        ]
    # Builders already create fresh values.  Deep-copying at the public
    # boundary also protects that contract if a future family uses constants.
    return copy.deepcopy(document)


def all_fixture_documents() -> tuple[JsonObject, ...]:
    """Return all fixture documents in :func:`fixture_ids` order."""

    return tuple(build_fixture_documents(fixture_id) for fixture_id in _FIXTURE_IDS)


__all__ = (
    "all_fixture_documents",
    "build_fixture_documents",
    "fixture_ids",
)
