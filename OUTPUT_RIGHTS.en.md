[简体中文](OUTPUT_RIGHTS.md) | **English**

# Tianlai output-rights statement

> **Translation notice:** This English page is an informational translation of
> the project-authored [Chinese source text](OUTPUT_RIGHTS.md). If the wording
> diverges, the Chinese source text describes the project's intent. Upstream
> license texts and applicable law—not this summary—govern third-party material.

This statement distinguishes the rendering engine, input works, instrument
resources, and rendered output. It describes the rights that the Tianlai
project itself grants and claims. It does not replace a third-party license or
decide the legal status of a particular work for the user.

## The code license does not automatically attach to music output

Project-authored programs, DSP, MCP/CLI, tools, schemas, tests, and project
configuration are provided under the Apache License 2.0 in the root
[`LICENSE`](LICENSE). Apache-2.0 governs copying, modifying, and redistributing
that software and derivative software.

Merely using Tianlai for one render does not automatically turn the input MIDI,
MusicXML, score, orchestration, or generated WAV into an Apache-2.0 software
derivative. The Tianlai project does not acquire copyright in a work because
the work used this engine, and it charges no engine royalty on ordinary
rendered output.

## Project-authored DSP

An instrument carrying the following complete structured declaration is
generated entirely by project-authored code and reads no third-party samples,
SoundFonts, SFZ files, impulse responses, or other external audio assets:

```json
{
  "provenance_kind": "project_authored_dsp",
  "implementation_license": "Apache-2.0",
  "external_audio_assets": [],
  "audio_asset_license": "not_applicable",
  "license_status": "approved"
}
```

For audio produced exclusively with these entries, the Tianlai project imposes
no attribution, royalty, or additional audio-asset license requirement based
on its DSP implementation. Copying or redistributing the DSP source itself
still requires compliance with Apache-2.0 and [`NOTICE`](NOTICE).

## Third-party sampled instruments

When sampled instruments are used, the original recordings, sample libraries,
SFZ files, and related materials remain governed by their respective upstream
terms. Tianlai's Apache-2.0 code license does not change those terms. Each render
produces `许可与署名.json/.txt` listing only the instruments actually used in
that render. Check the listed licenses, attribution, and restrictions before
publishing audio. The sidecar is a factual inventory and cannot expand an
upstream grant.

## Input works and user responsibility

- Rights in input scores, MIDI, MusicXML, lyrics, and arrangements remain with
  their authors or other applicable rightsholders.
- A user's lawful original creation and its personal or economic rights are not
  transferred to the project by using Tianlai.
- Protected compositions, performances, recordings, third-party samples,
  personality rights, and other rights may each require separate permission.
- Users are responsible for the inputs they provide and the way they publish
  final output.

Apache-2.0 does not grant a trademark license to the Tianlai name, Chinese name,
or project identity. You may truthfully state that a work or software product
comes from, is based on, or is compatible with Tianlai, but you may not imply
an unauthorized official status, sponsorship, or endorsement.
