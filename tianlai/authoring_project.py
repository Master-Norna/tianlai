"""Immutable, compare-and-swap authoring projects for Tianlai.

The caller authorises a final project root before calling this module.
This layer still treats every on-disk object as hostile: managed directories
must be ordinary directories, managed files must be single-link regular
files, and Windows reparse points are rejected alongside POSIX symlinks.

Each save publishes a complete, immutable three-document revision first.  A
single atomic replacement of ``tianlai-project.json`` is the only operation
that changes the current pointer.  Consequently a crash can leave an orphaned
valid revision, but can never expose a partially-written current revision.
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
from threading import Lock, RLock
import time
from typing import Any, Mapping
from weakref import WeakValueDictionary

from .authoring_json import (
    AuthoringJsonError,
    AuthoringJsonLimits,
    MAX_AUTHORING_DOCUMENT_BYTES,
    json_document_bytes,
    strict_json_loads,
    validate_request_size,
)
from .canonical_json import CANONICALIZATION, canonical_json_sha256
from .plain_file import read_plain_file_bytes, revalidate_plain_file
from .render_lock import capture_plain_directory
from .render_profile import parse_render_profile
from .resource_limits import ProjectLimits, validate_score_resource_limits
from .score import parse_score_document
from .score_time import validate_score_time_coordinates
from .utc_timestamp import (
    canonical_utc_now,
    validate_canonical_utc_timestamp,
)


PROJECT_MANIFEST_NAME = "tianlai-project.json"
PROJECT_KIND = "tianlai.authoring_project"
PROJECT_VERSION = 1
REVISION_KIND = "tianlai.authoring_project_revision"
REVISION_VERSION = 1
PRIVATE_DIRECTORY_NAME = ".tianlai"
REVISIONS_DIRECTORY_NAME = "revisions"
RENDERS_DIRECTORY_NAME = "renders"
PROJECT_LOCK_NAME = "project.lock"
MAX_AUTHORING_NOTES = 50_000
MAX_PROJECT_TITLE_BYTES = 1_024
MAX_SCORE_TITLE_BYTES = 1_024
MAX_METADATA_BYTES = 1024 * 1024

_DOCUMENT_FILENAMES = {
    "score": "score.json",
    "authoring_roster": "authoring-roster.json",
    "render_profile": "render-profile.json",
}
_PROJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SCORE_PITCH_PATTERN = re.compile(r"^[A-Ga-g][#b♯♭]*-?[0-9]+$")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_METADATA_LIMITS = AuthoringJsonLimits(
    max_document_bytes=MAX_METADATA_BYTES,
    max_depth=32,
    max_nodes=4096,
    max_string_bytes=4096,
    max_array_items=64,
    max_object_members=64,
)
_AUTHORING_PROJECT_LIMITS = ProjectLimits(
    max_score_json_bytes=MAX_AUTHORING_DOCUMENT_BYTES,
    max_parts=256,
    max_notes=MAX_AUTHORING_NOTES,
    max_executors=512,
    max_plan_seconds=2 * 60 * 60,
    max_audio_memory_bytes=2 * 1024 * 1024 * 1024,
    max_primary_output_bytes=64 * 1024 * 1024 * 1024,
)


class AuthoringProjectError(RuntimeError):
    """A stable, path-free authoring project failure."""

    def __init__(
        self,
        code: str,
        *,
        source: str = "project",
        location_segments: tuple[str | int, ...] = (),
    ) -> None:
        self.code = code
        self.message_key = f"authoringProject.{code.replace('.', '_')}"
        self.source = source
        self.location_segments = location_segments
        super().__init__(code)

    def to_issue(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message_key": self.message_key,
            "source": self.source,
            "severity": "error",
            "decision": "block",
            "location": {"segments": list(self.location_segments)},
        }


class _FrozenJsonObject(dict[str, Any]):
    """A ``dict``-compatible, recursively immutable JSON object.

    The score and roster parsers intentionally require concrete dictionaries,
    so a mapping proxy would make a valid project state unusable.  Retaining
    ``dict`` compatibility also keeps canonical JSON serialization stable,
    while every mutating entrypoint fails closed.
    """

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("authoring project state documents are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self) -> _FrozenJsonObject:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> _FrozenJsonObject:
        return self

    def __reduce_ex__(self, _protocol: int) -> tuple[object, tuple[object, ...]]:
        # The default dict pickle protocol restores items through
        # ``__setitem__``, which is intentionally disabled.  A named, bounded
        # factory rebuilds the already-decoded item sequence through dict's
        # constructor without opening a mutable restoration window.
        return (_restore_frozen_json_object, (tuple(self.items()),))


class _FrozenJsonArray(list[Any]):
    """A ``list``-compatible immutable JSON array."""

    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("authoring project state documents are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable

    def __copy__(self) -> _FrozenJsonArray:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> _FrozenJsonArray:
        return self

    def __reduce_ex__(self, _protocol: int) -> tuple[object, tuple[object, ...]]:
        return (_restore_frozen_json_array, (tuple(self),))


def _restore_frozen_json_object(
    items: tuple[tuple[str, Any], ...],
) -> _FrozenJsonObject:
    return _FrozenJsonObject(items)


def _restore_frozen_json_array(items: tuple[Any, ...]) -> _FrozenJsonArray:
    return _FrozenJsonArray(items)


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenJsonObject(
            (key, _freeze_json_value(item)) for key, item in value.items()
        )
    if isinstance(value, list):
        return _FrozenJsonArray(_freeze_json_value(item) for item in value)
    return value


def _thaw_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_thaw_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class AuthoringProjectState:
    project_id: str
    title: str
    created_at_utc: str
    updated_at_utc: str
    revision: str
    documents: dict[str, dict[str, Any]]
    document_revisions: dict[str, str]

    def __post_init__(self) -> None:
        # Detach first, then recursively freeze.  Neither the mappings supplied
        # by the caller nor any value reachable through this state can later be
        # changed in place through the normal public API.
        object.__setattr__(
            self,
            "documents",
            _freeze_json_value(copy.deepcopy(self.documents)),
        )
        object.__setattr__(
            self,
            "document_revisions",
            _freeze_json_value(copy.deepcopy(self.document_revisions)),
        )

    def detached_documents(self) -> dict[str, dict[str, Any]]:
        detached = _thaw_json_value(self.documents)
        assert isinstance(detached, dict)
        return detached


_LOCKS_GUARD = Lock()
_PROJECT_LOCKS: WeakValueDictionary[str, RLock] = WeakValueDictionary()
_PROJECT_LOCK_TIMEOUT_SECONDS = 5.0


def _project_lock(root: Path) -> RLock:
    identity = os.path.normcase(str(root)) if os.name == "nt" else str(root)
    with _LOCKS_GUARD:
        return _PROJECT_LOCKS.setdefault(identity, RLock())


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and left.st_nlink == 1
        and right.st_nlink == 1
        and not _is_reparse(left)
        and not _is_reparse(right)
    )


@contextmanager
def _exclusive_project_write_lock(root: Path):
    """Serialize CAS saves across authoring processes, not only threads."""

    lock_path = _managed_path(root, PRIVATE_DIRECTORY_NAME, PROJECT_LOCK_NAME)
    before = _require_plain_file(lock_path, code="unsafe_project_lock")
    if before.st_size != 1:
        raise AuthoringProjectError("unsafe_project_lock")
    try:
        handle = lock_path.open("r+b", buffering=0)
    except OSError as exc:
        raise AuthoringProjectError("project_lock_unavailable") from exc
    acquired = False
    try:
        opened = os.fstat(handle.fileno())
        after_open = _require_plain_file(
            lock_path, code="unsafe_project_lock"
        )
        if not _same_file_identity(opened, after_open):
            raise AuthoringProjectError("unsafe_project_lock")
        deadline = time.monotonic() + _PROJECT_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(
                        handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise AuthoringProjectError(
                        "project_lock_unavailable"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise AuthoringProjectError("project_busy") from exc
                time.sleep(0.025)
        after_lock = _require_plain_file(
            lock_path, code="unsafe_project_lock"
        )
        if not _same_file_identity(opened, after_lock):
            raise AuthoringProjectError("unsafe_project_lock")
        if after_lock.st_size != 1:
            raise AuthoringProjectError("unsafe_project_lock")
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _utc_now() -> str:
    return canonical_utc_now()


def _is_reparse(status: os.stat_result) -> bool:
    return bool(
        getattr(status, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _preserve_failed_entry(
    path: Path,
    *,
    parent: Path,
    prefix: str,
) -> None:
    """Move failed private state aside without deleting a mutable pathname.

    Validation followed by recursive deletion has an unavoidable replacement
    window on platforms without descriptor-relative tree removal.  A
    same-parent rename is recoverable even when the checked entry was swapped;
    the preserved name is outside every active staging namespace.
    """

    if (
        path.parent != parent
        or not path.name.startswith(prefix)
        or path != parent / path.name
        or not _lexists(path)
    ):
        return
    for _ in range(16):
        preserved = parent / (
            f"{path.name}.cleanup-preserved-{secrets.token_hex(16)}"
        )
        if _lexists(preserved):
            continue
        try:
            os.rename(path, preserved)
        except FileExistsError:
            continue
        except OSError:
            return
        return


def _require_plain_directory(path: Path, *, code: str) -> None:
    try:
        status = os.lstat(path)
    except OSError as exc:
        raise AuthoringProjectError(code) from exc
    if (
        not stat.S_ISDIR(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or _is_reparse(status)
    ):
        raise AuthoringProjectError(code)


def _require_plain_file(path: Path, *, code: str) -> os.stat_result:
    try:
        status = os.lstat(path)
    except OSError as exc:
        raise AuthoringProjectError(code) from exc
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or _is_reparse(status)
        or status.st_nlink != 1
    ):
        raise AuthoringProjectError(code)
    return status


def _absolute_root(value: str | os.PathLike[str]) -> Path:
    try:
        raw = Path(value).expanduser()
    except (TypeError, ValueError, OSError) as exc:
        raise AuthoringProjectError("invalid_project_root") from exc
    if not raw.is_absolute() or raw.name in {"", ".", ".."}:
        raise AuthoringProjectError("invalid_project_root")
    if _lexists(raw):
        try:
            return capture_plain_directory(raw).path
        except (OSError, RuntimeError) as exc:
            raise AuthoringProjectError("invalid_project_root") from exc
    try:
        parent = raw.parent
        resolved_parent = capture_plain_directory(parent).path
    except (OSError, RuntimeError) as exc:
        raise AuthoringProjectError("invalid_project_root") from exc
    final = resolved_parent / raw.name
    try:
        final.relative_to(resolved_parent)
    except ValueError as exc:  # pragma: no cover - name invariant defence
        raise AuthoringProjectError("invalid_project_root") from exc
    return final


def _managed_path(
    root: Path,
    *parts: str,
    escape_code: str = "managed_path_escape",
) -> Path:
    path = root.joinpath(*parts)
    try:
        path.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise AuthoringProjectError(escape_code) from exc
    return path


def _validate_title(value: object) -> str:
    if not isinstance(value, str):
        raise AuthoringProjectError(
            "invalid_title", location_segments=("title",)
        )
    title = value.strip()
    try:
        size = len(title.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise AuthoringProjectError(
            "invalid_title", location_segments=("title",)
        ) from exc
    if not title or size > MAX_PROJECT_TITLE_BYTES or "\x00" in title:
        raise AuthoringProjectError(
            "invalid_title", location_segments=("title",)
        )
    return title


def _validate_revision_id(value: object, *, code: str) -> str:
    if not isinstance(value, str) or _REVISION_PATTERN.fullmatch(value) is None:
        raise AuthoringProjectError(code)
    return value


def _validate_project_id(value: object) -> str:
    if not isinstance(value, str) or _PROJECT_ID_PATTERN.fullmatch(value) is None:
        raise AuthoringProjectError("invalid_project_manifest")
    return value


def _file_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _revision_identity(
    project_id: str,
    document_hashes: Mapping[str, str],
) -> str:
    return canonical_json_sha256(
        {
            "kind": "tianlai.authoring_revision_binding",
            "schema_version": 1,
            "project_id": project_id,
            "documents": {
                key: document_hashes[key]
                for key in (
                    "score",
                    "authoring_roster",
                    "render_profile",
                )
            },
        }
    )


def blank_score_document(title: str) -> dict[str, Any]:
    """Return the explicit, instrument-neutral score-v1 authoring template."""

    return {
        "schema_version": 1,
        "title": _validate_title(title),
        "sample_rate": 48_000,
        "tail_seconds": 2.0,
        "tuning": {"temperament": "equal", "a4_hz": 440.0},
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1.0,
                "bpm": 120.0,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "part-1",
                "name": "声部 1",
                "default_dynamic": "mf",
                "notes": [],
            }
        ],
    }


def blank_authoring_roster_document() -> dict[str, Any]:
    return {
        "kind": "tianlai.authoring_roster",
        "schema_version": 1,
        "name": "默认配器",
        "assignments": [{"part": "part-1", "instrument": None}],
    }


def blank_render_profile_document() -> dict[str, Any]:
    # This literal is the versioned authoring template, not an accidental
    # projection of whichever defaults a future RenderProfile constructor may
    # acquire.  Changing it requires a new template/version decision.
    return {
        "kind": "tianlai.render_profile",
        "schema_version": 1,
        "name": "preview-v1",
        "expression": "ensemble",
        "range_mode": "compatibility",
        "seed": 0,
        "master_gain_db": 0.0,
        "normalize_peak_db": -1.0,
        "space": {
            "enabled": True,
            "config": {
                "name": "小厅堂",
                "wet_db": -15.0,
                "room_size": 0.5,
                "predelay_ms": 18.0,
                "damping_hz": 6500.0,
                "highpass_hz": 150.0,
                "reference_distance_m": 3.0,
                "distance_exponent": 0.5,
                "min_send": 0.5,
                "max_send": 1.8,
            },
        },
        "collaboration_mode": None,
        "write_stems": True,
        "use_stem_cache": True,
        "refresh_stem_cache": False,
    }


def blank_authoring_documents(title: str) -> dict[str, dict[str, Any]]:
    return {
        "score": blank_score_document(title),
        "authoring_roster": blank_authoring_roster_document(),
        "render_profile": blank_render_profile_document(),
    }


def _public_number(value: object, field: str) -> None:
    """Enforce JSON Schema's ``number`` type without parser coercion."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a JSON number")


def _public_integer(value: object, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a JSON integer")


def _public_string(value: object, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a JSON string")


def _validate_public_score_types(document: dict[str, Any]) -> None:
    """Reject score values accepted only through internal coercion.

    The semantic score parser is deliberately convenient for trusted Python
    callers and supplies several legacy defaults.  Durable authoring input is
    a public JSON protocol instead, so its raw representation must also obey
    ``score.schema.json`` before that parser sees it.
    """

    if "title" in document:
        _public_string(document["title"], "score.title")
    if "sample_rate" in document:
        _public_integer(document["sample_rate"], "score.sample_rate")
    if "tail_seconds" in document:
        _public_number(document["tail_seconds"], "score.tail_seconds")
    if "tuning" in document:
        tuning = document["tuning"]
        if not isinstance(tuning, dict):
            raise ValueError("score.tuning must be a JSON object")
        if "a4_hz" in tuning:
            _public_number(tuning["a4_hz"], "score.tuning.a4_hz")

    tempo_map = document.get("tempo_map")
    if isinstance(tempo_map, list):
        for index, entry in enumerate(tempo_map):
            if not isinstance(entry, dict):
                continue
            for field in ("bar", "beats_per_bar", "beat_unit"):
                if field in entry:
                    _public_integer(
                        entry[field], f"score.tempo_map[{index}].{field}"
                    )
            for field in ("beat", "bpm"):
                if field in entry:
                    _public_number(
                        entry[field], f"score.tempo_map[{index}].{field}"
                    )

    parts = document.get("parts")
    if not isinstance(parts, list):
        return
    versioned = "schema_version" in document
    for part_index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        part_path = f"score.parts[{part_index}]"
        if "id" not in part or "notes" not in part:
            raise ValueError(f"{part_path} requires id and notes")
        _public_string(part["id"], f"{part_path}.id")
        for field in ("name", "default_articulation"):
            if field in part:
                _public_string(part[field], f"{part_path}.{field}")

        phrases = part.get("phrases")
        if "phrases" in part and not isinstance(phrases, list):
            # The semantic parser treats ``None`` like an omitted legacy
            # field.  JSON Schema does not: an explicitly present value must
            # be an array.
            raise ValueError(f"{part_path}.phrases must be a JSON array")
        if isinstance(phrases, list):
            for phrase_index, phrase in enumerate(phrases):
                if not isinstance(phrase, dict):
                    continue
                phrase_path = f"{part_path}.phrases[{phrase_index}]"
                if "start_bar" not in phrase or "end_bar" not in phrase:
                    raise ValueError(
                        f"{phrase_path} requires start_bar and end_bar"
                    )
                _public_integer(
                    phrase["start_bar"], f"{phrase_path}.start_bar"
                )
                _public_integer(phrase["end_bar"], f"{phrase_path}.end_bar")
                for field in ("start_beat", "end_beat"):
                    if field in phrase:
                        _public_number(phrase[field], f"{phrase_path}.{field}")

        notes = part.get("notes")
        if not isinstance(notes, list):
            continue
        for note_index, note in enumerate(notes):
            if not isinstance(note, dict):
                continue
            note_path = f"{part_path}.notes[{note_index}]"
            required = {"bar", "beat", "duration_beats", "pitch"}
            if not required.issubset(note):
                raise ValueError(f"{note_path} is missing a required field")
            _public_integer(note["bar"], f"{note_path}.bar")
            _public_number(note["beat"], f"{note_path}.beat")
            _public_number(
                note["duration_beats"], f"{note_path}.duration_beats"
            )
            pitch = note["pitch"]
            if isinstance(pitch, str):
                if _SCORE_PITCH_PATTERN.fullmatch(pitch) is None:
                    raise ValueError(f"{note_path}.pitch is not schema-valid")
            else:
                _public_number(pitch, f"{note_path}.pitch")
            for field in ("dynamic", "articulation", "voice"):
                if field in note:
                    _public_string(note[field], f"{note_path}.{field}")
            if "event_id" in note:
                _public_string(note["event_id"], f"{note_path}.event_id")
            elif versioned:
                raise ValueError(f"{note_path}.event_id is required in score v1")
            if "velocity" in note:
                _public_number(note["velocity"], f"{note_path}.velocity")
            if "staff" in note:
                _public_integer(note["staff"], f"{note_path}.staff")
            if "tie" in note and not isinstance(note["tie"], bool):
                raise ValueError(f"{note_path}.tie must be a JSON boolean")


def _validate_public_render_profile_types(document: dict[str, Any]) -> None:
    """Reject render-profile values that only the Python parser coerces."""

    for field in ("name",):
        if field in document:
            _public_string(document[field], f"render_profile.{field}")
    if "seed" in document:
        _public_integer(document["seed"], "render_profile.seed")
    for field in ("master_gain_db", "normalize_peak_db"):
        value = document.get(field)
        if field in document and not (
            field == "normalize_peak_db" and value is None
        ):
            _public_number(value, f"render_profile.{field}")

    if "space" not in document:
        return
    space = document["space"]
    if not isinstance(space, dict):
        raise ValueError("render_profile.space must be a JSON object")
    config = space.get("config")
    if config is None and "config" not in space:
        return
    if not isinstance(config, dict):
        raise ValueError("render_profile.space.config must be a JSON object")
    if "name" in config:
        _public_string(config["name"], "render_profile.space.config.name")
    if "说明" in config:
        _public_string(config["说明"], "render_profile.space.config.说明")
    for field in (
        "wet_db",
        "room_size",
        "predelay_ms",
        "damping_hz",
        "highpass_hz",
        "reference_distance_m",
        "distance_exponent",
        "min_send",
        "max_send",
    ):
        if field in config:
            _public_number(
                config[field], f"render_profile.space.config.{field}"
            )


def _validate_document_set(
    documents: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    if not isinstance(documents, Mapping) or set(documents) != set(
        _DOCUMENT_FILENAMES
    ):
        raise AuthoringProjectError("invalid_documents_shape")
    detached: dict[str, dict[str, Any]] = {}
    for key in _DOCUMENT_FILENAMES:
        value = documents[key]
        if not isinstance(value, dict):
            raise AuthoringProjectError(
                "document_object_required",
                source=key,
            )
        try:
            # Serialization also performs all depth/node/string/safe-number
            # gates and returns a detached on-wire-equivalent representation.
            payload = json_document_bytes(value)
            loaded = strict_json_loads(payload)
        except AuthoringJsonError as exc:
            raise AuthoringProjectError(
                f"json.{exc.code}",
                source=key,
                location_segments=exc.location_segments,
            ) from exc
        assert isinstance(loaded, dict)
        detached[key] = loaded
    try:
        validate_request_size(detached)
    except AuthoringJsonError as exc:
        raise AuthoringProjectError(
            f"json.{exc.code}", location_segments=exc.location_segments
        ) from exc

    try:
        _validate_public_score_types(detached["score"])
        score = parse_score_document(detached["score"])
        if score.schema_version != 1:
            raise ValueError("score v1 is required")
        score_title = detached["score"].get("title")
        if score_title is not None and (
            not isinstance(score_title, str)
            or "\x00" in score_title
            or len(score_title.encode("utf-8", errors="strict"))
            > MAX_SCORE_TITLE_BYTES
        ):
            raise ValueError("score title exceeds the authoring boundary")
        validate_score_time_coordinates(score)
        validate_score_resource_limits(
            detached["score"], score, _AUTHORING_PROJECT_LIMITS
        )
    except Exception as exc:
        if isinstance(exc, AuthoringProjectError):
            raise
        raise AuthoringProjectError("invalid_score", source="score") from exc

    try:
        from .authoring_roster import parse_authoring_roster_document

        authoring = parse_authoring_roster_document(
            detached["authoring_roster"], score
        )
        # The authoring parser retains all public fields verbatim.  This
        # round-trip guard makes any future coercion at this durable boundary
        # an explicit compatibility decision rather than silent persistence.
        if authoring.to_dict() != detached["authoring_roster"]:
            raise ValueError("authoring roster parser changed the document")
    except Exception as exc:
        raise AuthoringProjectError(
            "invalid_authoring_roster", source="authoring_roster"
        ) from exc
    try:
        _validate_public_render_profile_types(detached["render_profile"])
        parse_render_profile(detached["render_profile"])
    except Exception as exc:
        raise AuthoringProjectError(
            "invalid_render_profile", source="render_profile"
        ) from exc
    return detached


def validate_authoring_project_state(
    state: AuthoringProjectState,
) -> dict[str, dict[str, Any]]:
    """Validate the complete in-memory revision binding.

    This is intentionally repeated at snapshot/readiness boundaries.  A state
    constructed by an external caller, or even one modified by deliberately
    bypassing the read-only container methods, cannot pair new documents with
    stale hashes and obtain an apparently valid snapshot.
    """

    if not isinstance(state, AuthoringProjectState):
        raise AuthoringProjectError("invalid_project_state")
    project_id = _validate_project_id(state.project_id)
    try:
        title = _validate_title(state.title)
        created = validate_canonical_utc_timestamp(state.created_at_utc)
        updated = validate_canonical_utc_timestamp(state.updated_at_utc)
    except (AuthoringProjectError, ValueError) as exc:
        raise AuthoringProjectError("invalid_project_state_metadata") from exc
    if title != state.title or updated < created:
        raise AuthoringProjectError("invalid_project_state_metadata")
    revision = _validate_revision_id(
        state.revision, code="invalid_project_state_revision"
    )
    documents = _validate_document_set(state.documents)
    declared = state.document_revisions
    if not isinstance(declared, dict) or set(declared) != set(
        _DOCUMENT_FILENAMES
    ):
        raise AuthoringProjectError("invalid_state_document_revisions")
    actual = {
        key: canonical_json_sha256(documents[key]) for key in _DOCUMENT_FILENAMES
    }
    for key in _DOCUMENT_FILENAMES:
        recorded = declared[key]
        if (
            not isinstance(recorded, str)
            or _REVISION_PATTERN.fullmatch(recorded) is None
            or recorded != actual[key]
        ):
            raise AuthoringProjectError(
                "state_document_revision_mismatch", source=key
            )
    if _revision_identity(project_id, actual) != revision:
        raise AuthoringProjectError("state_revision_identity_mismatch")
    return documents


def _read_json_file(
    path: Path,
    *,
    source: str,
    limits: AuthoringJsonLimits | None = None,
) -> tuple[dict[str, Any], bytes]:
    status = _require_plain_file(path, code="unsafe_project_file")
    active_limits = limits or AuthoringJsonLimits()
    if status.st_size > active_limits.max_document_bytes:
        raise AuthoringProjectError("project_file_too_large", source=source)
    try:
        identity, payload = read_plain_file_bytes(
            path,
            maximum_bytes=active_limits.max_document_bytes,
        )
        document = strict_json_loads(payload, limits=active_limits)
        revalidate_plain_file(identity)
    except (OSError, AuthoringJsonError) as exc:
        raise AuthoringProjectError("invalid_project_file", source=source) from exc
    assert isinstance(document, dict)
    return document, payload


def _write_new_file(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise AuthoringProjectError("project_write_failed") from exc


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _revision_manifest(
    *,
    project_id: str,
    revision: str,
    created_at_utc: str,
    payloads: Mapping[str, bytes],
    documents: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    validate_canonical_utc_timestamp(created_at_utc)
    return {
        "kind": REVISION_KIND,
        "schema_version": REVISION_VERSION,
        "project_id": project_id,
        "revision": revision,
        "created_at_utc": created_at_utc,
        "canonicalization": CANONICALIZATION,
        "documents": {
            key: {
                "filename": _DOCUMENT_FILENAMES[key],
                "canonical_sha256": canonical_json_sha256(documents[key]),
                "file_sha256": _file_sha256(payloads[key]),
                "byte_length": len(payloads[key]),
            }
            for key in _DOCUMENT_FILENAMES
        },
    }


def _validate_revision_directory(
    directory: Path,
    *,
    expected_project_id: str,
    expected_revision: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    _require_plain_directory(directory, code="unsafe_revision_directory")
    expected_names = {*_DOCUMENT_FILENAMES.values(), "revision.json"}
    try:
        entries = {entry.name for entry in os.scandir(directory)}
    except OSError as exc:
        raise AuthoringProjectError("revision_unreadable") from exc
    if entries != expected_names:
        raise AuthoringProjectError("invalid_revision_shape")

    metadata, _ = _read_json_file(
        directory / "revision.json",
        source="revision",
        limits=_METADATA_LIMITS,
    )
    if set(metadata) != {
        "kind",
        "schema_version",
        "project_id",
        "revision",
        "created_at_utc",
        "canonicalization",
        "documents",
    }:
        raise AuthoringProjectError("invalid_revision_manifest")
    if (
        metadata.get("kind") != REVISION_KIND
        or metadata.get("schema_version") != REVISION_VERSION
        or metadata.get("project_id") != expected_project_id
        or metadata.get("revision") != expected_revision
        or metadata.get("canonicalization") != CANONICALIZATION
    ):
        raise AuthoringProjectError("invalid_revision_manifest")
    try:
        validate_canonical_utc_timestamp(metadata.get("created_at_utc"))
    except ValueError as exc:
        raise AuthoringProjectError("invalid_revision_manifest") from exc
    raw_records = metadata.get("documents")
    if not isinstance(raw_records, dict) or set(raw_records) != set(
        _DOCUMENT_FILENAMES
    ):
        raise AuthoringProjectError("invalid_revision_manifest")

    documents: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}
    for key, filename in _DOCUMENT_FILENAMES.items():
        record = raw_records[key]
        if not isinstance(record, dict) or set(record) != {
            "filename",
            "canonical_sha256",
            "file_sha256",
            "byte_length",
        }:
            raise AuthoringProjectError("invalid_revision_manifest")
        canonical_hash = record.get("canonical_sha256")
        file_hash = record.get("file_sha256")
        byte_length = record.get("byte_length")
        if (
            record.get("filename") != filename
            or not isinstance(canonical_hash, str)
            or _REVISION_PATTERN.fullmatch(canonical_hash) is None
            or not isinstance(file_hash, str)
            or _REVISION_PATTERN.fullmatch(file_hash) is None
            or isinstance(byte_length, bool)
            or not isinstance(byte_length, int)
            or byte_length < 0
        ):
            raise AuthoringProjectError("invalid_revision_manifest")
        document, payload = _read_json_file(
            directory / filename,
            source=key,
        )
        if (
            len(payload) != byte_length
            or _file_sha256(payload) != file_hash
            or canonical_json_sha256(document) != canonical_hash
        ):
            raise AuthoringProjectError("revision_tampered", source=key)
        documents[key] = document
        hashes[key] = canonical_hash
    if _revision_identity(expected_project_id, hashes) != expected_revision:
        raise AuthoringProjectError("revision_identity_mismatch")
    return documents, hashes


def _publish_revision(
    root: Path,
    *,
    project_id: str,
    documents: Mapping[str, Any],
    created_at_utc: str,
) -> tuple[str, dict[str, dict[str, Any]], dict[str, str]]:
    detached = _validate_document_set(documents)
    payloads = {
        key: json_document_bytes(detached[key]) for key in _DOCUMENT_FILENAMES
    }
    hashes = {
        key: canonical_json_sha256(detached[key]) for key in _DOCUMENT_FILENAMES
    }
    revision = _revision_identity(project_id, hashes)
    revisions = _managed_path(
        root, PRIVATE_DIRECTORY_NAME, REVISIONS_DIRECTORY_NAME
    )
    final = revisions / revision
    if _lexists(final):
        existing, existing_hashes = _validate_revision_directory(
            final,
            expected_project_id=project_id,
            expected_revision=revision,
        )
        if existing_hashes != hashes or existing != detached:
            raise AuthoringProjectError("revision_collision")
        return revision, existing, existing_hashes

    stage = revisions / f".revision-stage-{secrets.token_hex(16)}"
    try:
        os.mkdir(stage)
        _require_plain_directory(stage, code="unsafe_revision_staging")
        for key, filename in _DOCUMENT_FILENAMES.items():
            _write_new_file(stage / filename, payloads[key])
        metadata = _revision_manifest(
            project_id=project_id,
            revision=revision,
            created_at_utc=created_at_utc,
            payloads=payloads,
            documents=detached,
        )
        _write_new_file(
            stage / "revision.json",
            json_document_bytes(metadata, limits=_METADATA_LIMITS),
        )
        _fsync_directory(stage)
        _validate_revision_directory(
            stage,
            expected_project_id=project_id,
            expected_revision=revision,
        )
        if _lexists(final):
            raise AuthoringProjectError("revision_publish_conflict")
        os.replace(stage, final)
        _fsync_directory(revisions)
        stored, stored_hashes = _validate_revision_directory(
            final,
            expected_project_id=project_id,
            expected_revision=revision,
        )
        return revision, stored, stored_hashes
    except AuthoringProjectError:
        raise
    except OSError as exc:
        raise AuthoringProjectError("revision_publish_failed") from exc
    finally:
        if _lexists(stage):
            _preserve_failed_entry(
                stage,
                parent=revisions,
                prefix=".revision-stage-",
            )


def _project_manifest(
    *,
    project_id: str,
    title: str,
    created_at_utc: str,
    updated_at_utc: str,
    current_revision: str,
) -> dict[str, Any]:
    validate_canonical_utc_timestamp(created_at_utc)
    validate_canonical_utc_timestamp(updated_at_utc)
    if updated_at_utc < created_at_utc:
        raise ValueError("project update timestamp precedes creation")
    return {
        "kind": PROJECT_KIND,
        "schema_version": PROJECT_VERSION,
        "project_id": project_id,
        "title": title,
        "created_at_utc": created_at_utc,
        "updated_at_utc": updated_at_utc,
        "current_revision": current_revision,
    }


def _validate_project_manifest(document: dict[str, Any]) -> dict[str, Any]:
    if set(document) != {
        "kind",
        "schema_version",
        "project_id",
        "title",
        "created_at_utc",
        "updated_at_utc",
        "current_revision",
    }:
        raise AuthoringProjectError("invalid_project_manifest")
    if (
        document.get("kind") != PROJECT_KIND
        or document.get("schema_version") != PROJECT_VERSION
    ):
        raise AuthoringProjectError("invalid_project_manifest")
    project_id = _validate_project_id(document.get("project_id"))
    title = _validate_title(document.get("title"))
    revision = _validate_revision_id(
        document.get("current_revision"), code="invalid_project_manifest"
    )
    created = document.get("created_at_utc")
    updated = document.get("updated_at_utc")
    try:
        checked_created = validate_canonical_utc_timestamp(created)
        checked_updated = validate_canonical_utc_timestamp(updated)
    except ValueError as exc:
        raise AuthoringProjectError("invalid_project_manifest") from exc
    if checked_updated < checked_created:
        raise AuthoringProjectError("invalid_project_manifest")
    return {
        **document,
        "project_id": project_id,
        "title": title,
        "current_revision": revision,
    }


def _read_project_manifest(root: Path) -> dict[str, Any]:
    document, _ = _read_json_file(
        root / PROJECT_MANIFEST_NAME,
        source="project",
        limits=_METADATA_LIMITS,
    )
    return _validate_project_manifest(document)


def _replace_manifest_pointer(root: Path, manifest: dict[str, Any]) -> None:
    """Atomic pointer seam, intentionally separate for crash injection tests."""

    target = root / PROJECT_MANIFEST_NAME
    stage = root / f".{PROJECT_MANIFEST_NAME}.stage-{secrets.token_hex(16)}"
    try:
        _write_new_file(
            stage,
            json_document_bytes(manifest, limits=_METADATA_LIMITS),
        )
        staged, _ = _read_json_file(
            stage, source="project", limits=_METADATA_LIMITS
        )
        _validate_project_manifest(staged)
        if staged != manifest:
            raise AuthoringProjectError("manifest_staging_mismatch")
        os.replace(stage, target)
        _fsync_directory(root)
        if _read_project_manifest(root) != manifest:
            raise AuthoringProjectError("manifest_publish_mismatch")
    except AuthoringProjectError:
        raise
    except OSError as exc:
        raise AuthoringProjectError("manifest_publish_failed") from exc
    finally:
        if _lexists(stage):
            _preserve_failed_entry(
                stage,
                parent=root,
                prefix=f".{PROJECT_MANIFEST_NAME}.stage-",
            )


def _validate_managed_layout(root: Path) -> tuple[Path, Path, Path]:
    _require_plain_directory(root, code="unsafe_project_root")
    private = _managed_path(
        root,
        PRIVATE_DIRECTORY_NAME,
        escape_code="unsafe_private_directory",
    )
    revisions = _managed_path(
        root,
        PRIVATE_DIRECTORY_NAME,
        REVISIONS_DIRECTORY_NAME,
        escape_code="unsafe_revisions_directory",
    )
    renders = _managed_path(
        root,
        RENDERS_DIRECTORY_NAME,
        escape_code="unsafe_renders_directory",
    )
    _require_plain_directory(private, code="unsafe_private_directory")
    lock_status = _require_plain_file(
        _managed_path(
            root,
            PRIVATE_DIRECTORY_NAME,
            PROJECT_LOCK_NAME,
            escape_code="unsafe_project_lock",
        ),
        code="unsafe_project_lock",
    )
    if lock_status.st_size != 1:
        raise AuthoringProjectError("unsafe_project_lock")
    _require_plain_directory(revisions, code="unsafe_revisions_directory")
    _require_plain_directory(renders, code="unsafe_renders_directory")
    return private, revisions, renders


def open_authoring_project(
    project_root: str | os.PathLike[str],
    *,
    revision: str | None = None,
) -> AuthoringProjectState:
    root = _absolute_root(project_root)
    with _project_lock(root):
        _validate_managed_layout(root)
        manifest = _read_project_manifest(root)
        selected = (
            manifest["current_revision"]
            if revision is None
            else _validate_revision_id(revision, code="invalid_revision")
        )
        documents, hashes = _validate_revision_directory(
            _managed_path(
                root,
                PRIVATE_DIRECTORY_NAME,
                REVISIONS_DIRECTORY_NAME,
                selected,
            ),
            expected_project_id=manifest["project_id"],
            expected_revision=selected,
        )
        # Opening is a complete semantic validation, not merely a hash check.
        validated = _validate_document_set(documents)
        if validated != documents:
            raise AuthoringProjectError("revision_document_mismatch")
        return AuthoringProjectState(
            project_id=manifest["project_id"],
            title=manifest["title"],
            created_at_utc=manifest["created_at_utc"],
            updated_at_utc=manifest["updated_at_utc"],
            revision=selected,
            documents=validated,
            document_revisions=hashes,
        )


def create_authoring_project(
    project_root: str | os.PathLike[str],
    *,
    title: str,
) -> AuthoringProjectState:
    root = _absolute_root(project_root)
    checked_title = _validate_title(title)
    with _project_lock(root):
        created_root = False
        try:
            if _lexists(root):
                _require_plain_directory(root, code="unsafe_project_root")
                try:
                    if any(root.iterdir()):
                        raise AuthoringProjectError("project_root_not_empty")
                except OSError as exc:
                    raise AuthoringProjectError("project_root_unreadable") from exc
            else:
                parent = root.parent
                _require_plain_directory(parent, code="unsafe_project_parent")
                os.mkdir(root)
                created_root = True
                _require_plain_directory(root, code="unsafe_project_root")

            private = _managed_path(root, PRIVATE_DIRECTORY_NAME)
            revisions = _managed_path(
                root, PRIVATE_DIRECTORY_NAME, REVISIONS_DIRECTORY_NAME
            )
            renders = _managed_path(root, RENDERS_DIRECTORY_NAME)
            os.mkdir(private)
            _write_new_file(private / PROJECT_LOCK_NAME, b"\x00")
            os.mkdir(revisions)
            os.mkdir(renders)
            _validate_managed_layout(root)

            project_id = secrets.token_hex(16)
            timestamp = _utc_now()
            revision, _documents, _hashes = _publish_revision(
                root,
                project_id=project_id,
                documents=blank_authoring_documents(checked_title),
                created_at_utc=timestamp,
            )
            manifest = _project_manifest(
                project_id=project_id,
                title=checked_title,
                created_at_utc=timestamp,
                updated_at_utc=timestamp,
                current_revision=revision,
            )
            _replace_manifest_pointer(root, manifest)
            return open_authoring_project(root)
        except AuthoringProjectError:
            if created_root and _lexists(root):
                _preserve_failed_entry(
                    root,
                    parent=root.parent,
                    prefix=root.name,
                )
            raise
        except OSError as exc:
            if created_root and _lexists(root):
                _preserve_failed_entry(
                    root,
                    parent=root.parent,
                    prefix=root.name,
                )
            raise AuthoringProjectError("project_create_failed") from exc


def save_authoring_project(
    project_root: str | os.PathLike[str],
    *,
    expected_revision: str,
    documents: Mapping[str, Any],
) -> AuthoringProjectState:
    root = _absolute_root(project_root)
    expected = _validate_revision_id(
        expected_revision, code="invalid_expected_revision"
    )
    with _project_lock(root):
        _validate_managed_layout(root)
        with _exclusive_project_write_lock(root):
            current = open_authoring_project(root)
            if current.revision != expected:
                raise AuthoringProjectError("revision_conflict")
            observed_timestamp = _utc_now()
            try:
                validate_canonical_utc_timestamp(observed_timestamp)
            except ValueError as exc:
                raise AuthoringProjectError("invalid_system_timestamp") from exc
            # Wall clocks can move backwards after NTP correction, VM resume,
            # or a manual adjustment.  Clamp before publishing the immutable
            # revision so a valid edit never becomes an orphan merely because
            # the later project-manifest timestamp check would reject it.
            timestamp = max(
                observed_timestamp,
                current.created_at_utc,
                current.updated_at_utc,
            )
            revision, stored, _hashes = _publish_revision(
                root,
                project_id=current.project_id,
                documents=documents,
                created_at_utc=timestamp,
            )
            if revision == current.revision and stored == current.documents:
                return current
            manifest = _project_manifest(
                project_id=current.project_id,
                title=current.title,
                created_at_utc=current.created_at_utc,
                updated_at_utc=timestamp,
                current_revision=revision,
            )
            # If this raises, the immutable revision remains safely orphaned and
            # the old current pointer is untouched.
            _replace_manifest_pointer(root, manifest)
            return open_authoring_project(root)


__all__ = [
    "AuthoringProjectError",
    "AuthoringProjectState",
    "MAX_AUTHORING_NOTES",
    "MAX_SCORE_TITLE_BYTES",
    "PRIVATE_DIRECTORY_NAME",
    "PROJECT_LOCK_NAME",
    "PROJECT_KIND",
    "PROJECT_MANIFEST_NAME",
    "PROJECT_VERSION",
    "RENDERS_DIRECTORY_NAME",
    "REVISIONS_DIRECTORY_NAME",
    "blank_authoring_documents",
    "blank_authoring_roster_document",
    "blank_render_profile_document",
    "blank_score_document",
    "create_authoring_project",
    "open_authoring_project",
    "save_authoring_project",
    "validate_authoring_project_state",
]
