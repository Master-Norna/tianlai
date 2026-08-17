"""Stable text encoding for Tianlai's human-facing command-line entry points."""

from __future__ import annotations

import sys
from typing import TextIO


def _reconfigure_utf8(stream: TextIO) -> None:
    """Keep the existing stream target while making its byte contract UTF-8.

    Redirected standard streams on Western Windows runners otherwise inherit a
    legacy code page such as cp1252.  Tianlai accepts Unicode paths and emits
    Chinese diagnostics, so argparse help and ordinary errors must establish
    their encoding before the first write.  In-process callers commonly replace
    a stream with ``io.StringIO``; that already stores Unicode and deliberately
    has no ``reconfigure`` method.
    """

    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")


def configure_utf8_standard_streams(
    *,
    stdout: bool = True,
    stderr: bool = True,
) -> None:
    """Configure selected real standard text streams as strict UTF-8."""

    if stdout:
        _reconfigure_utf8(sys.stdout)
    if stderr:
        _reconfigure_utf8(sys.stderr)
