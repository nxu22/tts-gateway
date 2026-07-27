"""★ 所有 provider 必须通过的同一套契约测试(参数化)。

「新增 provider」的定义就是「让这个文件通过」。写实现之前先读它。

日常开发和 CI 只跑 `FakeProvider`(合成正弦波 PCM,可注入延迟和失败点),
不烧 API 额度。真实 provider 的用例标 `@pytest.mark.live`,默认跳过。

Week 0 待写的断言:采样率与位深、chunk 序号单调递增、首 chunk 前必有
`StreamStarted`、异常类型、取消时能干净退出。
"""
