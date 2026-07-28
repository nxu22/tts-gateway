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
import contextlib
import json
import os
import uuid
from collections.abc import AsyncIterator

import httpx
import websockets
from websockets.asyncio.client import connect

from gateway.interface import (
    MAX_TEXT_CHARS,
    SAMPLE_RATE,
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

_WS_BASE = "wss://api.elevenlabs.io/v1/text-to-speech"
#: Seconds the vendor keeps an idle socket alive. 180 is their documented maximum, and
#: the persistent session leans on it to keep the handshake off the critical path.
_INACTIVITY_TIMEOUT_S = 180
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


class _MultiplexedSession:
    """One long-lived WebSocket carrying several utterances at once.

    ElevenLabs' ``/multi-stream-input`` endpoint tags every message with a
    ``contextId``, so a single socket can serve concurrent syntheses. That requires one
    reader: if two `synthesize` calls both awaited ``socket.recv()`` they would steal
    each other's audio. So a background task owns the socket and fans messages out into
    per-context queues.

    The point of all this is latency. A socket opened per utterance charges every caller
    a ~340ms handshake; here it is paid once and amortized.
    """

    def __init__(self, socket) -> None:
        self._socket = socket
        self._queues: dict[str, asyncio.Queue] = {}
        self._reader = asyncio.create_task(self._pump())
        self.failure: Exception | None = None

    @property
    def alive(self) -> bool:
        return self.failure is None and not self._reader.done()

    def register(self, context_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[context_id] = queue
        return queue

    def unregister(self, context_id: str) -> None:
        self._queues.pop(context_id, None)

    async def send(self, message: dict) -> None:
        await self._socket.send(json.dumps(message))

    async def _pump(self) -> None:
        """Route every inbound message to the context that asked for it."""
        try:
            async for raw in self._socket:
                message = json.loads(raw)
                queue = self._queues.get(message.get("contextId"))
                if queue is not None:
                    queue.put_nowait(message)
        except Exception as exc:
            # Deliberately broad: whatever killed the socket has to reach every context
            # waiting on it, rather than vanishing inside a background task.
            self.failure = exc
        finally:
            if self.failure is None:
                self.failure = ConnectionError("socket closed")
            # Wake everyone still waiting; each decides whether it is a truncation.
            for queue in self._queues.values():
                queue.put_nowait(None)

    async def aclose(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader
        await self._socket.close()


class ElevenLabsProvider(TTSProvider):
    """ElevenLabs TTS over WebSocket.

    Args:
        api_key: Defaults to ``ELEVENLABS_API_KEY``.
        model: ElevenLabs model id.
        timeout_s: Applied to connection setup and to waiting for each message.
        persistent: Reuse one multiplexed socket across utterances, so the handshake is
            paid once instead of per request. Set to False for the original
            socket-per-utterance behaviour, which is kept so the before/after comparison
            can be re-run rather than resting on an archived number.
    """

    name = "elevenlabs"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        timeout_s: float = 30.0,
        persistent: bool = True,
    ) -> None:
        key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
        if not key:
            raise ValueError("ELEVENLABS_API_KEY is not set")
        self._api_key = key
        self._model = model
        self._timeout_s = timeout_s
        self._persistent = persistent
        self._http = httpx.AsyncClient(
            base_url=_HTTP_BASE, timeout=10.0, headers={"xi-api-key": key}
        )
        self._session: _MultiplexedSession | None = None
        self._session_lock = asyncio.Lock()
        #: Diagnostics, concurrency-1 only: how much of TTFA went to the handshake.
        #: 0.0 on a persistent session that was already warm — which is the point.
        self.last_handshake_ms: float | None = None

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.aclose()
            self._session = None
        await self._http.aclose()

    def _resolve_voice(self, voice: VoiceSpec) -> str:
        try:
            return _VOICE_IDS[voice.name]
        except KeyError:
            raise InvalidRequest(
                f"unknown logical voice {voice.name!r}; known: {sorted(_VOICE_IDS)}",
                provider=self.name,
            ) from None

    def _url(self, voice_id: str, *, multi: bool = False) -> str:
        endpoint = "multi-stream-input" if multi else "stream-input"
        url = (
            f"{_WS_BASE}/{voice_id}/{endpoint}"
            f"?model_id={self._model}&output_format=pcm_{SAMPLE_RATE}"
        )
        return f"{url}&inactivity_timeout={_INACTIVITY_TIMEOUT_S}" if multi else url

    async def synthesize(self, text: str, voice: VoiceSpec) -> AsyncIterator:
        if not text.strip():
            raise InvalidRequest("empty text", provider=self.name)
        if len(text) > MAX_TEXT_CHARS:
            raise InvalidRequest(
                f"text too long: {len(text)} > {MAX_TEXT_CHARS}", provider=self.name
            )
        voice_id = self._resolve_voice(voice)

        if self._persistent:
            async for event in self._synthesize_persistent(text, voice_id):
                yield event
            return

        async for event in self._synthesize_per_utterance(text, voice_id):
            yield event

    async def _synthesize_per_utterance(self, text: str, voice_id: str) -> AsyncIterator:
        """One socket per utterance: every caller pays the handshake. The baseline."""
        loop = asyncio.get_running_loop()
        t_connect = loop.time()
        assembler = ChunkAssembler()
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
                        chunk = assembler.push(audio)
                        if chunk is not None:
                            yield chunk
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

        tail = assembler.flush()
        if tail is not None:
            yield tail

        if assembler.total_samples == 0:
            raise StreamInterrupted("provider returned no audio", provider=self.name)

        yield StreamEnded(total_samples=assembler.total_samples)

    async def _ensure_session(self, voice_id: str) -> _MultiplexedSession:
        """Return a live multiplexed session, opening or replacing one if needed."""
        async with self._session_lock:
            if self._session is not None and self._session.alive:
                self.last_handshake_ms = 0.0
                return self._session
            if self._session is not None:
                await self._session.aclose()

            loop = asyncio.get_running_loop()
            t_connect = loop.time()
            socket = await connect(
                self._url(voice_id, multi=True),
                additional_headers={"xi-api-key": self._api_key},
                open_timeout=self._timeout_s,
                close_timeout=5,
                max_size=None,
            )
            self.last_handshake_ms = (loop.time() - t_connect) * 1000
            self._session = _MultiplexedSession(socket)
            return self._session

    async def _synthesize_persistent(self, text: str, voice_id: str) -> AsyncIterator:
        """Share one socket across utterances, keeping the handshake off the hot path.

        Each call gets its own ``context_id``; the session's reader routes messages back
        by that id.

        ``flush: true`` is what actually triggers generation on this endpoint, and that
        is not documented anywhere obvious — it was found by trying variants. The two
        plausible alternatives both fail *silently*: sending ``close_context`` on its own,
        or terminating with an empty-string message (which is exactly how the
        single-stream endpoint works), each return a well-formed ``isFinal`` message with
        zero bytes of audio and no error of any kind. A caller that trusted the protocol
        would ship a gateway that returns silence and reports success.
        """
        try:
            session = await self._ensure_session(voice_id)
        except websockets.exceptions.InvalidStatus as exc:
            raise self._connect_error(exc) from exc
        except (TimeoutError, OSError, websockets.exceptions.WebSocketException) as exc:
            raise ProviderUnavailable(f"connection failed: {exc}", provider=self.name) from exc

        context_id = f"ctx-{uuid.uuid4().hex[:12]}"
        queue = session.register(context_id)
        assembler = ChunkAssembler()
        started = False

        try:
            await session.send(
                {"text": " ", "voice_settings": _VOICE_SETTINGS, "context_id": context_id}
            )
            await session.send({"text": text, "context_id": context_id, "flush": True})
            await session.send({"context_id": context_id, "close_context": True})

            yield StreamStarted(provider=self.name, voice_resolved=voice_id)
            started = True

            while True:
                message = await asyncio.wait_for(queue.get(), timeout=self._timeout_s)
                if message is None:
                    # The reader stopped; the socket died under us mid-utterance.
                    raise StreamInterrupted(
                        f"session ended mid-stream: {session.failure}", provider=self.name
                    )
                audio, final = self._decode_payload(message)
                if audio:
                    chunk = assembler.push(audio)
                    if chunk is not None:
                        yield chunk
                if final:
                    break
        except TimeoutError as exc:
            raise StreamInterrupted(
                f"no audio within {self._timeout_s}s", provider=self.name
            ) from exc
        except websockets.exceptions.ConnectionClosed as exc:
            if started:
                raise StreamInterrupted(
                    f"socket closed mid-stream: {exc}", provider=self.name
                ) from exc
            raise ProviderUnavailable(f"socket closed: {exc}", provider=self.name) from exc
        finally:
            session.unregister(context_id)

        tail = assembler.flush()
        if tail is not None:
            yield tail

        if assembler.total_samples == 0:
            raise StreamInterrupted("provider returned no audio", provider=self.name)

        yield StreamEnded(total_samples=assembler.total_samples)

    def _decode_message(self, raw: str | bytes) -> tuple[bytes, bool]:
        """Return (pcm, is_final) for one raw WebSocket frame."""
        try:
            return self._decode_payload(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise StreamInterrupted(f"malformed message: {exc}", provider=self.name) from exc

    def _decode_payload(self, message: dict) -> tuple[bytes, bool]:
        """Return (pcm, is_final) for an already-parsed message."""
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
