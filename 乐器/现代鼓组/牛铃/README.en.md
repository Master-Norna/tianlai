[中文](README.md) | [English](README.en.md)

# Cowbell

A real, independent `formal` Cowbell based on Virtual Playing Orchestra / VSCO2 Community Edition. It uses the four Cowbell WAV files assigned to MIDI 56 in `misc.sfz`; it no longer routes through a General MIDI drum kit or incorrectly substitutes the adjacent Agogo.

## Current Capabilities

- 2 velocity layers × 2 Round Robin samples, for a total of 4 real cowbell WAV files;
- the soft layer fades out over velocities 54–104, the hard layer fades in across the same range, and Round Robin selection is globally synchronized across the two layers;
- preserves the upstream `amp_random=1 dB`, while deriving the random value deterministically from each event for reproducible rendering;
- fixed one-shot playback with natural decay: score `note_off` events do not truncate the metallic tail; `expression` is supported;
- MIDI 56 is only the SFZ trigger key. A cowbell is an unpitched percussion instrument, so no fabricated 12-tone equal-temperament calibration is provided.

## Usage

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/现代鼓组/牛铃/乐器.json `
  --events examples/牛铃_奏法.events.json `
  --output output/牛铃_奏法_candidate.wav
```

## What Single-Timbre `formal` Status Does Not Claim

- Only one Cowbell and one striking technique are available: there are no open/muted hits, rim hits, damping options, multiple sizes, or controllable pitch;
- the upstream resource provides no choke/group relationship; playback currently uses each recording as a complete one-shot;
- the pinned version has passed a single-timbre audition and is therefore `formal`; expanded capabilities and ensemble blind listening remain pending.

See [来源.md](来源.en.md) and [资源核验.json](资源核验.json) for the resource version, license, and aggregate hash.
