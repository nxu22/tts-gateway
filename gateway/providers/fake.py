"""`FakeProvider` — synthetic sine-wave PCM with injectable delays and failure points.

It lives in `providers/` rather than `tests/` for two reasons: it doubles as the
reference implementation someone reads to understand the contract, and it can be
routed to directly via ``TTS_PROVIDER_POOL=fake``, so the fault-injection work in
step 8 does not need a second fake.

The cost of that reachability: one day it gets misconfigured into production and
customers hear a sine wave. **The router must reject it at startup whenever
``APP_ENV != "dev"``.** See ROADMAP step 8.

It also exists to prove the contract tests have teeth. The ``cheat_*`` parameters
fake the behaviours where every assertion passes but the user experience is bad;
the contract tests must catch each one.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator

from gateway.interface import (
    MAX_TEXT_CHARS,
    SAMPLE_RATE,
    AudioChunk,
    HealthStatus,
    InvalidRequest,
    StreamEnded,
    StreamInterrupted,
    StreamStarted,
    TTSError,
    TTSEvent,
    TTSProvider,
    VoiceSpec,
)

_AMPLITUDE = 0.3
_TONE_HZ = 220.0


def _sine_pcm(start_sample: int, count: int) -> bytes:
    """Phase-continuous sine wave as 16-bit signed LE mono PCM.

    Phase carries across chunks so concatenated output has no clicks — the eval layer
    relies on this being a clean known signal.
    """
    out = bytearray(count * 2)
    for i in range(count):
        t = (start_sample + i) / SAMPLE_RATE
        value = int(_AMPLITUDE * 32767 * math.sin(2 * math.pi * _TONE_HZ * t))
        out[i * 2 : i * 2 + 2] = value.to_bytes(2, "little", signed=True)
    return bytes(out)


class FakeProvider(TTSProvider):
    """A deterministic fake provider. No network, no API credits.

    Args:
        audio_ms: Total milliseconds of audio to produce.
        chunk_ms: Milliseconds per chunk. The contract does not care; this just picks
            a concrete value.
        ttfa_ms: Artificial delay between `StreamStarted` and the first chunk.
        chunk_delay_ms: Artificial delay between subsequent chunks.
        fail_before_first_chunk: Raise this after `StreamStarted` but before any audio.
            The injection point for failover state 1 (silent switch).
        fail_on_start: Raise before even `StreamStarted` is emitted.
        fail_at_chunk: Raise `StreamInterrupted` when about to emit chunk N. Only N > 0
            counts as mid-stream. The injection point for failover state 2 (no switch,
            pad with silence).
        health: What `check_health()` returns.
        cheat_first_chunk: Fake a TTFA land-grab; the contract tests must catch it.
            ``"empty"`` emits no bytes, ``"silent"`` emits all zeros, ``"short"`` emits
            less than `MIN_FIRST_CHUNK_MS` of audio.
        cheat_end_on_error: Also emit `StreamEnded` on the error path. The contract
            tests must catch this — with it the router can no longer tell "finished"
            from "cut off".
    """

    name = "fake"

    def __init__(
        self,
        *,
        audio_ms: int = 480,
        chunk_ms: int = 40,
        ttfa_ms: float = 0.0,
        chunk_delay_ms: float = 0.0,
        fail_before_first_chunk: TTSError | None = None,
        fail_on_start: TTSError | None = None,
        fail_at_chunk: int | None = None,
        health: HealthStatus = HealthStatus.HEALTHY,
        cheat_first_chunk: str | None = None,
        cheat_end_on_error: bool = False,
    ) -> None:
        self.audio_ms = audio_ms
        self.chunk_ms = chunk_ms
        self.ttfa_ms = ttfa_ms
        self.chunk_delay_ms = chunk_delay_ms
        self.fail_before_first_chunk = fail_before_first_chunk
        self.fail_on_start = fail_on_start
        self.fail_at_chunk = fail_at_chunk
        self.health = health
        self.cheat_first_chunk = cheat_first_chunk
        self.cheat_end_on_error = cheat_end_on_error

        # For cancellation assertions. Counters rather than a bool, because the
        # contract requires one instance to serve concurrent streams.
        self.open_streams = 0
        self.closed_streams = 0

    async def synthesize(self, text: str, voice: VoiceSpec) -> AsyncIterator[TTSEvent]:
        if not text.strip():
            raise InvalidRequest("empty text", provider=self.name)
        if len(text) > MAX_TEXT_CHARS:
            raise InvalidRequest(
                f"text too long: {len(text)} > {MAX_TEXT_CHARS}", provider=self.name
            )
        if self.fail_on_start is not None:
            raise self.fail_on_start

        self.open_streams += 1
        try:
            yield StreamStarted(provider=self.name, voice_resolved=f"fake::{voice.name}")

            if self.ttfa_ms:
                await asyncio.sleep(self.ttfa_ms / 1000)
            if self.fail_before_first_chunk is not None:
                if self.cheat_end_on_error:
                    yield StreamEnded(total_samples=0)
                raise self.fail_before_first_chunk

            chunk_samples = int(SAMPLE_RATE * self.chunk_ms / 1000)
            total_samples = int(SAMPLE_RATE * self.audio_ms / 1000)
            emitted = 0
            seq = 0

            while emitted < total_samples:
                if self.fail_at_chunk is not None and seq == self.fail_at_chunk:
                    if self.cheat_end_on_error:
                        yield StreamEnded(total_samples=emitted)
                    raise StreamInterrupted(f"injected failure at chunk {seq}", provider=self.name)
                if seq > 0 and self.chunk_delay_ms:
                    await asyncio.sleep(self.chunk_delay_ms / 1000)

                count = min(chunk_samples, total_samples - emitted)
                pcm = _sine_pcm(emitted, count)
                if seq == 0 and self.cheat_first_chunk is not None:
                    pcm = self._cheated_first_chunk(count)

                yield AudioChunk(seq=seq, pcm=pcm)
                emitted += len(pcm) // 2
                seq += 1

            yield StreamEnded(total_samples=emitted)
        finally:
            self.open_streams -= 1
            self.closed_streams += 1

    def _cheated_first_chunk(self, count: int) -> bytes:
        """Fabricate a first chunk that jumps the gun on TTFA."""
        if self.cheat_first_chunk == "empty":
            return b""
        if self.cheat_first_chunk == "silent":
            return bytes(count * 2)
        if self.cheat_first_chunk == "short":
            return _sine_pcm(0, max(1, int(SAMPLE_RATE * 0.001)))  # 1ms
        raise ValueError(f"unknown cheat mode: {self.cheat_first_chunk}")

    async def check_health(self) -> HealthStatus:
        return self.health
