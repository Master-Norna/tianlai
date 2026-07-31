"""Constrain local files that an MCP client may ask Tianlai to read.

This policy is intentionally separate from the normal command-line
interface.  A person invoking a local CLI command has explicitly selected a
path; an MCP client, by contrast, should not gain ambient read access to the
whole machine merely because it can call an import tool.

Paths are canonicalised with ``Path.resolve(strict=True)`` before the
containment check.  Consequently ``..`` components and filesystem links
(including Windows reparse-point links resolved by Python) cannot be used to
escape an allowed root.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Iterable

from .runtime_layout import RuntimeLayout, discover_runtime_layout


INPUT_ROOTS_ENV = "TIANLAI_INPUT_ROOTS"


class InputPathPolicyError(ValueError):
    """A local input path or input-root configuration was rejected."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        requested_path: str | None = None,
        resolved_path: str | None = None,
        allowed_roots: Iterable[Path] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.requested_path = requested_path
        self.resolved_path = resolved_path
        self.allowed_roots = tuple(str(path) for path in allowed_roots)

    def to_issue(self, *, stage: str = "input_policy") -> dict[str, object]:
        """Return one issue compatible with Tianlai's structured MCP errors."""

        issue: dict[str, object] = {
            "severity": "error",
            "code": self.code,
            "stage": stage,
            "message": self.message,
        }
        if self.requested_path is not None:
            issue["requested_path"] = self.requested_path
        if self.resolved_path is not None:
            issue["resolved_path"] = self.resolved_path
        if self.allowed_roots:
            issue["allowed_roots"] = list(self.allowed_roots)
        return issue

    def to_result(self, *, stage: str = "input_policy") -> dict[str, object]:
        """Return a complete fail-closed MCP result."""

        return {
            "kind": "tianlai.input_path_result",
            "schema_version": 1,
            "ok": False,
            "issues": [self.to_issue(stage=stage)],
        }


def _canonical_directory(
    value: str | os.PathLike[str],
    *,
    source: str,
) -> Path:
    requested = os.fspath(value)
    try:
        candidate = Path(requested).expanduser()
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise InputPathPolicyError(
            code="input_roots.invalid",
            message=f"{source} contains a missing or invalid directory: {requested}",
            requested_path=requested,
        ) from exc
    if not resolved.is_dir():
        raise InputPathPolicyError(
            code="input_roots.not_directory",
            message=f"{source} entry is not a directory: {resolved}",
            requested_path=requested,
            resolved_path=str(resolved),
        )
    return resolved


def _deduplicate_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    keys: set[str] = set()
    for path in paths:
        # ``normcase`` preserves POSIX case sensitivity and applies Windows'
        # case-folding rules when the policy is running on Windows.
        key = os.path.normcase(os.path.normpath(str(path)))
        if key not in keys:
            keys.add(key)
            result.append(path)
    return tuple(result)


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class InputPathPolicy:
    """Canonical MCP input roots and deterministic relative-path behaviour."""

    allowed_roots: tuple[Path, ...]
    relative_base: Path

    @classmethod
    def from_roots(
        cls,
        roots: Iterable[str | os.PathLike[str]],
        *,
        relative_base: str | os.PathLike[str] | None = None,
        source: str = "input roots",
    ) -> "InputPathPolicy":
        canonical_roots = _deduplicate_paths(
            _canonical_directory(root, source=source) for root in roots
        )
        if not canonical_roots:
            raise InputPathPolicyError(
                code="input_roots.empty",
                message=f"{source} did not contain any usable directory",
            )
        base = _canonical_directory(
            relative_base if relative_base is not None else canonical_roots[0],
            source="relative input base",
        )
        if not any(_is_within(base, root) for root in canonical_roots):
            raise InputPathPolicyError(
                code="input_roots.relative_base_outside",
                message=(
                    "The relative input base must be inside an allowed input "
                    f"root: {base}"
                ),
                resolved_path=str(base),
                allowed_roots=canonical_roots,
            )
        return cls(allowed_roots=canonical_roots, relative_base=base)

    def resolve_file(self, value: str | os.PathLike[str]) -> Path:
        """Resolve and authorise one existing regular input file.

        Relative paths are interpreted below ``relative_base`` instead of the
        process working directory.  This keeps MCP behaviour stable when a
        server is launched by an editor, desktop application, or service.
        """

        try:
            requested = os.fspath(value)
        except TypeError as exc:
            raise InputPathPolicyError(
                code="input_path.invalid",
                message="Input path must be a string or path-like value",
                requested_path=repr(value),
                allowed_roots=self.allowed_roots,
            ) from exc
        if not requested.strip():
            raise InputPathPolicyError(
                code="input_path.invalid",
                message="Input path must not be empty",
                requested_path=requested,
                allowed_roots=self.allowed_roots,
            )

        try:
            raw_path = Path(requested).expanduser()
            candidate = (
                raw_path
                if raw_path.is_absolute()
                else self.relative_base / raw_path
            )
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise InputPathPolicyError(
                code="input_path.not_found",
                message=f"Input file does not exist: {requested}",
                requested_path=requested,
                allowed_roots=self.allowed_roots,
            ) from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise InputPathPolicyError(
                code="input_path.invalid",
                message=f"Input path could not be resolved safely: {requested}",
                requested_path=requested,
                allowed_roots=self.allowed_roots,
            ) from exc

        if not any(
            _is_within(resolved, root) for root in self.allowed_roots
        ):
            raise InputPathPolicyError(
                code="input_path.outside_allowed_roots",
                message=(
                    "MCP local input is outside the configured input roots. "
                    f"Move the file below the Tianlai project or score "
                    f"directory, or extend {INPUT_ROOTS_ENV}: {resolved}"
                ),
                requested_path=requested,
                resolved_path=str(resolved),
                allowed_roots=self.allowed_roots,
            )

        try:
            mode = resolved.stat().st_mode
        except OSError as exc:
            raise InputPathPolicyError(
                code="input_path.unreadable",
                message=f"Input file metadata could not be read: {resolved}",
                requested_path=requested,
                resolved_path=str(resolved),
                allowed_roots=self.allowed_roots,
            ) from exc
        if not stat.S_ISREG(mode):
            raise InputPathPolicyError(
                code="input_path.not_regular_file",
                message=f"Input path is not a regular file: {resolved}",
                requested_path=requested,
                resolved_path=str(resolved),
                allowed_roots=self.allowed_roots,
            )
        return resolved

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "tianlai.input_path_policy",
            "schema_version": 1,
            "allowed_roots": [str(path) for path in self.allowed_roots],
            "relative_base": str(self.relative_base),
            "extension_environment_variable": INPUT_ROOTS_ENV,
        }


def _environment_roots(value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    roots = tuple(item.strip() for item in value.split(os.pathsep) if item.strip())
    if not roots:
        raise InputPathPolicyError(
            code="input_roots.empty_environment",
            message=(
                f"{INPUT_ROOTS_ENV} is set but contains no directory. "
                f"Separate multiple roots with {os.pathsep!r}."
            ),
        )
    return roots


def discover_mcp_input_policy(
    *,
    layout: RuntimeLayout | None = None,
    start: str | Path | None = None,
) -> InputPathPolicy:
    """Build the fail-closed local-read policy used by MCP entry points.

    The runtime home is always the relative base and primary default root.
    Existing conventional score/example directories are retained explicitly
    for diagnostics.  A separately configured output directory is also
    trusted when it already exists so candidate inspection can remain local.
    ``TIANLAI_INPUT_ROOTS`` extends these defaults; it never silently replaces
    them.
    """

    runtime = layout or discover_runtime_layout(start=start)
    home = _canonical_directory(runtime.home, source="Tianlai runtime home")
    defaults: list[Path] = [home]
    for conventional in (home / "乐谱", home / "examples"):
        if conventional.is_dir():
            defaults.append(conventional.resolve(strict=True))
    if runtime.output.is_dir():
        defaults.append(runtime.output.resolve(strict=True))

    extensions = _environment_roots(os.environ.get(INPUT_ROOTS_ENV))
    canonical_extensions = [
        _canonical_directory(path, source=INPUT_ROOTS_ENV)
        for path in extensions
    ]
    return InputPathPolicy.from_roots(
        [*defaults, *canonical_extensions],
        relative_base=home,
        source="MCP input roots",
    )


__all__ = [
    "INPUT_ROOTS_ENV",
    "InputPathPolicy",
    "InputPathPolicyError",
    "discover_mcp_input_policy",
]
