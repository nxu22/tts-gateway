"""A multi-engine TTS gateway.

Exactly one output format leaves this package: 24000 Hz / 16-bit signed LE / mono PCM.
Vendor differences are absorbed entirely by `TTSProvider` subclasses under
`gateway.providers`.
"""
