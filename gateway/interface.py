"""网关的地基契约 —— `TTSProvider` ABC 及其事件类型。

Week 0 待定义:

- ``TTSProvider``  抽象基类。每家 provider 一个子类,子类之间互不引用。
- ``VoiceSpec``    调用方指定音色的方式,不含任何 vendor 字段。
- ``StreamStarted``首个 ``AudioChunk`` 之前必须发出的事件。
- ``AudioChunk``   固定 24000 Hz / 16-bit signed LE / mono PCM,序号单调递增。

接口签名定错的代价是后续每周返工 —— 先出方案,等 Alex 确认再写实现。
"""
