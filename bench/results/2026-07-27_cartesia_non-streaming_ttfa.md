# Cartesia TTFA baseline — non-streaming (buffered)

- date: 2026-07-27
- provider: cartesia (`sonic-3`, HTTP `/tts/bytes`, raw pcm_s16le 24kHz)
- implementation: **buffered** — the full utterance is fetched, then sliced
- runs: 20
- concurrency: 1
- hardware: Windows 11 / AMD64 / 16 cores
- text: 'Thanks for calling. I can book that appointment for you right now.' (66 chars)
- mean audio produced: 3.48s

TTFA is measured from before the request is issued to the arrival of the
first AudioChunk at the caller.

| metric | ms |
|---|---|
| min | 685 |
| p50 | 1079 |
| p95 | 1503 |
| max | 1557 |

Mean audio-seconds per TTFA-second: 3.1

## Why this number exists

A buffered implementation satisfies every contract assertion: the events
arrive in the right order, the first chunk carries real audio, sequence
numbers are contiguous. Nothing in the test suite can tell it apart from a
genuinely streaming one. Only TTFA can.

The streaming implementation will be measured with this same code,
unchanged, so the two numbers are comparable.

Raw samples (ms): [1290, 1557, 1027, 994, 890, 970, 985, 1300, 1283, 1115, 1490, 906, 1042, 1418, 1231, 685, 1282, 942, 1500, 933]
