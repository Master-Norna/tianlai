[简体中文](CONTRIBUTING.md) | **English**

# Contributing

Tianlai welcomes code, protocols, documentation, tests, sound-source leads,
and listening feedback. Contributions should help creators obtain a more
controllable, reproducible, and continuously editable music-rendering process,
not hide aesthetic decisions in unexplained defaults.

The public repository's “Timbre, range, or ensemble issue” form asks for an
instrument ID, candidate timestamp, stem scope, and reproducible evidence. A
reporter does not need to be an instrument expert and does not need to turn one
listening impression into a universal quality verdict. Reproducible software
defects use a separate form. Do not attach scores, recordings, or samples that
you do not have the right to publish to either kind of report.

## Development environment

```cmd
安装运行环境.cmd -SkipSmoke
```

Then run:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[mcp,dev]"
.\.venv\Scripts\python.exe -m pytest -q -m "not external_assets and not listening"
```

Windows 10/11 x64 is the current complete reference platform. Protocols and
pure-Python paths should remain usable on Linux / WSL and native macOS on both
Apple Silicon and Intel. Cross-platform changes should run the matching
bootstrap, diagnostics, and portable gate at minimum; never reuse a virtual
environment across operating systems or CPU architectures. New filenames may use Chinese,
but Python modules, command names, JSON fields, and stable IDs should use ASCII
English. Use `pathlib` for all path operations and never hard-code a personal
disk path.

The default gate is the complete portable suite, which needs no large sound
sources. When changing real-sample mappings, also run `-m external_assets` in
an environment with a complete resource installation. When changing the
frozen listening protocol or production listening evidence, run
`-m listening`. A wholly absent resource may be skipped, but a partial
installation, digest mismatch, or mismatched physical license evidence must
fail; do not hide a damaged state with a skip.

## Change principles

- Fail or report explicitly when a resource is missing, its license is
  unclear, its range is insufficient, or source semantics cannot be preserved.
  Never substitute another source silently.
- Instrumentation, mixing, and editing suggestions from an agent are
  non-executable by default and become formal input only after creator approval.
- Preserve the `event_id` when modifying an existing note in score v1; allocate
  a new unique ID for a new note.
- New protocols need a version field, strict parsing, boundary tests, and
  migration guidance.
- Audio improvements should include a reproducible test or a method for
  generating listening evidence. Do not promote “sounds better” into an
  unbounded global claim.
- Do not commit `.venv/`, `音源/`, user scores, rendered output, or local caches.

## Third-party material

Do not upload audio, scores, MIDI, MusicXML, images, or code with unclear
provenance, scraped from an aggregator, or lacking permission directly to an
issue or contribution. A sound-source candidate must at least record its
official origin, version, original license text, attribution requirements,
file digests, and permitted use and redistribution. Non-commercial project use
does not lower this threshold.

Project-authored contributions are submitted under Apache-2.0, and contributors
must have the right to provide their content. Third-party resources do not
become Apache-2.0 material by entering a Tianlai manifest; they remain governed
by their own licenses.

## Commits and review

Summarize the outcome in one sentence in the commit message. A pull request
should explain:

1. What problem it solves.
2. What users can observe changing.
3. Which tests or listening checks were run.
4. Whether it changes a protocol, default, audio behavior, resource, or license
   boundary.
5. Whether existing projects need a migration.

Original project attribution is in `NOTICE`. Under Apache-2.0, a modified
version should prominently identify its own changes.
