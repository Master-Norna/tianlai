[中文](README.md) | [English](README.en.md)

# Bianzhong (programmatic model)

This is the first programmatic-modeling backend for instrument 103. It
requires no downloaded sound source and synthesizes the bell-body response
frame by frame with a deterministic inharmonic modal bank. Its current range
is C2–D7 (MIDI 36–98).

Available articulations:

- `zhenggu`: central/front strike, the default; a fuller fundamental with more concentrated upper modes.
- `cegu`: side strike; uses a different representative set of modal ratios, with a different spectral center and a tighter decay (without assuming that it must be brighter in every register).

There is an important score-level semantic: when both articulations receive
the same MIDI note, they produce the same final fundamental pitch. On a real
two-tone bell, different strike positions on the same bell normally produce
two different pitches. To keep AI and scores from guessing a hidden
transposition, the engine interprets the articulation as “select a central-
strike or side-strike bell from an imaginary set that can produce the target
pitch.” It is therefore a sound model suitable for a composition tool, not a
strike-position-by-strike-position reconstruction of one physical bell.

`expression` controls overall dynamics, while `modulation` smoothly changes
the weight of upper modes. Velocity controls loudness, mallet transient, and
upper-mode brightness together. Every note is a naturally decaying one-shot;
`note_off` releases only the event ID and does not truncate the bell.

The current status is `formal / untested`: program-level tuning,
determinism, band limiting, polyphonic headroom, and manual single-instrument
listening review have passed. `formal` denotes acceptance of the solo entry
only; it does not mean that ensemble and mixing checks in real repertoire are
complete.

Run the current manual review with:

```powershell
& .venv\Scripts\python.exe 乐器\世界乐器\编钟\核验试听.py
```

It generates two temporary audio files:

- `01_编钟_正鼓全音域上行.wav`: one instance of every semitone from C2–D7, used to check range continuity, whether attacks become lighter in the upper portion, and whether dense long tails distort.
- `02_编钟_正鼓与侧鼓对照.wav`: paired “central strike, side strike” examples in the low, middle, and high registers. Each segment uses an independent engine instance, making it easier to judge whether both strike positions still sound like the same class of bell.

Known limitations:

- No per-bell dimensions, alloys, suspension data, or measured impulse responses from any specific historical bianzhong set are used.
- No long room reverberation is synthesized; the audible tail comes from the natural decay of the bell-body modes themselves.
- The range is representative coverage for a composition tool, not a per-bell range table for any physical set.
- There is currently one deterministic model version, with no multilayer samples of real mallet types, distances, or microphones.
