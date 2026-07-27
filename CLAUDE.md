# CLAUDE.md

给 Claude Code 的项目约束。每次会话开始前读这个文件。

---

## 项目是什么

多引擎 TTS 网关:把 ElevenLabs / Cartesia / Rime / 自托管模型藏在一个统一接口后面,
提供流式合成、健康检查、failover,以及一套语音质量评测 harness。

**这是一个作品集项目,面向 speech infra 岗位。** 优先级排序:

1. 代码清晰、抽象边界干净 —— 会被人一行行读
2. 有可复现的数字(延迟、WER、MOS)—— 这是项目的卖点
3. 功能覆盖 —— 最不重要,两家 provider 就够证明抽象是真的

## 项目不是什么

- ❌ 不训练 TTS 模型。不要引入训练代码、不要写 dataloader
- ❌ 不做 Web UI。CLI + FastAPI 端点就够了
- ❌ 不做用户系统、计费、多租户
- ❌ 不追求 provider 数量。宁可两家做透,不要六家做浅

---

## 硬约束(违反了就是 bug)

### 音频格式

网关对外**只吐一种格式**:`24000 Hz / 16-bit signed LE / mono PCM`。

各家原生格式(MP3、Opus、μ-law、44.1kHz)的解码和重采样,**必须在
`gateway/providers/` 内部完成**。这些差异绝不能泄漏到 `router.py` 或调用方。

### 抽象边界

`gateway/router.py` 和业务层代码里 **不允许出现任何 vendor 名字**。
没有 `if provider == "elevenlabs"`。差异全部由 `TTSProvider` 子类吸收。

新增一家 provider 的代价必须是:新建一个文件 + 注册一行,**不改任何已有文件**。
如果你发现要改 router 才能加 provider,说明接口设计错了 —— 停下来先改接口。

### Failover 三态

| 时机 | 行为 |
|---|---|
| 首个 audio chunk 发出**之前**失败 | 静默切换到备用 provider 重试,调用方无感知 |
| 流**中途**断掉 | **不切换**。补 200ms 静音收尾,打点告警,抛出可识别的异常 |
| 健康检查连续失败 | 从路由池摘除,半开探测恢复 |

> 中途换 vendor 会导致音色突变,听感比直接截断更糟。这是有意的设计取舍,不要"优化"掉它。

### 延迟测量口径

- **TTFA** 从「调用方发出请求」计时,不是从「provider 返回 header」计时
- **RTF** = 音频时长 / 合成耗时
- 任何延迟数字必须同时记录 **并发数** 和 **硬件配置**,单请求的裸数字不写进报告

---

## 目录结构

```
gateway/
  interface.py      # TTSProvider ABC + AudioChunk / VoiceSpec / StreamStarted
  providers/        # 每家一个文件,互不引用
  router.py         # 选路 + failover 状态机,不含 vendor 逻辑
  telemetry.py      # TTFA / RTF 埋点 → Langfuse
  main.py           # FastAPI:POST /synthesize,WS /stream
normalize/          # 文本正规化 + SSML(号码、货币、日期、邮编、法语人名)
  cases/            # 加拿大场景测试语料
eval/
  intelligibility.py  # TTS → ASR → WER
  naturalness.py      # UTMOS / NISQA
  latency.py          # TTFA / RTF / p50 / p95
  report.py           # 生成 markdown 报告
bench/results/      # 跑分归档,文件名带日期和硬件
tests/
  test_contract.py  # ★ 所有 provider 必须通过的同一套契约测试
```

---

## 开发规范

### 契约测试先行

`tests/test_contract.py` 是参数化的:每个 provider 跑同一套断言(采样率、
chunk 单调递增、首 chunk 前有 StreamStarted、异常类型、取消行为)。

**新增 provider 的定义就是「让契约测试通过」。** 先看这个文件,再写实现。

开发和 CI 用 `FakeProvider`(合成正弦波 PCM,可配置延迟和失败点),
不烧 API 额度。真实 provider 的测试标 `@pytest.mark.live`,默认跳过。

### 一次一件事

- 一个 PR / 一次会话 = 一个 provider,或一个子系统。不要同时改两家
- 涉及接口变更的任务,**先出方案再写码**(用 plan mode),等我确认
- 提交信息用 conventional commits:`feat(providers): add cartesia streaming`

### 依赖

尽量少。已定:`fastapi`、`httpx`、`websockets`、`numpy`、`soxr`(重采样)、
`pytest`、`pytest-asyncio`、`ruff`。
加任何新依赖前先问我,尤其是重的(torch、transformers 只在自托管 provider 里出现,
且必须是可选依赖 extras)。

### 密钥

只从环境变量读,`.env.example` 列出所有变量名。
**任何情况下不要把真实 key 写进代码、测试、或提交信息。**

---

## 常用命令

```bash
uv run pytest                      # 契约测试(用 FakeProvider)
uv run pytest -m live              # 打真实 API,烧额度,手动跑
uv run ruff check . && ruff format .
uv run python -m eval.report       # 生成对比报告 → bench/results/
uv run uvicorn gateway.main:app --reload
```

---

## 当前阶段

> 每完成一个阶段更新这一节。

**Week 0 —— 契约设计。**
现在只做 `interface.py` 和 `tests/test_contract.py`,**不写任何真实 provider**。
接口定错了,后面每周都要返工。

完整计划见 [ROADMAP.md](./ROADMAP.md)。

---

## 提醒

- 我(Alex)听不了你生成的音频质量,你也听不了。所有"好不好听"的判断必须走
  `eval/` 里的量化指标,不许在代码注释或 PR 描述里写"听起来更自然"
- 不确定的时候问我,不要猜着实现。尤其是接口签名和 failover 语义
