"""tests/unit/test_health_probe_cache.py

The /api/health component cache: shared across event loops, stale-while-revalidate, bounded, capped bodies.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import yaml

from dango.web.app import app as real_app
from dango.web.health_probe import _CachedProbe
from tests.factories.metabase_health_double import MetabaseDouble

_DOCKER_PS = "dango.platform.docker.DockerManager.is_docker_daemon_running"
_USES_HELPER = "dango.platform.compose_exec.uses_helper"
_CLOCK = "dango.web.health_probe._clock"
_MEGABYTE = 1024 * 1024


class FakeClock:
    """A settable clock for the cache tests: nothing here waits for a TTL."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def make_double() -> Iterator[Callable[..., MetabaseDouble]]:
    doubles: list[MetabaseDouble] = []

    def factory(mode: str, delay: float = 0.0) -> MetabaseDouble:
        doubles.append(MetabaseDouble(mode, delay))
        return doubles[-1]

    yield factory
    for double in doubles:
        double.close()


@pytest.fixture(autouse=True)
def isolated_app_and_docker() -> Iterator[None]:
    """The real app is a singleton: restore its project root; Docker is always a fake."""
    missing = object()
    saved = getattr(real_app.state, "project_root", missing)
    with patch(_DOCKER_PS, return_value=True), patch(_USES_HELPER, return_value=False):
        yield
    if saved is missing:
        if hasattr(real_app.state, "project_root"):
            del real_app.state.project_root
    else:
        real_app.state.project_root = saved


def _serve(tmp_path: Path, metabase_url: str) -> None:
    root = tmp_path / "proj"
    (root / ".dango").mkdir(parents=True)
    (root / ".dango" / "metabase.yml").write_text(
        yaml.safe_dump({"metabase_url": metabase_url}), encoding="utf-8"
    )
    real_app.state.project_root = root


def _scenario(run: Callable[[httpx.AsyncClient], Awaitable[Any]]) -> Any:
    """Run *run* with a client on the real app, all inside ONE event loop (as under uvicorn)."""

    async def main() -> Any:
        transport = httpx.ASGITransport(app=real_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await run(client)

    return asyncio.run(main())


async def _metabase_state(client: httpx.AsyncClient) -> str:
    return str((await client.get("/api/health")).json()["components"]["metabase"])


@pytest.mark.unit
def test_waiter_on_another_loop_survives_the_owner_loops_shutdown() -> None:
    """A probe started on loop A is awaited from loop B; A shutting down must not break B."""
    probe = _CachedProbe(lambda: 5.0, "unavailable", "unknown")

    async def slow() -> str:
        await asyncio.sleep(5)
        return "ok"

    outcome: dict[str, Any] = {}

    def owner() -> None:
        outcome["owner"] = asyncio.run(probe.get(slow, 0.2))

    def waiter() -> None:
        async def go() -> str:
            return await probe.get(slow, 1.5)

        try:
            outcome["waiter"] = asyncio.run(go())
        except BaseException as exc:
            outcome["waiter"] = type(exc).__name__

    first = threading.Thread(target=owner)
    first.start()
    time.sleep(0.05)
    second = threading.Thread(target=waiter)
    second.start()
    first.join(10)
    second.join(10)
    assert outcome["owner"] == "unavailable"
    assert outcome["waiter"] == "unavailable"


@pytest.mark.unit
def test_expired_state_is_reprobed_and_the_fresh_answer_returned() -> None:
    """A sparse monitor must see the current state, not the previous poll's."""
    clock = FakeClock()
    probe = _CachedProbe(lambda: 5.0, "unavailable", "unknown")
    answers = iter(["ok", "unavailable", "ok"])

    async def check() -> str:
        return next(answers)

    async def go() -> list[str]:
        seen = [await probe.get(check, 0.5)]
        for _ in range(2):
            clock.now += 300
            seen.append(await probe.get(check, 0.5))
        return seen

    with patch(_CLOCK, clock):
        assert asyncio.run(go()) == ["ok", "unavailable", "ok"]


@pytest.mark.unit
def test_a_probe_slower_than_the_budget_returns_the_last_known_state() -> None:
    clock = FakeClock()
    probe = _CachedProbe(lambda: 5.0, "unavailable", "unknown")
    answers = iter(["ok", "starting"])

    async def fast() -> str:
        return next(answers)

    async def slow_refresh() -> str:
        await asyncio.sleep(0.4)
        return next(answers)

    async def go() -> tuple[str, str, float, str]:
        first = await probe.get(fast, 0.2)
        clock.now += 6
        started = time.monotonic()
        during = await probe.get(slow_refresh, 0.2)
        elapsed = time.monotonic() - started
        await asyncio.sleep(0.5)  # the probe finishes in the background and caches its answer
        after = await probe.get(fast, 0.2)  # still inside the cache window: no new probe
        return first, during, elapsed, after

    with patch(_CLOCK, clock):
        first, during, elapsed, after = asyncio.run(go())
    assert (first, during, after) == ("ok", "ok", "starting")
    assert 0.15 < elapsed < 0.35  # it waited for the budget, then fell back


@pytest.mark.unit
def test_a_last_known_state_that_is_too_old_is_not_served_on_a_budget_timeout() -> None:
    clock = FakeClock()
    probe = _CachedProbe(lambda: 5.0, "unavailable", "unknown")

    async def fast() -> str:
        return "ok"

    async def slow() -> str:
        await asyncio.sleep(0.4)
        return "ok"

    async def go() -> tuple[str, str]:
        first = await probe.get(fast, 0.2)
        clock.now += 3600
        return first, await probe.get(slow, 0.2)

    with patch(_CLOCK, clock):
        assert asyncio.run(go()) == ("ok", "unavailable")


@pytest.mark.unit
def test_first_probe_without_a_cached_state_is_still_bounded_by_the_budget() -> None:
    probe = _CachedProbe(lambda: 5.0, "unavailable", "unknown")

    async def slow() -> str:
        await asyncio.sleep(1.0)
        return "ok"

    async def go() -> tuple[str, float]:
        started = time.monotonic()
        state = await probe.get(slow, 0.2)
        return state, time.monotonic() - started

    state, elapsed = asyncio.run(go())
    assert state == "unavailable"
    assert elapsed < 0.6


@pytest.mark.unit
def test_concurrent_requests_in_one_loop_share_a_single_probe() -> None:
    probe = _CachedProbe(lambda: 5.0, "unavailable", "unknown")
    calls: list[int] = []

    async def check() -> str:
        calls.append(1)
        await asyncio.sleep(0.1)
        return "ok"

    async def go() -> list[str]:
        return list(await asyncio.gather(*(probe.get(check, 0.5) for _ in range(8))))

    assert asyncio.run(go()) == ["ok"] * 8
    assert len(calls) == 1


@pytest.mark.unit
def test_a_probe_that_never_finishes_is_cut_off_at_the_hard_limit() -> None:
    """After the hard limit the probe's failure state is cached, so later requests do not wait."""
    probe = _CachedProbe(lambda: 5.0, "unavailable", "unknown")

    async def never() -> str:
        await asyncio.sleep(30)
        return "ok"

    async def go() -> tuple[str, str, float]:
        first = await probe.get(never, 0.2)
        await asyncio.sleep(0.5)
        started = time.monotonic()
        second = await probe.get(never, 0.2)
        return first, second, time.monotonic() - started

    with patch("dango.web.health_probe.PROBE_HARD_TIMEOUT_SECONDS", 0.3):
        first, second, elapsed = asyncio.run(go())
    assert (first, second) == ("unavailable", "unknown")
    assert elapsed < 0.1


@pytest.mark.unit
def test_cache_expires_and_probes_again_without_waiting_for_a_real_ttl(
    tmp_path: Path, make_double: Callable[..., MetabaseDouble]
) -> None:
    double = make_double("ok")
    _serve(tmp_path, double.url)
    clock = FakeClock()

    async def run(client: httpx.AsyncClient) -> list[str]:
        seen = [await _metabase_state(client), await _metabase_state(client)]
        assert double.hits == 1
        double.mode = "starting"
        clock.now += 6
        seen.append(await _metabase_state(client))
        return seen

    with patch(_CLOCK, clock):
        seen = _scenario(run)
    assert seen == ["ok", "ok", "starting"]
    assert double.hits == 2


@pytest.mark.unit
def test_sparse_polling_reports_each_change_on_the_poll_after_it(
    tmp_path: Path, make_double: Callable[..., MetabaseDouble]
) -> None:
    """A monitor polling every 5 minutes sees an outage, and the recovery, on the very next poll."""
    double = make_double("ok")
    _serve(tmp_path, double.url)
    clock = FakeClock()

    async def run(client: httpx.AsyncClient) -> list[str]:
        seen = [await _metabase_state(client)]
        for mode in ("error", "ok", "starting"):
            double.mode = mode
            clock.now += 300
            seen.append(await _metabase_state(client))
        return seen

    with patch(_CLOCK, clock):
        assert _scenario(run) == ["ok", "unavailable", "ok", "starting"]


@pytest.mark.unit
def test_a_metabase_slower_than_the_budget_keeps_its_last_known_state(
    tmp_path: Path, make_double: Callable[..., MetabaseDouble]
) -> None:
    double = make_double("ok")
    _serve(tmp_path, double.url)
    clock = FakeClock()

    async def run(client: httpx.AsyncClient) -> tuple[str, str, float, str]:
        first = await _metabase_state(client)
        double.mode, double.delay = "slow_starting", 0.9
        clock.now += 6
        started = time.monotonic()
        during = await _metabase_state(client)
        elapsed = time.monotonic() - started
        after = during
        for _ in range(20):  # inside the new cache window once the slow probe has finished
            after = await _metabase_state(client)
            if after != "ok":
                break
        return first, during, elapsed, after

    with patch(_CLOCK, clock):
        first, during, elapsed, after = _scenario(run)
    assert (first, during, after) == ("ok", "ok", "starting")
    assert elapsed < 1.2
    assert double.hits == 2


@pytest.mark.unit
def test_the_metabase_request_itself_has_a_timeout(
    tmp_path: Path, make_double: Callable[..., MetabaseDouble]
) -> None:
    """Without the request timeout the probe stays in flight until the hard limit."""
    double = make_double("hang")
    _serve(tmp_path, double.url)

    async def run(client: httpx.AsyncClient) -> tuple[str, str, float]:
        first = await _metabase_state(client)
        await asyncio.sleep(0.8)
        started = time.monotonic()
        second = await _metabase_state(client)
        return first, second, time.monotonic() - started

    with (
        patch("dango.web.health_probe.METABASE_REQUEST_TIMEOUT_SECONDS", 0.3),
        patch("dango.web.health_probe.RESPONSE_BUDGET_SECONDS", 0.5),
        patch("dango.web.health_probe.PROBE_HARD_TIMEOUT_SECONDS", 2.0),
    ):
        first, second, elapsed = _scenario(run)
    assert (first, second) == ("unavailable", "unavailable")
    assert elapsed < 0.2  # the finished probe's state is cached; nothing is still in flight


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["huge200", "huge503"])
def test_the_probe_does_not_pull_a_large_body_into_memory(
    tmp_path: Path, make_double: Callable[..., MetabaseDouble], mode: str
) -> None:
    double = make_double(mode)
    _serve(tmp_path, double.url)

    async def run(client: httpx.AsyncClient) -> str:
        state = await _metabase_state(client)
        await asyncio.sleep(1.0)
        return state

    state = _scenario(run)
    assert state in {"ok", "unavailable"}
    assert double.bytes_sent < 16 * _MEGABYTE, double.bytes_sent
