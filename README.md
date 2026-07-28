# tts-gateway

A multi-engine text-to-speech gateway. ElevenLabs, Cartesia, Rime, and self-hosted
models sit behind one streaming interface, with health checks, failover, and a
reproducible voice-quality evaluation harness.

The point of this project is not provider coverage. It is that the abstraction holds:
adding a vendor costs one new file and one registration line, and nothing above
`gateway/providers/` learns that vendor exists.

## The contract

The claim is tested rather than asserted: the second provider was added over a
different transport — a bidirectional WebSocket, against the first provider's HTTP
server-sent events — and passed all twelve live contract assertions with **no change to
`interface.py` or `router.py`**. The three-message send ritual, base64-in-JSON framing,
and per-utterance handshake are absorbed inside
[gateway/providers/elevenlabs.py](gateway/providers/elevenlabs.py).

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

| implementation | TTFA p50 | TTFA p95 |
|---|---|---|
| Cartesia `sonic-3`, buffered HTTP | 1117 ms | 1589 ms |
| Cartesia `sonic-3`, streaming SSE | **226 ms** | **311 ms** |
| ElevenLabs `flash_v2_5`, socket per utterance | 569 ms | 603 ms |
| ElevenLabs `flash_v2_5`, persistent multiplexed socket | **244 ms** | **293 ms** |

Roughly a 5× reduction from buffering to streaming, measured at concurrency 1 over 20
runs each. Full reports, including hardware and raw samples, are in
[bench/results/](bench/results/).

### Where the second improvement came from

ElevenLabs initially measured 2.5× slower than Cartesia, and the interesting part was
the attribution: it was not synthesis speed. Their protocol closes the WebSocket after
each utterance, so every caller paid a fresh handshake — **265–344 ms of it, measured
separately** — while Cartesia reuses a pooled HTTP connection.

Their `/multi-stream-input` endpoint tags messages with a context id, so one socket can
carry concurrent utterances. Moving to it means a background reader owning the socket
and fanning messages into per-context queues, because two callers both awaiting `recv()`
would steal each other's audio. That took the handshake off the hot path entirely:
**569 ms → 244 ms p50**, putting the two vendors within noise of each other.

This is a measurement of specific configurations, not a verdict on either vendor.

### Every optimization keeps its slow path

`buffered=True` on Cartesia, `persistent=False` on ElevenLabs — each improvement left
the slower implementation in place behind a flag rather than deleting it. So none of the
numbers above are archived history; every row is a controlled experiment that can be
re-run on today's network, today's hardware, against today's vendor deployment.

That matters because an archived benchmark expires quietly. A number measured on a
different machine, or before a vendor changed their infrastructure, is not comparable to
a number measured now — and nothing in the repository would tell you it had gone stale.
Keeping both arms runnable turns "this is 2.3× faster" from a claim into a command you
can execute.

The point is not that the gateway streams. It is that the cost of not streaming is a
known number rather than an assumption.

The same applies to the contract's own overhead. Requiring 20ms of audio in the first
chunk means early fragments may have to be held back, which is a delay charged to
TTFA — so it was measured too: **0.0 ms for Cartesia**, whose first SSE fragment
already carries 133ms of audio and clears the floor unaided. That is a fact about one
vendor's framing, not a general result, and it gets re-measured per provider.

## A defect the contract cannot see

Resampling has to happen inside a provider, and it has to use one resampler instance for
the whole stream. Build a fresh one per chunk and each boundary restarts the filter from
silence, leaving a discontinuity at every seam — audible as a click.

Nothing in the contract suite catches this. The sample rate is right, the frames are
whole, the sequence numbers are contiguous; only the waveform is wrong. So the waveform
is measured: the largest jump between consecutive samples, which for a continuous tone
is bounded by its own slope.

| | max sample-to-sample step |
|---|---|
| theoretical bound for the tone | 600 |
| one resampler per stream | **599** |
| one resampler per chunk | **3310** |

The first version of that test was green and proved nothing. It used a 220Hz tone in
100ms chunks — exactly 22 cycles, so every boundary landed on a zero crossing, where a
restarted filter leaves no step to find. Both implementations looked identical. Detuning
to 233Hz put the boundaries mid-waveform and the 5x gap appeared. The episode is written
up in [tests/test_resample.py](tests/test_resample.py), because a passing test has proven
nothing until it has been watched failing for the intended reason.

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
