"""Shared test setup.

Loads `.env` into the process environment so live tests can find provider keys. Kept
dependency-free on purpose — a full dotenv library is not worth a line item for this.
Existing environment variables always win, so CI secrets are never overwritten by a
stray local file.
"""

from __future__ import annotations

import os
import pathlib

_ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env"


def _load_env_file() -> None:
    if not _ENV_FILE.is_file():
        return
    for raw in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


_load_env_file()
