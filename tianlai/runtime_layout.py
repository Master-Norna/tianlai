"""Resolve Tianlai's code, catalogue, resources and writable output safely.

The project is currently distributed as a source workspace because the
instrument catalogue and its provenance records live beside the Python
package.  A wheel may still provide the reusable engine, but it must never
guess that ``site-packages`` is a writable Tianlai home.  Installed users can
point the engine at an unpacked catalogue with ``TIANLAI_HOME``.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Iterable


class RuntimeLayoutError(ValueError):
    """No complete or writable Tianlai runtime layout could be resolved."""


_ROOT_MARKERS = ("乐器", "可信乐器.json")


def _is_catalogue_root(path: Path) -> bool:
    return (
        (path / _ROOT_MARKERS[0]).is_dir()
        and (path / _ROOT_MARKERS[1]).is_file()
    )


def _parents_inclusive(path: Path) -> Iterable[Path]:
    yield path
    yield from path.parents


def _normalise_explicit_directory(value: str, variable: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise RuntimeLayoutError(
            f"{variable} points to a missing directory: {path}"
        )
    return path


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    home: Path
    catalog: Path
    allowlist: Path
    schemas: Path
    resources: Path
    output: Path
    source: str
    catalog_ready: bool

    def require_catalog(self) -> "RuntimeLayout":
        if not self.catalog_ready:
            raise RuntimeLayoutError(
                "Tianlai instrument catalogue was not found. Run inside the "
                "unpacked source release or set TIANLAI_HOME to that directory."
            )
        return self

    def to_dict(self) -> dict[str, object]:
        return {
            "home": str(self.home),
            "catalog": str(self.catalog),
            "allowlist": str(self.allowlist),
            "schemas": str(self.schemas),
            "resources": str(self.resources),
            "output": str(self.output),
            "source": self.source,
            "catalog_ready": self.catalog_ready,
        }


def discover_runtime_layout(
    *,
    start: str | Path | None = None,
    require_catalog: bool = False,
) -> RuntimeLayout:
    """Resolve one layout with explicit configuration taking precedence."""

    explicit_home = os.environ.get("TIANLAI_HOME")
    if explicit_home:
        home = _normalise_explicit_directory(
            explicit_home,
            "TIANLAI_HOME",
        )
        source = "environment"
        if not _is_catalogue_root(home):
            raise RuntimeLayoutError(
                "TIANLAI_HOME is not a complete Tianlai source/catalogue "
                f"directory: {home}"
            )
    else:
        starting_path = Path(start or Path.cwd()).expanduser().resolve()
        if starting_path.is_file():
            starting_path = starting_path.parent
        candidates: list[tuple[Path, str]] = [
            (candidate, "working_tree")
            for candidate in _parents_inclusive(starting_path)
        ]
        package_parent = Path(__file__).resolve().parent.parent
        if all(candidate != package_parent for candidate, _ in candidates):
            candidates.append((package_parent, "source_package"))
        match = next(
            (
                (candidate, candidate_source)
                for candidate, candidate_source in candidates
                if _is_catalogue_root(candidate)
            ),
            None,
        )
        if match is None:
            # Engine-only installs remain usable for modules that do not need
            # the catalogue.  CWD is deliberately chosen over site-packages so
            # any eventual output stays under a user-selected writable tree.
            home = starting_path
            source = "engine_only_working_directory"
        else:
            home, source = match

    resource_override = os.environ.get("TIANLAI_RESOURCE_DIR")
    resources = (
        _normalise_explicit_directory(
            resource_override,
            "TIANLAI_RESOURCE_DIR",
        )
        if resource_override
        else home / "音源"
    )
    output_override = os.environ.get("TIANLAI_OUTPUT_DIR")
    output = (
        Path(output_override).expanduser().resolve()
        if output_override
        else home / "output"
    )
    layout = RuntimeLayout(
        home=home,
        catalog=home / "乐器",
        allowlist=home / "可信乐器.json",
        schemas=home / "schemas",
        resources=resources,
        output=output,
        source=source,
        catalog_ready=_is_catalogue_root(home),
    )
    if require_catalog:
        layout.require_catalog()
    return layout


__all__ = [
    "RuntimeLayout",
    "RuntimeLayoutError",
    "discover_runtime_layout",
]
