[中文](README.md) | [English](README.en.md)

# Accordion (bounded-range formal)

The real source is the FreePats Hohner button accordion (Button Accordion HN).
It contains 17 looped attack samples paired with 17 genuine release samples.
Runtime reuses `tianlai/dedicated_sfz.py`, with no silent fallback to a
general-purpose SoundFont or synthesized timbre.

## Source and license

- Upstream: FreePats project: Button Accordion HN
- Official page: <https://freepats.zenvoid.org/Organ/accordion.html>
- Release version: 2024-03-29
- License: CC0-1.0
- Local evidence: `LICENSE.txt`, `README.txt`
- Per-file SHA-256 and format statistics: [`资源核验.json`](资源核验.json)

## Range policy

The SFZ has 17 attack root notes spanning MIDI 47–79. This instrument formally
uses D3–G5 (MIDI 50–79) as its core range. The highest real root is G5
(MIDI 79).

To retain the three immediately adjacent semitones, G#5–A#5 (MIDI 80–82) form
an explicitly marked limited extension, transposed upward by no more than 3
semitones from G5. This does not exceed the maximum +3-semitone key zone already
used by the upstream mapping in the low register. The former G6 (MIDI 91)
required transposing the same G5 root by a full 12 semitones, shifting its
transient, noise, and formants together. Runtime now explicitly rejects MIDI
83–91.

The tiers mean:

- MIDI 50–79: core range, within the span of adopted real attack roots;
- MIDI 80–82: limited extension, still transposed samples and not falsely claimed as per-key sampling;
- MIDI 83–91: no longer supported, pending a license-qualified higher real root.

See [`来源.md`](来源.en.md) for adopted-resource selection criteria, licensing,
and the high-note boundary.

## Articulation and dynamics

- `sustain`: a looped sustain sample with its paired release sample triggered at note-off.
- There is one recorded velocity layer. Velocity and expression change playback loudness only and must not be presented as real bellows pressure or a timbral dynamic layer.

## Tuning

Harmonically constrained FFT diagnostics cover the 17 root samples; playback
continues to use the verified upstream `pitch_keycenter` and `tune`. Accordion
reed beating affects single-window frequency estimates, so the report retains
measured residuals without treating one reed peak as the sole pitch of the
whole instrument. See [`音准校准.json`](音准校准.json).

## Recomputing evidence

```powershell
.\.venv\Scripts\python.exe 乐器/键盘乐器/手风琴/核验资源.py
.\.venv\Scripts\python.exe 乐器/键盘乐器/手风琴/校准音准.py
.\.venv\Scripts\python.exe 乐器/键盘乐器/手风琴/核验试听.py
.\.venv\Scripts\python.exe -m unittest tests.test_accordion_range -v
```

The fixed listening check covers the lowest note, a core midrange note, the
highest real root, and the top of the limited extension. Results are in
[`试听核验.json`](试听核验.json).

## Known limitations

- The current FreePats release has no real attack root above MIDI 79.
- One velocity, one articulation, no real bellows-pressure layers, and no round robin.
- MIDI 80–82 still has a slight upward-transposition timbral change.
- The currently bound version has passed single-instrument listening review and is therefore `formal`; manual blind re-review and extended/ensemble dimensions still await review.
