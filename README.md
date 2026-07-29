# tts-gateway

A multi-engine text-to-speech gateway. Several vendors sit behind one streaming
interface, so callers get identical audio and identical failure semantics regardless of
which engine served the request.

Two providers are implemented, over deliberately different transports — Cartesia on HTTP
server-sent events, ElevenLabs on a WebSocket — because the abstraction is only worth
anything if it survives that.

## Measured results

| implementation | TTFA p50 | TTFA p95 |
|---|---|---|
| Cartesia `sonic-3`, buffered HTTP | 1117 ms | 1589 ms |
| Cartesia `sonic-3`, streaming SSE | **226 ms** | **311 ms** |
| ElevenLabs `flash_v2_5`, socket per utterance | 569 ms | 603 ms |
| ElevenLabs `flash_v2_5`, persistent multiplexed socket | **244 ms** | **293 ms** |

Time-to-first-audio, measured from before the request is issued to the first PCM chunk
reaching the caller. 20 runs each, concurrency 1, Windows 11 / AMD64 / 16 cores. Full
reports with raw samples: [bench/results/](bench/results/).

**Every optimization kept its slow path.** `buffered=True` on Cartesia,
`persistent=False` on ElevenLabs — the slower implementation stayed behind a flag instead
of being deleted. So the rows above are not archived history; each pair is a controlled
experiment that re-runs on today's network against today's vendor deployment. An
archived benchmark expires silently, and nothing in a repository tells you when it did.

Adding the second provider required **no change to `interface.py` or `router.py`**, and
it passed the existing contract suite on the first run.

## Architecture

```
caller
  │
  ▼
gateway/router.py ········· provider selection, failover state machine, circuit breaker
  │                         contains no vendor names; branches only on error type
  ▼
gateway/interface.py ······ TTSProvider ABC + StreamStarted / AudioChunk / StreamEnded
  │                         one wire format leaves here: 24 kHz, 16-bit LE, mono PCM
  ▼
gateway/providers/ ········ one file per vendor, none importing another
  ├─ cartesia.py            HTTP, server-sent events
  ├─ elevenlabs.py          WebSocket, concurrent contexts multiplexed on one socket
  ├─ fake.py                sine wave with injectable delays and failure points
  ├─ _framing.py            vendor fragments → contract-shaped chunks
  └─ _resample.py           streaming resample to 24 kHz, one instance per stream
```

Every provider yields the same ordered stream:

```
StreamStarted → AudioChunk(seq=0) → AudioChunk(seq=1) → … → StreamEnded
```

Adding a vendor costs one new file plus one line in `providers/REGISTRY`.

## Design decisions

Each of these is a trade-off, not a feature. What was chosen, what was given up, and why.

### Mid-stream failure does not fail over

| when | behaviour |
|---|---|
| failure **before** the first audio chunk | silently retry on a backup provider; the caller never learns the first one existed |
| the stream dies **mid-flight** | **no switch** — pad 200 ms of silence, raise `StreamInterrupted` |
| repeated failures | eject from the pool, probe half-open for recovery |

The middle row costs availability on purpose. Switching vendors mid-utterance changes the
speaker mid-sentence; a truncated turn can be retried, a voice swap cannot be un-heard.

Two mechanisms make it work. The **router**, not the provider, tracks whether audio has
reached the caller — otherwise every vendor reimplements the same state machine and the
third one gets it wrong. And `StreamEnded` is the only marker of a normal end, so its
absence is what identifies a truncation.

### `StreamStarted` is withheld until audio is certain

It is emitted immediately before the first chunk, not when the socket opens. Otherwise a
silent switch would show the caller two `StreamStarted` events and the router's own
output would violate the contract it enforces. Useful side effect: the provider named in
`StreamStarted` is always the one that actually delivered.

### Callers name a logical voice, never a vendor voice id

`VoiceSpec(name="receptionist-en-ca-female")`; each provider maps that to its own id
internally. Passing a vendor id through would look correct in every single-provider test
and then kill failover in production — the backup cannot resolve an id belonging to
someone else, so the failover path returns silence. The bug is invisible until the exact
moment it matters.

### Errors are classified by what a retry would accomplish

`ProviderUnavailable` and `RateLimited` are retryable; another vendor may well succeed.
`InvalidRequest` is not — the next provider rejects the same text, so retrying only burns
latency. `StreamInterrupted` is not, for the reason above. The router branches on this
taxonomy and on `retryable`, never on which vendor raised it.

### `VoiceSpec` has no `speed` field

It had one. ElevenLabs supports speed, Cartesia does not expose it here, so `speed=1.2`
would succeed on one provider and raise `InvalidRequest` on another — and `InvalidRequest`
does not fail over. The same class of bug as leaking a vendor voice id. Nothing needed
speed, so it was removed rather than papered over. Supporting it properly means capability
declarations so the router can filter candidates, which is an interface change worth
making deliberately.

### The first chunk must carry real audio

A provider could report a better TTFA by emitting an empty or 1 ms chunk to stop the
clock. The contract requires ≥20 ms of non-silent audio in the first chunk, which means
early fragments are sometimes held back — a real cost, so it was measured: **0.0 ms on
Cartesia**, whose first fragment already carries 133 ms. That is a fact about one vendor's
framing, and it is re-measured per provider.

### One resampler per stream, never one per chunk

A resampler carries filter state across its input. Constructing a fresh one per chunk
restarts the filter from silence at every boundary, leaving a discontinuity at each seam.
Nothing in the contract suite can see this — sample rate, framing and sequence numbers are
all still correct — so the waveform is measured directly.

## Testing

91 tests. 62 run with no network and no API credits; 29 are marked `live` and skipped by
default.

```
tests/test_contract.py   18 test functions, parametrized across providers
tests/test_router.py     22 failover tests, all by fault injection
tests/test_framing.py     9 chunk-assembly edge cases
tests/test_resample.py    7 waveform tests
tests/test_ttfa_baseline.py  5 live measurements, archived to bench/results/
```

### One contract, every provider

`tests/test_contract.py` is parametrized: the same assertions run against every provider.
"Adding a provider" is defined as "making that file pass". Development and CI use
`FakeProvider`, so no credits are spent; the real providers run under `-m live`, twelve
assertions each.

### Tests that are checked for being able to fail

A passing test proves nothing until it has been watched failing for the intended reason.
Three mechanisms enforce that here.

**Cheat modes.** `FakeProvider` can deliberately misbehave — emit an empty first chunk, an
all-silent one, a 1 ms one, or send `StreamEnded` on an error path. The suite is run
against each, and the result is a clean diagonal:

| provider behaviour | `first_chunk_real` | `no_ended_on_error` |
|---|---|---|
| honest | pass | pass |
| silent first chunk | **caught** | pass |
| 1 ms first chunk | **caught** | pass |
| empty first chunk | **caught** | pass |
| `StreamEnded` on error | pass | **caught** |

Each cheat is caught by the assertion aimed at it and nothing else trips, so the
assertions are neither vacuous nor over-tight.

**Waveform measurement** for the defect assertions cannot see. Largest jump between
consecutive samples, where a continuous tone is bounded by its own slope:

| | max sample-to-sample step |
|---|---|
| theoretical bound for the tone | 600 |
| one resampler per stream | **599** |
| one resampler per chunk | **3310** |

**Mutation testing** on the router: it is deliberately broken four ways — state 2 switches
instead of raising, the silence pad removed, the dev-only guard removed, `StreamStarted`
forwarded eagerly — and each mutation must be caught by the test that claims to cover it.

### Two tests that were green and proved nothing

Both were caught by the checks above, and both are documented in place.

The seam test originally used a 220 Hz tone in 100 ms chunks. That is exactly 22 cycles,
so every chunk boundary landed on a zero crossing, where a restarted filter leaves no step
to detect. The broken and correct implementations produced indistinguishable waveforms.
Detuning to 233 Hz put the boundaries mid-waveform and the 5× gap appeared.

The failover test named `test_mid_stream_failure_does_not_switch` injected
`StreamInterrupted`, which is non-retryable — so the error type alone prevented the switch,
and the router's entire mid-stream branch could be deleted without the test noticing. It
was checking the exception taxonomy, not the bookkeeping it was named for. Injecting a
*retryable* error mid-stream is what actually pins the rule: the boundary is whether audio
has reached the caller, not which exception was raised.

## Status

Implemented and tested:

- the provider contract and its test suite
- Cartesia (HTTP SSE, streaming and buffered)
- ElevenLabs (WebSocket, persistent multiplexed session and socket-per-utterance)
- failover state machine, circuit breaker, startup guard against dev-only providers
- streaming resampler and chunk framing
- TTFA measurement, archived per run

Not implemented yet:

- **FastAPI endpoints** — `gateway/main.py` is a stub. The router is usable as a library.
- **Telemetry** — latency is measured by two `perf_counter` calls in the test harness.
  `gateway/telemetry.py` (Langfuse export) is a stub.
- **Evaluation harness** — `eval/` is stubs. No WER, UTMOS or NISQA numbers exist yet, so
  this repository contains no claims about audio quality of any kind.
- **Text normalization** — `normalize/` is stubs.
- **Self-hosted provider** — not started. This is also what will first exercise the
  resampler against real data, since both current vendors emit 24 kHz PCM natively.

Known gap: the logical voice `receptionist-en-ca-female` maps to an American-accented
ElevenLabs voice, chosen from published metadata rather than by listening. Neither the
author nor any tooling in this repository can judge how a voice sounds; that is what the
evaluation harness is for, and the mapping should be revisited once it can score
candidates.

## Getting started

```bash
uv sync
uv run pytest                 # 62 tests, no network, no credits
uv run ruff check . && uv run ruff format .
```

Live tests hit real APIs and consume credits:

```bash
cp .env.example .env          # add CARTESIA_API_KEY / ELEVENLABS_API_KEY
uv run pytest -m live
uv run pytest -m live tests/test_ttfa_baseline.py -s   # regenerate bench/results/
```

`fake` is routable through `TTS_PROVIDER_POOL` for development and is refused at startup
whenever `APP_ENV != "dev"` — a synthetic voice reaching production is a failure that
reports success.

Working notes ([CLAUDE.md](./CLAUDE.md), [ROADMAP.md](./ROADMAP.md)) are in Chinese; all
code and documentation are in English.
