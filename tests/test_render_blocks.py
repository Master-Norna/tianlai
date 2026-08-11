from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from tianlai.audio import write_wav_pcm24, write_wav_pcm24_blocks
from tianlai.bianzhong import BianzhongInstrument
from tianlai.cello import CelloInstrument
from tianlai.events import (
    PerformanceDocument,
    PerformanceEvent,
    parse_performance_document,
)
from tianlai.flute import FluteInstrument
from tianlai.instrument import Instrument
from tianlai.melodic_toms import MelodicTomsInstrument
from tianlai.modeled_instruments import (
    ENGINE_VERSION as MODELED_ENGINE_VERSION,
    ModeledInstrument,
)
from tianlai.oscillator import OscillatorInstrument
from tianlai.piano import PianoInstrument
from tianlai.renderer import (
    _exact_builtin_render_block,
    _prefer_dense_event_frame_path,
    _prefer_dense_synth_frame_path,
    _prefer_frame_stream_path,
    render_document,
    render_document_blocks,
)
from tianlai.sampler import (
    SampleInstrument,
    _Region,
    _SampleData,
)
from tianlai.synthesizer import PATCH_PROFILES, SynthesizerInstrument
from tianlai.tuning import EqualTemperament
from tianlai.violin import ViolinInstrument, _ScheduledRelease
from tianlai.vpo_percussion import VpoPercussionInstrument
from tianlai.vsco2_viola import Vsco2ViolaSectionInstrument


def _performance(*, delayed_note: bool = False):
    note_time = 0.08 if delayed_note else 0.0
    return parse_performance_document(
        {
            "sample_rate": 8_000,
            "duration_seconds": 0.24,
            "events": [
                {
                    "time": 0.0,
                    "type": "control",
                    "name": "expression",
                    "value": 0.23,
                },
                {
                    "time": 0.0,
                    "type": "control",
                    "name": "modulation",
                    "value": 0.71,
                },
                {
                    "time": note_time,
                    "type": "note_on",
                    "note_id": 1,
                    "midi_note": 60,
                    "velocity": 0.8,
                },
                {
                    "time": 0.15,
                    "type": "note_off",
                    "note_id": 1,
                },
            ],
        }
    )


def _oscillator() -> OscillatorInstrument:
    return OscillatorInstrument(
        8_000,
        harmonics=(1.0, 0.35, 0.12),
        attack_seconds=0.003,
        release_seconds=0.025,
        gain=0.18,
    )


def _synthesizer() -> SynthesizerInstrument:
    return SynthesizerInstrument(
        8_000,
        patch_name="warm_pad",
        profile=PATCH_PROFILES["warm_pad"],
        note_min=24.0,
        note_max=96.0,
        seed=120_031,
    )


def _modeled() -> ModeledInstrument:
    return ModeledInstrument(
        8_000,
        {
            "engine_version": MODELED_ENGINE_VERSION,
            "profile": "music_box",
            "seed": 9_117,
            "note_min": 36,
            "note_max": 96,
            "gain": 0.35,
        },
        ".",
    )


def _silent_sample() -> SampleInstrument:
    # No event can inspect the placeholder region.  The exact built-in still
    # requires a non-empty catalog at construction, while this fixture is
    # intentionally about an event-free, voice-free span only.
    return SampleInstrument(
        8_000,
        (object(),),  # type: ignore[arg-type]
        release_seconds=0.05,
        velocity_exponent=1.0,
        gain=0.5,
        attack_seconds=0.0,
    )


def _sounding_sample() -> SampleInstrument:
    frames = np.column_stack(
        (
            np.sin(np.arange(4_096, dtype=np.float64) * 0.17),
            np.sin(np.arange(4_096, dtype=np.float64) * 0.17),
        )
    ).astype(np.float32)
    sample = _SampleData(
        Path("synthetic.wav"),
        8_000,
        len(frames),
        1,
        frames,
    )
    region = _Region(
        path=sample.path,
        root_pitch_hz=440.0,
        velocity_min=0.0,
        velocity_max=1.0,
        key_min=None,
        key_max=None,
        gain=0.77,
        pan=0.25,
        delay_seconds=0.0,
        attack_seconds=0.01,
        decay_seconds=0.02,
        sustain_level=0.8,
        release_seconds=0.04,
        offset_frames=0,
        sample_end=None,
        loop_start=128,
        loop_end=4_000,
        loop_mode="loop_sustain",
        stereo_width=1.0,
        stable_key="synthetic.wav",
        native_playback_ratio=1.0,
        pitch_random_cents=0.0,
        amplitude_random_db=0.0,
        delay_random_seconds=0.0,
        round_robin_position=None,
        round_robin_length=None,
        random_min=0.0,
        random_max=1.0,
        sample=sample,
    )
    return SampleInstrument(
        8_000,
        (region,),
        release_seconds=0.04,
        velocity_exponent=0.8,
        gain=0.63,
        attack_seconds=0.0,
    )


class _SilentEngine:
    def __init__(self) -> None:
        self.render_calls = 0
        self.release_calls: list[tuple[int, float]] = []

    def render_frame(self) -> tuple[float, float]:
        self.render_calls += 1
        return 0.0, 0.0

    def release_note(
        self,
        note_id: int,
        *,
        release_seconds: float,
    ) -> None:
        self.release_calls.append((note_id, release_seconds))

    @property
    def active_voice_count(self) -> int:
        return 0


def _bare_piano() -> PianoInstrument:
    instrument = object.__new__(PianoInstrument)
    Instrument.__init__(instrument, 8_000)
    for name in (
        "main",
        "hammer",
        "resonance",
        "resonance_v3",
        "pedal_down",
        "pedal_up",
    ):
        setattr(instrument, name, _SilentEngine())
    return instrument


def _bare_melodic_toms() -> MelodicTomsInstrument:
    instrument = object.__new__(MelodicTomsInstrument)
    Instrument.__init__(instrument, 8_000)
    instrument._engine = _SilentEngine()
    return instrument


def _bare_bianzhong() -> BianzhongInstrument:
    instrument = object.__new__(BianzhongInstrument)
    Instrument.__init__(instrument, 8_000)
    instrument.expression = 1.0
    instrument.expression_target = 1.0
    instrument.modulation = 0.0
    instrument.modulation_target = 0.0
    instrument._expression_coefficient = 0.125
    instrument._modulation_coefficient = 0.0625
    instrument._voices = []
    instrument._active_notes = {}
    return instrument


def _bare_viola() -> Vsco2ViolaSectionInstrument:
    instrument = object.__new__(Vsco2ViolaSectionInstrument)
    Instrument.__init__(instrument, 8_000)
    instrument.engines = {}
    instrument.expression = 1.0
    instrument.expression_target = 1.0
    instrument._expression_coefficient = 0.125
    return instrument


def _bare_cello() -> CelloInstrument:
    instrument = object.__new__(CelloInstrument)
    Instrument.__init__(instrument, 8_000)
    instrument.engines = {}
    instrument.release_tails = []
    instrument.expression = 1.0
    instrument.expression_target = 1.0
    instrument._expression_coefficient = 0.125
    return instrument


def _bare_flute() -> FluteInstrument:
    instrument = object.__new__(FluteInstrument)
    Instrument.__init__(instrument, 8_000)
    instrument.engines = {}
    instrument.expression = 1.0
    instrument.expression_target = 1.0
    instrument.breath = 0.0
    instrument.breath_target = 0.0
    instrument._expression_coefficient = 0.125
    instrument._breath_coefficient = 0.0625
    return instrument


def _bare_violin() -> ViolinInstrument:
    instrument = object.__new__(ViolinInstrument)
    Instrument.__init__(instrument, 8_000)
    instrument.engines = {"accent_attack": _SilentEngine()}
    instrument.expression = 1.0
    instrument.expression_target = 1.0
    instrument._expression_coefficient = 0.125
    instrument._scheduled_accent_releases = [
        _ScheduledRelease(
            engine_name="accent_attack",
            note_id=91,
            remaining_samples=3,
            release_seconds=0.07,
        )
    ]
    return instrument


def _control(name: str, value: float, sequence: int = 0) -> PerformanceEvent:
    return PerformanceEvent(
        sample=0,
        sequence=sequence,
        type="control",
        payload={"name": name, "value": value},
    )


@pytest.mark.parametrize("factory", [_oscillator, _synthesizer, _modeled])
def test_blocks_are_float64_identical_to_the_frame_stream(factory) -> None:
    document = _performance()
    frames, frame_peak = render_document(factory(), document)
    expected = np.asarray(list(frames), dtype=np.float64)

    blocks, block_peak = render_document_blocks(
        factory(),
        document,
        maximum_block_frames=113,
    )
    rendered_blocks = list(blocks)
    actual = np.concatenate(
        [np.asarray(block, dtype=np.float64) for block in rendered_blocks]
    )

    assert actual.dtype == np.float64
    assert actual.shape == expected.shape
    assert actual.tobytes() == expected.tobytes()
    assert block_peak[0] == frame_peak[0]
    assert all(0 < len(block) <= 113 for block in rendered_blocks)


@pytest.mark.parametrize("factory", [_sounding_sample, _modeled])
def test_new_backends_preserve_control_then_delayed_note_bytes(factory) -> None:
    document = _performance(delayed_note=True)
    frames, frame_peak = render_document(factory(), document)
    expected = np.asarray(list(frames), dtype=np.float64)

    blocks, block_peak = render_document_blocks(
        factory(),
        document,
        maximum_block_frames=127,
    )
    actual = np.concatenate(list(blocks))

    assert actual.tobytes() == expected.tobytes()
    assert block_peak[0] == frame_peak[0]


def test_synth_smoothing_advances_during_a_silent_block() -> None:
    document = _performance(delayed_note=True)
    frames, _ = render_document(_synthesizer(), document)
    expected = np.asarray(list(frames), dtype=np.float64)

    blocks, _ = render_document_blocks(
        _synthesizer(),
        document,
        maximum_block_frames=8_000,
    )
    actual = np.concatenate(list(blocks))

    # The note begins after 640 silent frames.  Freezing expression or
    # modulation while zero-filling that rest changes its first samples.
    assert np.count_nonzero(expected[:640]) == 0
    assert actual.tobytes() == expected.tobytes()


class _StatefulCustomInstrument(Instrument):
    def __init__(self) -> None:
        super().__init__(8_000)
        self.frame_index = 0
        self.control = 0.0
        self.render_frame_calls = 0
        self.render_block_calls = 0

    def handle_event(self, event: PerformanceEvent, tuning) -> None:
        del tuning
        if event.type == "control":
            self.control = float(event.payload["value"])

    def render_frame(self) -> tuple[float, float]:
        self.render_frame_calls += 1
        value = self.control + self.frame_index * 1.0e-6
        self.frame_index += 1
        return value, -value

    def render_block(self, frame_count: int):
        self.render_block_calls += 1
        raise AssertionError(f"custom render_block must not run: {frame_count}")

    @property
    def active_voice_count(self) -> int:
        # Deliberately increases without an event.  The custom fallback must
        # retain the frame-by-frame peak observation of render_document.
        return self.frame_index


def test_custom_instrument_ignores_its_untrusted_block_method() -> None:
    document = parse_performance_document(
        {
            "sample_rate": 8_000,
            "duration_seconds": 0.001,
            "events": [
                {
                    "time": 0.0005,
                    "type": "control",
                    "name": "custom",
                    "value": 0.4,
                }
            ],
        }
    )
    instrument = _StatefulCustomInstrument()
    blocks, peak = render_document_blocks(
        instrument,
        document,
        maximum_block_frames=3,
    )
    actual = np.asarray(
        [frame for block in blocks for frame in block],
        dtype=np.float64,
    )

    reference = _StatefulCustomInstrument()
    frames, reference_peak = render_document(reference, document)
    expected = np.asarray(list(frames), dtype=np.float64)
    assert actual.tobytes() == expected.tobytes()
    assert peak[0] == reference_peak[0] == document.total_samples - 1
    assert instrument.render_frame_calls == document.total_samples
    assert instrument.render_block_calls == 0


def test_dense_custom_events_do_not_fragment_transport_blocks() -> None:
    event_count = 1_000
    duration_seconds = 0.2
    document = parse_performance_document(
        {
            "sample_rate": 8_000,
            "duration_seconds": duration_seconds,
            "events": [
                {
                    "time": index * duration_seconds / event_count,
                    "type": "control",
                    "name": "custom",
                    "value": (index % 101) / 100.0,
                }
                for index in range(event_count)
            ],
        }
    )
    expected_instrument = _StatefulCustomInstrument()
    frames, expected_peak = render_document(expected_instrument, document)
    expected = np.asarray(list(frames), dtype=np.float64)

    instrument = _StatefulCustomInstrument()
    blocks, peak = render_document_blocks(
        instrument,
        document,
        maximum_block_frames=257,
    )
    rendered_blocks = list(blocks)
    actual = np.concatenate(rendered_blocks)

    assert len(rendered_blocks) == (
        document.total_samples + 256
    ) // 257
    assert actual.tobytes() == expected.tobytes()
    assert peak[0] == expected_peak[0]
    assert instrument.render_frame_calls == document.total_samples
    assert instrument.render_block_calls == 0


class _OscillatorSubclass(OscillatorInstrument):
    def render_block(self, frame_count: int):
        raise AssertionError(f"inherited built-in subclass was trusted: {frame_count}")


class _SampleSubclass(SampleInstrument):
    pass


class _ModeledSubclass(ModeledInstrument):
    pass


class _SpoofedOscillator(Instrument):
    __module__ = "tianlai.oscillator"
    __qualname__ = "OscillatorInstrument"

    def __init__(self) -> None:
        super().__init__(8_000)
        self.calls = 0

    def handle_event(self, event, tuning) -> None:
        del event, tuning

    def render_frame(self) -> tuple[float, float]:
        self.calls += 1
        return 0.125, -0.125

    def render_block(self, frame_count: int):
        raise AssertionError(f"spoofed class was trusted: {frame_count}")

    @property
    def active_voice_count(self) -> int:
        return 0

    _tianlai_render_block_contract = (
        "tianlai-event-free-render-block-v2"
    )
    _tianlai_original_render_frame = render_frame
    _tianlai_original_render_block = render_block
    _tianlai_original_handle_event = handle_event
    _tianlai_original_active_voice_count = active_voice_count


def test_builtin_subclass_falls_back_to_render_frame() -> None:
    document = parse_performance_document(
        {
            "sample_rate": 8_000,
            "duration_seconds": 0.002,
            "events": [],
        }
    )
    blocks, peak = render_document_blocks(
        _OscillatorSubclass(8_000),
        document,
        maximum_block_frames=5,
    )
    actual = np.asarray([frame for block in blocks for frame in block])
    assert actual.shape == (document.total_samples, 2)
    assert not actual.any()
    assert peak[0] == 0


@pytest.mark.parametrize(
    "instrument",
    [
        _SampleSubclass(
            8_000,
            (object(),),  # type: ignore[arg-type]
            release_seconds=0.05,
            velocity_exponent=1.0,
            gain=0.5,
            attack_seconds=0.0,
        ),
        _ModeledSubclass(
            8_000,
            {
                "engine_version": MODELED_ENGINE_VERSION,
                "profile": "music_box",
                "seed": 9_117,
                "note_min": 36,
                "note_max": 96,
                "gain": 0.35,
            },
            ".",
        ),
    ],
)
def test_new_block_backend_subclasses_remain_on_frame_path(instrument) -> None:
    document = parse_performance_document(
        {
            "sample_rate": 8_000,
            "duration_seconds": 0.002,
            "events": [],
        }
    )
    assert _exact_builtin_render_block(instrument) is None
    blocks, peak = render_document_blocks(
        instrument,
        document,
        maximum_block_frames=5,
    )
    actual = np.concatenate(list(blocks))
    assert actual.shape == (document.total_samples, 2)
    assert not actual.any()
    assert peak[0] == 0


@pytest.mark.parametrize("factory", [_silent_sample, _modeled])
def test_new_exact_backends_zero_fill_a_voice_free_span(factory) -> None:
    instrument = factory()
    render_block = _exact_builtin_render_block(instrument)
    assert render_block is not None
    block = render_block(257, sample_dtype=np.float32)
    assert block.dtype == np.float32
    assert block.flags.c_contiguous
    assert block.shape == (257, 2)
    assert not block.any()


@pytest.mark.parametrize(
    ("dtype", "unsigned_dtype"),
    [(np.float32, np.uint32), (np.float64, np.uint64)],
)
def test_modeled_empty_block_preserves_negative_zero_bits(
    dtype,
    unsigned_dtype,
) -> None:
    def create() -> ModeledInstrument:
        return ModeledInstrument(
            8_000,
            {
                "engine_version": MODELED_ENGINE_VERSION,
                "profile": "music_box",
                "seed": 9_117,
                "note_min": 36,
                "note_max": 96,
                "gain": -0.0,
            },
            ".",
        )

    reference = create()
    expected = np.asarray(
        [reference.render_frame() for _ in range(5)],
        dtype=dtype,
    )
    instrument = create()
    render_block = _exact_builtin_render_block(instrument)
    assert render_block is not None
    actual = render_block(5, sample_dtype=dtype)

    assert actual.tobytes() == expected.tobytes()
    assert np.signbit(actual).all()
    assert (actual.view(unsigned_dtype) != 0).all()


def test_piano_event_free_block_still_advances_all_child_engines() -> None:
    instrument = _bare_piano()
    render_block = _exact_builtin_render_block(instrument)
    assert render_block is not None

    block = render_block(257, sample_dtype=np.float32)

    assert block.dtype == np.float32
    assert block.flags.c_contiguous
    assert block.shape == (257, 2)
    assert not block.any()
    assert all(
        engine.render_calls == 257
        for engine in (
            instrument.main,
            instrument.hammer,
            instrument.resonance,
            instrument.resonance_v3,
            instrument.pedal_down,
            instrument.pedal_up,
        )
    )


@pytest.mark.parametrize("factory", [_bare_melodic_toms, _bare_piano])
def test_nested_engine_instance_override_is_not_hidden_by_silent_block(
    factory,
) -> None:
    instrument = factory()
    engine = (
        instrument._engine
        if isinstance(instrument, MelodicTomsInstrument)
        else instrument.main
    )
    calls = 0

    def patched_render_frame() -> tuple[float, float]:
        nonlocal calls
        calls += 1
        return 0.25, -0.125

    engine.render_frame = patched_render_frame
    render_block = _exact_builtin_render_block(instrument)
    assert render_block is not None
    actual = render_block(5, sample_dtype=np.float64)

    assert calls == 5
    assert np.array_equal(
        actual,
        np.tile(np.asarray([[0.25, -0.125]]), (5, 1)),
    )


def test_block_reloads_render_frame_after_first_frame_self_replacement() -> None:
    def create() -> PianoInstrument:
        instrument = _bare_piano()
        calls = 0

        def installing_child_frame() -> tuple[float, float]:
            nonlocal calls
            calls += 1
            if calls == 1:
                instrument.render_frame = lambda: (0.5, -0.25)
                return 0.125, -0.0625
            return 0.0, 0.0

        instrument.main.render_frame = installing_child_frame
        return instrument

    reference = create()
    expected = np.asarray(
        [reference.render_frame() for _ in range(5)],
        dtype=np.float64,
    )
    instrument = create()
    render_block = _exact_builtin_render_block(instrument)
    assert render_block is not None
    actual = render_block(5, sample_dtype=np.float64)

    assert expected.tolist() == [
        [0.125, -0.0625],
        [0.5, -0.25],
        [0.5, -0.25],
        [0.5, -0.25],
        [0.5, -0.25],
    ]
    assert actual.tobytes() == expected.tobytes()


@pytest.mark.parametrize(
    ("factory", "controls", "state_fields"),
    [
        (
            _bare_bianzhong,
            (_control("expression", 0.23), _control("modulation", 0.71, 1)),
            ("expression", "modulation"),
        ),
        (
            _bare_viola,
            (_control("expression", 0.23),),
            ("expression",),
        ),
        (
            _bare_cello,
            (_control("expression", 0.23),),
            ("expression",),
        ),
        (
            _bare_flute,
            (_control("expression", 0.23), _control("breath", 0.71, 1)),
            ("expression", "breath"),
        ),
    ],
)
def test_central_smoothing_advances_byte_exactly_after_control_events(
    factory,
    controls,
    state_fields,
) -> None:
    reference = factory()
    instrument = factory()
    for event in controls:
        reference.handle_event(event, EqualTemperament())
        instrument.handle_event(event, EqualTemperament())

    expected = np.asarray(
        [reference.render_frame() for _ in range(257)],
        dtype=np.float64,
    )
    render_block = _exact_builtin_render_block(instrument)
    assert render_block is not None
    actual = render_block(257, sample_dtype=np.float64)

    assert actual.tobytes() == expected.tobytes()
    assert tuple(getattr(instrument, name) for name in state_fields) == tuple(
        getattr(reference, name) for name in state_fields
    )


def test_violin_block_preserves_smoothing_and_scheduled_release_order() -> None:
    reference = _bare_violin()
    instrument = _bare_violin()
    event = _control("expression", 0.23)
    reference.handle_event(event, EqualTemperament())
    instrument.handle_event(event, EqualTemperament())

    expected = np.asarray(
        [reference.render_frame() for _ in range(7)],
        dtype=np.float64,
    )
    render_block = _exact_builtin_render_block(instrument)
    assert render_block is not None
    actual = render_block(7, sample_dtype=np.float64)

    assert actual.tobytes() == expected.tobytes()
    assert instrument.expression == reference.expression
    assert instrument._scheduled_accent_releases == []
    assert instrument.engines["accent_attack"].release_calls == [
        (91, 0.07)
    ]
    assert (
        instrument.engines["accent_attack"].release_calls
        == reference.engines["accent_attack"].release_calls
    )


def test_local_central_thin_wrapper_provenance_forces_frame_fallback() -> None:
    instrument = _bare_piano()
    instrument._tianlai_factory_provenance = {
        "schema_version": 1,
        "factory_route": "local_implementation_factory",
    }
    assert _exact_builtin_render_block(instrument) is None

    document = parse_performance_document(
        {
            "sample_rate": 8_000,
            "duration_seconds": 0.001,
            "events": [],
        }
    )
    blocks, peak = render_document_blocks(
        instrument,
        document,
        maximum_block_frames=3,
    )
    actual = np.concatenate(list(blocks))

    assert not actual.any()
    assert peak[0] == 0
    assert all(
        engine.render_calls == document.total_samples
        for engine in (
            instrument.main,
            instrument.hammer,
            instrument.resonance,
            instrument.resonance_v3,
            instrument.pedal_down,
            instrument.pedal_up,
        )
    )


def test_stateful_sample_composite_advances_during_event_free_silence() -> None:
    def create() -> VpoPercussionInstrument:
        instrument = object.__new__(VpoPercussionInstrument)
        Instrument.__init__(instrument, 8_000)
        instrument.engines = {}
        instrument.release_engines = {}
        instrument.expression = 0.2
        instrument.expression_target = 0.9
        instrument._expression_coefficient = 0.125
        return instrument

    reference = create()
    for _ in range(257):
        assert reference.render_frame() == (0.0, 0.0)

    instrument = create()
    render_block = _exact_builtin_render_block(instrument)
    assert render_block is not None
    block = render_block(257, sample_dtype=np.float64)
    assert not block.any()
    assert instrument.expression == reference.expression


@pytest.mark.parametrize("factory", [_silent_sample, _modeled])
def test_new_exact_backend_local_provenance_keeps_frame_path(factory) -> None:
    instrument = factory()
    instrument._tianlai_factory_provenance = {
        "schema_version": 1,
        "factory_route": "local_implementation_factory",
    }
    assert _exact_builtin_render_block(instrument) is None


@pytest.mark.parametrize(
    ("name", "replacement"),
    [
        ("handle_event", lambda _event, _tuning: None),
        ("render_frame", lambda: (0.25, -0.25)),
        (
            "render_block",
            lambda _count, *, sample_dtype="float64": np.zeros(
                (0, 2), dtype=sample_dtype
            ),
        ),
        ("active_voice_count", 0),
    ],
)
def test_new_exact_backend_instance_overrides_keep_frame_path(
    name,
    replacement,
) -> None:
    instrument = _modeled()
    vars(instrument)[name] = replacement
    assert _exact_builtin_render_block(instrument) is None
    assert _prefer_frame_stream_path(
        instrument,
        parse_performance_document(
            {
                "sample_rate": 8_000,
                "duration_seconds": 0.001,
                "events": [],
            }
        ),
    )


def test_new_backend_rechecks_dynamic_method_installation_after_event() -> None:
    instrument = _modeled()

    class _InstallingPayload(dict):
        installed = False

        def __getitem__(self, key):
            value = super().__getitem__(key)
            if key == "note_id" and not self.installed:
                self.installed = True
                instrument.render_frame = lambda: (0.25, -0.25)
            return value

    document = PerformanceDocument(
        sample_rate=8_000,
        channels=2,
        total_samples=4,
        events=(
            PerformanceEvent(
                sample=0,
                sequence=0,
                type="note_on",
                payload=_InstallingPayload(
                    note_id=1,
                    midi_note=60,
                    velocity=0.8,
                ),
            ),
        ),
        tuning=EqualTemperament(),
    )
    blocks, peak = render_document_blocks(instrument, document)
    actual = np.concatenate(list(blocks))
    assert np.array_equal(
        actual,
        np.tile(np.asarray([[0.25, -0.25]]), (4, 1)),
    )
    assert peak[0] == 1


def test_new_backend_preserves_first_event_error_after_silent_prefix() -> None:
    document = parse_performance_document(
        {
            "sample_rate": 8_000,
            "duration_seconds": 0.002,
            "events": [
                {
                    "time": 0.001,
                    "type": "note_on",
                    "note_id": 1,
                    "midi_note": 120,
                    "velocity": 0.8,
                }
            ],
        }
    )
    frames, _ = render_document(_modeled(), document)
    for _ in range(8):
        assert next(frames) == (0.0, 0.0)
    with pytest.raises(ValueError, match="outside declared range"):
        next(frames)

    blocks, _ = render_document_blocks(
        _modeled(),
        document,
        maximum_block_frames=64,
    )
    prefix = next(blocks)
    assert prefix.shape == (8, 2)
    assert not prefix.any()
    with pytest.raises(ValueError, match="outside declared range"):
        next(blocks)


def test_builtin_name_and_private_marker_cannot_spoof_exact_class() -> None:
    document = parse_performance_document(
        {
            "sample_rate": 8_000,
            "duration_seconds": 0.001,
            "events": [],
        }
    )
    instrument = _SpoofedOscillator()
    blocks, _ = render_document_blocks(instrument, document)
    actual = np.concatenate(list(blocks))
    assert instrument.calls == document.total_samples
    assert np.array_equal(
        actual,
        np.tile(np.asarray([[0.125, -0.125]]), (document.total_samples, 1)),
    )


def test_local_exact_builtin_cannot_install_render_frame_during_event() -> None:
    instrument = _oscillator()
    instrument._tianlai_factory_provenance = {
        "schema_version": 1,
        "factory_route": "local_implementation_factory",
    }

    class _InstallingPayload(dict):
        installed = False

        def __getitem__(self, key):
            value = super().__getitem__(key)
            if key == "note_id" and not self.installed:
                self.installed = True
                instrument.render_frame = lambda: (0.25, -0.25)
            return value

    document = PerformanceDocument(
        sample_rate=8_000,
        channels=2,
        total_samples=4,
        events=(
            PerformanceEvent(
                sample=0,
                sequence=0,
                type="note_on",
                payload=_InstallingPayload(
                    note_id=1,
                    midi_note=60,
                    velocity=0.8,
                ),
            ),
        ),
        tuning=EqualTemperament(),
    )
    blocks, peak = render_document_blocks(instrument, document)
    actual = np.concatenate(list(blocks))
    assert np.array_equal(
        actual,
        np.tile(np.asarray([[0.25, -0.25]]), (4, 1)),
    )
    assert peak[0] == 1


def test_block_writer_is_byte_identical_and_preserves_global_error_index(
    tmp_path: Path,
) -> None:
    document = _performance()
    frames, _ = render_document(_oscillator(), document)
    frame_path = tmp_path / "frames.wav"
    assert write_wav_pcm24(
        frame_path,
        frames,
        document.sample_rate,
        reject_out_of_range=True,
    ) == document.total_samples

    blocks, _ = render_document_blocks(
        _oscillator(),
        document,
        maximum_block_frames=97,
    )
    block_path = tmp_path / "blocks.wav"
    assert write_wav_pcm24_blocks(
        block_path,
        blocks,
        document.sample_rate,
        reject_out_of_range=True,
    ) == document.total_samples
    assert hashlib.sha256(block_path.read_bytes()).digest() == hashlib.sha256(
        frame_path.read_bytes()
    ).digest()

    with pytest.raises(ValueError, match="3"):
        write_wav_pcm24_blocks(
            tmp_path / "invalid.wav",
            [
                np.zeros((3, 2), dtype=np.float64),
                np.asarray([[1.01, 0.0]], dtype=np.float64),
            ],
            8_000,
            reject_out_of_range=True,
        )


def test_block_writer_preserves_mixed_generic_and_numpy_order(tmp_path: Path) -> None:
    flattened = [
        (Fraction(1, 10), Fraction(-1, 10)),
        (Fraction(1, 5), Fraction(-1, 5)),
        (0.3, -0.3),
        (0.4, -0.4),
        (Fraction(1, 2), Fraction(-1, 2)),
    ]
    expected = tmp_path / "mixed-frames.wav"
    actual = tmp_path / "mixed-blocks.wav"
    assert write_wav_pcm24(expected, flattened, 8_000) == 5
    assert write_wav_pcm24_blocks(
        actual,
        [
            flattened[:2],
            np.asarray(flattened[2:4], dtype=np.float64),
            flattened[4:],
        ],
        8_000,
    ) == 5
    assert actual.read_bytes() == expected.read_bytes()


@pytest.mark.parametrize("invalid", [0, -1, True, 1.5])
def test_block_size_must_be_a_positive_integer(invalid) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        render_document_blocks(
            _oscillator(),
            _performance(),
            maximum_block_frames=invalid,
        )


@pytest.mark.parametrize("factory", [_oscillator, _synthesizer, _modeled])
def test_float32_blocks_match_direct_frame_conversion(factory) -> None:
    document = _performance()
    frames, frame_peak = render_document(factory(), document)
    expected = np.fromiter(
        (sample for frame in frames for sample in frame),
        dtype=np.float32,
        count=document.total_samples * 2,
    ).reshape(document.total_samples, 2)
    blocks, block_peak = render_document_blocks(
        factory(),
        document,
        maximum_block_frames=127,
        sample_dtype=np.float32,
    )
    rendered = list(blocks)
    actual = np.concatenate(rendered)
    assert all(block.dtype == np.float32 for block in rendered)
    assert actual.tobytes() == expected.tobytes()
    assert block_peak[0] == frame_peak[0]


def test_dense_synth_selector_is_conservative_and_does_not_mutate_state() -> None:
    def workload(note_on: float, note_off: float) -> PerformanceDocument:
        return parse_performance_document(
            {
                "sample_rate": 8_000,
                "duration_seconds": 3.0,
                "events": [
                    {
                        "time": note_on,
                        "type": "note_on",
                        "note_id": 1,
                        "midi_note": 60,
                        "velocity": 0.8,
                    },
                    {
                        "time": note_off,
                        "type": "note_off",
                        "note_id": 1,
                    },
                ],
            }
        )

    instrument = _synthesizer()
    before = dict(vars(instrument))
    assert _prefer_dense_synth_frame_path(
        instrument,
        workload(0.0, 2.5),
    )
    assert not _prefer_dense_synth_frame_path(
        instrument,
        workload(2.7, 2.8),
    )
    assert vars(instrument) == before


def _control_group_document(
    *,
    total_samples: int,
    event_samples: tuple[int, ...],
    duplicates: int = 1,
) -> PerformanceDocument:
    events: list[PerformanceEvent] = []
    sequence = 0
    for sample in event_samples:
        for duplicate in range(duplicates):
            events.append(
                PerformanceEvent(
                    sample=sample,
                    sequence=sequence,
                    type="control",
                    payload={
                        "name": (
                            "expression"
                            if duplicate % 2 == 0
                            else "modulation"
                        ),
                        "value": (sequence % 101) / 100.0,
                    },
                )
            )
            sequence += 1
    return PerformanceDocument(
        sample_rate=8_000,
        channels=2,
        total_samples=total_samples,
        events=tuple(events),
        tuning=EqualTemperament(),
    )


def test_every_frame_controls_fall_back_to_large_stream_blocks_byte_exactly() -> None:
    document = _control_group_document(
        total_samples=1_024,
        event_samples=tuple(range(1_024)),
    )
    assert _prefer_dense_event_frame_path(document)
    assert _prefer_frame_stream_path(_modeled(), document)

    reference = _modeled()
    frames, reference_peak = render_document(reference, document)
    expected = np.asarray(list(frames), dtype=np.float64)
    instrument = _modeled()
    blocks, peak = render_document_blocks(
        instrument,
        document,
        maximum_block_frames=257,
    )
    rendered = list(blocks)
    actual = np.concatenate(rendered)

    assert len(rendered) == 4
    assert actual.tobytes() == expected.tobytes()
    assert peak[0] == reference_peak[0]
    assert instrument.expression == reference.expression
    assert instrument.modulation == reference.modulation


def test_many_events_on_the_same_samples_do_not_trigger_dense_fallback() -> None:
    document = _control_group_document(
        total_samples=2_048,
        event_samples=(0, 1_024),
        duplicates=300,
    )

    assert len(document.events) == 600
    assert not _prefer_dense_event_frame_path(document)
    assert not _prefer_frame_stream_path(_modeled(), document)


def test_dense_event_selector_has_explicit_group_and_density_boundaries() -> None:
    samples_256 = tuple(range(0, 4_096, 16))
    assert len(samples_256) == 256
    at_boundary = _control_group_document(
        total_samples=4_096,
        event_samples=samples_256,
    )
    one_frame_below_density = _control_group_document(
        total_samples=4_097,
        event_samples=samples_256,
    )
    below_group_minimum = _control_group_document(
        total_samples=4_080,
        event_samples=samples_256[:-1],
    )
    native_at_32_frames = _control_group_document(
        total_samples=8_192,
        event_samples=tuple(range(0, 8_192, 32)),
    )

    assert _prefer_dense_event_frame_path(at_boundary)
    assert _prefer_frame_stream_path(_modeled(), at_boundary)
    assert not _prefer_dense_event_frame_path(one_frame_below_density)
    assert not _prefer_dense_event_frame_path(below_group_minimum)
    assert not _prefer_dense_event_frame_path(native_at_32_frames)
    assert not _prefer_frame_stream_path(_modeled(), native_at_32_frames)


def test_sparse_unsorted_handbuilt_document_uses_established_stream() -> None:
    document = _control_group_document(
        total_samples=64,
        event_samples=(32, 16),
    )

    assert len(document.events) < 256
    assert _prefer_dense_event_frame_path(document)
    assert _prefer_frame_stream_path(_modeled(), document)


def test_production_frame_selector_only_accelerates_exact_sparse_builtins() -> None:
    sparse = parse_performance_document(
        {
            "sample_rate": 8_000,
            "duration_seconds": 3.0,
            "events": [
                {
                    "time": 2.7,
                    "type": "note_on",
                    "note_id": 1,
                    "midi_note": 60,
                    "velocity": 0.8,
                },
                {"time": 2.8, "type": "note_off", "note_id": 1},
            ],
        }
    )
    dense = parse_performance_document(
        {
            "sample_rate": 8_000,
            "duration_seconds": 3.0,
            "events": [
                {
                    "time": 0.0,
                    "type": "note_on",
                    "note_id": 1,
                    "midi_note": 60,
                    "velocity": 0.8,
                },
                {"time": 2.5, "type": "note_off", "note_id": 1},
            ],
        }
    )

    assert not _prefer_frame_stream_path(_oscillator(), sparse)
    assert not _prefer_frame_stream_path(_synthesizer(), sparse)
    assert _prefer_frame_stream_path(_synthesizer(), dense)
    assert not _prefer_frame_stream_path(_modeled(), sparse)
    assert not _prefer_frame_stream_path(_silent_sample(), sparse)
    assert _prefer_frame_stream_path(_OscillatorSubclass(8_000), sparse)

    local = _oscillator()
    local._tianlai_factory_provenance = {
        "schema_version": 1,
        "factory_route": "local_implementation_factory",
    }
    assert _prefer_frame_stream_path(local, sparse)


@pytest.mark.parametrize("invalid", ["float16", "int32", object])
def test_block_sample_dtype_is_float32_or_float64(invalid) -> None:
    with pytest.raises(ValueError, match="float32 or float64"):
        render_document_blocks(
            _oscillator(),
            _performance(),
            sample_dtype=invalid,
        )
