"""The gateway's foundational contract: the `TTSProvider` ABC and its event types.

Design decisions (read CLAUDE.md and get Alex's sign-off before changing any of them):

- **One event stream.** ``synthesize()`` yields a single ordered stream,
  ``StreamStarted → AudioChunk* → StreamEnded``, rather than returning a chunk
  iterator plus a separate handle for the start signal. Ordering can only be
  asserted when everything travels through the same stream.
- **Exactly one output format**: 24000 Hz / 16-bit signed LE / mono PCM. Decoding
  and resampling from each vendor's native format happens inside
  `gateway/providers/`, never above it.
- **Vendor voice ids live inside providers only.** Callers pass a gateway-level
  logical voice name (`VoiceSpec.name`) and each provider maps it to its own id.
  Passing a vendor id through would quietly kill failover state 1: the backup
  provider cannot resolve it, and nothing looks wrong while a single provider is
  serving traffic.
- **The router owns the "has a chunk been emitted yet" bookkeeping**, not the
  provider. Providers only raise typed errors. Only the router knows what has
  actually reached the caller; pushing this into providers means every vendor
  reimplements the same state machine, and the third one gets it wrong.
- **`check_health()` has no default implementation.** See its docstring.

The dataclasses are deliberately dumb — no validation in ``__post_init__``. Format
constraints are enforced in exactly one place, `tests/test_contract.py`. Splitting
enforcement across both would turn the contract tests into tests of the dataclasses
rather than tests of the providers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum

# --- The one wire format ---------------------------------------------------

SAMPLE_RATE = 24_000
SAMPLE_WIDTH_BYTES = 2  # 16-bit signed LE
CHANNELS = 1

#: Minimum amount of real audio the first AudioChunk must carry.
#: Reason: without it a provider can "jump the gun" on TTFA by emitting an empty or
#: all-zero chunk. Every assertion passes, the latency numbers look great, and the
#: user still hears a gap.
MIN_FIRST_CHUNK_MS = 20

#: Gateway-level text length cap. Exceeding it is an InvalidRequest, not a failover
#: trigger — another vendor would reject it just the same.
MAX_TEXT_CHARS = 5_000


# --- Request ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VoiceSpec:
    """How a caller asks for a voice. **Contains no vendor-specific fields.**

    `name` is a gateway-level logical voice (e.g. ``"agent-fr-ca-female"``); each
    provider resolves it to its own vendor voice id in its own file. A logical name
    must resolve on every provider in the pool, otherwise failing over produces
    silence.

    **There is deliberately no `speed` field**, and the same reasoning applies to any
    future knob. Vendors do not support the same controls: ElevenLabs offers speed,
    Cartesia does not expose it here. A request carrying ``speed=1.2`` would succeed on
    one provider and be rejected by another, so failing over would kill the request
    outright — the same class of bug as leaking a vendor voice id, just slower to show
    up. Nothing needs speed today, so nothing here provides it.

    If a caller ever does need it, the fix is not to add the field. It is to add
    capability declarations, so the router can filter candidates by what each provider
    can actually honour. That is an interface change and should be made deliberately,
    with the "what happens on failover" question answered first.
    """

    name: str
    language: str = "en-US"


# --- Events ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StreamStarted:
    """The provider holds a live stream but has not produced audio yet.

    This is **not** the failover boundary — the first `AudioChunk` is. A failure
    after `StreamStarted` but before any audio can still be retried silently on a
    backup provider.
    """

    provider: str
    #: The vendor voice id the provider resolved. For logs and reproducibility only;
    #: the router must not branch on it.
    voice_resolved: str


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """A slice of PCM: 24000 Hz / 16-bit signed LE / mono.

    The contract does not dictate chunk size — native framing varies wildly across
    vendors, and forcing a uniform size belongs to the audio normalization layer.
    Required: non-empty, an even number of bytes (whole frames), and `seq` starting
    at 0 and incrementing by exactly 1.
    """

    seq: int
    pcm: bytes
    sample_rate: int = SAMPLE_RATE

    @property
    def sample_count(self) -> int:
        return len(self.pcm) // SAMPLE_WIDTH_BYTES

    @property
    def duration_ms(self) -> float:
        return self.sample_count * 1000 / self.sample_rate


@dataclass(frozen=True, slots=True)
class StreamEnded:
    """The only marker of a **normal** end of stream: exactly one, always last.

    It must never be emitted on an error path. The router distinguishes "finished
    playing" from "cut off" purely by whether this arrived; emitting it on failure
    destroys the trigger condition for failover state 2 (pad 200ms of silence).
    """

    total_samples: int


TTSEvent = StreamStarted | AudioChunk | StreamEnded


# --- Health ----------------------------------------------------------------


class HealthStatus(Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    #: This provider has no cheap liveness probe. A legitimate answer — the router
    #: falls back to passive signals.
    UNKNOWN = "unknown"


# --- Errors ----------------------------------------------------------------


class TTSError(Exception):
    """Base class for every provider-raised error.

    The router branches on this tree and on `retryable` only, never on vendor
    identity. Each provider translates its own HTTP status codes and WebSocket close
    codes into these types.
    """

    retryable: bool = False

    def __init__(self, message: str = "", *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class ProviderUnavailable(TTSError):
    """Connection refused, 5xx, handshake timeout. Another vendor may well work."""

    retryable = True


class RateLimited(TTSError):
    """Throttled by the vendor. Another vendor may well work."""

    retryable = True

    def __init__(
        self, message: str = "", *, provider: str | None = None, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message, provider=provider)
        self.retry_after_s = retry_after_s


class StreamInterrupted(TTSError):
    """The stream died mid-flight.

    **Never retried, never switched.** Swapping vendors mid-stream changes the voice
    and sounds worse than an honest truncation.
    """

    retryable = False


class InvalidRequest(TTSError):
    """Unknown voice, empty text, text over the cap. Another vendor gives the same
    answer, so this never triggers failover."""

    retryable = False


# --- The provider contract -------------------------------------------------


class TTSProvider(ABC):
    """One TTS engine.

    Adding a vendor must cost exactly: one new file plus one registration line, with
    no edits to existing files. Providers never import each other.
    """

    #: Registration name, also what appears in `TTS_PROVIDER_POOL`. Logs and
    #: telemetry only.
    name: str

    @abstractmethod
    def synthesize(self, text: str, voice: VoiceSpec) -> AsyncIterator[TTSEvent]:
        """Synthesize text and yield the event stream.

        Implementations are async generators (``async def`` + ``yield``), which is why
        this signature is a plain ``def``.

        Yields, in order: ``StreamStarted → AudioChunk(seq=0,1,2,…) → StreamEnded``.

        Raises:
            InvalidRequest: empty text, text longer than `MAX_TEXT_CHARS`, or a voice
                that does not resolve. **No `StreamStarted` is emitted in this case.**
            ProviderUnavailable | RateLimited: retryable; the router may fail over.
            StreamInterrupted: the stream died after audio had started flowing.

        Cancellation: the caller may ``aclose()`` the generator or cancel the
        surrounding task mid-stream. Implementations must release the underlying
        connection in a ``try/finally`` and must **not** swallow ``CancelledError``.
        """
        ...

    @abstractmethod
    async def check_health(self) -> HealthStatus:
        """Cheap liveness probe. **No default implementation — every provider decides.**

        Why no default: the only plausible default is "synthesize a short phrase",
        which is both expensive (four providers probed every 30s is thousands of real
        calls a day) and slow (hundreds of milliseconds, where a health verdict should
        take single-digit ones). It would also force that behaviour onto every vendor
        by inheritance. Most vendors have something lighter: an HTTP ping, a WebSocket
        connect test, or an honest ``return HealthStatus.UNKNOWN``.

        How the router uses it: **circuit breaking runs primarily on passive signals**
        — the failure rate of the last N real requests. Live traffic is fresher and
        more accurate than a synthetic probe. Active probing is reserved for half-open
        recovery: a provider already ejected from the pool gets probed occasionally to
        see whether it can come back. At that point the frequency is low and the cost
        is acceptable.

        Real synthesis belongs in an explicit smoke test, run by hand, **never on the
        health-check path.**
        """
        ...
