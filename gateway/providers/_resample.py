"""Streaming resampling to the gateway's wire format.

Shared by provider implementations; not a provider itself, hence the underscore. The
"providers never import each other" rule is about vendor files — this is plumbing.

**One resampler instance per stream, never one per chunk.** A resampler carries filter
state across its input: the tail of chunk N is what lets it compute the head of chunk
N+1 correctly. Construct a fresh one for every chunk and each boundary restarts from
silence, which puts a discontinuity at every seam — audible as a click or buzz.

Nothing in `tests/test_contract.py` can catch that. Sample rate, byte alignment, and
sequence numbers are all still perfect; the audio is simply wrong. So the seam is
measured directly in `tests/test_resample.py` instead.
"""

from __future__ import annotations

import numpy as np
import soxr

from gateway.interface import SAMPLE_RATE

_INT16_FULL_SCALE = 32768.0


class PcmResampler:
    """Resamples a stream of 16-bit mono PCM to `SAMPLE_RATE`.

    Feed it whatever chunk sizes the vendor happens to deliver; it keeps the filter
    state and the odd trailing byte between calls. Call `flush()` at end of stream to
    drain the filter's tail.

    A single instance handles exactly one stream and must not be reused.
    """

    def __init__(self, source_rate: int, *, quality: str = "HQ") -> None:
        self.source_rate = source_rate
        self._passthrough = source_rate == SAMPLE_RATE
        self._residue = b""
        self._stream = (
            None
            if self._passthrough
            else soxr.ResampleStream(source_rate, SAMPLE_RATE, 1, dtype="float32", quality=quality)
        )

    def feed(self, pcm: bytes) -> bytes:
        """Resample one chunk. Returns whatever is ready; may be empty."""
        if self._passthrough:
            return self._aligned(pcm)
        return self._process(self._aligned(pcm), last=False)

    def flush(self) -> bytes:
        """Drain the filter tail. Call once, at end of stream."""
        if self._passthrough:
            return b""
        return self._process(b"", last=True)

    def _aligned(self, pcm: bytes) -> bytes:
        """Carry a split 16-bit frame over to the next chunk.

        Vendor chunk boundaries do not respect sample boundaries, and half a sample
        interpreted as a whole one is a click.
        """
        data = self._residue + pcm
        usable = len(data) - (len(data) % 2)
        self._residue = data[usable:]
        return data[:usable]

    def _process(self, pcm: bytes, *, last: bool) -> bytes:
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / _INT16_FULL_SCALE
        assert self._stream is not None
        out = self._stream.resample_chunk(samples, last=last)
        if out.size == 0:
            return b""
        clipped = np.clip(out * _INT16_FULL_SCALE, -_INT16_FULL_SCALE, _INT16_FULL_SCALE - 1)
        return clipped.astype("<i2").tobytes()
