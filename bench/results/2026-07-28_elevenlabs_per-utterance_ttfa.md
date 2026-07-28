# elevenlabs TTFA — socket per utterance

- date: 2026-07-28
- provider: elevenlabs (raw pcm_s16le 24kHz, socket per utterance)
- runs: 20
- concurrency: 1
- hardware: Windows 11 / AMD64 / 16 cores
- text: 'Thanks for calling. I can book that appointment for you right now.' (66 chars)
- mean audio produced: 3.05s

TTFA is measured from before the request is issued to the arrival of the
first AudioChunk at the caller.

| metric | ms |
|---|---|
| min | 528 |
| p50 | 569 |
| p95 | 603 |
| max | 654 |

Mean audio-seconds per TTFA-second: 5.4

## Why this number exists

A buffered implementation satisfies every contract assertion: the events
arrive in the right order, the first chunk carries real audio, sequence
numbers are contiguous. Nothing in the test suite can tell it apart from a
genuinely streaming one. Only TTFA can.

Both modes are measured by the same instrumentation, unchanged, so the two
numbers are comparable.

Raw samples (ms): [584, 556, 553, 572, 571, 601, 531, 569, 542, 542, 579, 584, 594, 569, 564, 536, 570, 528, 654, 555]
