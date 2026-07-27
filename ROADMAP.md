# ROADMAP

任务顺序。**一次只做一件**,每件事开一个分支。硬约束见 [CLAUDE.md](./CLAUDE.md)。

标 🧭 的任务必须**先出方案(plan mode)、等 Alex 确认后再写码**。
标 ⛔ 的任务 Claude Code 不碰,Alex 自己上手。

---

## ① 脚手架 · ✅ 完成

纯样板:`pyproject.toml`(uv)、ruff + pytest 配置、`.env.example`、`.gitignore`、
docker-compose 占位。所有 `.py` 只放 docstring,不写实现。

## ② 契约 🧭 · ✅ 完成

`gateway/interface.py` + `gateway/providers/fake.py` + `tests/test_contract.py`。
23 条断言,全绿。定下来的决定:

- 单一事件流 `StreamStarted → AudioChunk* → StreamEnded`
- **逻辑音色名**,vendor voice id 只存在于 provider 内部(否则 failover 第一态废掉)
- 「首 chunk 是否已发出」由 router 记账,provider 只抛类型化异常
- chunk 大小不进契约,只约束非空 / 偶数字节 / seq 连续
- `check_health()` **无默认实现**;熔断以被动信号为主,主动探测只用于半开恢复
- 首个 chunk 必须携带 ≥20ms 真实音频(防 TTFA 抢跑)
- 异常路径上不许发 `StreamEnded`(否则第二态触发条件废掉)

推迟:`synthesize` 只收 `str`。LLM token 级串流是后面真正的延迟大头,到时候动接口 ——
是**新增重载**而不是破坏性改动,可以接受。

## ③ Cartesia provider · ✅ 完成

`gateway/providers/cartesia.py`。`interface.py` 和 `router.py` 一行未改,
`tests/test_contract.py` 只加了注册的那一行。12 条 live 断言通过。

TTFA(同一份测量代码,20 次,并发 1):

| 模式 | p50 | p95 |
|---|---|---|
| 非流式 `/tts/bytes` | 1117 ms | 1589 ms |
| 流式 `/tts/sse` | **226 ms** | **311 ms** |

非流式那条路留着没删 —— 不是 fallback,是为了随时能重跑这个对比。

首 chunk 合并(为满足 `MIN_FIRST_CHUNK_MS`)的代价也量了:**0.0ms**。Cartesia 首个
SSE 分片就带 133ms 音频,远超 20ms 地板,合并逻辑一次没触发。这是 Cartesia 分片
方式的事实,不是通用结论 —— 每家都要重新量。

`VoiceSpec.speed` 已删除(Alex 决定)。理由和删 `ProviderRegistration` 同类:
各家能力不对等,`speed=1.2` 在 ElevenLabs 成功、在 Cartesia 抛 `InvalidRequest`,
而 `InvalidRequest` 不 failover —— failover 第一态又会被"能力不对等"废掉。
真要支持得先引入**能力声明**(provider 声明支持什么,router 按能力过滤候选),
那是接口变更,等有场景再做。

⚠️ **这一步没有验证归一层。** Cartesia 原生就吐 24kHz pcm_s16le,重采样和解码
代码一次都没执行。真正的考验在 ④,ElevenLabs 吐 MP3 的时候。

## ④ ElevenLabs provider —— 真正的考验 · ← 当前

实现 `ElevenLabsProvider`。它是 WebSocket,Cartesia 是 HTTP chunked。

> 如果为了兼容需要改 `interface.py`,**停下来说明原因**,不要自己改。
> 第二家能接进去而不用改接口,才说明 ② 做对了。

## ⑤ 音频归一层

各家原生格式 → `24kHz / 16-bit LE / mono PCM`。解码 + 重采样(soxr)全部在
`providers/` 内部完成,不泄漏到 router。

## ⑥ 遥测 / TTFA 埋点

`telemetry.py`:TTFA(从调用方发请求起算)、RTF、并发数、硬件配置 → Langfuse。

## ⑦ eval harness

`eval/intelligibility.py`(TTS → ASR → WER)、`naturalness.py`(UTMOS / NISQA)、
`latency.py`(p50 / p95)、`report.py` → `bench/results/`。

## ⑧ router + failover 🧭

选路 + failover 状态机 + 故障注入测试。三态语义见 CLAUDE.md,不要"优化"掉
「中途不切换」这条。

必做的两道防护:

- **启动校验**:非 dev 环境(`APP_ENV != "dev"`)下 pool 里出现 `fake` 就报错退出。
  它能被 `TTS_PROVIDER_POOL` 路由到,总有一天会被误配到线上,然后客户听到正弦波。
- **熔断靠被动信号**:最近 N 次真实请求的失败率,不是定时探测。`check_health()`
  只用在半开恢复上。

## ⑨ normalize/

文本正规化 + SSML:号码、货币、日期、邮编、法语人名。测试语料放 `normalize/cases/`。

## Week 5 —— 自托管 GPU provider ⛔

装 CUDA、跑自托管模型是环境问题。Claude Code 看不到这台机器,只能猜。
Alex 自己上手,卡住了再贴报错。torch / transformers 只能作为可选 extras 出现。
