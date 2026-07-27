"""Tests for `PcmResampler`, including the seam defect the contract suite cannot see.

A per-chunk resampler passes every contract assertion — correct sample rate, whole
frames, contiguous sequence numbers — while producing audio with a discontinuity at
every chunk boundary. The only way to catch it is to look at the waveform, so that is
what these tests do.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from gateway.interface import SAMPLE_RATE
from gateway.providers._resample import PcmResampler

SOURCE_RATE = 44_100
#: Deliberately not a whole number of cycles per chunk. At 220Hz a 100ms chunk is
#: exactly 22 cycles, so every boundary lands on a zero crossing and a per-chunk
#: resampler looks perfectly continuous — the first version of this test proved
#: nothing for exactly that reason.
TONE_HZ = 233.0
AMPLITUDE = 0.3
CHUNK_MS = 100
DURATION_MS = 1000


def _sine_pcm(rate: int, duration_ms: int) -> bytes:
    n = int(rate * duration_ms / 1000)
    t = np.arange(n, dtype=np.float64) / rate
    wave = AMPLITUDE * np.sin(2 * math.pi * TONE_HZ * t)
    return (wave * 32767).astype("<i2").tobytes()


def _chunks(pcm: bytes, rate: int, chunk_ms: int) -> list[bytes]:
    size = int(rate * chunk_ms / 1000) * 2
    return [pcm[i : i + size] for i in range(0, len(pcm), size)]


def _max_step(pcm: bytes, *, edge_ms: int = 5) -> int:
    """Largest jump between consecutive samples, ignoring the stream's own edges.

    For a continuous tone the step is bounded by A*2*pi*f/rate; a seam is a step far
    above that. The first and last few milliseconds are excluded on purpose: a signal
    that starts or stops mid-cycle steps there by definition, and that is a property of
    the test tone, not a defect in the resampler. Seams are an interior phenomenon.
    """
    edge = int(SAMPLE_RATE * edge_ms / 1000)
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.int32)[edge:-edge]
    return int(np.abs(np.diff(samples)).max())


def _expected_max_step() -> float:
    return AMPLITUDE * 32767 * 2 * math.pi * TONE_HZ / SAMPLE_RATE


def _resample_streaming(chunks: list[bytes]) -> bytes:
    """The correct way: one resampler for the whole stream."""
    resampler = PcmResampler(SOURCE_RATE)
    out = bytearray()
    for chunk in chunks:
        out.extend(resampler.feed(chunk))
    out.extend(resampler.flush())
    return bytes(out)


def _resample_per_chunk(chunks: list[bytes]) -> bytes:
    """The trap: a fresh resampler per chunk, discarding filter state at every seam."""
    out = bytearray()
    for chunk in chunks:
        resampler = PcmResampler(SOURCE_RATE)
        out.extend(resampler.feed(chunk))
        out.extend(resampler.flush())
    return bytes(out)


def test_streaming_resample_preserves_duration() -> None:
    pcm = _sine_pcm(SOURCE_RATE, DURATION_MS)
    out = _resample_streaming(_chunks(pcm, SOURCE_RATE, CHUNK_MS))

    produced_ms = len(out) / 2 / SAMPLE_RATE * 1000
    assert abs(produced_ms - DURATION_MS) < 5, f"{produced_ms:.1f}ms out for {DURATION_MS}ms in"


def test_streaming_resample_has_no_seams() -> None:
    """One resampler across the stream leaves the waveform continuous."""
    pcm = _sine_pcm(SOURCE_RATE, DURATION_MS)
    out = _resample_streaming(_chunks(pcm, SOURCE_RATE, CHUNK_MS))

    limit = _expected_max_step() * 1.5
    assert _max_step(out) < limit, (
        f"max sample step {_max_step(out)} exceeds {limit:.0f} — the waveform is broken"
    )


def test_per_chunk_resampler_produces_audible_seams() -> None:
    """The mistake this class exists to prevent, demonstrated rather than asserted.

    Every contract assertion still passes on this output. Only the waveform shows it.
    """
    pcm = _sine_pcm(SOURCE_RATE, DURATION_MS)
    chunks = _chunks(pcm, SOURCE_RATE, CHUNK_MS)

    broken = _resample_per_chunk(chunks)
    clean = _resample_streaming(chunks)

    limit = _expected_max_step() * 1.5
    assert _max_step(broken) > limit, (
        "a per-chunk resampler was expected to leave discontinuities; if this fails the "
        "seam detector has stopped working, not the resampler"
    )
    assert _max_step(broken) > 3 * _max_step(clean)


def test_passthrough_when_rates_match() -> None:
    """24kHz in, 24kHz out: no filtering at all, bytes survive untouched."""
    pcm = _sine_pcm(SAMPLE_RATE, 200)
    resampler = PcmResampler(SAMPLE_RATE)

    out = b"".join(resampler.feed(c) for c in _chunks(pcm, SAMPLE_RATE, 50)) + resampler.flush()

    assert out == pcm


@pytest.mark.parametrize("split", [1, 3, 7], ids=["1-byte", "3-byte", "7-byte"])
def test_odd_byte_boundaries_are_carried_over(split: int) -> None:
    """Vendor chunks do not respect 16-bit frames; half a sample must never be emitted."""
    pcm = _sine_pcm(SOURCE_RATE, 200)
    resampler = PcmResampler(SOURCE_RATE)

    out = bytearray()
    for i in range(0, len(pcm), split):
        piece = resampler.feed(pcm[i : i + split])
        assert len(piece) % 2 == 0, "emitted a partial frame"
        out.extend(piece)
    out.extend(resampler.flush())

    produced_ms = len(out) / 2 / SAMPLE_RATE * 1000
    assert abs(produced_ms - 200) < 5
    assert _max_step(bytes(out)) < _expected_max_step() * 1.5
