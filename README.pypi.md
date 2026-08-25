# Tianlai Audio

Tianlai Audio is the reusable Python engine from the Tianlai project: a
local-first, deterministic runtime for importing scores, editing reproducible
music projects, rendering WAV candidates, and exposing the workflow to local
MCP clients.

This PyPI package intentionally contains the engine rather than the full
Tianlai product bundle. The full source release also carries the user-facing
launchers, examples, documentation, public JSON Schemas, and separately
restored instrument assets. The engine parsers do not load those repository
Schemas at runtime, so they are intentionally not duplicated into the wheel.

## Installation

Core CLI and programmatic instruments:

```console
python -m pip install tianlai-audio
tianlai --help
```

`tianlai-doctor` performs full source-layout and catalog diagnostics; run it
from a Tianlai source release (or with `TIANLAI_HOME` pointing to one), not as
an engine-only wheel smoke test.

The first formal `project-render-v2` command is likewise source-workspace-only:
the wheel exposes its parser and reusable modules, while an actual formal render
must run from the full source release whose loaded `tianlai/` package and
instrument catalog can be bound as one workspace generation.

Local MCP server:

```console
python -m pip install "tianlai-audio[mcp]"
tianlai-mcp
```

The creative workflow begins with the work's own charter. Its composition map,
question-complete review, derivation, evidence, acceptance, and continuation
remain fully available without any external constitution. The wheel carries only
the current bilingual Tianlai Music Constitution v0.2 as a stateless, optional
source of ideas that may be consulted after that charter is formed; its clauses
bind none of generation, review, acceptance, or continuation. Any binding frozen
in an older workflow remains immutable provenance only: the runtime neither admits
it into current judgment nor lets it block continued work. v0.1 is retired, so its
clauses are not looked up or mapped to v0.2.
Legacy clients should remove `constitution` / `active_clauses` from activation,
or pass `null` and `null`/an empty array respectively. When another thinking
perspective is useful, query current v0.2 separately after forming the charter.

Creative review may trace material that genuinely grows from the work's existing
relationships, or recognize material with no such lineage when it is globally
necessary to the complete work. It must never invent causality to preserve a
detail; silence, muting, or deletion are valid outcomes when neither path holds.

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
