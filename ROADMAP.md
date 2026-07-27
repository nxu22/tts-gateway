# ROADMAP

任务顺序。**一次只做一件**,每件事开一个分支。硬约束见 [CLAUDE.md](./CLAUDE.md)。

标 🧭 的任务必须**先出方案(plan mode)、等 Alex 确认后再写码**。
标 ⛔ 的任务 Claude Code 不碰,Alex 自己上手。

---

## ① 脚手架 · ✅ 完成

纯样板:`pyproject.toml`(uv)、ruff + pytest 配置、`.env.example`、`.gitignore`、
docker-compose 占位。所有 `.py` 只放 docstring,不写实现。

## ② 契约 🧭 · ← 当前

只写 `gateway/interface.py` 和 `tests/test_contract.py`。

- `TTSProvider` ABC、`AudioChunk`、`VoiceSpec`、`StreamStarted`
- `FakeProvider`:合成正弦波 PCM,可注入延迟和失败点
- 契约测试参数化,断言:采样率、chunk 序号单调、首 chunk 前必有 `StreamStarted`、
  取消时干净退出

**这一步是地基。接口签名定错的代价是后面全部返工。**

## ③ Cartesia provider

实现 `CartesiaProvider`,目标是通过现有契约测试。先非流式,跑通后再加流式。
**不许改 `interface.py` 和 `router.py`。**

## ④ ElevenLabs provider —— 真正的考验

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

## ⑨ normalize/

文本正规化 + SSML:号码、货币、日期、邮编、法语人名。测试语料放 `normalize/cases/`。

## Week 5 —— 自托管 GPU provider ⛔

装 CUDA、跑自托管模型是环境问题。Claude Code 看不到这台机器,只能猜。
Alex 自己上手,卡住了再贴报错。torch / transformers 只能作为可选 extras 出现。
