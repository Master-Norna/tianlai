[简体中文](README.md) | **English**

# Vibraphone

This entry point has migrated to a dedicated, strictly CC0 multisample `formal` implementation based on VCSL `1.2.2-RC`. It no longer depends on the grandfathered VPO/Iowa license, and adjacent root notes are no longer misrepresented as round robins.

## Genuine material

- Soft mallets: 11 recorded root notes × 2 genuine recorded velocity layers.
- Hard mallets: 11 recorded root notes × 2 genuine recorded velocity layers.
- Bowed: 6 recorded root notes × 1 layer.
- 50 stereo recordings in total, with a genuine round-robin count of 0.
- Soft- and hard-mallet range: F3–F6 (MIDI 53–89); bowed range: A3–F6 (MIDI 57–89).

## Articulations

- `damped` / `open`: soft mallet; the former uses a 350 ms engineering damping envelope on key release.
- `hard_damped` / `hard_open`: hard mallet, likewise distinguishing engineering damping from the natural tail.
- `bowed`: independent bowed recordings.

All 50/50 recordings use their measured pitch for runtime calibration; the upstream SFZ and WAV files remain unchanged. See `试听核验.json`, `资源核验.json`, and `音准校准.json` for the fixed audition, resource hashes, and tuning data, respectively.

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/颤音琴/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/颤音琴/校准音准.py
```

## Honest limitations

There is currently no same-note round robin, motor speed/depth control, independent pedal noise, or multiple microphone position. `damped` is a project release envelope rather than an independent damping recording. The single-timbre audition for the currently bound version has passed, so its status is `formal`; independent damping and blinded ensemble audition still require more detailed acceptance testing.
