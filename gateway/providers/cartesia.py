"""Cartesia provider.

**This is the buffered (non-streaming) baseline.** It fetches the entire utterance
over HTTP, then slices the buffer into chunks and emits them. The event stream has the
right shape, but no audio leaves this file until the last byte has arrived. Streaming
comes next; the whole point of measuring TTFA now is to have a number to compare
against once it does. See `bench/results/` for the baseline.

Two things this provider does **not** prove, worth stating plainly:

- **The normalization layer is untested by it.** Cartesia can emit
  ``pcm_s16le`` at 24000 Hz natively, which is exactly the gateway's wire format, so
  the decode/resample path never runs. The first real exercise of that path is
  ElevenLabs returning MP3.
- **It does not prove the transport abstraction holds.** Cartesia is HTTP; the
  interface only earns that claim when a WebSocket vendor goes in without changes.
"""

from __future__ import annotations

import os
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
    """

    name = "cartesia"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        timeout_s: float = 30.0,
    ) -> None:
        key = api_key or os.environ.get("CARTESIA_API_KEY", "")
        if not key:
            raise ValueError("CARTESIA_API_KEY is not set")
        self._api_key = key
        self._model = model
        self._timeout_s = timeout_s
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

    async def synthesize(self, text: str, voice: VoiceSpec) -> AsyncIterator:
        if not text.strip():
            raise InvalidRequest("empty text", provider=self.name)
        if len(text) > MAX_TEXT_CHARS:
            raise InvalidRequest(
                f"text too long: {len(text)} > {MAX_TEXT_CHARS}", provider=self.name
            )
        if voice.speed != 1.0:
            # Better to reject than to silently ignore what the caller asked for.
            raise InvalidRequest(
                f"speed={voice.speed} is not supported by this provider yet",
                provider=self.name,
            )
        voice_id = self._resolve_voice(voice)

        payload = {
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
