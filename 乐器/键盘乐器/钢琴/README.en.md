[中文](README.md) | [English](README.en.md)

# Piano

A Yamaha C5 grand piano based on Salamander Grand Piano V3.

## Current capabilities

- 30 sampled root notes cover A0–C8.
- Retains the upstream SFZ's 16 nonuniform velocity layers.
- Decodes FLAC on demand instead of loading the entire source at startup.
- Uses all 641 upstream FLAC files at runtime: 480 main-note samples, 88
  key-release sounds, 69 string-release resonances, and 4 pedal sounds.
- Independent key-release mechanical sounds for all 88 keys.
- Three layers of string-release resonance: `harmS` / `harmL` are selected by
  velocity, while `harmV3` releases in parallel with the selected layer.
- Sustain-pedal state and two groups of pedal-up/down mechanical sounds.
- Longer release times in the undamped high register.
- A first approximation of soft-pedal response.
- Versioned band-limited resampling on every piano sample path, with a direct
  bypass when the playback step is exactly `1:1` so the source is not changed
  unnecessarily.
- Sample-accurate scheduling and deterministic sample selection.

## Current limitations

- Half-pedaling accepts a continuous value, but damper switching still uses a threshold approximation.
- Sympathetic resonance uses upstream release samples rather than a global string-coupling model.
- The soft pedal has no independent sample layer yet.
- Upstream offers an optional sample-start offset for faster touch response.
  This entry preserves the full attack by default and does not enable that
  optional fast-response mode in this refinement.
- Repedaling, the sostenuto pedal, and a physical soundboard model are not yet implemented.

See [来源.md](来源.en.md) for licensing and sound-source provenance.

## Tuning verification

Actual measurement of middle-velocity A4:

```powershell
.\.venv\Scripts\python.exe -m tianlai analyze-pitch `
  --audio 音源/钢琴/SalamanderGrandPiano/Samples/A4v8.flac `
  --expected-hz 440
```

The current result is approximately `440.2334 Hz / +0.918 cents`; the dedicated
test requires an error below 2 cents.

## Obtaining the sound source

Sound-source assets are excluded from project code version control. In a new
environment, run:

```powershell
powershell -ExecutionPolicy Bypass -File 乐器/键盘乐器/钢琴/获取音源.ps1
```

The installer pins official repository commit
`3382bf9496bba2486f5ab0de55a264d1dfc38404`. In a temporary directory it
verifies the license, README, main SFZ, all 641 upstream FLAC files, and the
complete 668-file tree before atomically installing it. If an existing
directory matches exactly, it is only verified before exit. If it differs, a
new tree is built first, and the old directory is restored if switching fails.
See [`来源.md`](来源.en.md) for complete digests.

## Rendering example

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/键盘乐器/钢琴/乐器.json `
  --events examples/钢琴_C大调.events.json `
  --output output/钢琴_C大调.wav
```

## Quality status and verification

This entry is `formal`. Verification materials:

- [`资源核验.json`](资源核验.json): after constructing an instance, traverses all 641 samples **actually mapped** and computes per-file SHA-256; recompute with [`核验资源.py`](核验资源.py).
- [`音准校准.json`](音准校准.json): per-root harmonic FFT diagnostics only for the pitched main-note and three string-release-resonance layers. Key-release and pedal mechanical layers do not participate in tuning statistics or conclusions. Recompute with [`校准音准.py`](校准音准.py).
- [`试听核验.json`](试听核验.json): peak/RMS/clipping/WAV Hash for the
  88-key full-range stress scan; recompute with
  `tools/生成全部试音.py --only 键盘乐器/钢琴`.
- [`表现力试听核验.json`](表现力试听核验.json): a fixed four-part,
  four-velocity example covering sustain pedal and all three resonance layers;
  recompute with [`核验试听.py`](核验试听.py). The two evidence files are
  independent and never overwrite one another.

## Sample-mapping notes

### C8 sample group

The upstream `C8v*.flac` files measure approximately C#8.
`_ROOT_TUNING_CENTS` in the central implementation `tianlai/piano.py` declares
this group of root samples as
`+100 cents`, allowing the engine to resample the B7 / C8 keys back to their
correct pitches. This calibration is already active; C8 is not an unresolved
defect. Other root samples do not receive this correction, preserving the
piano's original stretch tuning. See [`音准校准.json`](音准校准.json) for the
complete measurements.

### Not a defect: +27 to +38 cents in the high register

F7–B7 measures about +27 to +38 cents as part of the piano's Railsback stretch
tuning: the high register is sharpened and the low register flattened. This is
not an equal-temperament error, and rendering does not forcibly flatten this
region.

### Other

- Key-release noise (`rel*.flac`) and pedal noise (`pedal*.flac`) are unpitched
  mechanical layers. The tuning report no longer tries to infer fundamentals
  for them and does not include them in tuning statistics or conclusions.
- The current report includes 480 main-note samples and 69 mapped
  string-release resonances. Main-note detuning has a `+0.766-cent` median, a
  `41.125-cent` maximum absolute value, and no outlier beyond 50 cents. All six
  larger outliers in the report belong to non-stationary resonance-release
  samples; they require path-specific listening review and are not evidence
  for automatically retuning the main piano.
