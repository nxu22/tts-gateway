"""Turning a vendor's byte fragments into contract-shaped `AudioChunk`s.

Every streaming provider has to do the same three things, and they are not incidental
similarities — they are what `tests/test_contract.py` requires of everyone:

1. emit whole 16-bit frames, carrying a split sample across fragment boundaries
2. hold the first chunk back until it carries at least `MIN_FIRST_CHUNK_MS` of audio
3. number chunks from 0, contiguously

Point 2 is the interesting one. Emitting a token first chunk would report a better TTFA
while delivering something too short to play as continuous audio — the exact land-grab
the contract test exists to catch. Waiting is the honest cost of the rule, and it is
measured (0.0ms on Cartesia, whose first fragment already clears the floor).

Deliberately *not* generalized: how fragments are obtained, how errors map, and the
buffered slice-a-whole-buffer path. Those differ per vendor and per transport, and
unifying them would be inventing a shape rather than implementing a specification.
"""

from __future__ import annotations

from gateway.interface import (
    MIN_FIRST_CHUNK_MS,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    AudioChunk,
)


class ChunkAssembler:
    """Accumulates vendor fragments and hands back contract-shaped chunks.

    One instance per stream. Feed it whatever the vendor delivers; take a chunk when
    `push` returns one, and call `flush` once at end of stream.
    """

    def __init__(self, *, min_first_chunk_ms: int = MIN_FIRST_CHUNK_MS) -> None:
        self._min_first_bytes = int(SAMPLE_RATE * min_first_chunk_ms / 1000) * SAMPLE_WIDTH_BYTES
        self._pending = bytearray()
        self.seq = 0
        self.total_samples = 0
        #: How many fragments were absorbed before the first chunk could be emitted.
        #: 1 means the vendor's own framing already satisfied the floor.
        self.fragments_before_first_chunk = 0
        #: Duration of the first chunk actually emitted, for the same diagnostics.
        self.first_chunk_ms: float | None = None

    def push(self, pcm: bytes) -> AudioChunk | None:
        """Absorb one vendor fragment. Returns a chunk when one is ready."""
        if self.seq == 0:
            self.fragments_before_first_chunk += 1
        self._pending.extend(pcm)

        ready = len(self._pending) - (len(self._pending) % SAMPLE_WIDTH_BYTES)
        if ready == 0 or (self.seq == 0 and ready < self._min_first_bytes):
            return None
        return self._emit(ready)

    def flush(self) -> AudioChunk | None:
        """Emit whatever whole frames remain. Call once, at end of stream.

        This can return a first chunk shorter than the floor, when the entire utterance
        was shorter than the floor. Dropping real audio to satisfy a threshold would be
        worse than admitting the utterance was tiny.
        """
        ready = len(self._pending) - (len(self._pending) % SAMPLE_WIDTH_BYTES)
        return self._emit(ready) if ready else None

    def _emit(self, ready: int) -> AudioChunk:
        pcm = bytes(self._pending[:ready])
        del self._pending[:ready]
        chunk = AudioChunk(seq=self.seq, pcm=pcm)
        if self.seq == 0:
            self.first_chunk_ms = chunk.duration_ms
        self.seq += 1
        self.total_samples += ready // SAMPLE_WIDTH_BYTES
        return chunk
