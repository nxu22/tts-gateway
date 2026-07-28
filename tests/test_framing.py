"""Unit tests for `ChunkAssembler`.

The live contract tests already cover this through both providers; these pin the edge
cases that are awkward to provoke over a real network — one-byte fragments, a first
fragment below the floor, an utterance shorter than the floor.
"""

from __future__ import annotations

import pytest

from gateway.interface import MIN_FIRST_CHUNK_MS, SAMPLE_RATE, SAMPLE_WIDTH_BYTES
from gateway.providers._framing import ChunkAssembler

FLOOR_BYTES = int(SAMPLE_RATE * MIN_FIRST_CHUNK_MS / 1000) * SAMPLE_WIDTH_BYTES


def _tone(n_bytes: int) -> bytes:
    """Non-silent filler; the assembler never inspects values, only lengths."""
    return bytes((i % 251) + 1 for i in range(n_bytes))


def test_first_chunk_is_held_until_the_floor_is_reached() -> None:
    assembler = ChunkAssembler()
    fragment = FLOOR_BYTES // 4

    assert assembler.push(_tone(fragment)) is None
    assert assembler.push(_tone(fragment)) is None
    assert assembler.push(_tone(fragment)) is None
    chunk = assembler.push(_tone(fragment))

    assert chunk is not None
    assert chunk.seq == 0
    assert chunk.duration_ms >= MIN_FIRST_CHUNK_MS
    assert assembler.fragments_before_first_chunk == 4


def test_generous_first_fragment_is_emitted_immediately() -> None:
    """A vendor whose own framing clears the floor pays nothing for the rule."""
    assembler = ChunkAssembler()

    chunk = assembler.push(_tone(FLOOR_BYTES * 4))

    assert chunk is not None and chunk.seq == 0
    assert assembler.fragments_before_first_chunk == 1


def test_later_chunks_pass_straight_through() -> None:
    """The floor applies to the first chunk only; after that, latency wins."""
    assembler = ChunkAssembler()
    assembler.push(_tone(FLOOR_BYTES))

    small = assembler.push(_tone(2))

    assert small is not None
    assert small.seq == 1
    assert small.duration_ms < MIN_FIRST_CHUNK_MS


@pytest.mark.parametrize("fragment", [1, 3, 7], ids=["1-byte", "3-byte", "7-byte"])
def test_split_samples_are_carried_across_fragments(fragment: int) -> None:
    """Vendor fragments do not respect 16-bit frames; half a sample is a click."""
    payload = _tone(FLOOR_BYTES * 2 + 1)
    assembler = ChunkAssembler()

    out = bytearray()
    for i in range(0, len(payload), fragment):
        chunk = assembler.push(payload[i : i + fragment])
        if chunk:
            assert len(chunk.pcm) % 2 == 0, "emitted a partial frame"
            out.extend(chunk.pcm)
    tail = assembler.flush()
    if tail:
        out.extend(tail.pcm)

    # One odd byte cannot be delivered; everything else survives, in order.
    assert bytes(out) == payload[: len(payload) - 1]


def test_sequence_numbers_are_contiguous() -> None:
    assembler = ChunkAssembler()

    chunks = [assembler.push(_tone(FLOOR_BYTES)) for _ in range(5)]

    assert [c.seq for c in chunks if c] == [0, 1, 2, 3, 4]
    assert assembler.total_samples == 5 * FLOOR_BYTES // SAMPLE_WIDTH_BYTES


def test_utterance_shorter_than_the_floor_is_still_delivered() -> None:
    """Dropping real audio to satisfy a threshold would be worse than a short chunk."""
    assembler = ChunkAssembler()
    assert assembler.push(_tone(FLOOR_BYTES // 2)) is None

    chunk = assembler.flush()

    assert chunk is not None
    assert chunk.seq == 0
    assert chunk.duration_ms < MIN_FIRST_CHUNK_MS


def test_flush_on_an_empty_stream_returns_nothing() -> None:
    assert ChunkAssembler().flush() is None
