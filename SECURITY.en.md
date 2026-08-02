[简体中文](SECURITY.md) | **English**

# Security policy

## Supported versions

The current `0.5.x` series receives security fixes. Earlier experimental
versions are handled only when a fix can be backported safely.

## Reporting a vulnerability

If the hosting platform has private vulnerability reporting enabled, use that
channel first. Otherwise, open only a minimal public issue that contains no
exploit code, private files, sound sources, or protected scores, and ask to
establish a private communication channel.

A useful report identifies the affected version, platform, entry point,
expected boundary, observed behavior, and minimum reproduction conditions. Do
not publish complete operational steps for arbitrary local-file reads,
arbitrary-path overwrites, arbitrary code execution, resource-package
poisoning, or denial of service before a fix is available.

## Current security boundaries

- MCP local score import reads only the project and score directories plus
  directories explicitly allowed through `TIANLAI_INPUT_ROOTS`.
- External paths are checked for containment after resolving symbolic links and
  Windows reparse points or junctions.
- Scores, MIDI, MusicXML/MXL, render duration, note counts, executors, memory,
  and output sizes all have default limits.
- With `trusted_only=true`, a missing or invalid trusted catalog fails closed.
- Candidates and imported projects are non-overwriting by default. Explicit
  candidate replacement binds both the old receipt and the preparation-stage
  candidate manifest, then recursively revalidates bound artifacts before and
  after the directory exchange.
- Sound-source installers should pin versions and digests, validate in a
  temporary directory, and publish only a complete result to the formal
  resource directory.

The ordinary local CLI lets a user select their own input path explicitly.
That does not grant an MCP agent arbitrary read access to the computer. Tianlai
is an offline renderer and should not be run with administrator privileges.
