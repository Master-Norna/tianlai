"""端到端音准巡检:渲染每件乐器声明音域内的每个半音并实测音高。

这不依赖任何"哪些采样是噪声层"的代理判断,而是直接回答演奏者关心的
问题:按下这个键,发出来的音准不准。做法是用真实事件接口逐音渲染,
再对渲染结果做谐波约束 FFT,与十二平均律目标比较。

无固定音高入口(打击、拟音、按键位选变体的乐器)自动跳过并计数。

**读结果时的三条注意**,否则很容易把正常现象当成缺陷:

1. **真钢琴的伸展调律不是走音**。三角钢琴高音本就调高、低音调低(Railsback
   曲线),本项目钢琴高音区 +27～+38 音分属正常,拉平反而失真。
2. **非谐乐器的频谱峰不等于听感音高**。管钟、定音鼓、锣一类的听感音高来自
   若干分音构成的虚拟基频,用频谱峰对比平均律没有意义,这里报出的偏差不能
   作为它们的走音判据。
3. **短促或带调制的音测不准**。拨奏、颤弓、带揉音的合唱在窗内音高本就在变,
   报出的数字反映的是测量歧义而非乐器缺陷;工具对置信度不足的音会如实标为
   "测不到明确基频",这与"没出声"是两回事。
4. **当前每个键位只抽一个确定性变体**。Round Robin 与随机变体尚未穷举;
   本轮结果能发现键位映射错误,但不能代替后续的逐奏法、逐力度、全变体覆盖。

用法:

    .\\.venv\\Scripts\\python.exe tools\\巡检全音域音准.py [--tolerance 25]
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.analysis import WidePitchAssessment, analyze_signal_wide_pitch
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament


INSTRUMENT_ROOT = ROOT / "乐器"
TEST_TOOL = "测试工具/参考振荡器"
NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# 这些入口按键位选择变体或本无固定音高,逐半音测音准没有意义。
NON_PITCHED_TYPES = {"procedural_sfx", "reversed_cymbal"}
WIDE_SEARCH_CENTS = 1_800.0
ANALYSIS_START_SECONDS = 0.06


def note_name(midi: int) -> str:
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def measure(
    buffer: object, sample_rate: int, expected_hz: float
) -> WidePitchAssessment:
    """Run the shared octave-aware acceptance estimator.

    The old implementation searched only ±150 cents and therefore could not
    possibly discover an instrument mapped one octave high or low.  Keep the
    range explicit here so a future default change cannot narrow this audit.
    """

    try:
        total_frames = len(buffer)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError("pitch-audit buffer must expose its frame count") from error
    available_frames = total_frames - round(ANALYSIS_START_SECONDS * sample_rate)

    return analyze_signal_wide_pitch(
        buffer,
        sample_rate,
        expected_hz,
        start_seconds=ANALYSIS_START_SECONDS,
        # ``render_note`` already renders a frequency-dependent long window for
        # low notes.  Passing its full usable length is essential: relying on
        # the analyzer's 32768-frame default discards several seconds and can
        # turn a low string's settling transient into a false tuning failure.
        maximum_frames=available_frames,
        search_cents=WIDE_SEARCH_CENTS,
    )


def classify_pitch(
    assessment: WidePitchAssessment, tolerance_cents: float
) -> str:
    """Return the mutually exclusive report bucket for one measurement."""

    if not assessment.clear_pitch:
        return "no_clear_pitch"
    if assessment.nearest_octave_error not in (None, 0):
        return "octave_error"
    if assessment.within_tolerance(tolerance_cents):
        return "within_tolerance"
    return "out_of_tolerance"


def manifest_midi_notes(
    manifest: dict,
    *,
    inferred_range: tuple[int, int] | None = None,
) -> tuple[int, ...]:
    """Enumerate declared playable integer notes without filling range holes."""

    raw_ranges = manifest.get("playable_ranges")
    ranges: list[tuple[float, float]] = []
    if raw_ranges is not None:
        if not isinstance(raw_ranges, list) or not raw_ranges:
            raise ValueError("playable_ranges must be a non-empty array")
        previous_high: float | None = None
        for index, raw_span in enumerate(raw_ranges):
            if not isinstance(raw_span, list) or len(raw_span) != 2:
                raise ValueError(
                    f"playable_ranges[{index}] must be a [minimum, maximum] pair"
                )
            if any(isinstance(value, bool) for value in raw_span):
                raise ValueError(f"playable_ranges[{index}] notes must be numbers")
            try:
                low, high = float(raw_span[0]), float(raw_span[1])
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"playable_ranges[{index}] notes must be numbers"
                ) from error
            if not math.isfinite(low) or not math.isfinite(high):
                raise ValueError(f"playable_ranges[{index}] notes must be finite")
            if not 0.0 <= low <= high <= 127.0:
                raise ValueError(
                    f"playable_ranges[{index}] must satisfy "
                    "0 <= minimum <= maximum <= 127"
                )
            if previous_high is not None and low <= previous_high:
                raise ValueError(
                    "playable_ranges must be ordered, non-overlapping spans"
                )
            ranges.append((low, high))
            previous_high = high
    else:
        has_minimum = "note_min" in manifest
        has_maximum = "note_max" in manifest
        if has_minimum != has_maximum:
            raise ValueError("note_min and note_max must be declared together")
        if has_minimum:
            low = float(manifest["note_min"])
            high = float(manifest["note_max"])
            if not 0.0 <= low <= high <= 127.0:
                raise ValueError("note_min/note_max form an invalid MIDI range")
            ranges.append((low, high))
        elif inferred_range is not None:
            ranges.append((float(inferred_range[0]), float(inferred_range[1])))

    return tuple(
        note
        for low, high in ranges
        for note in range(math.ceil(low), math.floor(high) + 1)
    )


def _infer_range(manifest: dict, base: Path) -> tuple[int, int] | None:
    """从实例实际加载的采样区推断可演奏音域。"""

    try:
        instrument = create_instrument(manifest, 48000, base_directory=str(base))
    except Exception:  # noqa: BLE001
        return None
    lows: list[float] = []
    highs: list[float] = []
    seen: set[int] = set()

    def visit(value, depth: int) -> None:
        if depth > 8 or id(value) in seen:
            return
        seen.add(id(value))
        regions = getattr(value, "regions", None)
        if regions is not None:
            try:
                for region in regions:
                    key_min = getattr(region, "key_min", None)
                    key_max = getattr(region, "key_max", None)
                    if key_min is not None and key_max is not None:
                        lows.append(float(key_min))
                        highs.append(float(key_max))
                        continue
                    # 有些后端(如钢琴)不写键位范围,靠根音频率选区;
                    # 这时用根音本身圈定可演奏范围。
                    root_hz = getattr(region, "root_pitch_hz", None)
                    if root_hz and root_hz > 0:
                        midi = 69.0 + 12.0 * math.log2(float(root_hz) / 440.0)
                        lows.append(midi)
                        highs.append(midi)
            except TypeError:
                pass
        if isinstance(value, dict):
            for item in value.values():
                visit(item, depth + 1)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                visit(item, depth + 1)
            return
        slots: list[str] = []
        for klass in type(value).__mro__:
            slots.extend(getattr(klass, "__slots__", ()) or ())
        for name in list(getattr(value, "__dict__", {})) + slots:
            if name.startswith("__"):
                continue
            try:
                visit(getattr(value, name), depth + 1)
            except AttributeError:
                continue

    visit(instrument, 0)
    close = getattr(instrument, "close", None)
    if callable(close):
        close()
    if not lows:
        return None
    low = int(math.ceil(min(lows)))
    high = int(math.floor(max(highs)))
    # 噪声/释音层常被映射到音域之外的极端键位,收缩到常规乐器范围。
    low = max(low, 12)
    high = min(high, 108)
    return (low, high) if high >= low else None


def render_note(manifest: dict, base: Path, midi: int, sample_rate: int):
    import numpy as np

    instrument = create_instrument(manifest, sample_rate, base_directory=str(base))
    tuning = EqualTemperament(440.0)
    try:
        sequence = 0
        raw_articulation = manifest.get("calibration_articulation")
        if raw_articulation is not None:
            articulation = str(raw_articulation).strip()
            if not articulation:
                raise ValueError("calibration_articulation must not be empty")
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    sequence,
                    "articulation",
                    {"name": articulation},
                ),
                tuning,
            )
            sequence += 1
        instrument.handle_event(
            PerformanceEvent(
                0,
                sequence,
                "note_on",
                {"note_id": 1, "midi_note": midi, "velocity": 0.75},
            ),
            tuning,
        )
        expected_hz = 440.0 * 2.0 ** ((midi - 69.0) / 12.0)
        # 窗长必须让一个 FFT 频点小于约 6 音分,否则低音区的测量下限会被
        # 当成走音。6 音分对应 df ≈ f × 0.00347,故 T ≈ 288 / f。
        seconds = max(0.5, min(6.0, 288.0 / expected_hz))
        frames = int(
            sample_rate * (seconds + ANALYSIS_START_SECONDS)
        )
        buffer = np.empty(frames, dtype=np.float64)
        for index in range(frames):
            left, right = instrument.render_frame()
            buffer[index] = 0.5 * (float(left) + float(right))
        return buffer, expected_hz
    finally:
        close = getattr(instrument, "close", None)
        if callable(close):
            close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tolerance", type=float, default=25.0,
                        help="判定走音的音分阈值")
    parser.add_argument("--only", default="", help="只测名字包含该串的乐器")
    arguments = parser.parse_args()

    sample_rate = 48000
    offenders: dict[str, list[tuple[int, float]]] = {}
    octave_errors: dict[str, list[tuple[int, float, int]]] = {}
    skipped: list[str] = []
    unclear: dict[str, list[tuple[int, str]]] = {}
    render_errors: dict[str, list[tuple[int, str]]] = {}
    range_errors: dict[str, str] = {}
    tested = 0
    notes_tested = 0

    for manifest_path in sorted(INSTRUMENT_ROOT.rglob("乐器.json")):
        directory = manifest_path.parent
        relative = directory.relative_to(INSTRUMENT_ROOT).as_posix()
        if relative == TEST_TOOL:
            continue
        if arguments.only and arguments.only not in relative:
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        kind = str(manifest.get("type", ""))
        calibration = directory / "音准校准.json"
        not_applicable = False
        if calibration.is_file():
            document = json.loads(calibration.read_text(encoding="utf-8"))
            not_applicable = document.get("applicable") is False
        if kind in NON_PITCHED_TYPES or not_applicable:
            skipped.append(relative)
            continue
        inferred: tuple[int, int] | None = None
        if (
            "playable_ranges" not in manifest
            and "note_min" not in manifest
            and "note_max" not in manifest
        ):
            # 未声明音域的入口(如钢琴)从实际加载的采样区推断,
            # 免得因为清单没写就漏测。
            inferred = _infer_range(manifest, directory)
            if inferred is None:
                skipped.append(relative)
                continue
        try:
            midi_notes = manifest_midi_notes(
                manifest,
                inferred_range=inferred,
            )
        except ValueError as error:
            range_errors[relative] = str(error)
            continue
        if not midi_notes:
            skipped.append(relative)
            continue

        tested += 1
        for midi in midi_notes:
            try:
                buffer, expected_hz = render_note(
                    manifest, directory, midi, sample_rate
                )
            except Exception as error:  # noqa: BLE001
                render_errors.setdefault(relative, []).append(
                    (midi, f"{type(error).__name__}: {error}")
                )
                continue
            notes_tested += 1
            assessment = measure(buffer, sample_rate, expected_hz)
            classification = classify_pitch(assessment, arguments.tolerance)
            if classification == "no_clear_pitch":
                unclear.setdefault(relative, []).append(
                    (midi, assessment.reason)
                )
            elif classification == "octave_error":
                assert assessment.detune_cents is not None
                assert assessment.nearest_octave_error is not None
                octave_errors.setdefault(relative, []).append(
                    (
                        midi,
                        assessment.detune_cents,
                        assessment.nearest_octave_error,
                    )
                )
            elif classification == "out_of_tolerance":
                assert assessment.detune_cents is not None
                cents = assessment.detune_cents
                offenders.setdefault(relative, []).append((midi, cents))

    print(f"实测 {tested} 件乐器、{notes_tested} 个半音"
          f"(阈值 {arguments.tolerance:.0f} 音分)\n")
    print(
        "变体覆盖:当前每个键位只抽取 1 个确定性变体;"
        "尚未穷举 Round Robin / 随机变体。后续全变体巡检应逐奏法、"
        "逐力度轮转 sequence,并用确定性分位点覆盖每个随机区间。\n"
    )
    if octave_errors:
        print(
            f"== 明确整八度映射错误:"
            f"{sum(len(v) for v in octave_errors.values())} 个音,"
            f"分布在 {len(octave_errors)} 件乐器\n"
        )
        for relative in sorted(octave_errors):
            items = sorted(
                octave_errors[relative],
                key=lambda item: -abs(item[1]),
            )
            print(f"### {relative}({len(items)} 个)")
            for midi, cents, octaves in items[:12]:
                direction = "高" if octaves > 0 else "低"
                print(
                    f"      {note_name(midi):>4s}({midi:3d})  "
                    f"{cents:+8.1f}c  明确{direction}{abs(octaves)}个八度"
                )
            if len(items) > 12:
                print(f"      … 其余 {len(items) - 12} 个")
            print()
    else:
        print("== 未发现明确整八度映射错误\n")

    if offenders:
        print(f"== 超容差音符(不含整八度错误):"
              f"{sum(len(v) for v in offenders.values())} 个,"
              f"分布在 {len(offenders)} 件乐器\n")
        for relative in sorted(
            offenders, key=lambda r: -max(abs(c) for _, c in offenders[r])
        ):
            items = sorted(offenders[relative], key=lambda x: -abs(x[1]))
            print(f"### {relative}({len(items)} 个,最大 {abs(items[0][1]):.1f}c)")
            for midi, cents in items[:12]:
                print(f"      {note_name(midi):>4s}({midi:3d})  {cents:+8.1f}c")
            if len(items) > 12:
                print(f"      … 其余 {len(items) - 12} 个")
            if relative == "键盘乐器/钢琴":
                print(
                    "      注:真钢琴高音调高、低音调低的 stretch tuning "
                    "(Railsback 曲线)不是映射错误,需结合原厂调律目标复核。"
                )
            print()
    else:
        print("== 未发现普通超容差音符\n")

    if unclear:
        total = sum(len(v) for v in unclear.values())
        print(
            "== no_clear_pitch:无法可靠确定基频"
            f"(静音/弱基频/噪声/非谐,不等于走音):"
            f"{total} 个音,{len(unclear)} 件乐器"
        )
        for relative in sorted(unclear):
            items = unclear[relative]
            preview = ", ".join(note_name(midi) for midi, _ in items[:8])
            more = "…" if len(items) > 8 else ""
            reasons = "; ".join(sorted({reason for _, reason in items}))
            print(
                f"   {relative}: {len(items)} 个({preview}{more});"
                f"分析说明:{reasons}"
            )
        print()

    if render_errors:
        total = sum(len(v) for v in render_errors.values())
        print(f"== 渲染失败:{total} 个音,{len(render_errors)} 件乐器")
        for relative in sorted(render_errors):
            items = render_errors[relative]
            preview = "; ".join(
                f"{note_name(midi)}: {reason}" for midi, reason in items[:4]
            )
            more = "; …" if len(items) > 4 else ""
            print(f"   {relative}: {preview}{more}")
        print()

    if range_errors:
        print(f"== 音域声明无效:{len(range_errors)} 件")
        for relative in sorted(range_errors):
            print(f"   {relative}: {range_errors[relative]}")
        print()

    print(f"== 跳过(无固定音高或未声明音域):{len(skipped)} 件")


if __name__ == "__main__":
    main()
