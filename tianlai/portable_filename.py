"""One fail-closed filename contract shared by roster and renderer.

The public roster may preserve Unicode display spelling, but every emitted
executor becomes a real stem filename.  Both the identifier and ``.wav`` name
therefore have to fit conservative UTF-8 and UTF-16 component budgets and must
avoid Windows device aliases on every supported platform.
"""

from __future__ import annotations

import unicodedata


MAX_PORTABLE_COMPONENT_BYTES = 255
MAX_PORTABLE_COMPONENT_UTF16_UNITS = 255
STEM_SUFFIX = ".wav"

_FORBIDDEN = frozenset('/\\:*?"<>|')
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "conin$",
        "conout$",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)


class PortableFilenameError(ValueError):
    """Raised when an executor cannot safely become a portable stem name."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def portable_filename_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def portable_utf16_units(value: str) -> int:
    try:
        return len(value.encode("utf-16-le", errors="strict")) // 2
    except UnicodeEncodeError as exc:
        raise PortableFilenameError("identifier contains invalid Unicode") from exc


def _utf8_bytes(value: str) -> int:
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise PortableFilenameError("identifier contains invalid Unicode") from exc


def is_windows_reserved_filename(value: str) -> bool:
    basename = portable_filename_key(value).partition(".")[0].rstrip(" ")
    if basename in _WINDOWS_RESERVED_BASENAMES:
        return True
    for prefix in ("com", "lpt"):
        suffix = basename.removeprefix(prefix)
        if suffix and len(suffix) != len(basename) and all(
            character in "¹²³" for character in suffix
        ):
            return True
    return False


def _check_component_budget(value: str, *, label: str) -> None:
    utf8_bytes = _utf8_bytes(value)
    utf16_units = portable_utf16_units(value)
    if utf8_bytes > MAX_PORTABLE_COMPONENT_BYTES:
        raise PortableFilenameError(
            f"{label} exceeds {MAX_PORTABLE_COMPONENT_BYTES} UTF-8 bytes"
        )
    if utf16_units > MAX_PORTABLE_COMPONENT_UTF16_UNITS:
        raise PortableFilenameError(
            f"{label} exceeds {MAX_PORTABLE_COMPONENT_UTF16_UNITS} UTF-16 code units"
        )


def validate_executor_id(value: object) -> str:
    """Validate one final emitted executor ID and its exact ``.wav`` name."""

    if not isinstance(value, str):
        raise PortableFilenameError("executor identifier must be a string")
    if not value:
        raise PortableFilenameError("executor identifier cannot be empty")
    if any(
        character in _FORBIDDEN
        or ord(character) < 32
        or ord(character) == 127
        for character in value
    ):
        raise PortableFilenameError(
            "executor identifier contains a filename-unsafe character"
        )
    if value != value.strip() or value.endswith("."):
        raise PortableFilenameError(
            "executor identifier cannot be empty or have boundary whitespace/dot"
        )
    if value in {".", ".."}:
        raise PortableFilenameError("executor identifier is not portable")
    if is_windows_reserved_filename(value):
        raise PortableFilenameError(
            "executor identifier uses a reserved Windows device basename"
        )
    _check_component_budget(value, label="executor identifier")
    stem_name = f"{value}{STEM_SUFFIX}"
    if is_windows_reserved_filename(stem_name):
        raise PortableFilenameError("stem filename uses a reserved Windows device basename")
    _check_component_budget(stem_name, label="stem filename")
    return value


def portable_stem_filename(executor_id: object) -> str:
    return f"{validate_executor_id(executor_id)}{STEM_SUFFIX}"


__all__ = (
    "MAX_PORTABLE_COMPONENT_BYTES",
    "MAX_PORTABLE_COMPONENT_UTF16_UNITS",
    "PortableFilenameError",
    "is_windows_reserved_filename",
    "portable_filename_key",
    "portable_stem_filename",
    "portable_utf16_units",
    "validate_executor_id",
)
