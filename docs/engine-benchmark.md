# Portable engine performance baseline

From a source checkout with the core dependencies installed:

```bash
python -m tools.benchmark_engine --seconds 5 --repeats 3
```

This optional benchmark creates a synthetic mono WAV in a private temporary
directory, then measures bandlimited sample rendering + PCM24 encoding,
post-render analysis, and source-tree hashing separately. It records the
platform, Python/NumPy versions, individual timings, medians, and output
fingerprints. It uses no external audio assets and sets no timing assertion in
CI. Run the same script on baseline and candidate checkouts on the same idle
machine with the same Python and dependencies; compare the render and report
fingerprints as well as medians. Source-tree hashes are expected to change
between source revisions.

The rendering measurement includes sample decoding, output writing and hashing,
not just DSP. It does not measure a multi-instrument production mix, real asset
restoration, or performance across different hardware. Do not relax generation
validation, PCM contracts or numerical analysis on the basis of a speed result.
