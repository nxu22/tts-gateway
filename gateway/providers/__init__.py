"""Provider implementations: one file per vendor, none importing another.

Adding a vendor must cost exactly one new file plus one registration line, with no
edits to existing files. If `router.py` has to change to accommodate a provider, the
interface is wrong — stop and fix the interface first.

Decoding and resampling from native formats (MP3, Opus, mu-law, 44.1kHz) happens
inside this layer. None of it may leak into the router or the caller.
"""
