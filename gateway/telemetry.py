"""TTFA / RTF instrumentation, exported to Langfuse.

Measurement rules — break these and the numbers mean nothing:

- TTFA is measured from **when the caller issued the request**, not from when the
  provider returned headers.
- RTF = audio duration / synthesis wall-clock time.
- Every latency record carries the concurrency level and a hardware label. A bare
  single-request number never goes into a report.
"""
