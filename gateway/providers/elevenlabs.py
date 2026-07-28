"""ElevenLabs provider, over WebSocket.

This is the transport the contract was designed for without being able to test it:
Cartesia is HTTP with server-sent events, ElevenLabs is a bidirectional WebSocket with
a send-then-flush handshake. Both have to fit the same `async generator`, and nothing in
`interface.py` or `router.py` may change to accommodate either.

Notable differences absorbed here rather than leaked upward:

- **Connection setup is per utterance.** ElevenLabs closes the socket once a generation
  is flushed, so every request pays a WebSocket handshake (~345ms observed) that
  Cartesia avoids through a pooled HTTP connection. That cost is real and belongs in
  TTFA, since the caller waits for it. Their `/multi-stream-input` endpoint supports
  reusing one socket across contexts and is the obvious optimization later.
- **Text is sent in a three-message ritual** (init, text, empty-string flush) rather
  than in one request body.
- **Audio arrives base64-encoded** inside JSON, alongside alignment metadata the gateway
  discards.

Output is requested as ``pcm_24000``, which is already the gateway's wire format, so no
decoding or resampling happens here. That keeps this step a single-subject exam: if it
fails, the transport abstraction is what failed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import AsyncIterator

import httpx
import websockets
from websockets.asyncio.client import connect

from gateway.interface import (
    MAX_TEXT_CHARS,
    MIN_FIRST_CHUNK_MS,
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

_WS_BASE = "wss://api.elevenlabs.io/v1/text-to-speech"
_HTTP_BASE = "https://api.elevenlabs.io"
#: ElevenLabs' lowest-latency model, the fair counterpart to Cartesia's sonic-3.
_DEFAULT_MODEL = "eleven_flash_v2_5"

#: Gateway logical voice name -> ElevenLabs voice id. The only place a vendor voice id
#: may appear, and it must cover every logical name the pool serves.
#:
#: Chosen from published voice metadata (female, English, conversational), **not by
#: listening** — neither the author nor the model can judge that, and pretending
#: otherwise is exactly the claim `eval/` exists to replace. Known gap: this voice is
#: American, while the logical name asks for Canadian English. Revisit once
#: `eval/naturalness.py` can score candidates.
_VOICE_IDS = {
    "receptionist-en-ca-female": "EXAVITQu4vr4xnSDxMaL",
}

#: Free-tier accounts cannot use voice-library voices over the API, and this key lacks
#: `voices_read`, so the voice list cannot be enumerated at runtime. Both are reasons
#: the mapping above is a literal rather than a lookup.
_VOICE_SETTINGS = {"stability": 0.5, "similarity_boost": 0.8}


class ElevenLabsProvider(TTSProvider):
    """ElevenLabs TTS over WebSocket.

    Args:
        api_key: Defaults to ``ELEVENLABS_API_KEY``.
        model: ElevenLabs model id.
        timeout_s: Applied to connection setup and to waiting for each message.
    """

    name = "elevenlabs"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        timeout_s: float = 30.0,
    ) -> None:
        key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        if not key:
            raise ValueError("ELEVENLABS_API_KEY is not set")
        self._api_key = key
        self._model = model
        self._timeout_s = timeout_s
        self._http = httpx.AsyncClient(
            base_url=_HTTP_BASE, timeout=10.0, headers={"xi-api-key": key}
        )
        #: Diagnostics, concurrency-1 only: how much of TTFA went to the handshake.
        self.last_handshake_ms: float | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    def _resolve_voice(self, voice: VoiceSpec) -> str:
        try:
            return _VOICE_IDS[voice.name]
        except KeyError:
            raise InvalidRequest(
                f"unknown logical voice {voice.name!r}; known: {sorted(_VOICE_IDS)}",
                provider=self.name,
            ) from None

    def _url(self, voice_id: str) -> str:
        return (
            f"{_WS_BASE}/{voice_id}/stream-input"
            f"?model_id={self._model}&output_format=pcm_{SAMPLE_RATE}"
        )

    async def synthesize(self, text: str, voice: VoiceSpec) -> AsyncIterator:
        if not text.strip():
            raise InvalidRequest("empty text", provider=self.name)
        if len(text) > MAX_TEXT_CHARS:
            raise InvalidRequest(
                f"text too long: {len(text)} > {MAX_TEXT_CHARS}", provider=self.name
            )
        voice_id = self._resolve_voice(voice)

        loop = asyncio.get_running_loop()
        t_connect = loop.time()
        seq = 0
        total_samples = 0
        pending = bytearray()
        min_first_bytes = int(SAMPLE_RATE * MIN_FIRST_CHUNK_MS / 1000) * SAMPLE_WIDTH_BYTES
        started = False

        try:
            async with connect(
                self._url(voice_id),
                additional_headers={"xi-api-key": self._api_key},
                open_timeout=self._timeout_s,
                close_timeout=5,
                max_size=None,
            ) as socket:
                self.last_handshake_ms = (loop.time() - t_connect) * 1000

                # The three-message ritual: prime the generation, send the text, then
                # flush with an empty string to tell the server nothing else is coming.
                await socket.send(json.dumps({"text": " ", "voice_settings": _VOICE_SETTINGS}))
                await socket.send(json.dumps({"text": text}))
                await socket.send(json.dumps({"text": ""}))

                yield StreamStarted(provider=self.name, voice_resolved=voice_id)
                started = True

                async for raw in socket:
                    audio, final = self._decode_message(raw)
                    if audio:
                        pending.extend(audio)
                        ready = len(pending) - (len(pending) % SAMPLE_WIDTH_BYTES)
                        if not (seq == 0 and ready < min_first_bytes) and ready > 0:
                            out = bytes(pending[:ready])
                            del pending[:ready]
                            yield AudioChunk(seq=seq, pcm=out)
                            seq += 1
                            total_samples += ready // SAMPLE_WIDTH_BYTES
                    if final:
                        break
        except websockets.exceptions.InvalidStatus as exc:
            raise self._connect_error(exc) from exc
        except websockets.exceptions.ConnectionClosed as exc:
            # Closed after audio started is a truncation, not a failover candidate.
            if started:
                raise StreamInterrupted(
                    f"socket closed mid-stream: {exc}", provider=self.name
                ) from exc
            raise ProviderUnavailable(
                f"socket closed during setup: {exc}", provider=self.name
            ) from exc
        except (TimeoutError, OSError) as exc:
            if started:
                raise StreamInterrupted(f"stream failed: {exc}", provider=self.name) from exc
            raise ProviderUnavailable(f"connection failed: {exc}", provider=self.name) from exc

        # Whatever is left is under the first-chunk floor only if the whole utterance was
        # tiny; emit it rather than silently dropping audio.
        tail = len(pending) - (len(pending) % SAMPLE_WIDTH_BYTES)
        if tail:
            yield AudioChunk(seq=seq, pcm=bytes(pending[:tail]))
            total_samples += tail // SAMPLE_WIDTH_BYTES

        if total_samples == 0:
            raise StreamInterrupted("provider returned no audio", provider=self.name)

        yield StreamEnded(total_samples=total_samples)

    def _decode_message(self, raw: str | bytes) -> tuple[bytes, bool]:
        """Return (pcm, is_final) for one WebSocket message."""
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StreamInterrupted(f"malformed message: {exc}", provider=self.name) from exc

        if message.get("error") or message.get("code") in {"invalid_api_key", "quota_exceeded"}:
            raise StreamInterrupted(
                f"provider error mid-stream: {message.get('message') or message}",
                provider=self.name,
            )

        audio = message.get("audio")
        return (base64.b64decode(audio) if audio else b""), bool(message.get("isFinal"))

    def _connect_error(self, exc: websockets.exceptions.InvalidStatus) -> Exception:
        """Map a rejected handshake onto the gateway's error taxonomy."""
        status = exc.response.status_code
        if status == 429:
            return RateLimited(f"rate limited during handshake (HTTP {status})", provider=self.name)
        if status in (400, 404, 422):
            # Bad voice or malformed request: another vendor answers the same way.
            return InvalidRequest(f"handshake rejected: HTTP {status}", provider=self.name)
        # 401/403/402 land here on purpose: the account is unusable for *this* vendor,
        # and a backup provider with a working account can still serve the request.
        return ProviderUnavailable(f"handshake rejected: HTTP {status}", provider=self.name)

    async def check_health(self) -> HealthStatus:
        """Authenticated GET of the default voice settings: cheap, and never synthesizes.

        This endpoint is used because it is the only one this API key has scope for —
        `/v1/models`, `/v1/voices` and `/v1/user` all require permissions the key lacks.
        A key scoped down to nothing but synthesis would legitimately return
        `HealthStatus.UNKNOWN` here and let the router fall back to passive signals.
        """
        try:
            response = await self._http.get("/v1/voices/settings/default")
        except httpx.HTTPError:
            return HealthStatus.UNHEALTHY
        return HealthStatus.HEALTHY if response.status_code < 400 else HealthStatus.UNHEALTHY
