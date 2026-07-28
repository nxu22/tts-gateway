# elevenlabs TTFA — persistent multiplexed socket

- date: 2026-07-28
- provider: elevenlabs (raw pcm_s16le 24kHz, persistent multiplexed socket)
- runs: 20
- concurrency: 1
- hardware: Windows 11 / AMD64 / 16 cores
- text: 'Thanks for calling. I can book that appointment for you right now.' (66 chars)
- mean audio produced: 3.10s

TTFA is measured from before the request is issued to the arrival of the
first AudioChunk at the caller.

| metric | ms |
|---|---|
| min | 223 |
| p50 | 244 |
| p95 | 293 |
| max | 862 |

Mean audio-seconds per TTFA-second: 12.4

## Why this number exists

A buffered implementation satisfies every contract assertion: the events
arrive in the right order, the first chunk carries real audio, sequence
numbers are contiguous. Nothing in the test suite can tell it apart from a
genuinely streaming one. Only TTFA can.

Both modes are measured by the same instrumentation, unchanged, so the two
numbers are comparable.

Raw samples (ms): [862, 236, 238, 256, 232, 246, 246, 225, 248, 242, 246, 237, 247, 238, 264, 233, 223, 246, 254, 232]
