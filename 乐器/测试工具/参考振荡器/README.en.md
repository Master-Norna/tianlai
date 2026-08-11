[中文](README.md) | [English](README.en.md)

# Reference oscillator

This is a calibration instrument in the base instrument layer; it does not
model a real acoustic instrument. It provides an accurate fundamental and
fixed harmonics for verifying:

- the A4 reference and twelve-tone equal-temperament conversion;
- whether events land at exact sample positions;
- polyphony and sustain-pedal state;
- whether identical inputs produce byte-identical WAV output.

Every formal instrument uses the same directory convention:

```text
乐器/分类/声部组/乐器名/
├─ 乐器.json       # metadata, parameters, and asset mappings
├─ 乐器.py         # optional compatibility entry; builtin dispatch remains the default
├─ 测试/
└─ README.md
```

Categories without a section group may omit that level; for example, the
piano is located at `乐器/键盘乐器/钢琴/`. Large audio assets are kept under
the project-root `音源/` directory.
