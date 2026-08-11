# Tianlai Audio

Tianlai Audio is the reusable Python engine from the Tianlai project: a
local-first, deterministic runtime for importing scores, editing reproducible
music projects, rendering WAV candidates, and exposing the workflow to local
MCP clients.

This PyPI package intentionally contains the engine rather than the full
Tianlai product bundle. The full source release also carries the user-facing
launchers, examples, documentation, and separately restored instrument assets.

## Installation

Core CLI and programmatic instruments:

```console
python -m pip install tianlai-audio
tianlai --help
```

`tianlai-doctor` performs full source-layout and catalog diagnostics; run it
from a Tianlai source release (or with `TIANLAI_HOME` pointing to one), not as
an engine-only wheel smoke test.

Local MCP server:

```console
python -m pip install "tianlai-audio[mcp]"
tianlai-mcp
```

Optional SoundFont Python binding:

```console
python -m pip install "tianlai-audio[soundfont]"
```

SoundFont rendering also requires a compatible native FluidSynth runtime and
properly licensed local SoundFont files; neither is bundled in the wheel.

## Supported runtime

- 64-bit CPython 3.11–3.14
- Windows x86_64
- Linux x86_64
- macOS Apple Silicon or Intel

The package includes programmatic instruments that do not require external
samples. Other instrument definitions and large audio resources belong to the
full source release and its resource-restoration workflow.

## Rights and licensing

The software is Apache-2.0 licensed. `LICENSE`, `NOTICE`, `TRADEMARKS.md`, and
`OUTPUT_RIGHTS.md` are included in source distributions. Instrument and sample
licenses remain separate from the engine license, and rendered-output rights
depend on the resources actually used.
