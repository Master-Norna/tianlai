"""Tianlai headless musical-instrument rendering engine."""

from .catalog import CatalogEntry, discover_instruments
from .renderer import RenderResult, render_document, render_to_wav
from .tuning import EqualTemperament

__all__ = [
    "CatalogEntry",
    "EqualTemperament",
    "RenderResult",
    "discover_instruments",
    "render_document",
    "render_to_wav",
]
__version__ = "0.8.0rc1"
