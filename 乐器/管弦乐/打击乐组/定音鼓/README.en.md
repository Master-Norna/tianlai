[简体中文](README.md) | **English**

# Timpani

Dedicated, strictly `CC0-1.0` multisample `formal` implementation based on VCSL `1.2.2-RC`. Input is actual sounding pitch, and each articulation enforces its own genuine coverage:

- `hit`: `Timpani 2 - Scale`, MIDI 38–59 (D2–B3). 54 PCM24 stereo WAV files, 9 recorded pitch groups, 3 recorded velocity layers, and genuine RR2 in each layer.
- `roll`: `Timpani 1 - Roll`, MIDI 41–55 (F2–G3). 10 PCM16 stereo WAV files, 5 recorded pitch groups, 2 recorded velocity layers, and no round robin.
- Rolls are naturally finite recordings lasting 15.7–29.5 seconds. They contain no WAV loop and are not claimed to provide infinite rolls.
- Single hits preserve the upstream velocity crossfades and short starting offsets. The 40 nonzero offsets remove at most 2.9% of the corresponding sample peak, and none reaches 5%.
- There is no synthetic random pitch, loudness, or delay, and repeated triggers are not misrepresented as recorded round robins.
- `hit` and `roll` come from two different recording sets in the same CC0 library and may have a timbral seam, explicitly retained in the machine report.

Timpani are strongly inharmonic instruments. This project plays the pinned SFZ `pitch_keycenter + tune` mapping. Low-frequency spectral modes are diagnostic only; a single FFT peak is never presented as a false automatic pitch correction.

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/定音鼓/比较VCSL候选.py
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/定音鼓/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/定音鼓/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/定音鼓/乐器.json --events examples/定音鼓_奏法.events.json --output output/定音鼓_奏法_candidate.wav
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/定音鼓/核验试听.py
```

The status remains `formal`: per-sample hashes, clipping, tails, offsets, mappings, and end-to-end tests are automatically verified. The timbral transition between the two recording sets and complete musical contexts remain marked for ensemble/long-context review.
