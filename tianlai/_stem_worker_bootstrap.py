"""Minimal private bootstrap for globally admitted stem workers.

This file is executed by path, not imported through :mod:`tianlai`.  It
claims the child's independent active-slot lock using only the standard
library and the standalone ``worker_slots.py`` module before importing the
audio engine or allocating the framed render request.  The lock remains live
until the real worker module exits.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import runpy
import sys


def _load_worker_slots() -> object:
    path = Path(__file__).resolve().with_name("worker_slots.py")
    name = "_tianlai_private_worker_slots_bootstrap"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("managed worker slot bootstrap is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    if len(sys.argv) not in {5, 6}:
        raise ValueError("managed worker bootstrap arguments are invalid")
    persistent = len(sys.argv) == 6
    if persistent and sys.argv[5] != "--persistent":
        raise ValueError("managed worker bootstrap mode is invalid")
    slots = _load_worker_slots()
    try:
        slot_index = int(sys.argv[2], 10)
        parent_pid = int(sys.argv[4], 10)
    except ValueError as exc:
        raise ValueError("managed worker bootstrap integers are invalid") from exc
    spec = slots.ChildSlotSpec(
        Path(sys.argv[1]),
        slot_index,
        sys.argv[3],
        parent_pid,
    )
    active = slots.claim_reserved_worker_slot(spec)
    # Keep the active object (and worker_slots' strong live-lock reference)
    # until OS process teardown.  Releasing it in a ``finally`` around runpy
    # would create a short high-RSS window before Python's atexit/module
    # cleanup completes.
    del active
    package_parent = str(Path(__file__).resolve().parent.parent)
    if not sys.path or sys.path[0] != package_parent:
        sys.path.insert(0, package_parent)
    # The actual module retains its established private command line and
    # stdin protocol.  Only the bootstrap receives slot hand-off facts.
    sys.argv = [str(Path(__file__).resolve().with_name("stem_worker.py"))]
    if persistent:
        sys.argv.append("--persistent")
    runpy.run_module("tianlai.stem_worker", run_name="__main__")
    raise RuntimeError("managed stem worker returned without exiting")


if __name__ == "__main__":
    raise SystemExit(main())
