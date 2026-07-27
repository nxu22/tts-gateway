# tts-gateway

A multi-engine text-to-speech gateway. ElevenLabs, Cartesia, Rime, and self-hosted
models sit behind one streaming interface, with health checks, failover, and a
reproducible voice-quality evaluation harness.

The point of this project is not provider coverage. It is that the abstraction holds:
adding a vendor costs one new file and one registration line, and nothing above
`gateway/providers/` learns that vendor exists.

## The contract

Every provider implements one ABC and yields one ordered event stream:

```
StreamStarted → AudioChunk(seq=0) → AudioChunk(seq=1) → … → StreamEnded
```

Exactly one format leaves the gateway: **24000 Hz / 16-bit signed LE / mono PCM**.
Vendors that speak MP3, Opus, mu-law, or 44.1kHz decode and resample inside their own
provider file. `router.py` contains no vendor names and no format branches.

## Failover has three states

| When | Behaviour |
|---|---|
| Failure **before** the first audio chunk | Silently retry on a backup provider; the caller never notices |
| The stream dies **mid-flight** | **No switch.** Pad 200ms of silence, alert, raise a recognizable error |
| Health checks fail repeatedly | Eject from the pool, probe half-open for recovery |

The middle row is a deliberate trade-off, not an oversight. Swapping vendors
mid-stream changes the voice mid-sentence, which sounds worse than an honest
truncation.

Two details make this work. The **router** — not the provider — tracks whether audio
has already reached the caller, so the same state machine is not reimplemented per
vendor. And `StreamEnded` is the only marker of a normal end of stream, so its absence
is what identifies a truncation.

## Contract tests, and tests for the tests

`tests/test_contract.py` is parametrized: every provider runs the same assertions.
"Adding a provider" is defined as "making that file pass." Day-to-day runs use
`FakeProvider` (synthetic sine-wave PCM with injectable delays and failure points), so
no API credits are burned; real-provider cases are marked `live` and skipped by
default.

Two of those assertions guard failure modes that are invisible to a naive test suite:

- **The first chunk must carry at least 20ms of non-silent audio.** Without this, a
  provider can emit an empty or all-zero chunk to win on time-to-first-audio. Every
  assertion passes, the latency numbers improve, and the user still hears a gap.
- **`StreamEnded` must never appear on an error path.** Emit it on failure and the
  router can no longer distinguish "finished" from "cut off," which silently disables
  failover state 2.

An assertion that is always true looks exactly like one that works, so `FakeProvider`
carries deliberate cheat modes and the suite is checked against them:

| Provider behaviour | `first_chunk_real` | `no_ended_on_error` |
|---|---|---|
| honest | pass | pass |
| silent first chunk | **caught** | pass |
| 1ms first chunk | **caught** | pass |
| empty first chunk | **caught** | pass |
| `StreamEnded` on error | pass | **caught** |

A clean diagonal: each cheat is caught by the assertion aimed at it, and nothing else
trips. The assertions are neither vacuous nor over-tight.

## What streaming is worth

The contract suite cannot tell a streaming implementation from a buffered one. A
provider that fetches the whole utterance and then slices it emits the same events in
the same order, with real audio in the first chunk and contiguous sequence numbers —
all 12 live assertions pass either way. Only latency separates them.

So both were built and measured with identical instrumentation, one provider, same
text, same voice, same machine:

| Cartesia `sonic-3` | TTFA p50 | TTFA p95 |
|---|---|---|
| buffered (`/tts/bytes`) | 1117 ms | 1589 ms |
| streaming (`/tts/sse`) | **226 ms** | **311 ms** |

Roughly a 5× reduction, measured at concurrency 1 over 20 runs each. Full reports,
including hardware and raw samples, are in [bench/results/](bench/results/).

The point is not that the gateway streams. It is that the cost of not streaming is a
known number rather than an assumption.

The same applies to the contract's own overhead. Requiring 20ms of audio in the first
chunk means early fragments may have to be held back, which is a delay charged to
TTFA — so it was measured too: **0.0 ms for Cartesia**, whose first SSE fragment
already carries 133ms of audio and clears the floor unaided. That is a fact about one
vendor's framing, not a general result, and it gets re-measured per provider.

## Measurement rules

- **TTFA** is measured from when the caller issued the request, not from when the
  provider returned headers.
- **RTF** = audio duration / synthesis wall-clock time.
- Every latency figure is archived with its concurrency level and hardware label. Bare
  single-request numbers do not go into reports.

## Layout

```
gateway/
  interface.py      # TTSProvider ABC + AudioChunk / VoiceSpec / StreamStarted
  providers/        # one file per vendor, none importing another
  router.py         # selection + failover state machine, no vendor logic
  telemetry.py      # TTFA / RTF instrumentation → Langfuse
  main.py           # FastAPI: POST /synthesize, WS /stream
normalize/          # text normalization + SSML (numbers, currency, dates, French names)
eval/               # WER, UTMOS/NISQA, latency percentiles, report generation
bench/results/      # archived benchmark runs, filenames carry date and hardware
tests/test_contract.py
```

## Running it

```bash
uv sync
uv run pytest                      # contract tests against FakeProvider
uv run pytest -m live              # hits real APIs, burns credits, run by hand
uv run ruff check . && uv run ruff format .
uv run uvicorn gateway.main:app --reload
```

Copy `.env.example` to `.env` for API keys. `fake` is routable through
`TTS_PROVIDER_POOL` for development and is rejected at startup outside `APP_ENV=dev`.

## Status

The contract is frozen and the first provider is in progress. See
[ROADMAP.md](./ROADMAP.md) (written in Chinese, as are the working notes in
[CLAUDE.md](./CLAUDE.md)).
