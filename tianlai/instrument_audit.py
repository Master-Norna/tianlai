"""跨后端的通用乐器审计:资源冻结、音准校准与试听核验。

`dedicated_candidates` 只服务于 SFZ 驱动的入口。本模块补上其余后端:

- 采样类(piano/violin/cello/flute/vpo_* 等):构造乐器实例后遍历它
  **实际加载**的采样区,对真实读到的文件逐个求 SHA-256,而不是按清单
  猜路径——这样"资源冻结"才是证据而不是声明;
- 程序合成类(synthesizer/procedural_sfx):冻结引擎源文件 SHA-256、
  补丁名与显式种子;有音高的做渲染自测 FFT 校准,无固定音高的写明
  不适用理由。

三类报告字段与 `dedicated_candidates` 对齐,便于清单统一复查。
"""

from __future__ import annotations

import datetime as _datetime
import fnmatch
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .audio import audio_file_info
from .events import PerformanceEvent
from .instrument import Instrument, create_instrument
from .provenance import project_authored_dsp_provenance
from .tuning import EqualTemperament


def collect_loaded_samples(instrument: Instrument) -> list[Path]:
    """遍历乐器实例,收集它实际加载的全部采样文件路径。

    递归走对象属性与容器,凡是带 ``regions`` 且区域带 ``path`` 的引擎都
    计入。用实际加载结果而非清单声明,可以发现"清单写了但没用上"和
    "用了但没登记"两类偏差。
    """

    found: dict[str, Path] = {}
    seen: set[int] = set()

    def visit(value: Any, depth: int) -> None:
        if depth > 8 or id(value) in seen:
            return
        seen.add(id(value))
        regions = getattr(value, "regions", None)
        if regions is not None:
            try:
                for region in regions:
                    path = getattr(region, "path", None)
                    if path is not None:
                        resolved = Path(path).resolve()
                        found[str(resolved)] = resolved
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
        names = list(getattr(value, "__dict__", {})) + slots
        for name in names:
            if name.startswith("__"):
                continue
            try:
                visit(getattr(value, name), depth + 1)
            except AttributeError:
                continue

    visit(instrument, 0)
    return sorted(found.values(), key=lambda item: item.as_posix())


def _pitch_calibration_include_globs(
    manifest: dict[str, Any],
) -> tuple[str, ...] | None:
    """读取并校验可选的音准校准采样白名单。"""

    field = "pitch_calibration_include_globs"
    raw = manifest.get(field)
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{field} 必须是非空 POSIX 相对 glob 数组")

    patterns: list[str] = []
    for pattern in raw:
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"{field} 的每一项都必须是非空字符串")
        parts = pattern.split("/")
        if (
            pattern.startswith("/")
            or "\\" in pattern
            or ":" in pattern
            or "\x00" in pattern
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError(
                f"{field} 只能包含 asset_root 下的 POSIX 相对 glob: {pattern!r}"
            )
        patterns.append(pattern)
    if len(set(patterns)) != len(patterns):
        raise ValueError(f"{field} 不得包含重复 glob")
    return tuple(patterns)


def _matches_posix_glob(relative_path: str, pattern: str) -> bool:
    """按 POSIX 路径分段、大小写敏感地匹配 glob；``**`` 可跨目录。"""

    path_parts = relative_path.split("/")
    pattern_parts = pattern.split("/")
    memo: dict[tuple[int, int], bool] = {}

    def match(path_index: int, pattern_index: int) -> bool:
        key = (path_index, pattern_index)
        cached = memo.get(key)
        if cached is not None:
            return cached
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = match(path_index, pattern_index + 1) or (
                path_index < len(path_parts)
                and match(path_index + 1, pattern_index)
            )
        else:
            result = (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(
                    path_parts[path_index], pattern_parts[pattern_index]
                )
                and match(path_index + 1, pattern_index + 1)
            )
        memo[key] = result
        return result

    return match(0, 0)


def generate_sampled_resource_verification(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
    license_note: str,
    upstream: str,
    origin: str,
    upstream_version: str,
    evidence_files: tuple[str, ...] = (),
) -> dict[str, Any]:
    """构造采样类乐器并冻结它真实加载的每个采样文件。"""

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    base = source_manifest.parent
    asset_root = (base / str(manifest["asset_root"])).resolve()
    instrument = create_instrument(manifest, 48000, base_directory=str(base))
    try:
        samples = collect_loaded_samples(instrument)
    finally:
        close = getattr(instrument, "close", None)
        if callable(close):
            close()
    if not samples:
        raise ValueError(f"未能从实例中收集到任何采样:{source_manifest}")

    lines: list[str] = []
    total_bytes = 0
    decoded_float32_stereo_bytes = 0
    formats: dict[str, int] = {}
    for path in samples:
        try:
            relative = path.relative_to(asset_root).as_posix()
        except ValueError:
            relative = path.name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {relative}\n")
        total_bytes += path.stat().st_size
        sample_rate, frame_count, channels = audio_file_info(path)
        if channels not in (1, 2):
            raise ValueError(f"采样声道数必须是 1 或 2:{path}")
        decoded_float32_stereo_bytes += frame_count * 2 * 4
        key = f"{path.suffix.lower()}:{sample_rate}Hz:{channels}ch"
        formats[key] = formats.get(key, 0) + 1

    evidence: dict[str, str] = {}
    for relative in evidence_files:
        path = asset_root / relative
        if not path.is_file():
            raise ValueError(f"许可证据文件缺失:{path}")
        evidence[relative] = hashlib.sha256(path.read_bytes()).hexdigest()

    report = {
        "upstream": upstream,
        "origin": origin,
        "upstream_version": upstream_version,
        "license": license_note,
        "evidence_sha256": evidence,
        "sample_count": len(samples),
        "sample_bytes": total_bytes,
        "decoded_float32_stereo_bytes": decoded_float32_stereo_bytes,
        "decoded_float32_stereo_algorithm": (
            "sum unique runtime sample frame_count * 2 output channels * "
            "4-byte float32; mono sources are expanded to stereo by "
            "read_audio_float"
        ),
        "sample_formats": formats,
        "sample_enumeration": (
            "constructed the instrument and walked its loaded sample regions; "
            "hashes cover exactly the files the engine reads at run time"
        ),
        "sample_set_sha256": hashlib.sha256("".join(lines).encode("utf-8")).hexdigest(),
        "sample_set_hash_algorithm": (
            "sort unique asset-root-relative UTF-8 paths; concatenate lowercase "
            "'<sha256>  <path>\\n>'; SHA-256 the UTF-8 bytes"
        ),
        "generated_at": _datetime.date.today().isoformat(),
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else base / str(manifest.get("resource_verification", "资源核验.json"))
    )
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def generate_sampled_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
    start_seconds: float = 0.12,
    search_cents: float = 180.0,
) -> dict[str, Any]:
    """对采样类乐器实际加载的有音高根采样做谐波 FFT 诊断校准。"""

    import statistics

    from .analysis import analyze_file_harmonic_pitch

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    base = source_manifest.parent
    asset_root = (base / str(manifest["asset_root"])).resolve()
    include_globs = _pitch_calibration_include_globs(manifest)
    instrument = create_instrument(manifest, 48000, base_directory=str(base))

    roots: dict[Path, float] = {}
    seen: set[int] = set()

    def visit(value: Any, depth: int) -> None:
        if depth > 8 or id(value) in seen:
            return
        seen.add(id(value))
        regions = getattr(value, "regions", None)
        if regions is not None:
            try:
                for region in regions:
                    path = getattr(region, "path", None)
                    root_hz = getattr(region, "root_pitch_hz", None)
                    if path is None or not root_hz or root_hz <= 0.0:
                        continue
                    roots.setdefault(Path(path).resolve(), float(root_hz))
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
    if not roots:
        raise ValueError(f"未收集到任何有音高根采样:{source_manifest}")

    root_samples: list[tuple[Path, float, str]] = []
    for path, root_hz in sorted(roots.items(), key=lambda item: item[0].as_posix()):
        try:
            relative = path.relative_to(asset_root).as_posix()
        except ValueError:
            if include_globs is not None:
                raise ValueError(
                    "配置 pitch_calibration_include_globs 时，有音高根采样必须位于 "
                    f"asset_root 内: {path}"
                ) from None
            relative = path.name
        root_samples.append((path, root_hz, relative))

    excluded: list[str] = []
    if include_globs is not None:
        included_root_samples: list[tuple[Path, float, str]] = []
        for root_sample in root_samples:
            relative = root_sample[2]
            if any(
                _matches_posix_glob(relative, pattern)
                for pattern in include_globs
            ):
                included_root_samples.append(root_sample)
            else:
                excluded.append(relative)
        if not included_root_samples:
            raise ValueError(
                "pitch_calibration_include_globs 未匹配任何已加载的有音高根采样: "
                + ", ".join(include_globs)
            )
    else:
        included_root_samples = root_samples

    samples: dict[str, dict[str, float]] = {}
    detunes: list[float] = []
    skipped: list[str] = []
    unreliable: list[str] = []
    outliers: list[dict[str, Any]] = []
    for path, root_hz, relative in included_root_samples:
        # 谐波约束只在低音区可靠:高音采样本就泛音稀少,继续按 10 个
        # 谐波求和会被噪声底带偏,收敛到 1000/2000/4000 Hz 这类整数假解。
        # 因此把参与求和的分音上限压在 4 kHz 以内。
        harmonic_count = max(1, min(10, int(4000.0 / root_hz)))
        try:
            measurement = analyze_file_harmonic_pitch(
                path,
                root_hz,
                start_seconds=start_seconds,
                maximum_frames=131_072,
                search_cents=search_cents,
                harmonic_count=harmonic_count,
            )
        except ValueError:
            # 过短或非稳态的采样(如踏板噪、释音尾)无法做可靠音高分析,
            # 如实登记跳过,不用猜测值充数。
            skipped.append(relative)
            continue
        detune = 1200.0 * math.log2(measurement.measured_hz / root_hz)
        record = {
            "source_root_hz": round(root_hz, 6),
            "measured_hz": round(measurement.measured_hz, 6),
            "detune_cents": round(detune, 6),
        }
        if abs(detune) >= search_cents - 1.0:
            # 测量撞到搜索边界,说明该采样在该根音附近没有稳定基频
            # (释键噪、踏板噪一类)。如实标记并排除出统计,不让这种
            # 无意义读数污染中位数和最大值。
            record["unreliable"] = True
            record["unreliable_reason"] = "measurement railed to the search boundary; no stable partial near the mapped root"
            samples[relative] = record
            unreliable.append(relative)
            continue
        record["harmonic_count"] = harmonic_count
        samples[relative] = record
        detunes.append(detune)
        if abs(detune) > 50.0:
            outliers.append({"sample": relative, "detune_cents": round(detune, 3)})

    if not detunes:
        raise ValueError(
            "音准校准未获得任何可靠测量: "
            f"{len(skipped)} 个无法分析，{len(unreliable)} 个撞到搜索边界"
        )

    sample_count = (
        len(included_root_samples) if include_globs is not None else len(samples)
    )
    document = {
        "applicable": True,
        "method": (
            "harmonic-constrained FFT of each loaded pitched root sample against the "
            "root frequency the engine actually plays it at; diagnostic only, playback "
            "follows the audited upstream map"
        ),
        "reference_a4_hz": float(manifest.get("reference_a4_hz", 440.0)),
        "summary": {
            "sample_count": sample_count,
            "measured_count": len(detunes),
            "skipped_count": len(skipped),
            "unreliable_count": len(unreliable),
            "median_detune_cents": round(statistics.median(detunes), 6),
            "maximum_absolute_detune_cents": round(max(abs(x) for x in detunes), 6),
            "outlier_count": len(outliers),
            "statistics_note": (
                "median/maximum cover only reliably measured pitched samples; "
                "railed and unanalysable samples are listed separately. Outliers "
                "beyond 50 cents are flagged for manual review rather than averaged "
                "away — a single sample that far off is usually a source problem, "
                "not natural stretch tuning"
            ),
        },
        "outliers_over_50_cents": outliers,
        "skipped_samples": skipped,
        "unreliable_samples": unreliable,
        "samples": samples,
        "generated_at": _datetime.date.today().isoformat(),
    }
    if include_globs is not None:
        document["pitch_calibration_include_globs"] = list(include_globs)
        document["summary"]["excluded_count"] = len(excluded)
        document["excluded_samples"] = excluded
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else base / str(manifest.get("pitch_calibration", "音准校准.json"))
    )
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return document


def generate_engine_resource_verification(
    manifest_path: str | Path,
    engine_module_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """冻结程序合成类乐器的引擎源文件、补丁与种子。"""

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    engine = Path(engine_module_path).resolve()
    # 证据沿用乐器清单自己的叫法:合成器写 patch,拟音写 profile。统一改名会
    # 让证据和清单对不上,审阅时得先在脑子里做一次翻译。
    patch_key = "profile" if "profile" in manifest else "patch"
    report = {
        "implementation": "Tianlai self-authored procedural DSP",
        "engine": engine.name,
        "engine_version": str(manifest.get("engine_version", "1.0.0")),
        patch_key: str(manifest.get(patch_key, "")),
        "seed": int(manifest.get("seed", 0)),
        "engine_sha256": hashlib.sha256(engine.read_bytes()).hexdigest().upper(),
        "external_assets": [],
        **project_authored_dsp_provenance(),
        "generated_at": _datetime.date.today().isoformat(),
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent
        / str(manifest.get("resource_verification", "资源核验.json"))
    )
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def generate_rendered_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
    probe_notes: tuple[int, ...] | None = None,
    method_note: str = "",
) -> dict[str, Any]:
    """渲染探测音并 FFT 实测,给有音高的程序合成留下机器音准证据。"""

    import numpy as np

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    base = source_manifest.parent
    sample_rate = 48000
    if probe_notes is None:
        low = int(manifest.get("note_min", 48))
        high = int(manifest.get("note_max", 84))
        probe_notes = tuple(sorted({low, (low + high) // 2, high}))

    tuning = EqualTemperament(440.0)
    probes: dict[str, dict[str, float]] = {}
    errors: list[float] = []
    for midi in probe_notes:
        instrument = create_instrument(manifest, sample_rate, base_directory=str(base))
        expected_hz = 440.0 * 2.0 ** ((midi - 69.0) / 12.0)
        instrument.handle_event(
            PerformanceEvent(
                0, 0, "note_on", {"note_id": 1, "midi_note": midi, "velocity": 0.8}
            ),
            tuning,
        )
        # 低音区一个 FFT 频点就能跨几十音分,必须按频率加长分析窗,
        # 否则会把测量下限误报成走音。
        analysis_seconds = 1.5 if expected_hz >= 200.0 else 5.0
        frames = int(sample_rate * (analysis_seconds + 0.4))
        buffer = np.empty(frames, dtype=np.float64)
        for index in range(frames):
            left, _ = instrument.render_frame()
            buffer[index] = left
        segment = buffer[int(sample_rate * 0.4) :]
        segment = segment - np.mean(segment)
        spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment))))
        frequencies = np.fft.rfftfreq(len(segment), 1.0 / sample_rate)
        ratio = 2.0 ** (120.0 / 1200.0)
        mask = (frequencies >= expected_hz / ratio) & (frequencies <= expected_hz * ratio)
        bins = np.flatnonzero(mask)
        if len(bins) == 0:
            raise ValueError(f"{expected_hz} Hz 附近没有 FFT 频点")
        peak = int(bins[np.argmax(spectrum[mask])])
        delta = 0.0
        if 0 < peak < len(spectrum) - 1:
            left_bin, center, right_bin = np.log(spectrum[peak - 1 : peak + 2] + 1e-20)
            denominator = left_bin - 2.0 * center + right_bin
            if denominator != 0.0:
                delta = float(0.5 * (left_bin - right_bin) / denominator)
        measured_hz = (peak + delta) * sample_rate / len(segment)
        error_cents = 1200.0 * math.log2(measured_hz / expected_hz)
        bin_width_hz = sample_rate / len(segment)
        bin_width_cents = 1200.0 * math.log2(
            (expected_hz + bin_width_hz) / expected_hz
        )
        probes[str(midi)] = {
            "expected_hz": round(expected_hz, 6),
            "measured_hz": round(measured_hz, 6),
            "error_cents": round(error_cents, 6),
            # 一个 FFT 频点在该频率处折合多少音分:低音区这个下限很大,
            # 报告里必须带上,否则读者会把测量分辨率当成乐器走音。
            "fft_bin_width_cents": round(bin_width_cents, 6),
            "analysis_seconds": round(len(segment) / sample_rate, 6),
        }
        errors.append(error_cents)

    document = {
        "applicable": True,
        "method": method_note
        or (
            "self-test: render each probe note with the released engine, FFT the "
            "steady segment, compare against the equal-temperament target"
        ),
        "reference_a4_hz": 440.0,
        "summary": {
            "probe_count": len(errors),
            "maximum_absolute_error_cents": round(max(abs(item) for item in errors), 6),
            "maximum_fft_bin_width_cents": round(
                max(item["fft_bin_width_cents"] for item in probes.values()), 6
            ),
        },
        "probes": probes,
        "generated_at": _datetime.date.today().isoformat(),
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent
        / str(manifest.get("pitch_calibration", "音准校准.json"))
    )
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return document


def generate_synth_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """合成器校准:把补丁设计的 unison 失谐与真实走音区分开。

    铺底类补丁常有意做 unison 失谐(例如 halo_pad 为 ±14 音分)。单纯
    取 FFT 峰值会落在外侧失谐声部上,如果直接记成"音准误差"就会把
    设计当缺陷。这里同时登记补丁的设计展宽,并按展宽给出判定。
    """

    from .synthesizer import PATCH_PROFILES

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    patch_name = str(manifest["patch"])
    profile = PATCH_PROFILES[patch_name]
    spread = float(profile.detune_cents)

    document = generate_rendered_pitch_calibration(
        manifest_path,
        output_path=output_path,
        method_note=(
            "self-test: render probe notes with the released synthesizer engine, FFT "
            "the sustained segment, then judge the peak against the patch's designed "
            "unison detune spread rather than against a bare equal-temperament target"
        ),
    )
    # 判定阈值 = 设计失谐展宽 + 该频率下的 FFT 测量下限。低音区一个频点
    # 就能跨几十音分,不把这个下限计入就会把测量分辨率误判成走音。
    measurement_floor = float(document["summary"]["maximum_fft_bin_width_cents"])
    tolerance = spread + measurement_floor
    worst = float(document["summary"]["maximum_absolute_error_cents"])
    document["designed_unison"] = {
        "patch": patch_name,
        "unison_voices": int(profile.unison_voices),
        "detune_cents_half_spread": round(spread, 6),
        "note": (
            "unison 声部按 ±detune_cents 均匀铺开,FFT 峰值可能落在任一声部上;"
            "判定阈值另加该频率下的 FFT 频点宽度作为测量下限"
        ),
    }
    document["summary"]["designed_spread_cents"] = round(spread, 6)
    document["summary"]["tolerance_cents"] = round(tolerance, 6)
    document["summary"]["within_designed_spread"] = bool(worst <= tolerance)
    document["summary"]["verdict"] = (
        "峰值落在补丁设计失谐与测量分辨率之内,非走音"
        if worst <= tolerance
        else "峰值超出设计失谐与测量下限之和,需复查补丁或引擎"
    )
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent
        / str(manifest.get("pitch_calibration", "音准校准.json"))
    )
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return document


def generate_not_applicable_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """为无固定音高入口写明不适用理由,拒绝伪造十二平均律校准。"""

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    reason = str(manifest.get("calibration_not_applicable_reason", "")).strip()
    if not reason:
        raise ValueError(
            f"无固定音高入口必须登记 calibration_not_applicable_reason:{source_manifest}"
        )
    document = {
        "applicable": False,
        "pitch_mode": str(manifest.get("pitch_mode", "not_pitched")),
        "reason": reason,
        "samples": {},
        "generated_at": _datetime.date.today().isoformat(),
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent
        / str(manifest.get("pitch_calibration", "音准校准.json"))
    )
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return document
