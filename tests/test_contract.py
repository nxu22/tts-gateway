"""★ The single contract every provider must satisfy (parametrized).

"Adding a provider" is defined as "making this file pass". Read it before writing an
implementation.

Local development and CI run against `FakeProvider` only, so no API credits are
burned. Real-provider cases are marked `@pytest.mark.live` and skipped by default
(`addopts = -m 'not live'` in pyproject.toml).

Adding a provider touches exactly one thing here: a line in `PROVIDERS`.
Fault-injection cases run over `FAULT_PROVIDERS` instead — real vendors cannot be
told to drop a stream on cue, so only implementations that support injection take
part in those.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from gateway.interface import (
    MAX_TEXT_CHARS,
    MIN_FIRST_CHUNK_MS,
    SAMPLE_RATE,
    AudioChunk,
    HealthStatus,
    InvalidRequest,
    ProviderUnavailable,
    StreamEnded,
    StreamInterrupted,
    StreamStarted,
    TTSEvent,
    TTSProvider,
    VoiceSpec,
)
from gateway.providers.cartesia import CartesiaProvider
from gateway.providers.elevenlabs import ElevenLabsProvider
from gateway.providers.fake import FakeProvider

TEXT = "The quick brown fox jumps over the lazy dog."

#: A gateway-level logical voice, not a vendor id. Every provider in PROVIDERS must be
#: able to resolve this name — that requirement is the whole reason failover can switch
#: vendors without the caller hearing a different speaker.
VOICE = VoiceSpec(name="receptionist-en-ca-female", language="en-US")

ProviderFactory = Callable[..., TTSProvider]

#: One line per provider. Real providers carry marks=pytest.mark.live and are skipped
#: unless you run `pytest -m live`.
PROVIDERS: list[pytest.param] = [
    pytest.param(FakeProvider, id="fake"),
    pytest.param(CartesiaProvider, id="cartesia", marks=pytest.mark.live),
    pytest.param(ElevenLabsProvider, id="elevenlabs", marks=pytest.mark.live),
]

#: Implementations that support fault injection (only FakeProvider today).
FAULT_PROVIDERS: list[pytest.param] = [
    pytest.param(FakeProvider, id="fake"),
]


@pytest.fixture(params=PROVIDERS)
def provider_factory(request: pytest.FixtureRequest) -> ProviderFactory:
    return request.param


@pytest.fixture(params=FAULT_PROVIDERS)
def fault_factory(request: pytest.FixtureRequest) -> ProviderFactory:
    return request.param


async def collect(stream: AsyncIterator[TTSEvent]) -> list[TTSEvent]:
    return [event async for event in stream]


def chunks(events: list[TTSEvent]) -> list[AudioChunk]:
    return [e for e in events if isinstance(e, AudioChunk)]


# The two assertions below are shared between the contract tests and the meta-tests at
# the bottom of this file. Sharing is the point: it makes the meta-tests guard these
# exact lines rather than a copy of them. Weaken an assertion here and the meta-tests
# immediately report that cheating is no longer caught.


def assert_first_chunk_is_real_audio(audio: list[AudioChunk]) -> None:
    assert audio, "a provider must emit at least one AudioChunk"
    first = audio[0]
    assert first.duration_ms >= MIN_FIRST_CHUNK_MS, (
        f"first chunk carries only {first.duration_ms:.1f}ms, "
        f"below the {MIN_FIRST_CHUNK_MS}ms floor — TTFA land-grab"
    )
    assert any(first.pcm), "first chunk is all zeros — silence is not audio, TTFA land-grab"


def assert_no_stream_ended(events: list[TTSEvent]) -> None:
    assert not any(isinstance(e, StreamEnded) for e in events), (
        "StreamEnded must never be emitted on an error path"
    )


# --- 1. StreamStarted ------------------------------------------------------


async def test_stream_started_exactly_once_before_first_chunk(
    provider_factory: ProviderFactory,
) -> None:
    events = await collect(provider_factory().synthesize(TEXT, VOICE))

    starts = [i for i, e in enumerate(events) if isinstance(e, StreamStarted)]
    first_chunk = next(i for i, e in enumerate(events) if isinstance(e, AudioChunk))

    assert starts == [0], "StreamStarted must be emitted exactly once, as the first event"
    assert starts[0] < first_chunk, "StreamStarted must precede the first AudioChunk"


# --- 2. Wire format --------------------------------------------------------


async def test_chunks_are_24k_16bit_mono_pcm(provider_factory: ProviderFactory) -> None:
    audio = chunks(await collect(provider_factory().synthesize(TEXT, VOICE)))

    assert audio, "a provider must emit at least one AudioChunk"
    for chunk in audio:
        assert chunk.sample_rate == SAMPLE_RATE, f"chunk {chunk.seq} is not 24000 Hz"
        assert chunk.pcm, f"chunk {chunk.seq} is empty"
        assert len(chunk.pcm) % 2 == 0, f"chunk {chunk.seq} has odd byte count, splitting a frame"


# --- 3. Sequence numbers ---------------------------------------------------


async def test_chunk_seq_is_dense_and_monotonic(provider_factory: ProviderFactory) -> None:
    audio = chunks(await collect(provider_factory().synthesize(TEXT, VOICE)))

    assert [c.seq for c in audio] == list(range(len(audio))), (
        "seq must start at 0 and increment by exactly 1"
    )


# --- 4. StreamEnded --------------------------------------------------------


async def test_stream_ended_is_last_and_counts_match(provider_factory: ProviderFactory) -> None:
    events = await collect(provider_factory().synthesize(TEXT, VOICE))

    ends = [e for e in events if isinstance(e, StreamEnded)]
    assert len(ends) == 1, "StreamEnded must be emitted exactly once"
    assert isinstance(events[-1], StreamEnded), "StreamEnded must be the final event"
    assert ends[0].total_samples == sum(c.sample_count for c in chunks(events))


# --- 5. The first chunk must carry real audio ------------------------------


async def test_first_chunk_carries_real_audio(provider_factory: ProviderFactory) -> None:
    """Block TTFA land-grabs via an empty or all-silent first chunk.

    Without this, every assertion passes and the latency numbers look great while the
    user still hears a gap.
    """
    audio = chunks(await collect(provider_factory().synthesize(TEXT, VOICE)))

    assert_first_chunk_is_real_audio(audio)


# --- 6. Invalid requests ---------------------------------------------------


@pytest.mark.parametrize("text", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
async def test_blank_text_raises_invalid_request_without_starting(
    provider_factory: ProviderFactory, text: str
) -> None:
    events: list[TTSEvent] = []
    with pytest.raises(InvalidRequest):
        async for event in provider_factory().synthesize(text, VOICE):
            events.append(event)

    assert events == [], "an InvalidRequest must emit no events at all, StreamStarted included"


async def test_overlong_text_raises_invalid_request(provider_factory: ProviderFactory) -> None:
    with pytest.raises(InvalidRequest):
        await collect(provider_factory().synthesize("a" * (MAX_TEXT_CHARS + 1), VOICE))


async def test_invalid_request_is_not_retryable(provider_factory: ProviderFactory) -> None:
    """The router decides on failover from `retryable`. The next vendor rejects the
    same text, so retrying only wastes time."""
    with pytest.raises(InvalidRequest) as excinfo:
        await collect(provider_factory().synthesize("", VOICE))

    assert excinfo.value.retryable is False


# --- 7. Failure before the first chunk (failover state 1) ------------------


async def test_failure_before_first_chunk_is_retryable(fault_factory: ProviderFactory) -> None:
    provider = fault_factory(fail_before_first_chunk=ProviderUnavailable("injected"))

    events: list[TTSEvent] = []
    with pytest.raises(ProviderUnavailable) as excinfo:
        async for event in provider.synthesize(TEXT, VOICE):
            events.append(event)

    assert excinfo.value.retryable is True
    assert chunks(events) == [], (
        "no audio reached the caller, which is what makes a silent switch safe"
    )


# --- 8. Mid-stream failure (failover state 2) ------------------------------


async def test_mid_stream_failure_raises_stream_interrupted(fault_factory: ProviderFactory) -> None:
    provider = fault_factory(fail_at_chunk=3)

    events: list[TTSEvent] = []
    with pytest.raises(StreamInterrupted) as excinfo:
        async for event in provider.synthesize(TEXT, VOICE):
            events.append(event)

    audio = chunks(events)
    assert len(audio) == 3, "the stream should have died on the fourth chunk"
    assert [c.seq for c in audio] == [0, 1, 2], "already-emitted chunks must stay contiguous"
    assert excinfo.value.retryable is False, (
        "never switch vendors mid-stream — a voice change is worse than a truncation"
    )


# --- 9. No StreamEnded on error paths --------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"fail_at_chunk": 3}, StreamInterrupted),
        ({"fail_before_first_chunk": ProviderUnavailable("injected")}, ProviderUnavailable),
    ],
    ids=["mid-stream", "before-first-chunk"],
)
async def test_no_stream_ended_on_failure(
    fault_factory: ProviderFactory, kwargs: dict, expected: type[Exception]
) -> None:
    """StreamEnded is the only marker of a normal end of stream.

    Emit it on an error path and the router can no longer tell "finished playing" from
    "cut off", which destroys the trigger for failover state 2 (pad 200ms of silence).
    """
    events: list[TTSEvent] = []
    with pytest.raises(expected):
        async for event in fault_factory(**kwargs).synthesize(TEXT, VOICE):
            events.append(event)

    assert_no_stream_ended(events)


# --- 10. Cancellation ------------------------------------------------------


async def test_early_aclose_releases_resources(fault_factory: ProviderFactory) -> None:
    provider = fault_factory(audio_ms=2000, chunk_delay_ms=1)
    stream = provider.synthesize(TEXT, VOICE)

    async for event in stream:
        if isinstance(event, AudioChunk) and event.seq == 1:
            break
    assert provider.open_streams == 1

    await stream.aclose()
    assert provider.open_streams == 0, "aclose() must run the finally block and drop the connection"
    assert provider.closed_streams == 1


async def test_cancellation_propagates_and_cleans_up(fault_factory: ProviderFactory) -> None:
    """CancelledError must not be swallowed — swallowing it breaks upstream timeouts
    and graceful shutdown."""
    provider = fault_factory(audio_ms=60_000, chunk_delay_ms=5)
    started = asyncio.Event()

    async def consume() -> None:
        async for event in provider.synthesize(TEXT, VOICE):
            if isinstance(event, AudioChunk):
                started.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The generator's finally may run one loop iteration later; yield once first.
    await asyncio.sleep(0)
    assert provider.open_streams == 0, "cancellation must leave nothing open"


# --- 11. Concurrency -------------------------------------------------------


async def test_concurrent_streams_do_not_interleave(provider_factory: ProviderFactory) -> None:
    """Two concurrent streams on one provider instance keep independent sequences.

    Byte-for-byte equality is deliberately *not* asserted here: real engines are not
    obliged to be deterministic across identical requests. The determinism check lives
    with FakeProvider at the bottom of this file, where it is a fair expectation.
    """
    provider = provider_factory()

    left, right = await asyncio.gather(
        collect(provider.synthesize(TEXT, VOICE)),
        collect(provider.synthesize(TEXT, VOICE)),
    )

    for events in (left, right):
        audio = chunks(events)
        assert [c.seq for c in audio] == list(range(len(audio))), "sequences must not interleave"
        assert sum(c.sample_count for c in audio) > 0

    durations = [sum(c.duration_ms for c in chunks(events)) for events in (left, right)]
    assert min(durations) > 0.5 * max(durations), (
        f"same text and voice produced wildly different durations {durations} — "
        "the two streams probably corrupted each other"
    )


# --- 12. Health ------------------------------------------------------------


async def test_check_health_returns_status_without_synthesizing(
    provider_factory: ProviderFactory,
) -> None:
    """Probes must be cheap. Real synthesis belongs in an explicit smoke test, never on
    the health-check path."""
    assert isinstance(await provider_factory().check_health(), HealthStatus)


# --- Meta-tests: prove the two anti-cheat assertions have teeth ------------
#
# Contract tests can themselves be wrong: an assertion that is always true looks
# exactly like one that works. These feed deliberately cheating providers into the two
# shared assertion helpers above. If an assertion stops catching its cheat, this goes
# red.


async def test_fake_provider_is_deterministic() -> None:
    """FakeProvider must be byte-identical across runs.

    Not a contract requirement — real engines are free to vary — but CI depends on it,
    and a fake that drifts would make every other assertion here unreliable.
    """
    provider = FakeProvider()

    left, right = await asyncio.gather(
        collect(provider.synthesize(TEXT, VOICE)),
        collect(provider.synthesize(TEXT, VOICE)),
    )

    assert [c.pcm for c in chunks(left)] == [c.pcm for c in chunks(right)]


@pytest.mark.parametrize("cheat", ["empty", "silent", "short"])
async def test_ttfa_cheating_is_caught(cheat: str) -> None:
    audio = chunks(await collect(FakeProvider(cheat_first_chunk=cheat).synthesize(TEXT, VOICE)))

    with pytest.raises(AssertionError):
        assert_first_chunk_is_real_audio(audio)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"fail_at_chunk": 2}, StreamInterrupted),
        ({"fail_before_first_chunk": ProviderUnavailable("injected")}, ProviderUnavailable),
    ],
    ids=["mid-stream", "before-first-chunk"],
)
async def test_stream_ended_on_error_is_caught(kwargs: dict, expected: type[Exception]) -> None:
    cheater = FakeProvider(cheat_end_on_error=True, **kwargs)

    events: list[TTSEvent] = []
    with pytest.raises(expected):
        async for event in cheater.synthesize(TEXT, VOICE):
            events.append(event)

    with pytest.raises(AssertionError):
        assert_no_stream_ended(events)
