"""Provider implementations: one file per vendor, none importing another.

Adding a vendor costs exactly one new file plus one line in `REGISTRY`, with no edits to
existing files. If `router.py` has to change to accommodate a provider, the interface is
wrong — stop and fix the interface first.

Decoding and resampling from native formats (MP3, Opus, mu-law, 44.1kHz) happens inside
this layer. None of it may leak into the router or the caller.
"""

from __future__ import annotations

from collections.abc import Callable

from gateway.interface import TTSProvider
from gateway.providers.cartesia import CartesiaProvider
from gateway.providers.elevenlabs import ElevenLabsProvider
from gateway.providers.fake import FakeProvider

#: Registration name -> factory. These names are what `TTS_PROVIDER_POOL` contains.
REGISTRY: dict[str, Callable[[], TTSProvider]] = {
    "cartesia": CartesiaProvider,
    "elevenlabs": ElevenLabsProvider,
    "fake": FakeProvider,
}

#: Providers that must never reach production. `fake` emits a sine wave; routed to by
#: accident, customers hear a test tone and nothing reports an error.
DEV_ONLY = frozenset({"fake"})

__all__ = ["DEV_ONLY", "REGISTRY"]
