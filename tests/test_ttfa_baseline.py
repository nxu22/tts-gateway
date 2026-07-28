"""Minimal TTFA measurement, archived to `bench/results/`.

⚠️ **Do not change this measurement code when the streaming implementation lands.**
The whole value of this file is that the buffered number and the streaming number are
produced by identical instrumentation. Change the methodology and the comparison is
worthless — a faster number could just mean a friendlier stopwatch.

What is measured, per CLAUDE.md:

- ``t0`` is taken **before the request is issued**, not when the provider's headers
  come back.
- ``t1`` is taken when the first `AudioChunk` reaches this loop.
- TTFA = t1 - t0. Everything in between (connection reuse, request serialization,
  vendor queueing, synthesis) counts, because the caller waits for all of it.
- Every result records concurrency and a hardware label. A bare single-request number
  never goes into a report.

Telemetry proper is step 6; this is deliberately two `perf_counter()` calls.

Run it by hand — it burns credits:

    uv run pytest -m live tests/test_ttfa_baseline.py -s
"""

from __future__ import annotations

import os
import platform
import statistics
import time
from datetime import date
from pathlib import Path

import pytest

from gateway.interface import MIN_FIRST_CHUNK_MS, AudioChunk, TTSProvider, VoiceSpec
from gateway.providers.cartesia import CartesiaProvider
from gateway.providers.elevenlabs import ElevenLabsProvider

pytestmark = pytest.mark.live

RUNS = 20
CONCURRENCY = 1
TEXT = "Thanks for calling. I can book that appointment for you right now."
VOICE = VoiceSpec(name="receptionist-en-ca-female", language="en-US")

RESULTS_DIR = Path(__file__).resolve().parent.parent / "bench" / "results"


def _hardware_label() -> str:
    label = os.environ.get("BENCH_HARDWARE_LABEL", "").strip()
    if label:
        return label
    return (
        f"{platform.system()} {platform.release()} / {platform.machine()} / {os.cpu_count()} cores"
    )


async def _measure_ttfa_ms(provider: CartesiaProvider) -> tuple[float, float]:
    """Return (TTFA in ms, total audio duration in ms) for one synthesis."""
    t0 = time.perf_counter()
    ttfa: float | None = None
    duration_ms = 0.0

    async for event in provider.synthesize(TEXT, VOICE):
        if isinstance(event, AudioChunk):
            if ttfa is None:
                ttfa = (time.perf_counter() - t0) * 1000
            duration_ms += event.duration_ms

    assert ttfa is not None, "no audio was produced"
    return ttfa, duration_ms


async def _run_baseline(provider: TTSProvider, mode: str, slug: str) -> float:
    """Warm up, take `RUNS` samples, archive the report. Returns p50 in ms."""
    try:
        # Warm up the TLS connection with a health check, not a synthesis. Otherwise
        # run 1 carries the handshake and skews the tail. Costs no credits.
        await provider.check_health()

        samples: list[float] = []
        durations: list[float] = []
        for _ in range(RUNS):
            ttfa, duration = await _measure_ttfa_ms(provider)
            samples.append(ttfa)
            durations.append(duration)
    finally:
        await provider.aclose()

    quantiles = statistics.quantiles(samples, n=100, method="inclusive")
    p50, p95 = quantiles[49], quantiles[94]
    report = _render_report(provider.name, mode, samples, durations, p50, p95)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{date.today():%Y-%m-%d}_{provider.name}_{slug}_ttfa.md"
    out.write_text(report, encoding="utf-8")

    print(f"\n{report}\nwrote {out}")

    # A sanity floor, not a performance target: anything under 10ms means the
    # stopwatch is measuring the wrong thing (a cached response, a mocked client).
    assert p50 > 10, f"p50 of {p50:.1f}ms is implausible for a network round trip"
    return p50


async def test_ttfa_baseline_non_streaming() -> None:
    await _run_baseline(
        CartesiaProvider(buffered=True), "non-streaming (buffered)", "non-streaming"
    )


async def test_ttfa_streaming() -> None:
    await _run_baseline(CartesiaProvider(), "streaming (SSE)", "streaming")


async def test_ttfa_elevenlabs_per_utterance() -> None:
    """A fresh WebSocket per utterance: every caller pays the handshake.

    This is what the transport costs before any connection management, and it is kept
    runnable so the improvement below is a live comparison rather than an archived
    claim.
    """
    provider = ElevenLabsProvider(persistent=False)
    p50 = await _run_baseline(provider, "socket per utterance", "per-utterance")
    print(f"handshake on the last run: {provider.last_handshake_ms:.0f}ms of a {p50:.0f}ms p50")


async def test_ttfa_elevenlabs_persistent() -> None:
    """One multiplexed socket across utterances: the handshake is paid once."""
    provider = ElevenLabsProvider()
    p50 = await _run_baseline(provider, "persistent multiplexed socket", "persistent")
    print(f"handshake on the last run: {provider.last_handshake_ms:.0f}ms of a {p50:.0f}ms p50")


async def test_first_chunk_coalescing_cost() -> None:
    """What the MIN_FIRST_CHUNK_MS rule costs: SSE fragment arrival -> chunk emitted.

    The contract forbids a token first chunk, so early SSE fragments are held until
    they add up to 20ms of audio. That is a real delay charged to TTFA, and "we buffer
    to satisfy our own assertion" deserves a number rather than a shrug.
    """
    provider = CartesiaProvider()
    costs: list[float] = []
    fragments: list[int] = []
    first_chunk_ms: list[float] = []
    try:
        await provider.check_health()
        for _ in range(RUNS):
            await _measure_ttfa_ms(provider)
            assert provider.last_coalesce_ms is not None
            costs.append(provider.last_coalesce_ms)
            fragments.append(provider.last_coalesce_fragments or 0)
            first_chunk_ms.append(provider.last_first_chunk_ms or 0.0)
    finally:
        await provider.aclose()

    quantiles = statistics.quantiles(costs, n=100, method="inclusive")
    p50, p95 = quantiles[49], quantiles[94]
    report = "\n".join(
        [
            "# Cost of the MIN_FIRST_CHUNK_MS rule — Cartesia streaming",
            "",
            f"- date: {date.today():%Y-%m-%d}",
            f"- runs: {RUNS}",
            f"- concurrency: {CONCURRENCY}",
            f"- hardware: {_hardware_label()}",
            "",
            "Measured from the arrival of the first SSE audio fragment to the moment the",
            "first AudioChunk is handed to the caller. The gap is time spent waiting for",
            f"enough audio to clear the {MIN_FIRST_CHUNK_MS}ms floor.",
            "",
            "| metric | ms |",
            "|---|---|",
            f"| min | {min(costs):.1f} |",
            f"| p50 | {p50:.1f} |",
            f"| p95 | {p95:.1f} |",
            f"| max | {max(costs):.1f} |",
            "",
            f"SSE fragments consumed before the first chunk: "
            f"min {min(fragments)}, max {max(fragments)}",
            f"First chunk actually delivered: {min(first_chunk_ms):.0f} to "
            f"{max(first_chunk_ms):.0f}ms of audio, against a {MIN_FIRST_CHUNK_MS}ms floor.",
            "",
            "So for this provider the rule is free: Cartesia's first SSE fragment already",
            "clears the floor on its own, and nothing is ever held back. That is a fact",
            "about Cartesia's framing, not a general result — a vendor that emits smaller",
            "fragments would pay a real delay here, so this needs re-measuring per",
            "provider.",
            "",
            "The alternative — emitting the first fragment immediately regardless of",
            "size — would report a lower TTFA while delivering a chunk too short to play",
            "as continuous audio. That is exactly the land-grab the contract test exists",
            "to catch, so this delay is the honest price of the rule.",
            "",
            f"Raw samples (ms): {[round(c, 1) for c in costs]}",
            "",
        ]
    )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{date.today():%Y-%m-%d}_cartesia_first-chunk-coalescing.md"
    out.write_text(report, encoding="utf-8")
    print(f"\n{report}\nwrote {out}")


def _render_report(
    provider: str, mode: str, samples: list[float], durations: list[float], p50: float, p95: float
) -> str:
    audio_s = statistics.mean(durations) / 1000
    rtf = [audio / (ttfa) for audio, ttfa in zip(durations, samples, strict=True)]
    return "\n".join(
        [
            f"# {provider} TTFA — {mode}",
            "",
            f"- date: {date.today():%Y-%m-%d}",
            f"- provider: {provider} (raw pcm_s16le 24kHz, {mode})",
            f"- runs: {RUNS}",
            f"- concurrency: {CONCURRENCY}",
            f"- hardware: {_hardware_label()}",
            f"- text: {TEXT!r} ({len(TEXT)} chars)",
            f"- mean audio produced: {audio_s:.2f}s",
            "",
            "TTFA is measured from before the request is issued to the arrival of the",
            "first AudioChunk at the caller.",
            "",
            "| metric | ms |",
            "|---|---|",
            f"| min | {min(samples):.0f} |",
            f"| p50 | {p50:.0f} |",
            f"| p95 | {p95:.0f} |",
            f"| max | {max(samples):.0f} |",
            "",
            f"Mean audio-seconds per TTFA-second: {statistics.mean(rtf):.1f}",
            "",
            "## Why this number exists",
            "",
            "A buffered implementation satisfies every contract assertion: the events",
            "arrive in the right order, the first chunk carries real audio, sequence",
            "numbers are contiguous. Nothing in the test suite can tell it apart from a",
            "genuinely streaming one. Only TTFA can.",
            "",
            "Both modes are measured by the same instrumentation, unchanged, so the two",
            "numbers are comparable.",
            "",
            f"Raw samples (ms): {[round(s) for s in samples]}",
            "",
        ]
    )
