#!/usr/bin/env python3
"""Build a tiny, first-party SoundFont for native-backend smoke tests.

The generated bank contains one looping sine-wave preset.  It is deliberately
created during a test run instead of being checked in as an audio asset: the
source is auditable, deterministic, small, and has no third-party sample or
licence dependency.
"""

from __future__ import annotations

import argparse
import hashlib
import math
from pathlib import Path
import struct
import sys
from typing import Final, Sequence


_SAMPLE_RATE: Final = 44_000
_SAMPLE_COUNT: Final = 4_400
_GUARD_SAMPLE_COUNT: Final = 46
_AMPLITUDE: Final = 12_000


class SmokeSoundFontError(RuntimeError):
    """Raised when the deterministic smoke-test bank cannot be published."""


def _fixed_name(value: str) -> bytes:
    encoded = value.encode("ascii")
    if len(encoded) > 20:
        raise SmokeSoundFontError(f"SoundFont record name is too long: {value!r}")
    return encoded.ljust(20, b"\0")


def _chunk(identifier: bytes, payload: bytes) -> bytes:
    if len(identifier) != 4:
        raise SmokeSoundFontError("RIFF identifiers must contain exactly four bytes")
    padding = b"\0" if len(payload) % 2 else b""
    return identifier + struct.pack("<I", len(payload)) + payload + padding


def _list_chunk(kind: bytes, *children: bytes) -> bytes:
    if len(kind) != 4:
        raise SmokeSoundFontError("RIFF LIST types must contain exactly four bytes")
    return _chunk(b"LIST", kind + b"".join(children))


def build_smoke_soundfont() -> bytes:
    """Return one deterministic SoundFont 2.01 bank as bytes."""

    # Exactly 44 cycles make the loop boundary continuous.  FluidSynth will
    # resample this 440 Hz waveform to the requested output rate.
    samples = [
        round(
            _AMPLITUDE
            * math.sin(2.0 * math.pi * 440.0 * index / _SAMPLE_RATE)
        )
        for index in range(_SAMPLE_COUNT)
    ]
    sample_payload = struct.pack(
        f"<{len(samples) + _GUARD_SAMPLE_COUNT}h",
        *samples,
        *([0] * _GUARD_SAMPLE_COUNT),
    )

    info = _list_chunk(
        b"INFO",
        _chunk(b"ifil", struct.pack("<HH", 2, 1)),
        _chunk(b"isng", b"EMU8000\0"),
        _chunk(b"INAM", b"Tianlai native smoke bank\0"),
        # INFO strings include a NUL terminator and must have an even chunk
        # size; some FluidSynth builds correctly reject odd-sized strings.
        _chunk(b"ISFT", b"Tianlai build_smoke_soundfont.py\0\0"),
    )
    sample_data = _list_chunk(b"sdta", _chunk(b"smpl", sample_payload))

    preset_headers = b"".join(
        (
            struct.pack(
                "<20sHHHIII",
                _fixed_name("Tianlai Sine"),
                0,
                0,
                0,
                0,
                0,
                0,
            ),
            struct.pack(
                "<20sHHHIII",
                _fixed_name("EOP"),
                0,
                0,
                1,
                0,
                0,
                0,
            ),
        )
    )
    preset_bags = struct.pack("<HHHH", 0, 0, 1, 0)
    terminal_modulator = b"\0" * 10
    # Generator 41 binds the preset zone to instrument record zero.
    preset_generators = struct.pack("<HH", 41, 0)

    instruments = b"".join(
        (
            struct.pack("<20sH", _fixed_name("Sine instrument"), 0),
            struct.pack("<20sH", _fixed_name("EOI"), 1),
        )
    )
    instrument_bags = struct.pack("<HHHH", 0, 0, 3, 0)
    # A full key range, continuous looping, then sample zero.  sampleID must be
    # the final generator in an instrument zone under the SoundFont contract.
    instrument_generators = b"".join(
        (
            struct.pack("<HH", 43, 0x7F00),
            struct.pack("<HH", 54, 1),
            struct.pack("<HH", 53, 0),
        )
    )
    terminal_sample = _SAMPLE_COUNT + _GUARD_SAMPLE_COUNT
    sample_headers = b"".join(
        (
            struct.pack(
                "<20sIIIIIBbHH",
                _fixed_name("Tianlai sine"),
                0,
                _SAMPLE_COUNT,
                0,
                _SAMPLE_COUNT,
                _SAMPLE_RATE,
                69,
                0,
                0,
                1,
            ),
            struct.pack(
                "<20sIIIIIBbHH",
                _fixed_name("EOS"),
                terminal_sample,
                terminal_sample,
                terminal_sample,
                terminal_sample,
                _SAMPLE_RATE,
                0,
                0,
                0,
                1,
            ),
        )
    )

    preset_data = _list_chunk(
        b"pdta",
        _chunk(b"phdr", preset_headers),
        _chunk(b"pbag", preset_bags),
        _chunk(b"pmod", terminal_modulator),
        _chunk(b"pgen", preset_generators),
        _chunk(b"inst", instruments),
        _chunk(b"ibag", instrument_bags),
        _chunk(b"imod", terminal_modulator),
        _chunk(b"igen", instrument_generators),
        _chunk(b"shdr", sample_headers),
    )
    payload = b"sfbk" + info + sample_data + preset_data
    return b"RIFF" + struct.pack("<I", len(payload)) + payload


def write_smoke_soundfont(output: str | Path) -> dict[str, object]:
    """Publish the bank without replacing an existing path."""

    destination = Path(output).expanduser().absolute()
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = build_smoke_soundfont()
    with destination.open("xb") as stream:
        stream.write(payload)
        stream.flush()
    return {
        "path": str(destination),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "sample_rate": _SAMPLE_RATE,
        "sample_count": _SAMPLE_COUNT,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build Tianlai's deterministic native SoundFont smoke bank."
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = write_smoke_soundfont(arguments.output)
    except (OSError, SmokeSoundFontError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"{result['sha256']}  {result['path']} "
        f"({result['bytes']} bytes, first-party synthetic fixture)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
