[简体中文](README.md) | **English**

# Snare Drum

Dedicated VPO 3.3 multisample `formal` entry with no fixed pitch, separating public playing techniques from the actual SFZ keys.

- `left` / `alternating` / `right`: SSO left hand, automatic alternation between two round robins, and right hand.
- `hit`: default VSCO2-CE strike with two velocities × 2 round robins.
- `kit2_left` / `kit2_right`: fixed left/right branches of the second drum.
- `tap`: two velocities × 2 round robins.
- `roll_looped`: continuous loop embedded in the SSO WAV; `roll`: finite VSCO2 recorded roll.
- `width=0` in the two roll mappings is applied as mid/side collapse before panning.
- SFZ random terms are replaced by stable variation reproducible across directories.

```powershell
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/小军鼓/乐器.json --events examples/小军鼓_奏法.events.json --output output/小军鼓_奏法.wav
```

The single-timbre audition for the currently bound version has passed, so its status is `formal`. The original 54–104 velocity crossfade is currently still selected discretely at the midpoint. There is no rimshot, brushes, snare-wire switch, or multiple microphone position; extended/blinded ensemble audition still requires review.
