"""tests/factories/driver_fakes.py

Test doubles for the pinned Metabase DuckDB driver: controlled jar bytes, a patched pin,
and a fake ``urllib.request.urlopen`` so no test ever fetches the real jar.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

GOOD_JAR = b"PK\x03\x04 controlled driver bytes " * 64
CORRUPT_JAR = GOOD_JAR[:-7] + b"tampered"  # same length, different bytes
TRUNCATED_JAR = GOOD_JAR[:100]
JAR_NAME = "duckdb.metabase-driver.jar"


class FakeResponse:
    """Context-manager response yielding *payload* in chunks."""

    def __init__(self, payload: bytes, content_length: str | None = "auto") -> None:
        self._payload = payload
        self._pos = 0
        length = str(len(payload)) if content_length == "auto" else content_length
        self.headers: dict[str, str] = {} if length is None else {"Content-Length": length}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        end = len(self._payload) if size < 0 else self._pos + size
        chunk = self._payload[self._pos : end]
        self._pos += len(chunk)
        return chunk


def pin_driver(monkeypatch: pytest.MonkeyPatch, payload: bytes = GOOD_JAR) -> None:
    """Pin the expected digest and size to *payload* (test-controlled, not downloaded)."""
    from dango.utils import driver

    monkeypatch.setattr(
        driver, "METABASE_DUCKDB_DRIVER_SHA256", hashlib.sha256(payload).hexdigest(), raising=False
    )
    monkeypatch.setattr(driver, "METABASE_DUCKDB_DRIVER_SIZE", len(payload), raising=False)


def seed_jar(project_root: Path, payload: bytes, marker: str | None) -> Path:
    plugins = project_root / "metabase-plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / JAR_NAME).write_bytes(payload)
    if marker is not None:
        (plugins / ".driver-version").write_text(marker + "\n")
    return plugins


def plugin_files(plugins: Path) -> list[str]:
    return sorted(p.name for p in plugins.iterdir()) if plugins.exists() else []


class UrlopenScript:
    """Scripted ``urlopen``: each item is a payload (bytes) or an exception to raise."""

    def __init__(self, steps: list[bytes | BaseException]) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: Any, *args: Any, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, "args": args, "kwargs": kwargs})
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, BaseException):
            raise step
        return FakeResponse(step)


@contextmanager
def patch_network(steps: list[bytes | BaseException]) -> Iterator[tuple[UrlopenScript, MagicMock]]:
    """Patch urlopen with a script and time.sleep; the legacy urlretrieve path must not run."""
    script = UrlopenScript(steps)

    def _no_retrieve(*_a: Any, **_k: Any) -> None:
        raise AssertionError("legacy urlretrieve path must not be used")

    with (
        patch("urllib.request.urlopen", side_effect=script),
        patch("urllib.request.urlretrieve", side_effect=_no_retrieve),
        patch("time.sleep") as sleep,
    ):
        yield script, sleep
