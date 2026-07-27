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

from gateway.interface import AudioChunk, VoiceSpec
from gateway.providers.cartesia import CartesiaProvider

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


async def test_ttfa_baseline_non_streaming() -> None:
    provider = CartesiaProvider()
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
    report = _render_report(samples, durations, p50, p95)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{date.today():%Y-%m-%d}_cartesia_non-streaming_ttfa.md"
    out.write_text(report, encoding="utf-8")

    print(f"\n{report}\nwrote {out}")

    # A sanity floor, not a performance target: anything under 10ms means the
    # stopwatch is measuring the wrong thing (a cached response, a mocked client).
    assert p50 > 10, f"p50 of {p50:.1f}ms is implausible for a network round trip"


def _render_report(samples: list[float], durations: list[float], p50: float, p95: float) -> str:
    audio_s = statistics.mean(durations) / 1000
    rtf = [audio / (ttfa) for audio, ttfa in zip(durations, samples, strict=True)]
    return "\n".join(
        [
            "# Cartesia TTFA baseline — non-streaming (buffered)",
            "",
            f"- date: {date.today():%Y-%m-%d}",
            "- provider: cartesia (`sonic-3`, HTTP `/tts/bytes`, raw pcm_s16le 24kHz)",
            "- implementation: **buffered** — the full utterance is fetched, then sliced",
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
            "The streaming implementation will be measured with this same code,",
            "unchanged, so the two numbers are comparable.",
            "",
            f"Raw samples (ms): {[round(s) for s in samples]}",
            "",
        ]
    )
