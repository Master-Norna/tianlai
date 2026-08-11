from __future__ import annotations

from abc import ABC, abstractmethod
import hashlib
import importlib.util
from pathlib import Path
import sys
from typing import Any

from .canonical_json import canonical_json_bytes
from .events import PerformanceEvent
from .tuning import EqualTemperament


StereoFrame = tuple[float, float]

# Private contract shared by the built-in renderers and ``renderer``.  A
# class must declare this value in its *own* namespace; inherited declarations
# are deliberately ignored so local subclasses keep the ordinary per-frame
# path.
_EVENT_FREE_RENDER_BLOCK_CONTRACT = (
    "tianlai-event-free-render-block-v2"
)


def _render_frame_block(
    render_frame: Any,
    frame_count: int,
    *,
    sample_dtype: Any,
) -> Any:
    """Call one established frame renderer exactly ``frame_count`` times.

    The helper only removes the surrounding Python tuple/list materialisation.
    It intentionally does not vectorise DSP arithmetic, so state transitions
    and every emitted Python float remain identical to repeated
    ``render_frame()`` calls.
    """

    if isinstance(frame_count, bool) or not isinstance(frame_count, int):
        raise ValueError("render block frame_count must be an integer")
    if frame_count < 0:
        raise ValueError("render block frame_count must not be negative")

    import numpy as np

    dtype = np.dtype(sample_dtype)
    if dtype not in (np.dtype(np.float32), np.dtype(np.float64)):
        raise ValueError("render block sample_dtype must be float32 or float64")

    def scalars() -> Any:
        for _ in range(frame_count):
            left, right = render_frame()
            yield left
            yield right

    return np.fromiter(
        scalars(),
        dtype=dtype,
        count=frame_count * 2,
    ).reshape(frame_count, 2)


def factory_manifest_sha256(manifest: dict[str, Any]) -> str:
    """Hash the exact manifest document used to construct an instrument."""

    if not isinstance(manifest, dict):
        raise ValueError("instrument factory manifest must be an object")
    try:
        encoded = canonical_json_bytes(manifest)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"instrument factory manifest is not canonical JSON: {error}"
        ) from error
    return hashlib.sha256(
        b"tianlai-instrument-factory-manifest-v1\0" + encoded
    ).hexdigest()


def _bind_factory_provenance(
    instrument: "Instrument",
    manifest: dict[str, Any],
    *,
    sample_rate: int,
    factory_route: str,
) -> "Instrument":
    """Attach immutable-by-contract construction facts for audit APIs.

    Python callers can mutate private attributes, so this is not a defence
    against a hostile interpreter.  It prevents accidental or forged
    manifest/instance pairing while the Tianlai runtime itself is trusted.
    """

    instrument._tianlai_factory_provenance = {
        "schema_version": 1,
        "manifest_sha256": factory_manifest_sha256(manifest),
        "sample_rate_hz": int(sample_rate),
        "factory_route": factory_route,
    }
    return instrument


class Instrument(ABC):
    """Language-neutral contract implemented by every rendered instrument."""

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self._tianlai_factory_provenance: dict[str, Any] | None = None

    @abstractmethod
    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        raise NotImplementedError

    @abstractmethod
    def render_frame(self) -> StereoFrame:
        raise NotImplementedError

    @property
    @abstractmethod
    def active_voice_count(self) -> int:
        raise NotImplementedError

    def runtime_variant_contract(self) -> dict[str, Any] | None:
        """Declare a narrowly auditable runtime-audio selection contract.

        The default is deliberately uncertifiable.  The onset evidence
        protocol accepts only exact, built-in backend classes from an
        allow-list in :mod:`tianlai.runtime_variants`; a local subclass cannot
        make itself trusted merely by overriding this method.
        """

        return None


def create_instrument(
    manifest: dict[str, Any], sample_rate: int, *, base_directory: str
) -> Instrument:
    implementation = manifest.get("implementation")
    if implementation is not None:
        implementation_path = (Path(base_directory) / str(implementation)).resolve()
        if not implementation_path.is_file():
            raise ValueError(f"instrument implementation does not exist: {implementation_path}")
        module_suffix = hashlib.sha256(str(implementation_path).encode("utf-8")).hexdigest()[:16]
        spec = importlib.util.spec_from_file_location(
            f"tianlai_local_instrument_{module_suffix}", implementation_path
        )
        if spec is None or spec.loader is None:
            raise ValueError(f"cannot load instrument implementation: {implementation_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        factory = getattr(module, "create", None)
        if not callable(factory):
            raise ValueError(f"instrument implementation must define create(): {implementation_path}")
        instrument = factory(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
        if not isinstance(instrument, Instrument):
            raise ValueError("instrument create() must return an Instrument instance")
        return _bind_factory_provenance(
            instrument,
            manifest,
            sample_rate=sample_rate,
            factory_route="local_implementation_factory",
        )

    instrument_type = str(manifest.get("type", ""))
    if instrument_type == "oscillator":
        from .oscillator import OscillatorInstrument

        instrument = OscillatorInstrument.from_manifest(manifest, sample_rate)
    elif instrument_type == "soundfont":
        from .soundfont import SoundFontInstrument

        instrument = SoundFontInstrument.from_manifest(
            manifest, sample_rate, base_directory=base_directory
        )
    elif instrument_type == "synthesizer":
        from .synthesizer import SynthesizerInstrument

        instrument = SynthesizerInstrument.from_manifest(manifest, sample_rate)
    elif instrument_type == "procedural_sfx":
        from .procedural_sfx import ProceduralSfxInstrument

        instrument = ProceduralSfxInstrument.from_manifest(manifest, sample_rate)
    elif instrument_type == "dedicated_sfz":
        from .dedicated_sfz import DedicatedSfzInstrument

        instrument = DedicatedSfzInstrument(sample_rate, manifest, base_directory)
    elif instrument_type == "dedicated_fx":
        from .dedicated_fx import DedicatedFxInstrument

        instrument = DedicatedFxInstrument(sample_rate, manifest, base_directory)
    elif instrument_type == "reversed_cymbal":
        from .reversed_cymbal import ReversedCymbalInstrument

        instrument = ReversedCymbalInstrument(sample_rate, manifest, base_directory)
    elif instrument_type == "melodic_toms":
        from .melodic_toms import MelodicTomsInstrument

        instrument = MelodicTomsInstrument(sample_rate, manifest, base_directory)
    elif instrument_type == "modeled_instrument":
        from .modeled_instruments import ModeledInstrument

        instrument = ModeledInstrument(sample_rate, manifest, base_directory)
    elif instrument_type == "modeled_bianzhong":
        from .bianzhong import create as create_bianzhong

        instrument = create_bianzhong(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "vsco2_viola_section":
        from .vsco2_viola import create as create_vsco2_viola

        instrument = create_vsco2_viola(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "cello":
        from .cello import create as create_cello

        instrument = create_cello(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "violin":
        from .violin import create as create_violin

        instrument = create_violin(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "flute":
        from .flute import create as create_flute

        instrument = create_flute(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "piano":
        from .piano import create as create_piano

        instrument = create_piano(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "mtg_solo_sax":
        from .mtg_sax import create_mtg_sax

        instrument = create_mtg_sax(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "vpo_brass":
        from .vpo_brass import create_vpo_brass

        instrument = create_vpo_brass(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "vpo_woodwind":
        from .vpo_woodwinds import create_vpo_solo_woodwind

        instrument = create_vpo_solo_woodwind(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "vpo_percussion":
        from .vpo_percussion import create_vpo_percussion

        instrument = create_vpo_percussion(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "vpo_solo_string":
        from .vpo_strings import create_vpo_solo_string

        instrument = create_vpo_solo_string(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "vpo_string_section":
        from .vpo_strings import create_vpo_string_section

        instrument = create_vpo_string_section(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "vpo_harp":
        from .vpo_strings import create_vpo_harp

        instrument = create_vpo_harp(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "vpo_celesta":
        from .vpo_specials import create_vpo_celesta

        instrument = create_vpo_celesta(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "vpo_mixed_choir":
        from .vpo_specials import create_vpo_mixed_choir

        instrument = create_vpo_mixed_choir(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "vpo_cowbell":
        from .vpo_specials import create_vpo_cowbell

        instrument = create_vpo_cowbell(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "vpo_orchestral_hit":
        from .vpo_specials import create_vpo_orchestral_hit

        instrument = create_vpo_orchestral_hit(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type == "sample":
        from .sampler import SampleInstrument

        instrument = SampleInstrument.from_manifest(
            manifest,
            sample_rate,
            base_directory=base_directory,
        )
    elif instrument_type != "oscillator":
        raise ValueError(f"unsupported instrument type: {instrument_type!r}")
    return _bind_factory_provenance(
        instrument,
        manifest,
        sample_rate=sample_rate,
        factory_route="builtin_manifest_dispatch_no_implementation",
    )
