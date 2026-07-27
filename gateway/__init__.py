"""多引擎 TTS 网关。

对外只有一种音频格式:24000 Hz / 16-bit signed LE / mono PCM。
vendor 差异全部被 `gateway.providers` 下的 `TTSProvider` 子类吸收。
"""
