[简体中文](README.md) | **English**

# Instrument index

Instruments are organized as “instrument family → section → specific
instrument.” Tianlai currently registers **103 sound entries** plus one test
utility. Every registered entry has a loadable dedicated implementation; none
relies on a generic SoundFont merely to produce sound. The currently bound
version of every entry has passed an isolated single-instrument,
single-timbre listening check and is therefore marked
`quality_tier=formal`.

## Two independent status axes

- **Formal single-timbre layer (all 103):** `quality_tier=formal` means only
  that the currently bound version passed isolated listening as one instrument
  and one timbre. Every entry has its own sample mapping, physical/signal model,
  or deterministic DSP, together with frozen-resource evidence, pitch
  calibration or a justified not-applicable declaration, a fixed listening
  check, and reproducibility scripts. All 103 entries use the same quality
  states; their tier does not depend on when they were added.
- **Ensemble review pending (all 103):**
  `collaboration_review_status=untested` means that multi-instrument
  collaboration, orchestration, mixing, and acceptance with actual repertoire
  have not yet passed.
- **Test utility (1):** the reference oscillator validates pitch, scheduling,
  and determinism. It does not model an acoustic instrument and is not included
  in the 103 entries.

`formal` does not mean that every articulation, velocity layer, Round Robin,
strict high-quality range, or expert-review status is covered. It also says
nothing by itself about licensing, public release, default `trusted` curation,
or ensemble acceptance. License status and `可信乐器.json` are independent of
quality tier. Current third-party resources comprise 43 `approved`, 31
`grandfathered`, and 0 `quarantined` resources. `grandfathered` permits use only
within the frozen license and official-installation boundary; it must not be
interpreted as permission to redistribute the original samples.

Run `python -m tianlai progress` from the project root to inspect the
machine-readable state of the 98-item extended registry. That number is not the
total number of 103 sound entries. See
[`docs/当前状态.en.md`](../docs/当前状态.en.md) for the overall capability boundary.

## Category overview

| Category | Sound entries | Implementation path |
|---|---:|---|
| World instruments | 9 | 5 dedicated VCSL/FreePats/VPO sample instruments; 4 deterministic models, including project-authored Chinese chime bells |
| Voice instruments | 1 | Dedicated VPO mixed-choir samples |
| Bass instruments | 3 | Dedicated FreePats / Karoryfer samples |
| Plucked instruments | 6 | FreePats / Karoryfer samples, with deterministic effects chains layered onto 3 entries |
| Environment and foley | 8 | Project-authored deterministic foley DSP |
| Modern winds | 4 | Dedicated MTG Solo Sax samples |
| Modern drum kit | 10 | Dedicated VCSL percussion samples |
| Electronic instruments | 11 | 10 project-authored deterministic synthesizers; 1 real layered VPO orchestral hit |
| Orchestra / strings | 7 | Dedicated VPO samples, including violin and cello |
| Orchestra / woodwinds | 10 | 8 dedicated VPO sample instruments, including flute; 2 deterministic models |
| Orchestra / brass | 6 | 5 dedicated VPO sample instruments; 1 VPO sample instrument with mute modeling |
| Orchestra / percussion | 17 | 14 dedicated VPO/VCSL sample instruments; 3 deterministic models |
| Orchestra / plucked strings | 1 | Dedicated VPO harp samples |
| Keyboard instruments | 10 | 8 dedicated sample instruments, including piano; 1 deterministic model; 1 sample instrument with a chorus chain |

## Uniform artifacts for every entry

Each of the 103 directories contains:

- `乐器.json`: type, quality tier, explicit fallback policy, and parameters;
- `资源核验.json` + `核验资源.py`: per-file SHA-256 values and license evidence,
  or source-file hashes for a project-authored engine;
- `音准校准.json` + `校准音准.py`: measured pitch diagnostics or a justified
  not-applicable declaration;
- `试听核验.json` + `核验试听.py`: peak, RMS, clipping, and WAV hashes from a
  fixed-score render;
- `README.md` and `来源.md`: implementation notes, source licenses, and known
  limitations.

## The precise boundary of `formal`

An entry may be marked `quality_tier=formal` only after its currently bound
implementation, resources, and isolated-listening material agree and its
single-instrument, single-timbre check passes. This is an availability
conclusion for the base instrument layer, not a master switch declaring every
quality question solved.

The finer articulation × pitch × velocity × RR/runtime-variant matrix remains
independently gated by `range_profiles` and `strict_hq`. Expert review,
licensing, and public-release status retain separate conclusions as well. A
multi-instrument work may advance an entry's or work's
`collaboration_review_status` beyond `untested` only after collaboration,
orchestration, part balance, mixing, and listening with actual repertoire. All
103 entries are currently `formal` on the single-timbre axis and `untested` on
the ensemble axis. See
[`docs/当前状态.en.md`](../docs/当前状态.en.md) for the authoritative details.

## Directory conventions

- Each instrument keeps its manifest, behavior code, calibration results,
  provenance, and documentation in its own Chinese-named directory.
- Multiple instruments in one family may share a common backend, but they may
  not use the same samples or one GM drum note to masquerade as different
  instruments.
- All large samples, upstream sound libraries, native runtime libraries, and
  download caches belong under the root-level `音源/` directory.
- All 103 entries declare
  `fallback_policy: explicit_only_no_silent_gm`: a missing resource causes an
  explicit error and is never silently degraded to a generic SoundFont
  pretending to be the dedicated timbre.

GeneralUser GS and TimGM are available only when a user explicitly selects them
for local compatibility or backend testing. They do not participate in default,
`public/trusted`, MCP, or collaboration-layer routing, and failure to load one
never causes an automatic switch to the other.

Existing implementations may serve separately as references for dynamics and
pedaling, bowed-string articulations, independent release samples, and woodwind
legato. A new instrument must still be designed around its own samples and
performance mechanism; changing only the name is not sufficient.
