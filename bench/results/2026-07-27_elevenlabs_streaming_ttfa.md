# elevenlabs TTFA — streaming (WebSocket)

- date: 2026-07-27
- provider: elevenlabs (raw pcm_s16le 24kHz, streaming (WebSocket))
- runs: 20
- concurrency: 1
- hardware: Windows 11 / AMD64 / 16 cores
- text: 'Thanks for calling. I can book that appointment for you right now.' (66 chars)
- mean audio produced: 3.07s

TTFA is measured from before the request is issued to the arrival of the
first AudioChunk at the caller.

| metric | ms |
|---|---|
| min | 566 |
| p50 | 669 |
| p95 | 769 |
| max | 787 |

Mean audio-seconds per TTFA-second: 4.7

## Why this number exists

A buffered implementation satisfies every contract assertion: the events
arrive in the right order, the first chunk carries real audio, sequence
numbers are contiguous. Nothing in the test suite can tell it apart from a
genuinely streaming one. Only TTFA can.

Both modes are measured by the same instrumentation, unchanged, so the two
numbers are comparable.

Raw samples (ms): [701, 759, 710, 585, 581, 675, 712, 626, 589, 566, 727, 669, 624, 787, 670, 674, 646, 768, 569, 626]
