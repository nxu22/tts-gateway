# Benchmark archive

One file per run. Filenames carry the date and the configuration; every report records
its own concurrency level and hardware label, because a latency number without them is
not comparable to anything.

| file | what it measured |
|---|---|
| `2026-07-27_cartesia_non-streaming_ttfa.md` | Cartesia buffered over `/tts/bytes` |
| `2026-07-27_cartesia_streaming_ttfa.md` | Cartesia streaming over `/tts/sse` |
| `2026-07-27_elevenlabs_streaming_ttfa.md` | ElevenLabs, socket per utterance — see note |
| `2026-07-28_elevenlabs_per-utterance_ttfa.md` | ElevenLabs, socket per utterance |
| `2026-07-28_elevenlabs_persistent_ttfa.md` | ElevenLabs, persistent multiplexed socket |
| `*_first-chunk-coalescing.md` | cost of the 20ms first-chunk floor |

## Note on the two ElevenLabs per-utterance runs

The 07-27 file was measured before the persistent-session work existed, when
socket-per-utterance was the only implementation and was simply called "streaming". It
reports 669 ms p50.

The README quotes the 07-28 pair (569 ms → 244 ms) instead, because those two were
measured minutes apart on the same network conditions. Subtracting numbers taken a day
apart would attribute network variance to the change under test — the 07-27 and 07-28
per-utterance runs differ by 100 ms with no code change between them, which is exactly
the size of the error being avoided.

The older file is kept rather than deleted: it is what the measurement said at the time.
