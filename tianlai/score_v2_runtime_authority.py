"""Short-lived runtime authority for the first formal Score-v2 render slice.

The serialised documents in this module are evidence only.  Rendering rights
exist exclusively while :func:`open_score_v2_oscillator_runtime_authority`
holds a live, registered lease.  The first slice deliberately supports one
built-in oscillator executor with a declared empty external-asset graph.
Sampled backends, custom factories and lazy assets remain fail-closed until a
descriptor-backed asset lease exists for them.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import marshal
import math
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from types import FunctionType, ModuleType
from typing import Any, Iterator, NamedTuple
import weakref

from .canonical_json import canonical_json_bytes
from .events import PerformanceEvent
from .oscillator import OscillatorInstrument
from .score_v2_performance import (
    ScoreV2PerformanceBundle,
    ScoreV2PerformanceError,
)
from .score_v2_runtime_source import (
    NO_EXTERNAL_ASSET_INVENTORY_STATUS,
    ScoreV2RuntimeSourceError,
)
from .tuning import EqualTemperament


SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_KIND = (
    "tianlai.score_v2_runtime_authority_acquisition"
)
SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_SCHEMA_VERSION = 1
SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_CONTRACT = (
    "score-v2-active-runtime-lease-single-oscillator-v1"
)
SCORE_V2_RUNTIME_AUTHORITY_KIND = "tianlai.score_v2_runtime_authority"
SCORE_V2_RUNTIME_AUTHORITY_SCHEMA_VERSION = 1
SCORE_V2_RUNTIME_AUTHORITY_CONTRACT = (
    "score-v2-runtime-authority-single-oscillator-asset-free-v1"
)
SCORE_V2_RUNTIME_FACTORY_ROUTE = (
    "score_v2_runtime_authority_builtin_oscillator_v1"
)

_HEX = frozenset("0123456789abcdef")
_MAX_BLOCK_FRAMES = 65_536
_READ_CHUNK = 1024 * 1024
_REPARSE_POINT = 0x400


class ScoreV2RuntimeAuthorityError(ValueError):
    """A stable failure at the live Score-v2 authority boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.message_key = f"scoreV2RuntimeAuthority.{code.replace('.', '_')}"
        super().__init__(code)


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _add_note_safely(error: BaseException, note: str) -> None:
    try:
        error.add_note(note)
    except BaseException:
        pass


def _hash_descriptor(descriptor: int, *, expected_size: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    observed = 0
    while True:
        chunk = os.read(descriptor, _READ_CHUNK)
        if not chunk:
            break
        observed += len(chunk)
        if observed > expected_size:
            raise OSError("runtime authority source grew while hashing")
        digest.update(chunk)
    if observed != expected_size:
        raise OSError("runtime authority source size changed while hashing")
    return digest.hexdigest()


def _read_descriptor(descriptor: int, *, expected_size: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    observed = 0
    while True:
        chunk = os.read(descriptor, _READ_CHUNK)
        if not chunk:
            break
        observed += len(chunk)
        if observed > expected_size:
            raise OSError("runtime authority source grew while reading")
        chunks.append(chunk)
    if observed != expected_size:
        raise OSError("runtime authority source size changed while reading")
    return b"".join(chunks)


class _HeldSource(NamedTuple):
    label: str
    path: str
    descriptor: int
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.label,
            "sha256": self.sha256,
            "size_bytes": self.size,
        }


def _plain_status(value: os.stat_result) -> bool:
    return (
        stat.S_ISREG(value.st_mode)
        and int(value.st_nlink) == 1
        and int(value.st_ino) != 0
        and not bool(
            getattr(value, "st_file_attributes", 0) & _REPARSE_POINT
        )
    )


def _is_windows_runtime() -> bool:
    return os.name == "nt"


def _same_creation_time_when_available(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    left_value = getattr(left, "st_birthtime_ns", None)
    right_value = getattr(right, "st_birthtime_ns", None)
    return (
        left_value is None
        or right_value is None
        or int(left_value) == int(right_value)
    )


def _same_source_object(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    """Compare one path stat with one descriptor stat.

    Supported Windows runtimes do not guarantee identical ``st_ctime_ns``
    semantics for path-based stat and descriptor-based fstat.  The stable file
    ID, size and mtime remain mandatory; separately exposed creation time is
    compared when both stat views provide it.
    Descriptor-to-descriptor comparisons below continue to require ctime too.
    """

    return (
        int(left.st_dev) == int(right.st_dev)
        and int(left.st_ino) == int(right.st_ino)
        and int(left.st_size) == int(right.st_size)
        and int(left.st_mtime_ns) == int(right.st_mtime_ns)
        and _same_creation_time_when_available(left, right)
        and (
            _is_windows_runtime()
            or int(left.st_ctime_ns) == int(right.st_ctime_ns)
        )
    )


def _same_handle_snapshot(
    left: os.stat_result,
    right: os.stat_result,
) -> bool:
    return _same_source_object(left, right) and int(
        left.st_ctime_ns
    ) == int(right.st_ctime_ns)


def _open_held_source(
    path: Path,
    *,
    label: str,
    expected_sha256: str,
) -> _HeldSource:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        try:
            os.set_inheritable(descriptor, False)
        except OSError:
            pass
        opened = os.fstat(descriptor)
        current = os.lstat(path)
        if (
            not _plain_status(opened)
            or not _plain_status(current)
            or int(opened.st_dev) != int(current.st_dev)
            or int(opened.st_ino) != int(current.st_ino)
            or int(opened.st_size) != int(current.st_size)
        ):
            raise OSError("runtime authority source is not one plain generation")
        digest = _hash_descriptor(descriptor, expected_size=int(opened.st_size))
        finished = os.fstat(descriptor)
        final_path = os.lstat(path)
        if (
            digest != expected_sha256
            or not _plain_status(finished)
            or not _plain_status(final_path)
            or not _same_handle_snapshot(opened, finished)
            or not _same_source_object(finished, final_path)
        ):
            raise OSError("runtime authority source digest changed")
        return _HeldSource(
            label=label,
            path=str(path),
            descriptor=descriptor,
            device=int(opened.st_dev),
            inode=int(opened.st_ino),
            size=int(opened.st_size),
            modified_ns=int(opened.st_mtime_ns),
            changed_ns=int(opened.st_ctime_ns),
            sha256=digest,
        )
    except BaseException:
        os.close(descriptor)
        raise


def _revalidate_held_source(source: _HeldSource, *, full_digest: bool) -> None:
    current = os.fstat(source.descriptor)
    path_status = os.lstat(source.path)
    if (
        not _plain_status(current)
        or not _plain_status(path_status)
        or any(
            int(getattr(current, name)) != expected
            for name, expected in (
                ("st_dev", source.device),
                ("st_ino", source.inode),
                ("st_size", source.size),
                ("st_mtime_ns", source.modified_ns),
                ("st_ctime_ns", source.changed_ns),
            )
        )
        or not _same_source_object(current, path_status)
    ):
        raise OSError("runtime authority source generation changed")
    if full_digest and _hash_descriptor(
        source.descriptor,
        expected_size=source.size,
    ) != source.sha256:
        raise OSError("runtime authority source bytes changed")


def _project_path(project_root: Path, label: object) -> Path:
    if type(label) is not str or not label or "\\" in label or ":" in label:
        raise ScoreV2RuntimeAuthorityError("authority.runtime_fingerprint_invalid")
    pure = PurePosixPath(label)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ScoreV2RuntimeAuthorityError("authority.runtime_fingerprint_invalid")
    path = project_root.joinpath(*pure.parts).resolve()
    if not path.is_relative_to(project_root) or not path.is_file():
        raise ScoreV2RuntimeAuthorityError("authority.runtime_fingerprint_invalid")
    return path


def _derive_project_root(manifest_path: Path, manifest_label: object) -> Path:
    if type(manifest_label) is not str:
        raise ScoreV2RuntimeAuthorityError("authority.runtime_fingerprint_invalid")
    pure = PurePosixPath(manifest_label)
    if pure.is_absolute() or not pure.parts or any(
        part in {"", ".", ".."} for part in pure.parts
    ):
        raise ScoreV2RuntimeAuthorityError("authority.runtime_fingerprint_invalid")
    root = manifest_path.resolve()
    for expected in reversed(pure.parts):
        if root.name != expected:
            raise ScoreV2RuntimeAuthorityError("authority.runtime_layout_mismatch")
        root = root.parent
    return root.resolve()


def _code_sha256(value: object) -> str:
    if not isinstance(value, FunctionType):
        raise ScoreV2RuntimeAuthorityError("authority.loaded_code_invalid")
    return hashlib.sha256(
        b"tianlai-score-v2-loaded-code-v1\0" + marshal.dumps(value.__code__)
    ).hexdigest()


class _LoadedRoot(NamedTuple):
    label: str
    owner: object
    attribute: str
    value: object
    code: object | None


def _function_root(label: str, owner: object, attribute: str) -> _LoadedRoot:
    namespace = getattr(owner, "__dict__", None)
    if type(namespace) is not dict and not hasattr(namespace, "__getitem__"):
        raise ScoreV2RuntimeAuthorityError("authority.loaded_code_invalid")
    value = namespace[attribute]
    if isinstance(value, classmethod):
        code_value: object = value.__func__
    elif isinstance(value, property):
        code_value = value.fget
    else:
        code_value = value
    if not isinstance(code_value, FunctionType):
        raise ScoreV2RuntimeAuthorityError("authority.loaded_code_invalid")
    return _LoadedRoot(label, owner, attribute, value, code_value.__code__)


def _module_root(label: str, owner: ModuleType, attribute: str) -> _LoadedRoot:
    value = owner.__dict__.get(attribute)
    code = value.__code__ if isinstance(value, FunctionType) else None
    return _LoadedRoot(label, owner, attribute, value, code)


def _loaded_roots() -> tuple[_LoadedRoot, ...]:
    from . import events as events_module
    from . import instrument as instrument_module
    from . import oscillator as oscillator_module
    from . import tuning as tuning_module

    return (
        _module_root("instrument.class", instrument_module, "Instrument"),
        _function_root(
            "instrument.init",
            instrument_module.Instrument,
            "__init__",
        ),
        _module_root("instrument.bind_factory", instrument_module, "_bind_factory_provenance"),
        _module_root("instrument.manifest_hash", instrument_module, "factory_manifest_sha256"),
        _module_root("events.pitch_hz", events_module, "event_pitch_hz"),
        _module_root("oscillator.class", oscillator_module, "OscillatorInstrument"),
        _function_root("oscillator.init", OscillatorInstrument, "__init__"),
        _function_root("oscillator.from_manifest", OscillatorInstrument, "from_manifest"),
        _function_root("oscillator.begin_release", OscillatorInstrument, "_begin_release"),
        _function_root("oscillator.handle_event", OscillatorInstrument, "handle_event"),
        _function_root("oscillator.render_frame", OscillatorInstrument, "render_frame"),
        _function_root("oscillator.active_voice_count", OscillatorInstrument, "active_voice_count"),
        _module_root("oscillator.voice", oscillator_module, "_Voice"),
        _function_root("oscillator.voice_init", oscillator_module._Voice, "__init__"),
        _module_root("oscillator.event_pitch_hz", oscillator_module, "event_pitch_hz"),
        _module_root("oscillator.math", oscillator_module, "math"),
        _module_root("tuning.equal_temperament", tuning_module, "EqualTemperament"),
        _module_root("tuning.math", tuning_module, "math"),
        _function_root("tuning.note_to_hz", EqualTemperament, "note_to_hz"),
    )


def _root_current(root: _LoadedRoot) -> object:
    namespace = getattr(root.owner, "__dict__", None)
    if namespace is None:
        raise ScoreV2RuntimeAuthorityError("authority.loaded_code_changed")
    try:
        return namespace[root.attribute]
    except (KeyError, TypeError) as exc:
        raise ScoreV2RuntimeAuthorityError("authority.loaded_code_changed") from exc


def _root_function(root: _LoadedRoot) -> FunctionType | None:
    value = root.value
    if isinstance(value, classmethod):
        return value.__func__
    if isinstance(value, property):
        return value.fget
    return value if isinstance(value, FunctionType) else None


def _root_source_module(root: _LoadedRoot) -> str:
    owner = root.owner
    if isinstance(owner, ModuleType):
        module_name = owner.__name__
    else:
        module_name = getattr(owner, "__module__", None)
    if type(module_name) is not str or not module_name.startswith("tianlai."):
        raise ScoreV2RuntimeAuthorityError("authority.loaded_code_invalid")
    return module_name


def _bind_loaded_source_modules(
    roots: tuple[_LoadedRoot, ...],
    *,
    closure_files: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Bind each retained Python root to the matching closure source bytes."""

    result: dict[str, dict[str, str]] = {}
    for module_name in sorted({_root_source_module(root) for root in roots}):
        label = f"{module_name.replace('.', '/')}.py"
        expected = closure_files.get(label)
        module = sys.modules.get(module_name)
        raw_path = getattr(module, "__file__", None)
        if not _is_sha256(expected) or type(raw_path) is not str or not raw_path:
            raise ScoreV2RuntimeAuthorityError(
                "authority.loaded_source_mismatch"
            )
        path = Path(raw_path).resolve()
        if path.suffix in {".pyc", ".pyo"}:
            path = path.with_suffix(".py")
        probe: _HeldSource | None = None
        try:
            probe = _open_held_source(
                path,
                label=label,
                expected_sha256=expected,
            )
        except OSError as exc:
            raise ScoreV2RuntimeAuthorityError(
                "authority.loaded_source_mismatch"
            ) from exc
        finally:
            if probe is not None:
                os.close(probe.descriptor)
        result[module_name] = {
            "path": label,
            "sha256": expected,
        }
    return result


def _loaded_projection(
    roots: tuple[_LoadedRoot, ...],
    *,
    source_modules: dict[str, dict[str, str]],
) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for root in roots:
        function = _root_function(root)
        module_name = _root_source_module(root)
        records.append(
            {
                "label": root.label,
                "kind": "python_function" if function is not None else "python_object",
                "module": getattr(function or root.value, "__module__", None),
                "qualname": getattr(function or root.value, "__qualname__", None),
                "code_sha256": _code_sha256(function) if function is not None else None,
                "source": dict(source_modules[module_name]),
            }
        )
    return {
        "algorithm": "tianlai-loaded-python-object-projection-v1",
        "roots": records,
    }


def _revalidate_loaded_roots(roots: tuple[_LoadedRoot, ...]) -> None:
    for root in roots:
        current = _root_current(root)
        if current is not root.value:
            raise ScoreV2RuntimeAuthorityError("authority.loaded_code_changed")
        function = _root_function(root)
        if function is not None and function.__code__ is not root.code:
            raise ScoreV2RuntimeAuthorityError("authority.loaded_code_changed")


@dataclass(slots=True)
class _AuthorityGeneration:
    bundle: ScoreV2PerformanceBundle
    bundle_sha256: str
    runtime_source_sha256: str
    capability_plan_sha256: str
    capability_source_sha256: str
    roster_projection_sha256: str
    executor_order: int
    executor_id: str
    part_id: str
    sample_rate: int
    frame_count: int
    effective_manifest: dict[str, Any]
    effective_manifest_sha256: str
    manifest_raw_sha256: str
    manifest_bytes: bytes
    held_sources: tuple[_HeldSource, ...]
    roots: tuple[_LoadedRoot, ...]
    loaded_projection: dict[str, object]
    loaded_projection_sha256: str
    instrument: OscillatorInstrument
    handle_event: FunctionType
    render_frame: FunctionType
    active_voice_getter: FunctionType
    tuning: EqualTemperament
    factory_static_state: dict[str, object]
    math_module: ModuleType
    math_sin: object
    math_cos: object
    math_isfinite: object
    math_pi: float
    math_tau: float
    numpy_module: ModuleType
    numpy_version: str
    numpy_empty: object
    numpy_isfinite: object
    numpy_max: object
    numpy_abs: object
    numpy_ascontiguousarray: object
    numpy_frombuffer: object
    factory_generation_sha256: str
    acquisition_bytes: bytes
    acquisition_sha256: str
    active: bool = True
    execution_finished: bool = False
    consumption_bytes: bytes | None = None
    consumption_sha256: str | None = None
    dispatched_events: int = 0
    rendered_frames: int = 0


_LEASES: dict[int, tuple[weakref.ReferenceType[object], _AuthorityGeneration]] = {}


@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class ScoreV2OscillatorRuntimeAuthority:
    """Unforgeable process-local handle to one active oscillator generation."""

    _token: object = field(repr=False, compare=False)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ScoreV2OscillatorRuntimeAuthority cannot be subclassed")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("runtime authority must be acquired through its context manager")

    def _generation(self, *, allow_finished: bool = True) -> _AuthorityGeneration:
        registered = _LEASES.get(id(self))
        if (
            registered is None
            or registered[0]() is not self
            or self._token is not registered[1]
            or not registered[1].active
            or (not allow_finished and registered[1].execution_finished)
        ):
            raise ScoreV2RuntimeAuthorityError("authority.lease_inactive")
        return registered[1]

    @property
    def executor_id(self) -> str:
        return self._generation().executor_id

    @property
    def part_id(self) -> str:
        return self._generation().part_id

    @property
    def sample_rate(self) -> int:
        return self._generation().sample_rate

    @property
    def frame_count(self) -> int:
        return self._generation().frame_count

    @property
    def performance_bundle_sha256(self) -> str:
        return self._generation().bundle_sha256

    @property
    def runtime_source_sha256(self) -> str:
        return self._generation().runtime_source_sha256

    @property
    def effective_manifest_sha256(self) -> str:
        return self._generation().effective_manifest_sha256

    @property
    def manifest_raw_sha256(self) -> str:
        return self._generation().manifest_raw_sha256

    @property
    def manifest_bytes(self) -> bytes:
        return bytes(self._generation().manifest_bytes)

    @property
    def factory_generation_sha256(self) -> str:
        return self._generation().factory_generation_sha256

    @property
    def loaded_code_generation_sha256(self) -> str:
        return self._generation().loaded_projection_sha256

    @property
    def acquisition_canonical_bytes(self) -> bytes:
        return bytes(self._generation().acquisition_bytes)

    @property
    def acquisition_sha256(self) -> str:
        return self._generation().acquisition_sha256

    def checkpoint(self, *, full_sources: bool = True) -> None:
        generation = self._generation()
        _checkpoint_generation(generation, full_sources=full_sources)

    def dispatch_event(self, event: PerformanceEvent) -> None:
        generation = self._generation(allow_finished=False)
        if type(event) is not PerformanceEvent:
            raise TypeError("event must be PerformanceEvent")
        try:
            _checkpoint_generation(generation, full_sources=False)
            generation.handle_event(
                generation.instrument,
                event,
                generation.tuning,
            )
            generation.dispatched_events += 1
            _checkpoint_generation(generation, full_sources=False)
        except BaseException:
            generation.active = False
            raise

    def render_block(self, frame_count: int) -> Any:
        generation = self._generation(allow_finished=False)
        if type(frame_count) is not int or not 1 <= frame_count <= _MAX_BLOCK_FRAMES:
            raise ValueError("frame_count must be an integer from 1 to 65536")
        if generation.rendered_frames + frame_count > generation.frame_count:
            raise ScoreV2RuntimeAuthorityError(
                "authority.frame_budget_exceeded"
        )
        try:
            _checkpoint_generation(generation, full_sources=False)
            result = generation.numpy_empty(
                (frame_count, 2),
                dtype="<f8",
            )
            for index in range(frame_count):
                left, right = generation.render_frame(generation.instrument)
                result[index, 0] = left
                result[index, 1] = right
            if (
                not bool(generation.numpy_isfinite(result).all())
                or float(
                    generation.numpy_max(
                        generation.numpy_abs(result),
                        initial=0.0,
                    )
                )
                > 1.0
            ):
                raise ScoreV2RuntimeAuthorityError(
                    "authority.audio_block_invalid"
                )
            generation.rendered_frames += frame_count
            _checkpoint_generation(generation, full_sources=False)
            payload = generation.numpy_ascontiguousarray(
                result,
                dtype="<f8",
            ).tobytes(order="C")
            return generation.numpy_frombuffer(payload, dtype="<f8").reshape(
                frame_count,
                2,
            )
        except BaseException:
            generation.active = False
            raise

    def active_voice_count(self) -> int:
        generation = self._generation()
        try:
            _checkpoint_generation(generation, full_sources=False)
            value = generation.active_voice_getter(generation.instrument)
            if type(value) is not int or value < 0:
                raise ScoreV2RuntimeAuthorityError(
                    "authority.instance_generation_changed"
                )
            _checkpoint_generation(generation, full_sources=False)
            return value
        except BaseException:
            generation.active = False
            raise

    def finish_execution(self) -> dict[str, object]:
        generation = self._generation(allow_finished=False)
        try:
            _checkpoint_generation(generation, full_sources=True)
            generation.execution_finished = True
            document = _consumption_document(generation)
            payload = canonical_json_bytes(document)
            digest = hashlib.sha256(payload).hexdigest()
            generation.consumption_bytes = payload
            generation.consumption_sha256 = digest
            return json.loads(payload)
        except BaseException:
            generation.active = False
            raise

    @property
    def consumed_canonical_bytes(self) -> bytes:
        generation = self._generation()
        if not generation.execution_finished or generation.consumption_bytes is None:
            raise ScoreV2RuntimeAuthorityError("authority.execution_not_finished")
        return bytes(generation.consumption_bytes)

    @property
    def consumed_sha256(self) -> str:
        generation = self._generation()
        if not generation.execution_finished or generation.consumption_sha256 is None:
            raise ScoreV2RuntimeAuthorityError("authority.execution_not_finished")
        return generation.consumption_sha256


def _register(
    authority: ScoreV2OscillatorRuntimeAuthority,
    generation: _AuthorityGeneration,
) -> None:
    authority_id = id(authority)

    def remove(reference: weakref.ReferenceType[object]) -> None:
        current = _LEASES.get(authority_id)
        if current is not None and current[0] is reference:
            _LEASES.pop(authority_id, None)

    reference = weakref.ref(authority, remove)
    _LEASES[authority_id] = (reference, generation)


def _factory_provenance_ok(generation: _AuthorityGeneration) -> bool:
    provenance = getattr(generation.instrument, "_tianlai_factory_provenance", None)
    try:
        return (
            type(generation.instrument) is OscillatorInstrument
            and type(provenance) is dict
            and set(provenance)
            == {"schema_version", "manifest_sha256", "sample_rate_hz", "factory_route"}
            and provenance.get("schema_version") == 1
            and provenance.get("manifest_sha256") == generation.effective_manifest_sha256
            and provenance.get("sample_rate_hz") == generation.sample_rate
            and provenance.get("factory_route") == SCORE_V2_RUNTIME_FACTORY_ROUTE
            and type(generation.instrument.sample_rate) is int
            and generation.instrument.sample_rate == generation.sample_rate
            and _oscillator_static_state(generation.instrument)
            == generation.factory_static_state
            and not any(
                name in getattr(generation.instrument, "__dict__", {})
                for name in (
                    "_begin_release",
                    "handle_event",
                    "render_frame",
                    "active_voice_count",
                )
            )
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _oscillator_static_state(
    instrument: OscillatorInstrument,
) -> dict[str, object]:
    harmonics = instrument.harmonics
    numeric = (
        instrument.harmonic_normalizer,
        instrument.gain,
        instrument.velocity_exponent,
        instrument.pan,
    )
    if (
        type(harmonics) is not tuple
        or not harmonics
        or any(type(item) is not float or not math.isfinite(item) for item in harmonics)
        or any(type(item) is not float or not math.isfinite(item) for item in numeric)
        or type(instrument.attack_samples) is not int
        or instrument.attack_samples < 1
        or type(instrument.release_samples) is not int
        or instrument.release_samples < 1
        or not -1.0 <= instrument.pan <= 1.0
        or instrument.harmonic_normalizer <= 0.0
    ):
        raise ValueError("invalid oscillator factory state")
    return {
        "harmonics": list(harmonics),
        "harmonic_normalizer": instrument.harmonic_normalizer,
        "attack_samples": instrument.attack_samples,
        "release_samples": instrument.release_samples,
        "gain": instrument.gain,
        "velocity_exponent": instrument.velocity_exponent,
        "pan": instrument.pan,
    }


def _checkpoint_generation(
    generation: _AuthorityGeneration,
    *,
    full_sources: bool,
) -> None:
    try:
        if not generation.active:
            raise ValueError
        if generation.bundle.artifact_sha256 != generation.bundle_sha256:
            raise ValueError
        _revalidate_loaded_roots(generation.roots)
        if (
            sys.modules.get("numpy") is not generation.numpy_module
            or str(getattr(generation.numpy_module, "__version__", ""))
            != generation.numpy_version
            or generation.numpy_module.empty is not generation.numpy_empty
            or generation.numpy_module.isfinite is not generation.numpy_isfinite
            or generation.numpy_module.max is not generation.numpy_max
            or generation.numpy_module.abs is not generation.numpy_abs
            or generation.numpy_module.ascontiguousarray
            is not generation.numpy_ascontiguousarray
            or generation.numpy_module.frombuffer
            is not generation.numpy_frombuffer
            or sys.modules.get("math") is not generation.math_module
            or generation.math_module.sin is not generation.math_sin
            or generation.math_module.cos is not generation.math_cos
            or generation.math_module.isfinite is not generation.math_isfinite
            or type(generation.math_module.pi) is not float
            or generation.math_module.pi != generation.math_pi
            or type(generation.math_module.tau) is not float
            or generation.math_module.tau != generation.math_tau
        ):
            raise ValueError
        if not _factory_provenance_ok(generation):
            raise ValueError
        for source in generation.held_sources:
            _revalidate_held_source(source, full_digest=full_sources)
    except ScoreV2RuntimeAuthorityError:
        generation.active = False
        raise
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        generation.active = False
        raise ScoreV2RuntimeAuthorityError("authority.generation_changed") from exc


def _manifest_scope(manifest: dict[str, Any], *, inventory_status: str) -> None:
    external = manifest.get("external_audio_assets")
    if (
        manifest.get("type") != "oscillator"
        or manifest.get("implementation") is not None
        or manifest.get("runtime_asset_policy") != "no_external_audio_assets"
        or any(key in manifest for key in ("asset_root", "soundfont", "sample", "regions"))
        or ("external_audio_assets" in manifest and external != [])
        or inventory_status != NO_EXTERNAL_ASSET_INVENTORY_STATUS
    ):
        raise ScoreV2RuntimeAuthorityError("authority.backend_scope_unsupported")


def _validated_closure_files(closure: dict[str, Any]) -> dict[str, str]:
    if (
        set(closure)
        != {"algorithm", "entry_modules", "file_count", "files", "sha256"}
        or closure.get("algorithm") != "ast-render-import-closure-v1"
        or type(closure.get("entry_modules")) is not list
        or type(closure.get("files")) is not list
        or type(closure.get("file_count")) is not int
        or not _is_sha256(closure.get("sha256"))
    ):
        raise ScoreV2RuntimeAuthorityError(
            "authority.runtime_fingerprint_invalid"
        )
    entries = closure["entry_modules"]
    if (
        not entries
        or any(type(item) is not str or not item for item in entries)
        or entries != sorted(set(entries))
    ):
        raise ScoreV2RuntimeAuthorityError(
            "authority.runtime_fingerprint_invalid"
        )
    files = closure["files"]
    records: list[tuple[str, str]] = []
    for item in files:
        if (
            type(item) is not dict
            or set(item) != {"path", "sha256"}
            or type(item.get("path")) is not str
            or not item["path"]
            or not _is_sha256(item.get("sha256"))
        ):
            raise ScoreV2RuntimeAuthorityError(
                "authority.runtime_fingerprint_invalid"
            )
        records.append((item["path"], item["sha256"]))
    if (
        not records
        or len(records) != closure["file_count"]
        or records != sorted(records)
        or len({path for path, _digest in records}) != len(records)
    ):
        raise ScoreV2RuntimeAuthorityError(
            "authority.runtime_fingerprint_invalid"
        )
    aggregate = hashlib.sha256(
        "".join(
            f"{digest}  {path}\n" for path, digest in records
        ).encode("utf-8")
    ).hexdigest()
    if aggregate != closure["sha256"]:
        raise ScoreV2RuntimeAuthorityError(
            "authority.runtime_fingerprint_invalid"
        )
    return dict(records)


def _capture_sources(
    local: Any,
) -> tuple[Path, tuple[_HeldSource, ...], bytes, dict[str, str]]:
    fingerprint = local.runtime.runtime_binding.fingerprint_copy()
    manifest_record = fingerprint.get("manifest")
    closure = fingerprint.get("render_python_closure")
    if type(manifest_record) is not dict or type(closure) is not dict:
        raise ScoreV2RuntimeAuthorityError("authority.runtime_fingerprint_invalid")
    closure_files = _validated_closure_files(closure)
    manifest_path = Path(local.runtime.manifest_path).resolve()
    project_root = _derive_project_root(manifest_path, manifest_record.get("path"))
    records: list[tuple[str, Path, str]] = []
    records.append((str(manifest_record.get("path")), manifest_path, str(manifest_record.get("sha256"))))
    for label, digest in closure_files.items():
        records.append((label, _project_path(project_root, label), digest))
    seen: set[str] = set()
    held: list[_HeldSource] = []
    try:
        for label, path, digest in records:
            if type(label) is not str or label in seen or not _is_sha256(digest):
                raise ScoreV2RuntimeAuthorityError("authority.runtime_fingerprint_invalid")
            seen.add(label)
            held.append(_open_held_source(path, label=label, expected_sha256=digest))
        manifest_source = held[0]
        manifest_bytes = _read_descriptor(
            manifest_source.descriptor,
            expected_size=manifest_source.size,
        )
        if hashlib.sha256(manifest_bytes).hexdigest() != local.runtime.runtime_binding.manifest_raw_sha256:
            raise ScoreV2RuntimeAuthorityError("authority.manifest_generation_mismatch")
        return project_root, tuple(held), manifest_bytes, closure_files
    except BaseException:
        for source in held:
            try:
                os.close(source.descriptor)
            except OSError:
                pass
        raise


def _acquisition_document(generation: _AuthorityGeneration) -> dict[str, object]:
    return {
        "kind": SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_KIND,
        "schema_version": SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_SCHEMA_VERSION,
        "contract": SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_CONTRACT,
        "document_authority": False,
        "active_lease_required": True,
        "bindings": {
            "performance_bundle_sha256": generation.bundle_sha256,
            "runtime_source_sha256": generation.runtime_source_sha256,
            "capability_plan_sha256": generation.capability_plan_sha256,
            "capability_source_sha256": generation.capability_source_sha256,
            "roster_projection_sha256": generation.roster_projection_sha256,
            "effective_manifest_sha256": generation.effective_manifest_sha256,
            "manifest_raw_sha256": generation.manifest_raw_sha256,
            "sample_rate": generation.sample_rate,
        },
        "executor": {
            "executor_order": generation.executor_order,
            "executor_id": generation.executor_id,
            "part_id": generation.part_id,
        },
        "loaded_python_generation": {
            "projection_sha256": generation.loaded_projection_sha256,
            "projection": generation.loaded_projection,
        },
        "held_sources": [source.to_dict() for source in generation.held_sources],
        "assets": {
            "policy": "no_external_audio_assets",
            "descriptor_count": 0,
            "descriptors": [],
            "inventory_status": NO_EXTERNAL_ASSET_INVENTORY_STATUS,
        },
        "factory_generation_sha256": generation.factory_generation_sha256,
        "limitations": {
            "transferable_authority": False,
            "candidate_authority": False,
            "sampled_backends_supported": False,
            "custom_factories_supported": False,
            "trusted_python_interpreter_required": True,
        },
    }


def _consumption_document(generation: _AuthorityGeneration) -> dict[str, object]:
    return {
        "kind": SCORE_V2_RUNTIME_AUTHORITY_KIND,
        "schema_version": SCORE_V2_RUNTIME_AUTHORITY_SCHEMA_VERSION,
        "contract": SCORE_V2_RUNTIME_AUTHORITY_CONTRACT,
        "historical_evidence_only": True,
        "document_authority": False,
        "status": "consumed",
        "bindings": {
            "performance_bundle_sha256": generation.bundle_sha256,
            "runtime_source_sha256": generation.runtime_source_sha256,
            "capability_plan_sha256": generation.capability_plan_sha256,
            "capability_source_sha256": generation.capability_source_sha256,
            "roster_projection_sha256": generation.roster_projection_sha256,
            "effective_manifest_sha256": generation.effective_manifest_sha256,
            "manifest_raw_sha256": generation.manifest_raw_sha256,
            "sample_rate": generation.sample_rate,
            "acquisition_sha256": generation.acquisition_sha256,
        },
        "executor": {
            "executor_order": generation.executor_order,
            "executor_id": generation.executor_id,
            "part_id": generation.part_id,
        },
        "assets": {
            "policy": "no_external_audio_assets",
            "descriptor_count": 0,
            "descriptors": [],
            "inventory_status": NO_EXTERNAL_ASSET_INVENTORY_STATUS,
        },
        "loaded_python_generation": {
            "projection_sha256": generation.loaded_projection_sha256,
            "held_source_count": len(generation.held_sources),
        },
        "lifecycle": {
            "lease_consumed_once": True,
            "execution_retired_before_receipt": True,
            "source_descriptors_held_until_context_exit": True,
            "dispatched_event_count": generation.dispatched_events,
            "rendered_frame_count": generation.rendered_frames,
        },
        "factory_generation_sha256": generation.factory_generation_sha256,
        "limitations": {
            "reusable_runtime_authority": False,
            "authorship_proof": False,
            "hostile_interpreter_resistance": False,
            "external_asset_support": False,
        },
    }


def _make_generation(
    bundle: ScoreV2PerformanceBundle,
    executor_id: str,
) -> _AuthorityGeneration:
    try:
        local = bundle._local_execution_input_for_executor(executor_id)
        bundle_document = bundle.to_dict()
        bindings = bundle_document["bindings"]
        manifest = local.runtime.manifest_copy()
        fingerprint = local.runtime.runtime_binding.fingerprint_copy()
        graph = fingerprint["runtime_asset_graph"]
        inventory_status = local.runtime.runtime_binding.asset_inventory_status
    except (
        AttributeError,
        IndexError,
        KeyError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        ScoreV2PerformanceError,
        ScoreV2RuntimeSourceError,
    ) as exc:
        raise ScoreV2RuntimeAuthorityError("authority.execution_input_invalid") from exc
    if (
        type(bundle) is not ScoreV2PerformanceBundle
        or bundle.executor_count != 1
        or type(bindings) is not dict
        or type(graph) is not dict
        or graph.get("file_count") != 0
        or graph.get("total_bytes") != 0
        or graph.get("region_count") != 0
    ):
        raise ScoreV2RuntimeAuthorityError("authority.backend_scope_unsupported")
    _manifest_scope(manifest, inventory_status=inventory_status)
    (
        _project_root,
        held_sources,
        manifest_bytes,
        closure_files,
    ) = _capture_sources(local)
    roots: tuple[_LoadedRoot, ...] = ()
    instrument: OscillatorInstrument | None = None
    try:
        roots = _loaded_roots()
        _revalidate_loaded_roots(roots)
        source_modules = _bind_loaded_source_modules(
            roots,
            closure_files=closure_files,
        )
        loaded_projection = _loaded_projection(
            roots,
            source_modules=source_modules,
        )
        import numpy as np
        from . import oscillator as oscillator_runtime_module
        from . import tuning as tuning_runtime_module

        dependencies = fingerprint.get("runtime_dependencies")
        numpy_dependency = (
            dependencies.get("numpy")
            if type(dependencies) is dict
            else None
        )
        numpy_version = str(np.__version__)
        if (
            type(numpy_dependency) is not dict
            or numpy_dependency.get("version") != numpy_version
            or oscillator_runtime_module.math is not math
            or tuning_runtime_module.math is not math
        ):
            raise ScoreV2RuntimeAuthorityError(
                "authority.runtime_dependency_mismatch"
            )
        loaded_projection["runtime_dependencies"] = {
            "numpy": {
                "version": numpy_version,
                "module": "numpy",
            }
        }
        loaded_projection_sha = hashlib.sha256(
            canonical_json_bytes(loaded_projection)
        ).hexdigest()
        roots_by_label = {root.label: root for root in roots}
        oscillator_class = roots_by_label["oscillator.class"].value
        from_manifest_root = roots_by_label["oscillator.from_manifest"]
        from_manifest_function = _root_function(from_manifest_root)
        provenance_binder = _root_function(
            roots_by_label["instrument.bind_factory"]
        )
        manifest_hasher = _root_function(
            roots_by_label["instrument.manifest_hash"]
        )
        if (
            oscillator_class is not OscillatorInstrument
            or not isinstance(from_manifest_root.value, classmethod)
            or from_manifest_function is None
            or provenance_binder is None
            or manifest_hasher is None
        ):
            raise ScoreV2RuntimeAuthorityError("authority.loaded_code_invalid")
        instrument = from_manifest_function(
            oscillator_class,
            manifest,
            bundle.sample_rate,
        )
        provenance_binder(
            instrument,
            manifest,
            sample_rate=bundle.sample_rate,
            factory_route=SCORE_V2_RUNTIME_FACTORY_ROUTE,
        )
        if type(instrument) is not OscillatorInstrument:
            raise ScoreV2RuntimeAuthorityError("authority.factory_instance_invalid")
        try:
            factory_static_state = _oscillator_static_state(instrument)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ScoreV2RuntimeAuthorityError(
                "authority.factory_instance_invalid"
            ) from exc
        handle = OscillatorInstrument.__dict__["handle_event"]
        render = OscillatorInstrument.__dict__["render_frame"]
        active = OscillatorInstrument.__dict__["active_voice_count"]
        if (
            not isinstance(handle, FunctionType)
            or not isinstance(render, FunctionType)
            or not isinstance(active, property)
            or not isinstance(active.fget, FunctionType)
        ):
            raise ScoreV2RuntimeAuthorityError("authority.loaded_code_invalid")
        effective_hash = manifest_hasher(manifest)
        if (
            not _is_sha256(effective_hash)
            or effective_hash != local.runtime.effective_manifest_sha256
        ):
            raise ScoreV2RuntimeAuthorityError(
                "authority.manifest_generation_mismatch"
            )
        factory_document = {
            "route": SCORE_V2_RUNTIME_FACTORY_ROUTE,
            "effective_manifest_sha256": effective_hash,
            "loaded_python_projection_sha256": loaded_projection_sha,
            "sample_rate": bundle.sample_rate,
            "backend": "tianlai.oscillator.OscillatorInstrument",
            "instance_static_state_sha256": hashlib.sha256(
                canonical_json_bytes(factory_static_state)
            ).hexdigest(),
        }
        factory_hash = hashlib.sha256(
            canonical_json_bytes(factory_document)
        ).hexdigest()
        generation = _AuthorityGeneration(
            bundle=bundle,
            bundle_sha256=bundle.artifact_sha256,
            runtime_source_sha256=bundle.runtime_source_sha256,
            capability_plan_sha256=bundle.capability_plan_sha256,
            capability_source_sha256=local.runtime.runtime_binding.capability_source_sha256,
            roster_projection_sha256=local.runtime.runtime_binding.roster_projection_sha256,
            executor_order=local.executor_order,
            executor_id=local.executor_id,
            part_id=local.part_id,
            sample_rate=bundle.sample_rate,
            frame_count=bundle.frame_count,
            effective_manifest=manifest,
            effective_manifest_sha256=effective_hash,
            manifest_raw_sha256=local.runtime.runtime_binding.manifest_raw_sha256,
            manifest_bytes=manifest_bytes,
            held_sources=held_sources,
            roots=roots,
            loaded_projection=loaded_projection,
            loaded_projection_sha256=loaded_projection_sha,
            instrument=instrument,
            handle_event=handle,
            render_frame=render,
            active_voice_getter=active.fget,
            tuning=EqualTemperament(a4_hz=440.0),
            factory_static_state=factory_static_state,
            math_module=math,
            math_sin=math.sin,
            math_cos=math.cos,
            math_isfinite=math.isfinite,
            math_pi=math.pi,
            math_tau=math.tau,
            numpy_module=np,
            numpy_version=numpy_version,
            numpy_empty=np.empty,
            numpy_isfinite=np.isfinite,
            numpy_max=np.max,
            numpy_abs=np.abs,
            numpy_ascontiguousarray=np.ascontiguousarray,
            numpy_frombuffer=np.frombuffer,
            factory_generation_sha256=factory_hash,
            acquisition_bytes=b"",
            acquisition_sha256="",
        )
        acquisition = _acquisition_document(generation)
        acquisition_bytes = canonical_json_bytes(acquisition)
        generation.acquisition_bytes = acquisition_bytes
        generation.acquisition_sha256 = hashlib.sha256(acquisition_bytes).hexdigest()
        _checkpoint_generation(generation, full_sources=True)
        return generation
    except BaseException:
        for source in held_sources:
            try:
                os.close(source.descriptor)
            except OSError:
                pass
        raise


@contextmanager
def open_score_v2_oscillator_runtime_authority(
    bundle: ScoreV2PerformanceBundle,
    executor_id: str,
) -> Iterator[ScoreV2OscillatorRuntimeAuthority]:
    """Acquire one non-transferable, single-use formal oscillator lease."""

    if type(bundle) is not ScoreV2PerformanceBundle:
        raise TypeError("bundle must be ScoreV2PerformanceBundle")
    if type(executor_id) is not str or not executor_id:
        raise ValueError("executor_id must be a non-empty string")
    try:
        generation = _make_generation(bundle, executor_id)
    except ScoreV2RuntimeAuthorityError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ScoreV2RuntimeAuthorityError("authority.acquisition_failed") from exc
    authority = object.__new__(ScoreV2OscillatorRuntimeAuthority)
    object.__setattr__(authority, "_token", generation)
    _register(authority, generation)
    primary: BaseException | None = None
    try:
        yield authority
    except BaseException as exc:
        primary = exc
        raise
    finally:
        checkpoint_error: BaseException | None = None
        try:
            if generation.active:
                _checkpoint_generation(generation, full_sources=True)
        except BaseException as exc:
            checkpoint_error = exc
        generation.execution_finished = True
        generation.active = False
        _LEASES.pop(id(authority), None)
        close_error: BaseException | None = None
        for source in generation.held_sources:
            try:
                os.close(source.descriptor)
            except BaseException as exc:
                if close_error is None:
                    close_error = exc
        if primary is not None:
            if checkpoint_error is not None:
                _add_note_safely(
                    primary,
                    "runtime authority final checkpoint failed: "
                    f"{checkpoint_error}",
                )
            if close_error is not None:
                _add_note_safely(
                    primary,
                    "runtime authority descriptor close failed: "
                    f"{close_error}",
                )
        elif checkpoint_error is not None:
            if close_error is not None:
                _add_note_safely(
                    checkpoint_error,
                    "runtime authority descriptor close also failed: "
                    f"{close_error}",
                )
            if not isinstance(checkpoint_error, Exception):
                raise checkpoint_error
            raise ScoreV2RuntimeAuthorityError(
                "authority.final_checkpoint_failed"
            ) from checkpoint_error
        elif close_error is not None:
            if not isinstance(close_error, Exception):
                raise close_error
            raise ScoreV2RuntimeAuthorityError(
                "authority.close_failed"
            ) from close_error


__all__ = [
    "SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_CONTRACT",
    "SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_KIND",
    "SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_SCHEMA_VERSION",
    "SCORE_V2_RUNTIME_AUTHORITY_CONTRACT",
    "SCORE_V2_RUNTIME_AUTHORITY_KIND",
    "SCORE_V2_RUNTIME_AUTHORITY_SCHEMA_VERSION",
    "SCORE_V2_RUNTIME_FACTORY_ROUTE",
    "ScoreV2OscillatorRuntimeAuthority",
    "ScoreV2RuntimeAuthorityError",
    "open_score_v2_oscillator_runtime_authority",
]
