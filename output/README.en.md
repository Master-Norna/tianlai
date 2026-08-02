[简体中文](README.md) | **English**

# Render-output directory

`output/` stores rendered results, candidate receipts, machine reports, and
deletable caches. Except for this README, its contents are excluded from Git by
default and are not part of the lightweight source release.

## Common locations

| Path | Purpose |
| --- | --- |
| `作品/<title>/<version>/` | Creator-approved ensembles, stems, plans, receipts, and license sidecars |
| `mcp/<work ID>/<candidate ID>/` | Immutable candidates produced by MCP `render` |
| `mcp-workspaces/<session or title>/` | Unconfirmed scores, rosters, patch results, and client state |
| `诊断/` | One-off environment checks, protocol checks, and troubleshooting evidence |
| `.tianlai-cache/stems/` | Raw stem cache used to accelerate re-rendering |

Other audition, full-range scan, or A/B directories are also generated on
demand. They should not be kept as source code or as the only creative source.

## Candidates and works

CLI `project-render` writes to `output/候选/` by default; MCP `render` writes to
`output/mcp/`. A candidate directory is bound by `候选.json` and the render
receipt and must be treated as an immutable snapshot. After accepting a result,
copy or re-render the same score, roster, and optional space into
`作品/<title>/<version>/`. Do not move or overwrite the original candidate and
present it as a new result.

A reproducible work version should retain at least:

- the score and roster, plus any space or render parameters used;
- the ensemble and required stems;
- `渲染回执.json`;
- `许可与署名.json/.txt`.

A directory containing only a WAV, without reproducible input and a receipt,
is not a complete project archive.

## Workspaces and caches

An agent or client may write unconfirmed material to `mcp-workspaces/`. After
creator approval, move editable sources into `乐谱/曲目/` and write finished
work to `作品/`. Do not use `tools/`, `docs/`, or product source directories as
session scratch space.

While a render is running, do not move or clean the active candidate directory,
`.tianlai-cache/`, `.candidate-*.render-stage.*`, or
`.tianlai-render-*.lock`. Wait for the process to finish and verify that its
receipt is complete before organizing files.

The entire `.tianlai-cache/` directory may be deleted. Deleting it does not
damage scores or works; it only causes the next render to recompute stems. A
cache is not a formal asset and cannot replace an attribution sidecar bound to
audio.

## Rights and backups

Do not treat an original work, the only manual export, or a result whose score
has not been saved elsewhere as a temporary file. Before cleaning output,
preserve the creative sources, score, roster, space, and necessary receipts,
then archive them under your own backup policy.

The Apache-2.0 code license does not automatically attach to input works or
music output. Third-party sound sources remain governed by their own licenses.
Before publishing audio, inspect the attribution sidecar generated for that
render and read [`OUTPUT_RIGHTS.en.md`](../OUTPUT_RIGHTS.en.md).
