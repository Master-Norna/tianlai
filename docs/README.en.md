[简体中文](README.md) | **English**

# Documentation

This map collects the user and integration documentation shipped with Tianlai
`1.0.0`. The current `tianlai-doctor` result is authoritative for the
runtime environment, instrument availability, and local resource state.

## Quick start

- [Windows minimal start](Windows最小启动.en.md): create an isolated virtual
  environment, run diagnostics, and generate a first WAV without external
  samples.
- [Windows installation and inspection](Windows安装与巡检.en.md): restore
  optional sound sources, inspect resource plans, and resolve common
  installation problems.
- [Linux / WSL quick start](Linux快速开始.en.md): Bash entry point, support
  boundary, MCP configuration, and external-sample installation and restoration.
- [macOS quick start](macOS快速开始.en.md): native Apple Silicon / Intel setup,
  portable gates, MCP configuration, and cross-platform resource-restoration
  boundaries.
- [From score to second render](从乐谱到第二次渲染.en.md): the complete flow from
  MIDI/MusicXML import and explicit instrumentation through first render,
  time-based location, editing, and A/B comparison.
- [Post-render self-check](渲染后自检.en.md): strict final-PCM integrity gates,
  True Peak/LUFS measurements, risk review, and the human-listening boundary.
- [Score v2: exact, portable work semantics](score-v2.en.md): rational time,
  stable identity, the v1 migration bundle, and the restricted direct-v2
  formal-render and Candidate-v3 boundary.
- [Realization v1](realization-v1.en.md): optional per-note and continuous
  performance control bound to a score hash, including quantization,
  approximation, and resolved-evidence contracts.

Shortest Windows entry point:

```cmd
安装运行环境.cmd
```

Shortest Linux / WSL entry point:

```bash
bash ./bootstrap_linux.sh
```

Shortest macOS entry point:

```bash
bash ./bootstrap_macos.sh
```

All three entry points create a platform-specific `.venv`, run environment
diagnostics, and produce a first sound with the reference oscillator. Never
share a virtual environment across Windows, Linux / WSL, macOS, or CPU
architectures.

## AI and MCP

- [MCP interface](MCP.en.md): current tools, stdio configuration, input-root
  permissions, explicit instrumentation, immutable candidates, and cache
  boundaries.
- [Optional creative workflow](创作工作流.en.md): work charters, layered review,
  evidence binding, managed rendering, bounded iteration, and the explicit
  boundary that workflow completion does not certify good music.

The CLI and MCP use the same score, instrumentation, rendering, and candidate
contracts. An agent may read contracts, propose changes, and execute an approved
plan. Connecting it does not automatically grant whole-computer file access,
instrument-selection authority, or the right to replace human aesthetic
judgment.

## Creative reference

- [Tianlai Music Constitution v0.2](音乐创作参考笔记/天籁音乐宪法-v0.2.en.md):
  non-normative creative principles for human creators and AI agents, protecting
  emergence, consequence, qiyun, and multiple complete worlds without requiring
  every small sound to justify its existence in advance. Its text is CC BY 4.0;
  the project software remains Apache-2.0.

## Capabilities and limitations

- [Current capabilities and limitations](当前状态.en.md): platform support,
  sound entries, resource restoration, rendering capabilities, and known
  boundaries.
- [Instrument index](../乐器/README.en.md): 103 sound entries, resource types,
  and quality states.

`quality_tier=formal` means that the currently bound version completed an
isolated single-instrument, single-timbre listening check. It does not mean
that every register, dynamic, articulation, combination, or work has received
expert approval. Always listen to the finished work.

## Sound sources, licenses, and output

- [Sound-source license policy](音源许可政策.en.md): admission, quarantine,
  attribution, and redistribution boundaries for third-party resources.
- [VPO license and installation guide](VPO音源许可与安装说明.en.md): official VPO
  installation method, pinned versions, and mixed-license boundary.
- [Output-rights statement](../OUTPUT_RIGHTS.en.md): distinguishes project code,
  third-party samples, input works, and final audio.
- [Project name and identity](../TRADEMARKS.en.md): naming and attribution for
  redistribution.

The source package contains no large third-party samples. Use diagnostics to
establish the actual installed resource state. Before publishing music, also
check the corresponding `许可与署名.json/.txt`, rights in the input work, and
upstream resource terms.

## Local data directories

- [Score directory](../乐谱/README.en.md): user scores, score revisions, and
  rosters.
- [Sound-source directory](../音源/README.en.md): large runtime resources,
  download caches, and installation receipts.
- [Output directory](../output/README.en.md): candidates, stems, works,
  diagnostics, and caches.

Each directory has an independent lifecycle. Do not add virtual environments,
large sound sources, private scores, or render caches directly to a source
distribution.

## Participate

- [Contributing](../CONTRIBUTING.en.md)
- [Security policy](../SECURITY.en.md)
