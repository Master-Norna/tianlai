[简体中文](README.md) | **English**

# Taiko (`formal`, deterministic modeling)

Circular-membrane modal taiko model with Bessel modal ratios, drumhead-impact noise, and three keys. It is implemented in `tianlai/modeled_instruments.py` (profile `taiko`, engine 1.1.0) with explicit seed 41035. The same event sequence always produces the same output; there is no silent fallback to a general-purpose SoundFont.

## Reason for modeling (honest disclosure)

No genuine sample set with a clear public license was found, so deterministic physical modeling currently fills the gap. Genuine samples remain a future fidelity upgrade.

## Source and licensing

Project-developed deterministic DSP. The engine source-file SHA-256 and all parameters are available in [`资源核验.json`](资源核验.json); reproduce them with [`核验资源.py`](核验资源.py).

## Range

See the key mapping.

## Key mapping

| MIDI key | Content |
| --- | --- |
| 60 | Center `don` strike (82 Hz circular-membrane mode) |
| 61 | Edge strike (118 Hz, emphasizing higher modes) |
| 62 | `ka` wooden rim strike (short, high-frequency) |

## Tuning

Taiko is membranophone percussion. Keys 60/61/62 select a center `don`, edge strike, or wooden-rim `ka`; no equal-temperament calibration is performed (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/太鼓_奏法.events.json`; render duration 11.89 s, peak 0.349451, RMS 0.044988, clipping 0; WAV SHA-256 `68036221…`. Reproduce with [`核验试听.py`](核验试听.py).

## Known limitations

The deterministic modal model is not a recording, and it does not model interactions between the tails of repeated strikes. The currently bound version has passed single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and real repertoire remain untested.
