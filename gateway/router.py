"""选路 + failover 状态机。

**这个文件里不允许出现任何 vendor 名字。** 没有 `if provider == "elevenlabs"`。

failover 三态(语义见 CLAUDE.md,不要"优化"掉第二条):

1. 首个 audio chunk 发出**之前**失败 → 静默切到备用 provider 重试,调用方无感知
2. 流**中途**断掉 → 不切换。补 200ms 静音收尾,打点告警,抛可识别异常
3. 健康检查连续失败 → 摘除出池,半开探测恢复
"""
