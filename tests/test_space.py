"""空间层(算法厅堂)的单元测试:纯 DSP,不加载音源,跑得快。

覆盖:梳状/全通滤波器的数学正确性、混响的确定性与衰减、配置解析与
距离送出曲线。厅堂只作用于合奏总线且是确定性 DSP——同输入必同输出——
这些性质在这里逐条钉死。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import numpy as np

import tianlai.space as space_module
from tianlai.space import (
    SpaceConfig,
    render_reverb,
    render_reverb_stereo,
    _apply_spectral_response,
    _apply_spectral_response_in_place,
    _allpass,
    _comb,
    _feedback_along_rows,
    _reshape_columns,
    _spectral_response,
    _spectral_shape,
)


class CombAllpassTest(unittest.TestCase):
    def test_comb_impulse_is_decaying_echo_train(self):
        imp = np.zeros(21)
        imp[0] = 1.0
        out = _comb(imp, 5, 0.5)
        # y[n] = x[n] + 0.5 y[n-5]:回声在 0,5,10,15… 处按 0.5 幂衰减
        self.assertAlmostEqual(out[0], 1.0)
        self.assertAlmostEqual(out[5], 0.5)
        self.assertAlmostEqual(out[10], 0.25)
        self.assertAlmostEqual(out[15], 0.125)
        # 非回声位置为零
        self.assertAlmostEqual(out[3], 0.0)
        self.assertAlmostEqual(out[7], 0.0)

    def test_allpass_preserves_broadband_energy(self):
        rng = np.random.default_rng(0)
        x = rng.standard_normal(20000)
        out = _allpass(x, 113, 0.5)
        ratio = (out**2).sum() / (x**2).sum()
        # 全通滤波器幅频平坦,整段能量近似守恒
        self.assertAlmostEqual(ratio, 1.0, delta=0.02)

    def test_comb_length_matches_input(self):
        x = np.ones(1000)
        self.assertEqual(_comb(x, 37, 0.7).shape, x.shape)
        self.assertEqual(_allpass(x, 37, 0.5).shape, x.shape)
        self.assertEqual(_allpass(np.zeros(0), 37, 0.5).shape, (0,))

    def test_allpass_matches_the_allocation_heavy_equation_bit_for_bit(self):
        rng = np.random.default_rng(20260811)
        x = rng.standard_normal(10003)
        x[:4] = (
            0.0,
            -0.0,
            np.nextafter(0.0, 1.0),
            -np.nextafter(0.0, 1.0),
        )
        delay = 113
        gain = 0.5

        grid, n = _reshape_columns(x, delay)
        x_prev = np.zeros_like(grid)
        x_prev[1:] = grid[:-1]
        expected = -gain * grid + x_prev
        _feedback_along_rows(expected, gain)
        expected = expected.reshape(-1)[:n]

        actual = _allpass(x, delay, gain)
        np.testing.assert_array_equal(
            actual.view(np.uint64),
            expected.view(np.uint64),
        )
        self.assertTrue(actual.flags.owndata)
        self.assertIsNone(actual.base)


class SpectralShapeTest(unittest.TestCase):
    def test_in_place_fft_matches_the_owned_formula_bit_for_bit(self):
        rng = np.random.default_rng(20260813)
        original = rng.standard_normal(10003)
        response = _spectral_response(10003, 48000, 150.0, 6500.0)
        expected = _apply_spectral_response(original, response)
        actual = original.copy()

        returned = _apply_spectral_response_in_place(actual, response)

        self.assertIs(returned, actual)
        self.assertTrue(actual.flags.owndata)
        self.assertIsNone(actual.base)
        np.testing.assert_array_equal(
            actual.view(np.uint64),
            expected.view(np.uint64),
        )

    def test_in_place_spectrum_matches_the_previous_formula_bit_for_bit(self):
        rng = np.random.default_rng(20260812)
        x = rng.standard_normal(10001)
        sr = 48000
        highpass_hz = 150.0
        lowpass_hz = 6500.0

        freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
        response = np.ones_like(freqs)
        with np.errstate(divide="ignore"):
            response *= 1.0 / np.sqrt(
                1.0 + (highpass_hz / np.maximum(freqs, 1e-9)) ** 4
            )
        response *= 1.0 / np.sqrt(1.0 + (freqs / lowpass_hz) ** 4)
        expected = np.fft.irfft(np.fft.rfft(x) * response, x.size)

        actual = _spectral_shape(x, sr, highpass_hz, lowpass_hz)
        np.testing.assert_array_equal(
            actual.view(np.uint64),
            expected.view(np.uint64),
        )

    def test_lowpass_attenuates_highs_keeps_lows(self):
        sr = 48000
        t = np.arange(sr) / sr
        low = np.sin(2 * np.pi * 200 * t)
        high = np.sin(2 * np.pi * 12000 * t)
        shaped_low = _spectral_shape(low, sr, 0.0, 6000.0)
        shaped_high = _spectral_shape(high, sr, 0.0, 6000.0)
        # 200Hz 基本保留,12kHz 被压掉
        self.assertGreater((shaped_low**2).sum() / (low**2).sum(), 0.9)
        self.assertLess((shaped_high**2).sum() / (high**2).sum(), 0.2)

    def test_highpass_removes_subbass(self):
        sr = 48000
        t = np.arange(sr) / sr
        sub = np.sin(2 * np.pi * 40 * t)
        shaped = _spectral_shape(sub, sr, 150.0, 0.0)
        self.assertLess((shaped**2).sum() / (sub**2).sum(), 0.2)


class ReverbTest(unittest.TestCase):
    def test_deterministic(self):
        rng = np.random.default_rng(1)
        send = rng.standard_normal(48000) * 0.05
        cfg = SpaceConfig()
        a_l, a_r = render_reverb(send, 48000, cfg)
        b_l, b_r = render_reverb(send, 48000, cfg)
        np.testing.assert_array_equal(a_l, b_l)
        np.testing.assert_array_equal(a_r, b_r)

    def test_tail_decays_after_impulse(self):
        sr = 48000
        imp = np.zeros(sr * 3)
        imp[0] = 1.0
        wet_l, _ = render_reverb(imp, sr, SpaceConfig())
        head = np.sqrt((wet_l[: sr // 2] ** 2).mean())
        tail = np.sqrt((wet_l[int(sr * 2.5):] ** 2).mean())
        # 冲激后尾部远低于头部,且全程有限
        self.assertTrue(np.isfinite(wet_l).all())
        self.assertLess(tail, head * 0.1)

    def test_stereo_channels_decorrelated(self):
        rng = np.random.default_rng(2)
        send = rng.standard_normal(48000) * 0.05
        wet_l, wet_r = render_reverb(send, 48000, SpaceConfig())
        corr = np.corrcoef(wet_l, wet_r)[0, 1]
        # 左右用错开的梳状延时,尾部应基本不相关(立体声更宽)
        self.assertLess(abs(corr), 0.3)

    def test_fills_silent_gaps(self):
        # 干声硬切的静音处,厅堂应填入余韵(这正是"悠扬")
        sr = 48000
        send = np.zeros(sr)
        send[: sr // 4] = np.sin(2 * np.pi * 440 * np.arange(sr // 4) / sr) * 0.2
        wet_l, _ = render_reverb(send, sr, SpaceConfig(wet_db=-6.0))
        gap = wet_l[int(sr * 0.6):]  # 干声早已静音的区段
        self.assertGreater(np.sqrt((gap**2).mean()), 1e-5)

    def test_default_config_remains_finite_at_minimum_sample_rate(self):
        sr = 8000
        impulse = np.zeros(sr)
        impulse[0] = 0.5
        wet_l, wet_r = render_reverb(impulse, sr, SpaceConfig())
        self.assertTrue(np.isfinite(wet_l).all())
        self.assertTrue(np.isfinite(wet_r).all())
        self.assertEqual(wet_l.shape, impulse.shape)
        self.assertEqual(wet_r.shape, impulse.shape)

    def test_render_rejects_invalid_sample_rates(self):
        send = np.zeros(32)
        for sample_rate in (7999, 384001, 48000.0, True):
            with self.subTest(sample_rate=sample_rate):
                with self.assertRaisesRegex(ValueError, "sample_rate"):
                    render_reverb(send, sample_rate, SpaceConfig())

    def test_channel_pair_builds_one_shared_spectral_response(self):
        send = np.zeros(4000)
        send[0] = 1.0
        with patch.object(
            space_module,
            "_spectral_response",
            wraps=space_module._spectral_response,
        ) as build_response:
            render_reverb(send, 48000, SpaceConfig())

        self.assertEqual(build_response.call_count, 1)

    def test_predelay_keeps_exact_sized_owned_output_buffers(self):
        send = np.zeros(4000)
        send[0] = 1.0
        wet_l, wet_r = render_reverb(
            send,
            48000,
            SpaceConfig(predelay_ms=18.0),
        )

        self.assertTrue(wet_l.flags.owndata)
        self.assertTrue(wet_r.flags.owndata)
        self.assertIsNone(wet_l.base)
        self.assertIsNone(wet_r.base)
        self.assertEqual(wet_l.shape, send.shape)
        self.assertEqual(wet_r.shape, send.shape)


class StereoReverbTest(unittest.TestCase):
    @staticmethod
    def _allocation_heavy_reference(left, right, sr, cfg):
        """Reproduce the former simultaneous mid/side topology exactly."""

        left_channel = space_module._validated_audio_channel(left, "left")
        right_channel = space_module._validated_audio_channel(right, "right")
        mid = 0.5 * left_channel + 0.5 * right_channel
        side = 0.5 * left_channel - 0.5 * right_channel
        spread = round(space_module._STEREO_SPREAD_44K * sr / 44100.0)
        wet_mid_l, wet_mid_r = space_module._render_reverb_pair(
            mid,
            sr,
            cfg,
            0,
            spread,
        )
        side_left_offset = max(1, round(spread / 3.0))
        side_right_offset = min(
            spread - 1,
            max(side_left_offset + 1, round(2.0 * spread / 3.0)),
        )
        wet_side_l, wet_side_r = space_module._render_reverb_pair(
            side,
            sr,
            cfg,
            side_left_offset,
            side_right_offset,
        )
        wet_mid_l += wet_side_l
        wet_mid_r -= wet_side_r
        return wet_mid_l, wet_mid_r

    def test_reduced_peak_topology_matches_the_previous_stereo_bits(self):
        rng = np.random.default_rng(20260814)
        random_left = rng.standard_normal(5003) * 0.02
        random_right = rng.standard_normal(5003) * 0.02
        same = rng.standard_normal(5003) * 0.02
        impulse = np.zeros(5003)
        impulse[0] = 0.5
        cases = (
            ("random", random_left, random_right, SpaceConfig()),
            ("in_phase", same, same.copy(), SpaceConfig()),
            (
                "anti_phase",
                same,
                -same,
                SpaceConfig(
                    predelay_ms=0.0,
                    damping_hz=0.0,
                    highpass_hz=0.0,
                ),
            ),
            ("impulse_tail", impulse, np.zeros_like(impulse), SpaceConfig()),
        )

        for name, left, right, cfg in cases:
            with self.subTest(name=name):
                expected_l, expected_r = self._allocation_heavy_reference(
                    left,
                    right,
                    48000,
                    cfg,
                )
                actual_l, actual_r = render_reverb_stereo(
                    left,
                    right,
                    48000,
                    cfg,
                )
                np.testing.assert_array_equal(
                    actual_l.view(np.uint64),
                    expected_l.view(np.uint64),
                )
                np.testing.assert_array_equal(
                    actual_r.view(np.uint64),
                    expected_r.view(np.uint64),
                )

    def test_array_like_inputs_are_normalized_once_before_rendering(self):
        class ChangingArray:
            def __init__(self, first, later):
                self.first = np.asarray(first)
                self.later = np.asarray(later)
                self.calls = 0

            def __array__(self, dtype=None, copy=None):
                self.calls += 1
                value = self.first if self.calls <= 2 else self.later
                return np.array(value, dtype=dtype, copy=True)

        first_left = np.zeros(1201)
        first_right = np.zeros(1201)
        first_left[0] = 0.5
        later_left = np.zeros(1201)
        later_right = np.zeros(1201)
        later_left[17] = -0.5
        later_right[29] = 0.5
        cfg = SpaceConfig(
            predelay_ms=0.0,
            damping_hz=0.0,
            highpass_hz=0.0,
        )
        expected_l, expected_r = render_reverb_stereo(
            first_left,
            first_right,
            8000,
            cfg,
        )
        changing_left = ChangingArray(first_left, later_left)
        changing_right = ChangingArray(first_right, later_right)

        actual_l, actual_r = render_reverb_stereo(
            changing_left,
            changing_right,
            8000,
            cfg,
        )

        self.assertEqual(changing_left.calls, 2)
        self.assertEqual(changing_right.calls, 2)
        np.testing.assert_array_equal(
            actual_l.view(np.uint64),
            expected_l.view(np.uint64),
        )
        np.testing.assert_array_equal(
            actual_r.view(np.uint64),
            expected_r.view(np.uint64),
        )

    def test_same_phase_input_matches_the_legacy_mono_entry(self):
        rng = np.random.default_rng(31)
        send = rng.standard_normal(12000) * 0.03
        cfg = SpaceConfig(predelay_ms=7.0)

        legacy_l, legacy_r = render_reverb(send, 48000, cfg)
        stereo_l, stereo_r = render_reverb_stereo(send, send, 48000, cfg)

        np.testing.assert_array_equal(stereo_l, legacy_l)
        np.testing.assert_array_equal(stereo_r, legacy_r)

    def test_antiphase_input_produces_wet_sound_and_nonzero_mono_fold(self):
        sr = 8000
        signal = np.zeros(sr * 2)
        signal[0] = 0.5
        cfg = SpaceConfig(
            wet_db=-6.0,
            predelay_ms=0.0,
            damping_hz=0.0,
            highpass_hz=0.0,
        )

        wet_l, wet_r = render_reverb_stereo(signal, -signal, sr, cfg)
        mono_fold = 0.5 * wet_l + 0.5 * wet_r

        self.assertGreater(float(np.sum(wet_l * wet_l + wet_r * wet_r)), 1e-8)
        self.assertGreater(float(np.sum(mono_fold * mono_fold)), 1e-10)
        self.assertTrue(np.isfinite(wet_l).all())
        self.assertTrue(np.isfinite(wet_r).all())

    def test_stereo_render_is_deterministic(self):
        rng = np.random.default_rng(32)
        left = rng.standard_normal(10000) * 0.02
        right = rng.standard_normal(10000) * 0.02
        cfg = SpaceConfig()

        first_l, first_r = render_reverb_stereo(left, right, 44100, cfg)
        second_l, second_r = render_reverb_stereo(left, right, 44100, cfg)

        np.testing.assert_array_equal(first_l, second_l)
        np.testing.assert_array_equal(first_r, second_r)

    def test_stereo_render_is_linear(self):
        rng = np.random.default_rng(33)
        a_l = rng.standard_normal(4096) * 0.01
        a_r = rng.standard_normal(4096) * 0.01
        b_l = rng.standard_normal(4096) * 0.01
        b_r = rng.standard_normal(4096) * 0.01
        cfg = SpaceConfig(predelay_ms=0.0)

        sum_l, sum_r = render_reverb_stereo(
            a_l + b_l,
            a_r + b_r,
            8000,
            cfg,
        )
        a_wet_l, a_wet_r = render_reverb_stereo(a_l, a_r, 8000, cfg)
        b_wet_l, b_wet_r = render_reverb_stereo(b_l, b_r, 8000, cfg)

        np.testing.assert_allclose(
            sum_l,
            a_wet_l + b_wet_l,
            rtol=1e-12,
            atol=1e-12,
        )
        np.testing.assert_allclose(
            sum_r,
            a_wet_r + b_wet_r,
            rtol=1e-12,
            atol=1e-12,
        )

    def test_silence_length_and_inputs_are_preserved(self):
        left = np.zeros(1234, dtype=np.float32)
        right = np.zeros(1234, dtype=np.float64)
        left_before = left.copy()
        right_before = right.copy()

        wet_l, wet_r = render_reverb_stereo(
            left,
            right,
            48000,
            SpaceConfig(),
        )

        self.assertEqual(wet_l.shape, left.shape)
        self.assertEqual(wet_r.shape, right.shape)
        np.testing.assert_array_equal(wet_l, np.zeros_like(wet_l))
        np.testing.assert_array_equal(wet_r, np.zeros_like(wet_r))
        np.testing.assert_array_equal(left, left_before)
        np.testing.assert_array_equal(right, right_before)
        self.assertTrue(np.isfinite(wet_l).all())
        self.assertTrue(np.isfinite(wet_r).all())

    def test_nonempty_inputs_are_not_modified(self):
        rng = np.random.default_rng(34)
        left = rng.standard_normal(5000)
        right = rng.standard_normal(5000)
        left_before = left.copy()
        right_before = right.copy()

        render_reverb_stereo(left, right, 48000, SpaceConfig())

        np.testing.assert_array_equal(left, left_before)
        np.testing.assert_array_equal(right, right_before)

    def test_empty_stereo_input_returns_empty_finite_channels(self):
        wet_l, wet_r = render_reverb_stereo(
            np.zeros(0),
            np.zeros(0),
            48000,
            SpaceConfig(),
        )
        self.assertEqual(wet_l.shape, (0,))
        self.assertEqual(wet_r.shape, (0,))
        self.assertTrue(np.isfinite(wet_l).all())
        self.assertTrue(np.isfinite(wet_r).all())

    def test_stereo_input_validation(self):
        cfg = SpaceConfig()
        with self.assertRaisesRegex(ValueError, "长度必须一致"):
            render_reverb_stereo(np.zeros(4), np.zeros(5), 48000, cfg)
        with self.assertRaisesRegex(ValueError, "一维实数音频"):
            render_reverb_stereo(np.zeros((2, 2)), np.zeros(4), 48000, cfg)
        with self.assertRaisesRegex(ValueError, "NaN"):
            render_reverb_stereo(
                np.array([0.0, np.nan]),
                np.zeros(2),
                48000,
                cfg,
            )


class ConfigTest(unittest.TestCase):
    def test_from_dict_variants(self):
        self.assertIsNone(SpaceConfig.from_dict(None))
        self.assertIsNone(SpaceConfig.from_dict({"enabled": False}))
        self.assertEqual(SpaceConfig.from_dict({}).name, SpaceConfig().name)
        self.assertEqual(SpaceConfig.from_dict({"wet_db": -10.0}).wet_db, -10.0)

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            SpaceConfig.from_dict({"reverberation": 9})

    def test_non_object_config_is_rejected(self):
        for raw in ("small hall", 1, [], True):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ValueError, "必须是对象"):
                    SpaceConfig.from_dict(raw)

    def test_to_dict_round_trips_every_sound_parameter(self):
        cfg = SpaceConfig(
            name="审计厅",
            wet_db=-9.5,
            room_size=0.7,
            predelay_ms=31.0,
            damping_hz=7200.0,
            highpass_hz=180.0,
            reference_distance_m=4.5,
            distance_exponent=0.75,
            min_send=0.25,
            max_send=2.4,
        )
        encoded = cfg.to_dict()
        self.assertEqual(
            set(encoded),
            {
                "name",
                "wet_db",
                "room_size",
                "predelay_ms",
                "damping_hz",
                "highpass_hz",
                "reference_distance_m",
                "distance_exponent",
                "min_send",
                "max_send",
            },
        )
        self.assertEqual(SpaceConfig.from_dict(encoded), cfg)

    def test_all_numeric_parameters_reject_nan_and_infinity(self):
        fields = (
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
        for field_name in fields:
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field_name, value=value):
                    with self.assertRaisesRegex(ValueError, "有限数值"):
                        SpaceConfig(**{field_name: value})

    def test_numeric_parameters_reject_strings_and_booleans(self):
        for value in ("0.5", True):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "有限数值"):
                    SpaceConfig(room_size=value)

    def test_unstable_or_incoherent_ranges_are_rejected(self):
        invalid = (
            {"wet_db": -121.0},
            {"wet_db": 13.0},
            {"room_size": -0.01},
            {"room_size": 1.01},
            {"predelay_ms": -0.01},
            {"predelay_ms": 2001.0},
            {"damping_hz": -1.0},
            {"damping_hz": 192001.0},
            {"highpass_hz": -1.0},
            {"highpass_hz": 7000.0, "damping_hz": 6500.0},
            {"reference_distance_m": 0.0},
            {"reference_distance_m": 1001.0},
            {"distance_exponent": -0.01},
            {"distance_exponent": 4.01},
            {"min_send": -0.01},
            {"max_send": 8.01},
            {"min_send": 2.0, "max_send": 1.0},
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    SpaceConfig(**values)

    def test_low_sample_rate_frequency_guard_is_explicit_and_reproducible(self):
        cfg = SpaceConfig(highpass_hz=5000.0, damping_hz=6500.0)
        self.assertEqual(
            cfg.effective_filter_frequencies(8000),
            (3600.0, 3920.0),
        )
        self.assertEqual(
            cfg.effective_filter_frequencies(8000),
            cfg.effective_filter_frequencies(8000),
        )

    def test_send_scale_grows_with_distance(self):
        cfg = SpaceConfig()
        near = cfg.send_scale(2.5)
        far = cfg.send_scale(6.0)
        self.assertLess(near, far)  # 越远越湿
        # 夹在 [min_send, max_send] 内
        self.assertGreaterEqual(cfg.send_scale(0.1), cfg.min_send)
        self.assertLessEqual(cfg.send_scale(100.0), cfg.max_send)

    def test_send_scale_rejects_nonfinite_or_nonpositive_distance(self):
        cfg = SpaceConfig()
        for distance in (0.0, -1.0, float("nan"), float("inf"), True):
            with self.subTest(distance=distance):
                with self.assertRaises(ValueError):
                    cfg.send_scale(distance)

    def test_feedback_in_stable_range(self):
        self.assertLess(SpaceConfig(room_size=1.0).feedback, 1.0)
        self.assertGreater(SpaceConfig(room_size=0.0).feedback, 0.0)

    def test_tail_estimate_is_finite_deterministic_and_grows_with_room_size(self):
        small = SpaceConfig(room_size=0.2).tail_seconds(48000)
        large = SpaceConfig(room_size=0.9).tail_seconds(48000)
        self.assertTrue(np.isfinite(small))
        self.assertGreater(small, 0.0)
        self.assertGreater(large, small)
        self.assertEqual(small, SpaceConfig(room_size=0.2).tail_seconds(48000))

    def test_tail_estimate_rejects_invalid_sample_rates(self):
        for sample_rate in (7999, 384001, 48000.0, True):
            with self.subTest(sample_rate=sample_rate):
                with self.assertRaisesRegex(ValueError, "sample_rate"):
                    SpaceConfig().tail_seconds(sample_rate)


if __name__ == "__main__":
    unittest.main()
