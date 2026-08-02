[中文](README.md) | [English](README.en.md)

# Acoustic bass (formal)

Karoryfer Meatbass double bass, primarily pizzicato with arco as an alternative.
This directory is the dedicated implementation for SAM-10 in the 98-item
inventory. It reuses `tianlai/dedicated_sfz.py` as its rendering engine and has
no silent fallback to a general-purpose SoundFont.

## Source and license

- Upstream: Karoryfer Samples: Meatbass (1958 Otto Rubner double bass)
- Version: master @ ac9e859564bd, license: CC0-1.0
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py)

## Mapping and articulations

- `pizzicato`: `Programs/04_pizz.sfz`
- `arco`: `Programs/02_arco_3vel.sfz`

The default articulation is `pizzicato`; `pitch_mode` is `pitched`. The adapter
applies the upstream `<control>` CC initial values: CC107 selects only the basic
map by default, while CC103=127 correctly enables sustain. Each note therefore
triggers only one mapping, and neither pizzicato nor arco becomes incorrectly
silent after 200 ms.

After resource verification, the implementation actually uses 264 pizzicato
regions (deterministic random variants) and 102 arco regions (Round Robin). The
102 arco regions have valid loop boundaries. An old report loaded multiple
CC107 maps simultaneously and incorrectly reported thousands of regions; that
report is obsolete.

## Range

E1(28) - G3(55)

## Tuning

Narrow-window FFT diagnostics across 366 root samples have a median residual
of -2.394 c. Some low-string fundamentals are weak and can hit the boundary of
the narrow ±180 c search, so that maximum is diagnostic only and is not used
as the acceptance criterion. The final gate renders MIDI 28, 42, and 55 for
both pizzicato and arco from the real manifest and checks them with a broad
±1800 c search. All six probes have no octave error and remain within ±35 c.

## Listening check

Fixed events: `examples/原声贝斯_奏法.events.json`;
render duration 12.20 s, peak 0.443506,
RMS 0.037189, clipping 0;
WAV SHA-256 `d979ae3c…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

The recording is oriented toward folk/pop use. Pizzicato is the primary entry;
for classical solo arco, use the double-bass entry. On low strings the
fundamental can be weaker than the second harmonic, so acceptance must examine
periodicity and odd harmonics together rather than taking only the strongest
spectral peak. The currently bound version has passed single-instrument
listening review and is marked `formal`; ensemble use, the complete
articulation set, and real repertoire remain untested.
