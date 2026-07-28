"""Cartesia provider.

Two transports, both satisfying the same contract:

- **streaming** (default): server-sent events from ``/tts/sse``; audio is forwarded as
  it arrives.
- **buffered**: ``/tts/bytes`` fetches the whole utterance, then slices it.

The buffered path is kept deliberately. It is not a fallback and nothing depends on
it — it exists so the streaming-vs-buffered TTFA comparison can be re-run at any time
rather than resting on one archived measurement. Both are measured by identical
instrumentation in `tests/test_ttfa_baseline.py`.

That comparison is the only thing that distinguishes them: a buffered implementation
satisfies every assertion in the contract suite. Ordering, first-chunk content, and
sequence numbers all look identical. Only TTFA tells them apart.

Two things this provider does **not** prove, worth stating plainly:

- **The normalization layer is untested by it.** Cartesia emits ``pcm_s16le`` at
  24000 Hz natively, which is exactly the gateway's wire format, so the
  decode/resample path never runs. The first real exercise of that path is ElevenLabs
  returning MP3.
- **It does not prove the transport abstraction holds.** Cartesia is HTTP either way;
  the interface only earns that claim when a WebSocket vendor goes in unchanged.
"""

from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import AsyncIterator

import httpx

from gateway.interface import (
    MAX_TEXT_CHARS,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    AudioChunk,
    HealthStatus,
    InvalidRequest,
    ProviderUnavailable,
    RateLimited,
    StreamEnded,
    StreamInterrupted,
    StreamStarted,
    TTSProvider,
    VoiceSpec,
)
from gateway.providers._framing import ChunkAssembler

_BASE_URL = "https://api.cartesia.ai"
_API_VERSION = "2025-04-16"
_DEFAULT_MODEL = "sonic-3"

#: Gateway logical voice name -> Cartesia voice id. This table is the only place a
#: vendor voice id may appear. Every logical name in the pool must have a row here,
#: otherwise failing over to this provider produces silence.
#:
#: Voice ids are public identifiers, not secrets — they belong in code, not in .env.
_VOICE_IDS = {
    "receptionist-en-ca-female": "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc",
}

#: How finely the buffered response is sliced. The contract does not constrain chunk
#: size; 40ms just matches what a streaming implementation would plausibly deliver.
_CHUNK_MS = 40


class CartesiaProvider(TTSProvider):
    """Cartesia TTS over HTTP.

    Args:
        api_key: Defaults to ``CARTESIA_API_KEY``.
        model: Cartesia model id.
        timeout_s: Applied to the whole request.
        buffered: Fetch the complete utterance before emitting anything. Off by
            default; used to reproduce the non-streaming TTFA baseline.
    """

    name = "cartesia"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        timeout_s: float = 30.0,
        buffered: bool = False,
    ) -> None:
        key = api_key or os.environ.get("CARTESIA_API_KEY", "")
        if not key:
            raise ValueError("CARTESIA_API_KEY is not set")
        self._api_key = key
        self._model = model
        self._timeout_s = timeout_s
        self._buffered = buffered
        #: Diagnostics for the MIN_FIRST_CHUNK_MS coalescing; see _record_coalesce_cost.
        self.last_coalesce_ms: float | None = None
        self.last_coalesce_fragments: int | None = None
        self.last_first_chunk_ms: float | None = None
        # One client per provider instance so connections are reused across requests.
        # A fresh TLS handshake on every call would inflate TTFA and misrepresent the
        # baseline.
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=timeout_s,
            headers={
                "X-API-Key": self._api_key,
                "Cartesia-Version": _API_VERSION,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _resolve_voice(self, voice: VoiceSpec) -> str:
        try:
            return _VOICE_IDS[voice.name]
        except KeyError:
            raise InvalidRequest(
                f"unknown logical voice {voice.name!r}; known: {sorted(_VOICE_IDS)}",
                provider=self.name,
            ) from None

    def _build_payload(self, text: str, voice: VoiceSpec, voice_id: str) -> dict:
        return {
            "model_id": self._model,
            "transcript": text,
            "voice": {"mode": "id", "id": voice_id},
            # Native gateway format, so nothing needs decoding or resampling here.
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": SAMPLE_RATE,
            },
            "language": voice.language.split("-")[0],
        }

    def _validate(self, text: str, voice: VoiceSpec) -> str:
        if not text.strip():
            raise InvalidRequest("empty text", provider=self.name)
        if len(text) > MAX_TEXT_CHARS:
            raise InvalidRequest(
                f"text too long: {len(text)} > {MAX_TEXT_CHARS}", provider=self.name
            )
        return self._resolve_voice(voice)

    async def synthesize(self, text: str, voice: VoiceSpec) -> AsyncIterator:
        voice_id = self._validate(text, voice)
        payload = self._build_payload(text, voice, voice_id)

        if self._buffered:
            async for event in self._synthesize_buffered(payload, voice_id):
                yield event
        else:
            async for event in self._synthesize_streaming(payload, voice_id):
                yield event

    async def _synthesize_buffered(self, payload: dict, voice_id: str) -> AsyncIterator:
        """Fetch the whole utterance, then slice it. The non-streaming baseline."""
        try:
            response = await self._client.post("/tts/bytes", json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(f"request timed out: {exc}", provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"request failed: {exc}", provider=self.name) from exc

        self._raise_for_status(response)

        pcm = response.content
        if not pcm:
            raise StreamInterrupted("provider returned no audio", provider=self.name)
        # Never hand out a partial frame; the contract requires whole 16-bit samples.
        usable = len(pcm) - (len(pcm) % SAMPLE_WIDTH_BYTES)

        yield StreamStarted(provider=self.name, voice_resolved=voice_id)

        chunk_bytes = int(SAMPLE_RATE * _CHUNK_MS / 1000) * SAMPLE_WIDTH_BYTES
        seq = 0
        emitted = 0
        while emitted < usable:
            chunk = pcm[emitted : emitted + chunk_bytes]
            yield AudioChunk(seq=seq, pcm=chunk)
            emitted += len(chunk)
            seq += 1

        yield StreamEnded(total_samples=usable // SAMPLE_WIDTH_BYTES)

    async def _synthesize_streaming(self, payload: dict, voice_id: str) -> AsyncIterator:
        """Forward audio as the SSE stream delivers it.

        Cartesia sends ``event: chunk`` / ``data: {"type": "chunk", "data": "<base64>"}``
        with raw PCM inside. Reframing those fragments into contract-shaped chunks is
        `ChunkAssembler`'s job; everything here is Cartesia-specific.
        """
        assembler = ChunkAssembler()
        started = False
        first_fragment_at: float | None = None

        try:
            async with self._client.stream("POST", "/tts/sse", json=payload) as response:
                if response.status_code >= 400:
                    await response.aread()
                    self._raise_for_status(response)

                yield StreamStarted(provider=self.name, voice_resolved=voice_id)
                started = True

                async for line in response.aiter_lines():
                    fragment = self._decode_sse_line(line)
                    if fragment is None:
                        continue
                    if first_fragment_at is None:
                        first_fragment_at = time.perf_counter()

                    chunk = assembler.push(fragment)
                    if chunk is None:
                        continue
                    if chunk.seq == 0:
                        self._record_coalesce_cost(first_fragment_at, assembler)
                    yield chunk
        except httpx.TimeoutException as exc:
            if started:
                raise StreamInterrupted(f"stream timed out: {exc}", provider=self.name) from exc
            raise ProviderUnavailable(f"request timed out: {exc}", provider=self.name) from exc
        except httpx.HTTPError as exc:
            # Once audio is flowing this is a truncation, not a failover candidate.
            if started:
                raise StreamInterrupted(f"stream failed: {exc}", provider=self.name) from exc
            raise ProviderUnavailable(f"request failed: {exc}", provider=self.name) from exc

        tail = assembler.flush()
        if tail is not None:
            yield tail

        if assembler.total_samples == 0:
            raise StreamInterrupted("provider returned no audio", provider=self.name)

        yield StreamEnded(total_samples=assembler.total_samples)

    def _record_coalesce_cost(self, first_fragment_at: float, assembler: ChunkAssembler) -> None:
        """Record what the `MIN_FIRST_CHUNK_MS` rule cost this stream, in milliseconds.

        Diagnostics only — nothing in the gateway reads this, and it is only meaningful
        at concurrency 1 since it is per-instance rather than per-stream. It exists so
        "we buffer the first chunk to satisfy our own assertion" comes with a number
        attached instead of a shrug.
        """
        self.last_coalesce_ms = (time.perf_counter() - first_fragment_at) * 1000
        self.last_coalesce_fragments = assembler.fragments_before_first_chunk
        self.last_first_chunk_ms = assembler.first_chunk_ms

    def _decode_sse_line(self, line: str) -> bytes | None:
        """Return the PCM carried by one SSE line, or None if it carries no audio."""
        if not line.startswith("data:"):
            return None
        raw = line[len("data:") :].strip()
        if not raw:
            return None
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StreamInterrupted(f"malformed SSE payload: {exc}", provider=self.name) from exc

        kind = event.get("type")
        if kind == "error":
            raise StreamInterrupted(
                f"provider error mid-stream: {event.get('error', event)}", provider=self.name
            )
        if kind != "chunk" or not event.get("data"):
            return None
        return base64.b64decode(event["data"])

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Translate HTTP status into the gateway's error taxonomy.

        The router branches on these types, so the mapping is what decides whether a
        failure triggers failover.
        """
        status = response.status_code
        if status < 400:
            return

        detail = response.text[:300]
        if status == 429:
            retry_after = response.headers.get("retry-after")
            raise RateLimited(
                f"rate limited: {detail}",
                provider=self.name,
                retry_after_s=float(retry_after) if retry_after else None,
            )
        if status in (400, 404, 422):
            # Malformed request or unknown voice/model: another vendor answers the
            # same way, so this must not trigger failover.
            raise InvalidRequest(f"HTTP {status}: {detail}", provider=self.name)
        # 401/403 included on purpose: our credentials are bad for *this* vendor, and
        # a backup provider with working credentials can still serve the request.
        raise ProviderUnavailable(f"HTTP {status}: {detail}", provider=self.name)

    async def check_health(self) -> HealthStatus:
        """Authenticated GET against the voice list — cheap, and never synthesizes.

        Real synthesis on the health path would cost credits on every probe and take
        hundreds of milliseconds; see `TTSProvider.check_health`.
        """
        try:
            response = await self._client.get("/voices/", params={"limit": 1}, timeout=5.0)
        except httpx.HTTPError:
            return HealthStatus.UNHEALTHY
        return HealthStatus.HEALTHY if response.status_code < 400 else HealthStatus.UNHEALTHY
