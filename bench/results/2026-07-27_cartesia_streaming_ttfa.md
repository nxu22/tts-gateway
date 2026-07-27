# Cartesia TTFA — streaming (SSE)

- date: 2026-07-27
- provider: cartesia (`sonic-3`, raw pcm_s16le 24kHz, streaming (SSE))
- runs: 20
- concurrency: 1
- hardware: Windows 11 / AMD64 / 16 cores
- text: 'Thanks for calling. I can book that appointment for you right now.' (66 chars)
- mean audio produced: 3.22s

TTFA is measured from before the request is issued to the arrival of the
first AudioChunk at the caller.

| metric | ms |
|---|---|
| min | 159 |
| p50 | 226 |
| p95 | 311 |
| max | 326 |

Mean audio-seconds per TTFA-second: 14.5

## Why this number exists

A buffered implementation satisfies every contract assertion: the events
arrive in the right order, the first chunk carries real audio, sequence
numbers are contiguous. Nothing in the test suite can tell it apart from a
genuinely streaming one. Only TTFA can.

Both modes are measured by the same instrumentation, unchanged, so the two
numbers are comparable.

Raw samples (ms): [218, 186, 162, 266, 213, 200, 237, 220, 240, 326, 233, 222, 242, 159, 283, 224, 310, 199, 259, 228]
