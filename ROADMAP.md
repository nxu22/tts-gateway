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

## ④ ElevenLabs provider —— 真正的考验 · ✅ 通过

**判卷结果:`interface.py` 和 `router.py` 一行没动,12 条 live 断言第一次跑就全过。**
`test_contract.py` 只加了注册的那一行。契约被 WebSocket 吸收掉的东西:三段式发送
仪式(init / text / 空串 flush)、base64-in-JSON、每句话一次握手。

TTFA(同一份测量代码):

| 实现 | p50 | p95 |
|---|---|---|
| Cartesia 非流式 HTTP | 1117 ms | 1589 ms |
| Cartesia 流式 SSE | **226 ms** | **311 ms** |
| ElevenLabs 每句一条 socket | 569 ms | 603 ms |
| ElevenLabs 持久多路复用 socket | **244 ms** | **293 ms** |

两家流式最初差 2.5 倍,而差的**不是合成速度是连接复用策略** —— 见下面 ④.5。

免费档的两个坑(都记一下,换 key 时会再遇到):
- **library voices 免费账户不能用 API 调**。Rachel / Aria 都 402。可用的是
  Sarah / Laura / Jessica 这类 default voice。
- key 的权限是分项的。这把 key 缺 `voices_read` / `user_read`,所以运行时列不出
  voice 列表 —— 正好印证映射表写成字面量是对的。`check_health()` 用
  `/v1/voices/settings/default`,是这把 key 唯一有权限的便宜端点。

## ④.5 抽 ChunkAssembler + 干掉握手延迟 · ✅ 完成

**抽 `ChunkAssembler`**(`providers/_framing.py`):首 chunk 合并、半采样余数携带、
seq 编号 —— 这三样不是碰巧相似,是契约强制每家做的同一件事,所以抽出来是把规格
落地而不是投机性抽象。只抽了字面上完全相同的部分;传输、错误映射、非流式切片
路径都留在各自文件里。9 条单元测试 + 两家 live 全过,诊断数字一模一样。

**干掉握手**:ElevenLabs 改用 `/multi-stream-input`,一条 socket 上用 `context_id`
跑多个上下文。需要一个后台 reader 独占 socket、按 contextId 分发到各自队列 ——
两个调用方同时 `recv()` 会互相偷音频。

`p50 569ms → 244ms`,和 Cartesia 的 226ms 进入同一量级。

协议上的坑:`flush: true` 才是这个端点的触发信号。只发 `close_context`(或空串)
上下文会直接空掉,返回 `isFinal` 但一个字节音频都没有。

`persistent=False` 保留了旧路径,理由和 Cartesia 的 `buffered=True` 一样 ——
对比要能随时重跑,不能只剩一个归档数字。

### 顺带产出:流式重采样器

`gateway/providers/_resample.py` —— **一条流一个实例**,跨 chunk 保留滤波器状态和
半个采样的余数。④ 用 `pcm_24000` 所以它没被触发,真正验证在 ⑤。

接缝缺陷契约测试抓不到(采样率、字节对齐、seq 全对,只有波形是坏的),所以
`tests/test_resample.py` 直接量波形:相邻采样最大跳变,流式 **599** vs 每 chunk
新建 **3310**(纯音理论上限 600)。

写这个测试时踩了两个坑,经过写进了测试文件的 docstring:
- 第一版用 220Hz + 100ms chunk = 正好 22 个整周期,每个接缝都落在过零点上,
  缺陷完全隐形,测试是空转的。改成 233Hz 才暴露出来。
- 检测器把信号自身的首尾截断也算成了接缝。接缝是**内部**现象,现在只测内部。

范围是 Alex 定的,事后看是对的:**只考一科** —— WebSocket 能不能塞进同一个
async generator。`output_format=pcm_24000`,不碰 MP3,不加任何新依赖。同时塞进
MP3 解码等于一场考试考两科,跑挂了分不清是抽象漏了还是解码器写错了。

也没换 Rime:④ 考的就是传输层差异,Rime 如果也是 HTTP 流,这场考试就白考了。

实现 `ElevenLabsProvider`。它是 WebSocket,Cartesia 是 HTTP chunked。

> 如果为了兼容需要改 `interface.py`,**停下来说明原因**,不要自己改。
> 第二家能接进去而不用改接口,才说明 ② 做对了。

## ⑤ 音频归一层

各家原生格式 → `24kHz / 16-bit LE / mono PCM`。解码 + 重采样(soxr)全部在
`providers/` 内部完成,不泄漏到 router。

重采样器已经在 ④ 之前写好并量过(`_resample.py`)。**归一层的真实验证放在这一步**:
自托管模型很多原生输出 22050Hz 或 44100Hz,那时重采样会被真实数据触发,而且
零新依赖。真出现只能吐压缩格式的 provider,再加解码器 —— 那时它有明确的消费者。

## ⑥ 遥测 / TTFA 埋点

`telemetry.py`:TTFA(从调用方发请求起算)、RTF、并发数、硬件配置 → Langfuse。

## ⑦ eval harness

`eval/intelligibility.py`(TTS → ASR → WER)、`naturalness.py`(UTMOS / NISQA)、
`latency.py`(p50 / p95)、`report.py` → `bench/results/`。

## ⑧ router + failover 🧭 · ✅ 完成

`gateway/router.py` + `tests/test_router.py`(22 条,全部 FakeProvider 故障注入,
不烧额度)。三态都落地了,②时记的那笔账(非 dev 环境 pool 里出现 `fake` 就启动
报错)也一起还了。

`StreamStarted` 改成**扣留到首个 chunk 到手才发** —— 这样 router 自己的输出仍然
符合契约(恰好一个、在音频之前),而静默切换仍然可能:一个宣布了流却没产出音频的
provider,调用方从来不知道它存在过。而且调用方看到的 `StreamStarted.provider`
永远是真正交付的那家。

`providers/__init__.py` 现在有 `REGISTRY`,"新建一个文件 + 注册一行"是字面意义上的。

### mutation testing 抓到一条装饰性测试

把 router 故意改坏四种(三态第二态改成切换、去掉补静音、去掉启动校验、
`StreamStarted` 立即转发),确认每种都被对应的测试抓住。

结果第一条**不是**被那条以它命名的测试抓住的。`test_mid_stream_failure_does_not_switch`
注入的是 `StreamInterrupted`,而它 `retryable=False` —— 光靠异常分类就阻止了切换,
所以把 router 里整个 `emitted` 分支删掉,那条测试照样绿。它测的是异常分类,不是
记账逻辑。

改成注入**可重试**的 `ProviderUnavailable` 才真正钉住规则:**停止切换的是"音频已经
交出去了",不是异常类型。** 另加一条镜像测试(同样的异常、在音频之前 → 确实切换)。

## ⑨ normalize/

文本正规化 + SSML:号码、货币、日期、邮编、法语人名。测试语料放 `normalize/cases/`。

## Week 5 —— 自托管 GPU provider ⛔

装 CUDA、跑自托管模型是环境问题。Claude Code 看不到这台机器,只能猜。
Alex 自己上手,卡住了再贴报错。torch / transformers 只能作为可选 extras 出现。
