# Cost of the MIN_FIRST_CHUNK_MS rule — Cartesia streaming

- date: 2026-07-27
- runs: 20
- concurrency: 1
- hardware: Windows 11 / AMD64 / 16 cores

Measured from the arrival of the first SSE audio fragment to the moment the
first AudioChunk is handed to the caller. The gap is time spent waiting for
enough audio to clear the 20ms floor.

| metric | ms |
|---|---|
| min | 0.0 |
| p50 | 0.0 |
| p95 | 0.0 |
| max | 0.0 |

SSE fragments consumed before the first chunk: min 1, max 1
First chunk actually delivered: 133 to 133ms of audio, against a 20ms floor.

So for this provider the rule is free: Cartesia's first SSE fragment already
clears the floor on its own, and nothing is ever held back. That is a fact
about Cartesia's framing, not a general result — a vendor that emits smaller
fragments would pay a real delay here, so this needs re-measuring per
provider.

The alternative — emitting the first fragment immediately regardless of
size — would report a lower TTFA while delivering a chunk too short to play
as continuous audio. That is exactly the land-grab the contract test exists
to catch, so this delay is the honest price of the rule.

Raw samples (ms): [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
