[简体中文](README.md) | **English**

# Tubular Bells

The current `formal` entry is pinned to VCSL `Tubular Bells 2`. Sounding and written range are both C4–G5 (MIDI 60–79). This is a single, verifiable CC0 source and no longer references the former VPO/NBO mixed-license samples.

The manifest's `type: vpo_percussion` is a historical interface name retained to reuse the shared percussion SFZ adapter; it does not mean that this instrument still uses VPO samples. The actual source is locked entirely by the VCSL path and pinned hashes.

## Genuine material and mapping

- 22 PCM16 / 44.1 kHz / stereo WAV files.
- 11 genuine root notes: 60, 62, 64, 65, 67, 69, 71, 72, 74, 76, 77.
- Each root has 2 genuine recorded velocity layers: MIDI velocity 0–83 and 84–127.
- Interpolation between adjacent roots covers C4–G5, with a maximum stretch of 2 semitones.
- 0 round robins. The `_1` / `_2` suffixes in filenames identify takes selected upstream; the SFZ has no sequential-alternation opcode, so they are not misrepresented as round robins.
- 0 loops. `open` plays the complete natural tail and preserves stereo spatial information.

`damped` is not an independently recorded damping articulation. It reuses the same recordings and applies a 120 ms de-click release envelope at the project layer on `note_off` or pedal release. It can be used for arrangement control, but it must not be described as containing genuine damper noise or independent damping samples.

## Project-level corrections

Two nonzero starting offsets in upstream `Tubular Bells 2.sfz` cut into clear strike attacks:

- `TB_hit_B4_v2_1.wav`: 1026 frames.
- `TB_hit_C5_v4_1.wav`: 2727 frames.

This project explicitly overrides these two `offset` values to 0 after loading the mapping; the VCSL source files are not modified. Upstream region gain can raise original peaks to approximately +3.046 dBFS, so the instrument's total gain is fixed at 0.35, retaining approximately 6.07 dB of headroom in the static worst case.

## Tuning interpretation

The SFZ `pitch_keycenter` already includes the `--transpose 12` result from its generation comment and must not be shifted up another octave. Tubular-bell spectra are strongly inharmonic, and the largest individual FFT peak is not necessarily the perceived fundamental. Automatic per-sample cents correction remains disabled; only root notes, range, octave, and mapping are verified. Human spectral and perceptual pitch review remains `pending`.

## Verification

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/管钟/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/管钟/校准音准.py
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/管钟/核验试听.py
.\.venv\Scripts\python.exe -m unittest tests.test_vcsl_tubular_bells
```

The single-timbre audition for the currently bound version has passed, so its status is `formal`. Genuine remaining limitations are only 11 root notes and 2 velocity layers, no round robin, envelope-simulated `damped`, and pending human blind listening and inharmonic pitch review.
