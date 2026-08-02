[中文](README.md) | [English](README.en.md)

# Kalimba (formal)

Dedicated samples of the VCSL 15-key Kenyan kalimba. This directory is the
dedicated implementation for SAM-02 in the 98-item inventory. It reuses
`tianlai/dedicated_sfz.py` as its rendering engine and has no silent fallback
to a general-purpose SoundFont.

## Source and license

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC, pinned commit `b6e6ac82d22248edee98a0bde185eb9ef6d439ad`, license: CC0-1.0
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py)

## Mapping and articulation

- `normal`: `Idiophones/Plucked Idiophones/Kalimba, Kenya.sfz`

The default articulation is `normal`; `pitch_mode` is `pitched`.

The 15 recordings represent 15 physical tines and 11 distinct root notes.
D♯3, F♯3, G♯3, and B3 each have two different tines at the same pitch, and the
SFZ explicitly groups those 8 regions into 4 RR2 families. They are not
alternate recordings of the same tine, nor are they a second velocity layer;
they are separate physical tines on the same instrument that share a pitch.

## Range

B3(59) - C6(84), with at most 1 semitone of transposition between the 11
recorded root notes.

## Tuning

The metal cantilever tines of a kalimba have strong non-integer modes, so the
string-oriented “highest aggregate integer-harmonic score” algorithm cannot be
reused. Calibration now uses a tine-specific direct local-modal check:

- It normally reads the strongest sustained local mode within ±180 cents of the labeled root note.
- If low-octave resonance dominates the sustain, but a clear mode at the labeled pitch exists during the 50–170 ms attack, that attack mode validates the tine label and the low-octave component is explicitly recorded as genuine resonance.
- Across the 15 mapped tines, the median modal residual is `+6.552 cents`, the maximum absolute residual is `27.160 cents`, and none exceeds 50 cents.
- This report does not automatically reset root notes or force twelve-tone equal temperament. The original VCSL SFZ, physical-tine tuning, and resonance are all preserved.

## Listening check

Fixed events: `examples/卡林巴_奏法.events.json`.
The listening sequence explicitly passes through MIDI 75 and 83, which
correspond to the two disputed tines. Objective results are in
[`试听核验.json`](试听核验.json): 10.15 s, peak `0.380029`,
RMS `0.015418`, 0 clipped samples, WAV SHA-256 `ffa39406…`. Recompute with
[`核验试听.py`](核验试听.py).

## Known limitations

There is one recorded velocity layer, so velocity controls volume only. Four
pitches have two physical tines available for round robin; the rest have no RR.
Not every semitone has an independent recording, though adjacent keys are
resampled by at most 1 semitone. The attack of the highest B tine is close to
the target B5, but after about 100 ms the octave resonance from the same
instrument's lower B tine becomes dominant. This is real acoustic behavior in
the recording and is unsuitable when a pure, stable electronic B5 is required.
The currently bound version has passed single-instrument listening review and
is marked `formal`; ensemble use and complete capability coverage remain
untested.
