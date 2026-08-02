[中文](README.md) | [English](README.en.md)

# Orchestral Hit

A layered `formal` orchestral hit built from real Virtual Playing Orchestra 3.3 samples. It is neither a GM Orchestra Hit nor a synthesizer substitute: each trigger layers real string and brass accents at the same concert pitch with a fixed bass drum at D2 and Crash cymbal at F♯4.

## Current Capabilities

- four string sections and four brass sections crossfade over their shared upstream key range, D1–C6 (MIDI 26–84);
- 182 string-accent regions and 155 brass-accent regions; the brass is divided into 79 short-attack regions and 76 looped-sustain regions;
- the bass drum at D2 uses 2RR × 2 crossfaded velocity layers; the Crash cymbal at F♯4 uses 2RR × 2 discrete velocity layers;
- each event releases pitched sustain layers at a fixed gate threshold, while percussion decays as recorded one-shots; the external note length does not accidentally truncate the hit;
- 140 stable sustained root samples were calibrated with a harmonically constrained FFT: median deviation `-0.081554 cents`, maximum raw deviation `22.954514 cents`; short noisy attacks are not subjected to unreliable FFT calibration;
- 4 SFZ files reference 345 regions and 278 deduplicated WAV files; deterministic rendering preserves real Round Robin behavior and velocity layers.

## Why It Remains Under “Electronic Instruments”

Here, “Orchestral Hit” is a production-oriented composite patch/effect, not an acoustic instrument playable by a single performer. The category describes its use, not a claim that electronic waveforms imitate the source: all four underlying material groups are recordings of real instruments.

## Usage

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/电子乐器/管弦重击/乐器.json `
  --events examples/管弦重击_奏法.events.json `
  --output output/管弦重击_奏法_candidate.wav
```

## What Single-Timbre `formal` Status Does Not Claim

- This is a programmatically layered unison tutti, not a dedicated Orchestra Hit recorded as one naturally phase-coherent studio performance, and not a set of prerecorded major/minor chords;
- the bass drum and cymbal remain at fixed pitches while the strings and brass transpose; there are no room-microphone positions, separate releases, or additional cymbal/drum choices;
- the pinned version has passed a single-timbre audition and is therefore `formal`; layer balance, impact, and ensemble blind listening remain pending.

See [来源.md](来源.en.md) and [资源核验.json](资源核验.json) for the resource versions, licenses, and aggregate hash.
