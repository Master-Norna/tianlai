[简体中文](README.md) | **English**

# Render-output directory

`output/` stores rendered results, candidate receipts, machine reports, and
deletable caches. Except for this README, its contents are excluded from Git by
default and are not part of the lightweight source release.

## Common locations

| Path | Purpose |
| --- | --- |
| `作品/<title>/<version>/` | Creator-approved ensembles, stems, plans, receipts, and license sidecars |
| `mcp/<sanitized title>/<candidate ID>/` | Immutable ordinary MCP `render` candidates; the title directory has no identity hash |
| `mcp-workspaces/<session or title>/` | Unconfirmed scores, rosters, patch results, and client state |
| `全音域试音/<instrument category>/` | Regenerable full-range scans and their license sidecars |
| `表现力试听/<instrument category>/` | Fixed examples, refinement A/B renders, and matching license sidecars |
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

A new ordinary MCP candidate uses
`output/mcp/<sanitized title without an identity hash>/<candidate_id>/`. The
title directory is only for organizing works. Identity remains bound by the
hash-bound `work_id` and unchanged `candidate_id` in `候选.json`, so `work_id`
normally differs from the parent-directory name. Loading and integrity
verification accept either this clean parent or the legacy
`<work_id>/<candidate_id>/` layout, but the candidate directory itself must
remain exactly `candidate_id`. Authoring and workflow internals remain managed
separately under `output/mcp/authoring-projects/`; do not manually rearrange
them as ordinary candidates.

A reproducible work version should retain at least:

- the score and roster, plus any space or render parameters used;
- the ensemble and required stems;
- `渲染后自检.json`;
- `渲染回执.json`;
- `许可与署名.json/.txt`.

A directory containing only a WAV, without reproducible input, a self-check,
and a receipt, is not a complete project archive. A self-check warning must be
reviewed with stems and actual listening; it is not a machine verdict that the
work is bad.

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
