"""Failover state machine, exercised by fault injection.

`FakeProvider`'s injection points were built in step 2 for exactly this. No network, no
credits, and every failure happens precisely where the test wants it.

The centrepiece is `test_mid_stream_failure_does_not_switch`. Every other behaviour here
is what people expect a failover layer to do; that one is what they expect it *not* to
do, and it is the design decision most likely to be "fixed" by someone who has not
thought about what a voice changing mid-sentence sounds like.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.interface import (
    SAMPLE_RATE,
    AudioChunk,
    HealthStatus,
    InvalidRequest,
    ProviderUnavailable,
    RateLimited,
    StreamEnded,
    StreamInterrupted,
    StreamStarted,
    TTSEvent,
    VoiceSpec,
)
from gateway.providers.fake import FakeProvider
from gateway.router import SILENCE_PAD_MS, ConfigurationError, Router

TEXT = "Thanks for calling."
VOICE = VoiceSpec(name="receptionist-en-ca-female")


class _NamedFake(FakeProvider):
    """A FakeProvider with an identity, so tests can tell which one answered."""

    def __init__(self, name: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.name = name
        self.calls = 0

    async def synthesize(self, text: str, voice: VoiceSpec):  # type: ignore[override]
        self.calls += 1
        async for event in super().synthesize(text, voice):
            yield event


async def collect(router: Router) -> list[TTSEvent]:
    return [event async for event in router.synthesize(TEXT, VOICE)]


def chunks(events: list[TTSEvent]) -> list[AudioChunk]:
    return [e for e in events if isinstance(e, AudioChunk)]


# --- State 1: silent switch before any audio -------------------------------


async def test_failure_before_first_chunk_switches_silently() -> None:
    primary = _NamedFake("primary", fail_before_first_chunk=ProviderUnavailable("injected"))
    backup = _NamedFake("backup")
    router = Router([primary, backup])

    events = await collect(router)

    assert primary.calls == 1 and backup.calls == 1
    starts = [e for e in events if isinstance(e, StreamStarted)]
    assert len(starts) == 1, "the caller must see exactly one stream, not two attempts"
    assert starts[0].provider == "backup", "StreamStarted must name whoever actually delivered"
    assert isinstance(events[0], StreamStarted), "and it must still come first"
    assert isinstance(events[-1], StreamEnded)
    assert [c.seq for c in chunks(events)] == list(range(len(chunks(events))))


async def test_switch_happens_even_after_stream_started() -> None:
    """StreamStarted is not the boundary — the first AudioChunk is.

    A provider can announce a live stream and still die before producing audio. Nothing
    has reached the caller, so that is still a silent switch.
    """
    primary = _NamedFake("primary", ttfa_ms=1, fail_before_first_chunk=RateLimited("injected"))
    backup = _NamedFake("backup")

    events = await collect(Router([primary, backup]))

    assert [e.provider for e in events if isinstance(e, StreamStarted)] == ["backup"]


async def test_all_providers_failing_raises_the_last_error() -> None:
    a = _NamedFake("a", fail_on_start=ProviderUnavailable("a down"))
    b = _NamedFake("b", fail_on_start=ProviderUnavailable("b down"))

    with pytest.raises(ProviderUnavailable):
        await collect(Router([a, b]))

    assert a.calls == 1 and b.calls == 1


# --- State 2: mid-stream failure must NOT switch ---------------------------


async def test_mid_stream_failure_does_not_switch() -> None:
    """The load-bearing test of this file.

    Audio is already playing at the caller. Switching vendors now changes the speaker
    mid-sentence, which is worse than an honest truncation: a truncated turn can be
    retried, a voice swap cannot be un-heard.

    The injected error is deliberately **retryable**. An earlier version of this test
    injected `StreamInterrupted`, which is non-retryable — so it passed even with the
    router's entire mid-stream branch deleted, because the error type alone prevented the
    switch. It was testing the exception taxonomy, not the bookkeeping. Mutation testing
    caught that; a retryable error is what actually exercises "audio has been sent, so
    stop regardless".
    """
    primary = _NamedFake("primary", fail_at_chunk=3, fail_at_chunk_error=ProviderUnavailable("x"))
    backup = _NamedFake("backup")
    router = Router([primary, backup])

    events: list[TTSEvent] = []
    with pytest.raises(StreamInterrupted):
        async for event in router.synthesize(TEXT, VOICE):
            events.append(event)

    assert backup.calls == 0, "the backup must never be consulted once audio is flowing"
    assert not any(isinstance(e, StreamEnded) for e in events), (
        "StreamEnded marks a normal end; emitting it here would hide the truncation"
    )
    assert [e.provider for e in events if isinstance(e, StreamStarted)] == ["primary"]


async def test_retryable_error_before_audio_still_switches() -> None:
    """The mirror image: the same error type, but before any audio, does fail over.

    Together with the test above this pins the actual rule — the boundary is whether a
    chunk has reached the caller, not which exception was raised.
    """
    primary = _NamedFake("primary", fail_before_first_chunk=ProviderUnavailable("x"))
    backup = _NamedFake("backup")

    events = await collect(Router([primary, backup]))

    assert backup.calls == 1
    assert isinstance(events[-1], StreamEnded)


async def test_mid_stream_failure_pads_silence() -> None:
    """End on silence rather than cutting off mid-syllable."""
    router = Router([_NamedFake("primary", fail_at_chunk=3)])

    events: list[TTSEvent] = []
    with pytest.raises(StreamInterrupted):
        async for event in router.synthesize(TEXT, VOICE):
            events.append(event)

    audio = chunks(events)
    tail = audio[-1]
    assert tail.duration_ms == pytest.approx(SILENCE_PAD_MS), "the pad must be 200ms"
    assert not any(tail.pcm), "the pad must be actual silence"
    assert [c.seq for c in audio] == list(range(len(audio))), (
        "the pad continues the sequence; a gap would look like dropped audio"
    )
    assert audio[-2].pcm != tail.pcm, "real audio must precede the pad"


async def test_truncation_without_an_exception_is_still_state_two() -> None:
    """A provider that just stops, without raising, is not a normal end of stream."""

    class _SilentlyTruncating(_NamedFake):
        async def synthesize(self, text: str, voice: VoiceSpec):  # type: ignore[override]
            self.calls += 1
            yield StreamStarted(provider=self.name, voice_resolved="x")
            yield AudioChunk(seq=0, pcm=bytes(int(SAMPLE_RATE * 0.05) * 2))
            # ...and simply returns. No StreamEnded, no exception.

    backup = _NamedFake("backup")

    with pytest.raises(StreamInterrupted):
        await collect(Router([_SilentlyTruncating("primary"), backup]))

    assert backup.calls == 0


# --- Non-retryable errors never fail over ----------------------------------


async def test_invalid_request_is_not_retried_elsewhere() -> None:
    """Another provider would reject the same text; retrying only burns latency."""
    primary = _NamedFake("primary")
    backup = _NamedFake("backup")

    with pytest.raises(InvalidRequest):
        async for _ in Router([primary, backup]).synthesize("", VOICE):
            pass

    assert backup.calls == 0


# --- State 3: circuit breaking ---------------------------------------------


async def test_provider_is_ejected_after_repeated_failures() -> None:
    flaky = _NamedFake("flaky", fail_on_start=ProviderUnavailable("down"))
    backup = _NamedFake("backup")
    router = Router([flaky, backup], failure_threshold=3)

    for _ in range(3):
        await collect(router)
    assert flaky.calls == 3
    assert router.status()["flaky"] == "ejected"

    await collect(router)

    assert flaky.calls == 3, "an ejected provider must not be tried again"
    assert backup.calls == 4


async def test_success_resets_the_failure_count() -> None:
    """Two failures then a success must not leave a provider one strike from ejection."""
    provider = _NamedFake("intermittent", fail_on_start=ProviderUnavailable("down"))
    router = Router([provider, _NamedFake("backup")], failure_threshold=3)

    await collect(router)
    await collect(router)
    provider.fail_on_start = None
    await collect(router)
    provider.fail_on_start = ProviderUnavailable("down again")
    await collect(router)

    assert router.status()["intermittent"] == "available"


async def test_half_open_probe_restores_a_recovered_provider() -> None:
    provider = _NamedFake("recovering", fail_on_start=ProviderUnavailable("down"))
    backup = _NamedFake("backup")
    router = Router([provider, backup], failure_threshold=2, probe_interval_s=0)

    await collect(router)
    await collect(router)
    assert router.status()["recovering"] == "ejected"

    provider.fail_on_start = None
    await collect(router)

    assert provider.calls == 3, "the probe being due should buy exactly one trial request"
    assert router.status()["recovering"] == "available"


async def test_unhealthy_probe_keeps_a_provider_out() -> None:
    provider = _NamedFake(
        "still-down", fail_on_start=ProviderUnavailable("down"), health=HealthStatus.UNHEALTHY
    )
    router = Router([provider, _NamedFake("backup")], failure_threshold=2, probe_interval_s=0)

    await collect(router)
    await collect(router)
    calls_at_ejection = provider.calls
    await collect(router)

    assert provider.calls == calls_at_ejection, "an unhealthy probe must not spend a real request"
    assert router.status()["still-down"] == "ejected"


async def test_unknown_health_still_gets_a_trial() -> None:
    """UNKNOWN means "no cheap probe", not "unhealthy" — passive signals decide."""
    provider = _NamedFake(
        "opaque", fail_on_start=ProviderUnavailable("down"), health=HealthStatus.UNKNOWN
    )
    router = Router([provider, _NamedFake("backup")], failure_threshold=2, probe_interval_s=0)

    await collect(router)
    await collect(router)
    provider.fail_on_start = None
    await collect(router)

    assert router.status()["opaque"] == "available"


async def test_failed_trial_re_ejects_immediately() -> None:
    """A half-open provider gets one chance, not another full threshold's worth."""
    provider = _NamedFake("flapping", fail_on_start=ProviderUnavailable("down"))
    router = Router([provider, _NamedFake("backup")], failure_threshold=2, probe_interval_s=0)

    await collect(router)
    await collect(router)
    await collect(router)  # trial request, fails again

    assert router.status()["flapping"] == "ejected"


# --- The router's own output satisfies the contract ------------------------


async def test_router_output_is_contract_shaped() -> None:
    """Whatever happens underneath, the caller sees one well-formed stream."""
    router = Router(
        [_NamedFake("primary", fail_before_first_chunk=ProviderUnavailable("x")), _NamedFake("b")]
    )

    events = await collect(router)

    assert isinstance(events[0], StreamStarted)
    assert isinstance(events[-1], StreamEnded)
    audio = chunks(events)
    assert [c.seq for c in audio] == list(range(len(audio)))
    assert all(c.sample_rate == SAMPLE_RATE and len(c.pcm) % 2 == 0 for c in audio)
    assert events[-1].total_samples == sum(c.sample_count for c in audio)


async def test_concurrent_requests_are_independent() -> None:
    router = Router([_NamedFake("only")])

    left, right = await asyncio.gather(collect(router), collect(router))

    for events in (left, right):
        assert isinstance(events[-1], StreamEnded)
        assert [c.seq for c in chunks(events)] == list(range(len(chunks(events))))


# --- Startup guard ---------------------------------------------------------


def test_fake_provider_is_refused_outside_dev() -> None:
    """The debt recorded in step 2: a sine wave served to customers is a silent failure."""
    with pytest.raises(ConfigurationError, match="APP_ENV=dev"):
        Router.from_names(["fake", "cartesia"], app_env="prod")


def test_fake_provider_is_allowed_in_dev() -> None:
    router = Router.from_names(["fake"], app_env="dev")

    assert router.status() == {"fake": "available"}


def test_unknown_provider_name_fails_at_startup() -> None:
    with pytest.raises(ConfigurationError, match="unknown providers"):
        Router.from_names(["cartesia", "nonesuch"], app_env="dev")


def test_empty_pool_fails_at_startup() -> None:
    with pytest.raises(ConfigurationError):
        Router.from_names([], app_env="dev")


def test_from_env_reads_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_PROVIDER_POOL", "fake")
    monkeypatch.setenv("APP_ENV", "dev")

    assert Router.from_env().status() == {"fake": "available"}


def test_from_env_refuses_fake_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTS_PROVIDER_POOL", "fake")
    monkeypatch.setenv("APP_ENV", "prod")

    with pytest.raises(ConfigurationError):
        Router.from_env()
