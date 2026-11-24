"""dango/web/health_probe.py

Dango's own health for the public ``GET /api/health``: Dango's state plus bounded, cached
Metabase and Docker component states.

Nothing here raises and nothing returns text from a probed system: every value is one of a
few fixed state words, so an error message, path, version or host can never reach the
unauthenticated response.
"""

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel

# How long a probed state is reused. Metabase is cheap to ask; ``docker ps`` is a subprocess.
METABASE_CACHE_SECONDS: float = 5.0
DOCKER_CACHE_SECONDS: float = 15.0

# One request waits at most this long for the component probes (they run in parallel).
# ``dango start`` polls this endpoint with a 1 second client timeout, so stay under it.
RESPONSE_BUDGET_SECONDS: float = 0.75
# Network timeout of the Metabase request (connect, write, read, pool each).
METABASE_REQUEST_TIMEOUT_SECONDS: float = 0.5
# A probe that outlives the response budget keeps running for this long, then is dropped.
PROBE_HARD_TIMEOUT_SECONDS: float = 3.0

DangoState = Literal["ok", "degraded"]
MetabaseState = Literal["ok", "starting", "unavailable", "unknown"]
DockerState = Literal["ok", "unavailable", "not_applicable", "unknown"]


class HealthComponents(BaseModel):
    """Coarse state of each component; the only values the endpoint can report."""

    dango: DangoState
    metabase: MetabaseState
    docker: DockerState


class BasicHealth(BaseModel):
    """Body of the public ``GET /api/health``."""

    status: DangoState
    components: HealthComponents


class _CachedProbe:
    """One component's state: cached for a window, with a single probe in flight at a time.

    Requests that arrive while a probe runs wait on the same result (bounded by their own
    budget) instead of starting another one. State is guarded by a thread lock and the
    in-flight result is a ``concurrent.futures.Future``, so it is shared across event loops.
    """

    def __init__(self, ttl: Callable[[], float], timeout_state: str, failed_state: str) -> None:
        self._ttl = ttl
        self._timeout_state = timeout_state
        self._failed_state = failed_state
        self._lock = threading.Lock()
        self._state: str | None = None
        self._expires = 0.0
        self._inflight: concurrent.futures.Future[str] | None = None
        self._started = 0.0
        self._task: asyncio.Task[None] | None = None

    async def get(self, probe: Callable[[], Awaitable[str]], budget: float) -> str:
        """Return the cached state, or probe (once) and wait at most ``budget`` seconds."""
        now = time.monotonic()
        with self._lock:
            if self._state is not None and now < self._expires:
                return self._state
            future = self._inflight
            # A probe whose event loop went away never finishes: replace it after its hard limit.
            stale = future is not None and now - self._started > PROBE_HARD_TIMEOUT_SECONDS + 1.0
            owner = future is None or stale
            if owner:
                future = concurrent.futures.Future()
                self._inflight = future
                self._started = now
        if future is None:
            return self._failed_state
        if owner:
            self._task = asyncio.get_running_loop().create_task(self._run(probe, future))
        try:
            return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), budget)
        except asyncio.TimeoutError:
            return self._timeout_state
        except Exception:
            return self._failed_state

    async def _run(
        self, probe: Callable[[], Awaitable[str]], future: concurrent.futures.Future[str]
    ) -> None:
        state = self._failed_state
        try:
            state = await asyncio.wait_for(probe(), PROBE_HARD_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            future.cancel()
            raise
        except Exception:
            state = self._failed_state
        finally:
            with self._lock:
                if self._inflight is future:
                    self._inflight = None
                    if not future.cancelled():
                        self._state = state
                        self._expires = time.monotonic() + self._ttl()
        if not future.done():
            future.set_result(state)


_PROBES_LOCK = threading.Lock()
_PROBES: dict[tuple[str, str], _CachedProbe] = {}


def _probe_for(component: str, project_root: Path) -> _CachedProbe:
    key = (component, str(project_root))
    with _PROBES_LOCK:
        cached = _PROBES.get(key)
        if cached is None:
            if component == "metabase":
                cached = _CachedProbe(lambda: METABASE_CACHE_SECONDS, "unavailable", "unknown")
            else:
                cached = _CachedProbe(lambda: DOCKER_CACHE_SECONDS, "unknown", "unknown")
            _PROBES[key] = cached
        return cached


async def _probe_metabase(project_root: Path) -> str:
    """Ask Metabase's own health endpoint; no credentials, no redirects, no proxy settings."""
    from dango.security.metabase_config import resolve_metabase_url

    try:
        url = await asyncio.to_thread(resolve_metabase_url, project_root)
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(METABASE_REQUEST_TIMEOUT_SECONDS),
        ) as client:
            response = await client.get(f"{url}/api/health")
    except Exception:
        return "unavailable"
    if response.status_code == 200:
        return "ok"
    if response.status_code == 503:
        try:
            body = response.json()
        except ValueError:
            return "unavailable"
        if isinstance(body, dict) and body.get("status") == "initializing":
            return "starting"
    return "unavailable"


def _docker_state(project_root: Path) -> str:
    """Docker daemon state. Servers using the compose helper have no Docker access by design."""
    from dango.platform import compose_exec
    from dango.platform.docker import DockerManager

    if compose_exec.uses_helper():
        return "not_applicable"
    return "ok" if DockerManager(project_root).is_docker_daemon_running() else "unavailable"


async def _probe_docker(project_root: Path) -> str:
    return await asyncio.to_thread(_docker_state, project_root)


async def collect_health(project_root: Path | None) -> BasicHealth:
    """Dango's own health. Never raises; the HTTP status stays 200 whatever the states are."""
    if project_root is None or not project_root.is_dir():
        return BasicHealth(
            status="degraded",
            components=HealthComponents(dango="degraded", metabase="unknown", docker="unknown"),
        )
    metabase, docker = await asyncio.gather(
        _probe_for("metabase", project_root).get(
            lambda: _probe_metabase(project_root), RESPONSE_BUDGET_SECONDS
        ),
        _probe_for("docker", project_root).get(
            lambda: _probe_docker(project_root), RESPONSE_BUDGET_SECONDS
        ),
    )
    try:
        components = HealthComponents.model_validate(
            {"dango": "ok", "metabase": metabase, "docker": docker}
        )
    except ValueError:
        components = HealthComponents(dango="ok", metabase="unknown", docker="unknown")
    return BasicHealth(status="ok", components=components)
