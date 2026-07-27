"""Provider selection and the failover state machine.

**No vendor name may appear in this file.** No ``if provider == "elevenlabs"``.
Branching is allowed only on the error types and `TTSError.retryable` defined in
`gateway.interface`.

Failover has three states (semantics in CLAUDE.md; do not "optimize away" the second):

1. Failure **before** the first audio chunk: silently retry on a backup provider; the
   caller never notices.
2. The stream dies **mid-flight**: **no switch.** Pad 200ms of silence, emit an alert,
   raise a recognizable error.
3. Health checks fail repeatedly: eject from the pool, probe half-open for recovery.

## The router owns the "has a chunk been emitted" bookkeeping

Providers only raise typed errors. Whether audio has already reached the caller is
something only the router knows. Pushing this into providers means every vendor
reimplements the same state machine, and the third one gets it wrong.

The boundary is the first `AudioChunk`, **not** `StreamStarted`. A failure after
`StreamStarted` but before any audio is still state 1 and can be switched silently.

`StreamEnded` is the only marker of a normal end of stream. Its absence is state 2.

## Circuit breaking: passive first, probes only for half-open recovery

- **Passive**: track the failure rate of the last N *real* requests. Live traffic
  arrives constantly and is a fresher, more accurate signal than a synthetic probe.
  Eject once consecutive failures cross the threshold.
- **Active**: only for providers already ejected — probe occasionally to see whether
  they can rejoin the pool. Low frequency, acceptable cost.
- **Never synthesize on the health path.** It is both expensive and slow (hundreds of
  milliseconds, where a health verdict should take single-digit ones). Real synthesis
  belongs in an explicit smoke test, run by hand.
- `HealthStatus.UNKNOWN` is a legitimate answer and does not mean unhealthy — it just
  means passive signals are all the router has to go on.

## Startup validation

Whenever ``APP_ENV != "dev"``, finding ``fake`` in the pool must **abort startup with
an error**. `FakeProvider` is routable through `TTS_PROVIDER_POOL`, so sooner or later
it gets misconfigured into production and customers hear a sine wave.
"""
