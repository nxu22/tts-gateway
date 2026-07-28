"""Provider selection and the failover state machine.

**No vendor name appears in this file.** No ``if provider == "elevenlabs"``. Branching is
allowed only on the error types and `TTSError.retryable` defined in `gateway.interface`.

Failover has three states, and the second one is the interesting one:

1. Failure **before** the first audio chunk: silently retry on a backup provider; the
   caller never notices.
2. The stream dies **mid-flight**: **no switch.** Pad 200ms of silence, then raise
   `StreamInterrupted`.
3. Health checks fail repeatedly: eject from the pool, probe half-open for recovery.

State 2 is deliberate and counter-intuitive. Swapping vendors mid-utterance changes the
voice mid-sentence, which sounds worse than an honest truncation — a caller can retry a
truncated turn, but cannot un-hear a speaker changing identity halfway through. Do not
"optimize" it into a switch.

## The router owns the "has a chunk been emitted" bookkeeping

Providers only raise typed errors. Whether audio has already reached the caller is
something only this file knows. Pushing it into providers means every vendor
reimplements the same state machine, and the third one gets it wrong.

The boundary is the first `AudioChunk`, **not** `StreamStarted` — which is why
`StreamStarted` is withheld until audio is certain. See `synthesize`.

## Circuit breaking: passive first, probes only for half-open recovery

Consecutive failures on real traffic eject a provider. Live requests are a fresher and
more accurate signal than synthetic probes, and they cost nothing extra. `check_health()`
is called only to decide whether an already-ejected provider may return, where its
frequency is low and its cost is acceptable.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from gateway.interface import (
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    AudioChunk,
    HealthStatus,
    ProviderUnavailable,
    StreamEnded,
    StreamInterrupted,
    StreamStarted,
    TTSError,
    TTSEvent,
    TTSProvider,
    VoiceSpec,
)
from gateway.providers import DEV_ONLY, REGISTRY

#: Played out when a stream dies mid-flight, so the audio ends on silence rather than
#: cutting off mid-syllable.
SILENCE_PAD_MS = 200

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_PROBE_INTERVAL_S = 30.0


class ConfigurationError(Exception):
    """The pool cannot be built as configured. Raised at startup, never at request time."""


@dataclass
class _Breaker:
    """Per-provider failure bookkeeping. Passive by default."""

    threshold: int
    probe_interval_s: float
    consecutive_failures: int = 0
    ejected_at: float | None = None
    #: True while a provider is being given one trial request after ejection.
    half_open: bool = False

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.ejected_at = None
        self.half_open = False

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.half_open or self.consecutive_failures >= self.threshold:
            # A failed trial re-ejects immediately; no second chance per probe.
            self.ejected_at = time.monotonic()
            self.half_open = False

    @property
    def ejected(self) -> bool:
        return self.ejected_at is not None

    def probe_due(self, now: float) -> bool:
        return self.ejected_at is not None and now - self.ejected_at >= self.probe_interval_s


@dataclass
class Router:
    """Selects a provider per request and applies the failover state machine.

    Args:
        providers: Candidates in priority order.
        failure_threshold: Consecutive failures before a provider is ejected.
        probe_interval_s: How long an ejected provider stays out before being probed.
    """

    providers: Sequence[TTSProvider]
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    probe_interval_s: float = DEFAULT_PROBE_INTERVAL_S
    _breakers: dict[str, _Breaker] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        if not self.providers:
            raise ConfigurationError("router needs at least one provider")
        self._breakers = {
            provider.name: _Breaker(self.failure_threshold, self.probe_interval_s)
            for provider in self.providers
        }

    @classmethod
    def from_env(cls, **kwargs: object) -> Router:
        """Build the pool from ``TTS_PROVIDER_POOL``, refusing unsafe configurations."""
        pool = [name.strip() for name in os.environ.get("TTS_PROVIDER_POOL", "").split(",")]
        pool = [name for name in pool if name]
        return cls.from_names(pool, app_env=os.environ.get("APP_ENV", "dev"), **kwargs)

    @classmethod
    def from_names(cls, names: Sequence[str], *, app_env: str = "dev", **kwargs: object) -> Router:
        """Resolve provider names against the registry, with the dev-only guard.

        The guard exists because `FakeProvider` is routable by configuration. Sooner or
        later a deployment inherits ``TTS_PROVIDER_POOL=fake``, and the failure mode is
        the worst kind: customers hear a sine wave while every health check reports
        success. Failing to start is loud; serving a test tone is silent.
        """
        if not names:
            raise ConfigurationError("TTS_PROVIDER_POOL is empty")

        unknown = [name for name in names if name not in REGISTRY]
        if unknown:
            raise ConfigurationError(f"unknown providers {unknown}; known: {sorted(REGISTRY)}")

        if app_env != "dev":
            forbidden = [name for name in names if name in DEV_ONLY]
            if forbidden:
                raise ConfigurationError(
                    f"{forbidden} may only be used when APP_ENV=dev, not {app_env!r}. "
                    "These providers emit synthetic audio and would serve it to callers."
                )

        return cls([REGISTRY[name]() for name in names], **kwargs)  # type: ignore[arg-type]

    async def synthesize(self, text: str, voice: VoiceSpec) -> AsyncIterator[TTSEvent]:
        """Run the request against the pool, applying the three failover states.

        `StreamStarted` is withheld until the first `AudioChunk` is in hand, then emitted
        immediately before it. That keeps the router's own output contract-shaped (one
        StreamStarted, before any audio) while a silent switch is still possible: a
        provider that dies before producing audio is abandoned without the caller ever
        having been told it existed. It also means the `StreamStarted.provider` the
        caller sees is always the provider that actually delivered.
        """
        last_error: TTSError | None = None

        for provider in await self._candidates():
            emitted = False
            withheld_start: StreamStarted | None = None
            last_seq = -1

            try:
                async for event in provider.synthesize(text, voice):
                    if isinstance(event, StreamStarted):
                        withheld_start = event
                        continue

                    if isinstance(event, AudioChunk):
                        if not emitted:
                            emitted = True
                            if withheld_start is not None:
                                yield withheld_start
                        last_seq = event.seq
                        yield event
                        continue

                    if isinstance(event, StreamEnded):
                        self._breakers[provider.name].record_success()
                        yield event
                        return

                # The provider's stream ended without StreamEnded: a truncation that did
                # not bother to raise. Treated exactly like one that did.
                raise StreamInterrupted("stream ended without StreamEnded", provider=provider.name)

            except TTSError as exc:
                if emitted:
                    # State 2. Audio is already playing at the caller; switching now
                    # would change the voice mid-sentence.
                    self._breakers[provider.name].record_failure()
                    yield self._silence(last_seq + 1)
                    raise StreamInterrupted(
                        f"stream from {provider.name} died after {last_seq + 1} chunks",
                        provider=provider.name,
                    ) from exc

                if not exc.retryable:
                    # Bad request, unknown voice: the next provider answers identically.
                    raise

                # State 1. Nothing reached the caller, so this never happened.
                self._breakers[provider.name].record_failure()
                last_error = exc

        raise last_error or ProviderUnavailable("no providers available in the pool")

    def _silence(self, seq: int) -> AudioChunk:
        samples = int(SAMPLE_RATE * SILENCE_PAD_MS / 1000)
        return AudioChunk(seq=seq, pcm=bytes(samples * SAMPLE_WIDTH_BYTES))

    async def _candidates(self) -> list[TTSProvider]:
        """Providers eligible for this request, in priority order.

        Ejected providers are skipped until their probe is due, then given exactly one
        trial request if a health probe does not actively rule them out.
        """
        now = time.monotonic()
        eligible: list[TTSProvider] = []

        for provider in self.providers:
            breaker = self._breakers[provider.name]
            if not breaker.ejected:
                eligible.append(provider)
                continue
            if not breaker.probe_due(now):
                continue

            status = await self._probe(provider)
            if status is HealthStatus.UNHEALTHY:
                # Still down. Restart the clock rather than probing on every request.
                breaker.ejected_at = now
                continue
            # HEALTHY or UNKNOWN: let one real request decide. UNKNOWN is not a failure —
            # it means the provider has no cheap probe, so passive signals are all there is.
            breaker.half_open = True
            breaker.ejected_at = None
            eligible.append(provider)

        return eligible

    async def _probe(self, provider: TTSProvider) -> HealthStatus:
        try:
            return await provider.check_health()
        except Exception:
            # A probe that raises is not a healthy provider, but it is also not a
            # request failure — it must not crash selection.
            return HealthStatus.UNHEALTHY

    def status(self) -> dict[str, str]:
        """Pool state, for the health endpoint. Diagnostics only."""
        return {
            provider.name: ("ejected" if self._breakers[provider.name].ejected else "available")
            for provider in self.providers
        }
