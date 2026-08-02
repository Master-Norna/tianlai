[简体中文](README.md) | **English**

# French Horn

This `formal` solo French horn is based on Virtual Playing Orchestra 3.3. By default it reads VPO's independent WAV multisamples directly and never falls back silently to a GM SoundFont.

- Actual sounding range: MIDI `35–77`, B1–F5. Out-of-range notes fail immediately rather than being stretched and misrepresented.
- Notation semantics: all events use concert pitch. Written pitch for a horn in F is a perfect fifth above concert pitch, `concert = written - 7`; the corresponding written range is F#2–C6.
- Articulations: `normal`/`sustain`, `slow_sustain`, `staccato`, and `accent`.
- Continuous controls: `expression` and `breath` smoothly control loudness; `modulation` selects one of 9 attack lengths for subsequent long notes; `sustain_pedal` holds released long notes.
- Dynamics: VPO has two sustain/staccato layers, currently selected deterministically at the midpoint of the upstream crossfade.
- Tuning: all 39 sustain root samples were measured individually by FFT and recorded at A4=440 Hz. Rendering still follows the A4 reference and fractional MIDI pitches in the performance document.
- Release: SFZ envelopes and embedded loops, with no independent release samples.

The single-timbre audition for the currently bound version has passed, so its status is `formal`. Random pitch, random volume, and random delay from the upstream SFZ are disabled to keep repeat renders byte-identical. The current implementation does not include the upstream EQ/LFO, genuine continuous dynamic crossfading, or any blinded ensemble-audition conclusion and therefore must not be described as a 100% reproduction.

Reproduce the calibration and resource verification with:

```powershell
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\圆号\校准音准.py
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\圆号\核验资源.py
```

Audition events are in `examples/圆号_奏法.events.json`. The original source page is `来源.md`; see [来源.en.md](来源.en.md) for its English translation and `资源核验.json` for the licensing boundaries and pinned hashes.
