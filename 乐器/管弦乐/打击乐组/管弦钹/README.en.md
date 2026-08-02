[简体中文](README.md) | **English**

# Orchestral Cymbals

Dedicated VPO 3.3 multisample `formal` entry with no fixed pitch and 15 deduplicated WAV files.

Public articulations: `roll_soft`, `piatti`, `roll_alt`, `piatti_high`, `crescendo_short`, `crash`, `crescendo_medium`, `suspended_hit`, `crescendo_long`, and `suspended_high`. Crash and suspended-cymbal hits include two velocity layers × 2 round robins. Rolls can be released early, while prerecorded crescendos and crashes preserve their long tails as one-shots.

```powershell
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/管弦钹/乐器.json --events examples/管弦钹_奏法.events.json --output output/管弦钹_奏法.wav
```

The single-timbre audition for the currently bound version has passed, so its status is `formal`. Prerecorded crescendo lengths still cannot be stretched losslessly to match a score; there is no genuine hand-damped choke, mallet/edge position, or multiple microphone position. Upstream EQ/spatial processing and blinded ensemble audition remain incomplete.
