"""tests/integration/test_release_readiness.py

Release-only clean-flow checks: real nginx proxy asset loading, and a
second-source Metabase schema-sync race. Requires Docker. Not run in normal
CI — see .github/workflows/release-readiness.yml (workflow_dispatch only).

Note on "the real nginx proxy": ``dango/templates/nginx.conf.j2`` (a path-based
reverse-proxy config for Metabase) was grepped against the current codebase
while writing this test and confirmed to be dead code — nothing renders or
loads it (its own module doc, ``dango/templates/CLAUDE.md``, calls it "not yet
consumed (placeholder)"). The actual reverse proxy a real ``dango start``
local project serves ``/metabase/*`` through is FastAPI's own
``dango/web/routes/metabase_proxy.py``, mounted on the production
``dango.web.app:app`` ASGI app run for real under uvicorn — this is also the
exact code path 1.0.8-W's Site URL fix (``_set_metabase_site_url()`` /
``_should_apply_local_site_url()`` in ``dango/visualization/metabase.py``)
targets.

This test starts the real FastAPI server the same way `dango start` does
(``dango.cli.helpers.process_manager.start_fastapi_server()``, a real uvicorn
subprocess with ``cwd``/``DANGO_PROJECT_ROOT`` set to the project root — the
in-process ``dango.web.app.create_app()`` factory cannot be reused for this:
all of the app's routers are attached via ``include_router()`` at *module*
level against a single process-wide singleton, and its ``AuthMiddleware`` is
bound once, at that singleton's first import, to whatever ``project_root``
was current then — calling ``create_app()`` again produces a bare app with no
routers at all, confirmed by inspecting ``app.routes`` while writing this
test) and drives it with a real logged-in ``requests.Session`` against a real
Metabase Docker container — nothing here is a shortcut around the actual
local proxy or sync pipeline.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

_ADMIN_EMAIL = "admin@dango-release-readiness.test"
_ADMIN_PASSWORD = "ReleaseReadinessTest123!@#$"
_ASSET_JS_CONTENT_TYPES = ("application/javascript", "text/javascript")


def _free_port() -> int:
    """Find a free TCP port on localhost (same pattern as test_metabase_duckdb.py)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def _write_csv_source(project_root: Path, name: str) -> None:
    """Write a minimal one-file CSV source directory and register it in sources.yml."""
    from dango.config import ConfigLoader, CSVSourceConfig, DataSource, SourceType

    data_dir = project_root / "data" / "uploads" / name
    data_dir.mkdir(parents=True, exist_ok=True)
    csv_path = data_dir / f"{name}.csv"
    csv_path.write_text("id,value\n1,alpha\n2,beta\n")

    loader = ConfigLoader(project_root)
    sources_config = loader.load_sources_config()
    sources_config.sources.append(
        DataSource(
            name=name,
            type=SourceType.CSV,
            csv=CSVSourceConfig(directory=Path("data") / "uploads" / name, file_pattern="*.csv"),
        )
    )
    loader.save_sources_config(sources_config)


def _sync_source(project_root: Path, name: str) -> dict[str, Any]:
    """Run a real `dango sync`-equivalent for a single source (no dbt/Metabase shortcuts)."""
    from dango.config import ConfigLoader
    from dango.ingestion import run_sync

    loader = ConfigLoader(project_root)
    sources_config = loader.load_sources_config()
    matches = [s for s in sources_config.sources if s.name == name]
    assert matches, f"Source {name!r} not found in sources.yml"
    return run_sync(project_root, matches)


@pytest.mark.integration
class TestReleaseReadinessCleanFlow:
    """Full clean-flow checks against a real project + real Metabase container.

    Requires Docker. Both tests share one project + one running Docker Compose
    stack (Metabase + dbt-docs) + one running real FastAPI server process via
    the class-scoped ``project`` fixture — building ``Dockerfile.metabase`` and
    starting a real project both take several minutes, so this is not repeated
    per test (same rationale as the real production `dango start` flow this
    session is meant to validate).
    """

    @pytest.fixture(autouse=True)
    def _skip_if_no_docker(self) -> None:
        if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1":
            pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 to run (heavyweight, needs Docker)")
        if shutil.which("docker") is None:
            pytest.skip("Docker not available")

    @pytest.fixture(scope="class")
    def project(self, tmp_path_factory: pytest.TempPathFactory) -> Any:
        """Build a real dango project, start its real Docker + FastAPI stack, log in.

        Equivalent to `dango init --skip-wizard` (project scaffold) + `dango
        start` (Docker Compose up, Metabase auto-setup, FastAPI server), all
        via the same library functions the CLI itself calls — not a
        hand-rolled shortcut.
        """
        if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1" or shutil.which("docker") is None:
            pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 and have Docker available to run")

        import requests

        project_root = tmp_path_factory.mktemp("release-readiness")

        prev_admin_email = os.environ.get("DANGO_ADMIN_EMAIL")
        os.environ["DANGO_ADMIN_EMAIL"] = _ADMIN_EMAIL

        docker_manager = None
        fastapi_started = False
        base_url = ""
        try:
            from dango.cli.init import ProjectInitializer, init_project

            # 1. Scaffold a real project (docker-compose.yml, Dockerfile.metabase,
            #    dbt project, auth.db + admin user, dbt docs — the real `dango
            #    init --skip-wizard` flow).
            init_project(project_root, skip_wizard=True)

            # 2. Give this project unique ports so it can't collide with any
            #    other Dango project (e.g. tests/beta-1) already running on
            #    this machine, then re-render docker-compose.yml with them.
            from dango.config import ConfigLoader

            loader = ConfigLoader(project_root)
            config = loader.load_config()
            config.platform.port = _free_port()
            config.platform.metabase_port = _free_port()
            config.platform.dbt_docs_port = _free_port()
            loader.save_config(config)
            ProjectInitializer(project_root)._create_docker_compose(config)

            # 3. Set a known password on the auto-generated admin user so this
            #    test can actually log in (skip-wizard mode always generates a
            #    random one it doesn't hand back to the caller).
            from dango.auth.admin import get_auth_db_path
            from dango.auth.database import get_user_by_email, update_user
            from dango.auth.models import UserUpdate
            from dango.auth.security import hash_password

            db_path = get_auth_db_path(project_root)
            admin_user = get_user_by_email(db_path, _ADMIN_EMAIL)
            assert admin_user is not None, "init_project() did not create the expected admin user"
            update_user(
                db_path,
                admin_user.id,
                UserUpdate(
                    password_hash=hash_password(_ADMIN_PASSWORD),
                    must_change_password=False,
                ),
            )

            # 4. Start the real Docker Compose stack (Metabase + dbt-docs) —
            #    the exact entry point `dango start` itself calls.
            from dango.platform import DockerManager
            from dango.platform.common.startup import (
                setup_metabase_if_needed,
                start_docker_services,
            )

            docker_manager = DockerManager(project_root)
            start_docker_services(project_root)

            # 5. Auto-configure Metabase (admin user, DuckDB connection, and
            #    1.0.8-W's Site URL fix) — the exact entry point `dango start`
            #    itself calls.
            setup_result = setup_metabase_if_needed(
                project_root, config.project.name, organization=None
            )
            assert setup_result.get("success"), f"Metabase setup failed: {setup_result}"

            import yaml

            mb_yml = yaml.safe_load((project_root / ".dango" / "metabase.yml").read_text())
            database_id = mb_yml.get("database", {}).get("id")
            assert database_id, f"No database id in .dango/metabase.yml: {mb_yml}"

            # 6. Start the real FastAPI server the same way `dango start`
            #    does: a real uvicorn subprocess running dango.web.app:app,
            #    with cwd + DANGO_PROJECT_ROOT set to this project — not an
            #    in-process app instance (see module docstring for why that
            #    doesn't work: routers/middleware are bound at singleton
            #    import time, not per `create_app()` call).
            from dango.cli.helpers.process_manager import start_fastapi_server

            web_port = config.platform.port
            start_fastapi_server(project_root, host="127.0.0.1", port=web_port)
            fastapi_started = True
            base_url = f"http://127.0.0.1:{web_port}"

            deadline = time.monotonic() + 30
            last_exc: Exception | None = None
            while time.monotonic() < deadline:
                try:
                    health_resp = requests.get(f"{base_url}/api/health", timeout=2)
                    if health_resp.status_code == 200:
                        break
                except requests.RequestException as exc:
                    last_exc = exc
                time.sleep(1)
            else:
                pytest.fail(f"FastAPI server never became healthy at {base_url}: {last_exc}")

            # 7. Log in as the real Dango admin, so requests go through the
            #    real auth + proxy middleware stack.
            session = requests.Session()
            login_resp = session.post(
                f"{base_url}/api/auth/login",
                json={"email": _ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
                headers={"X-Requested-With": "XMLHttpRequest"},
                timeout=10,
            )
            assert login_resp.status_code == 200, f"Admin login failed: {login_resp.text}"

            yield {
                "root": project_root,
                "metabase_url": mb_yml["metabase_url"],
                "base_url": base_url,
                "session": session,
                "database_id": database_id,
            }
        finally:
            if fastapi_started:
                from dango.cli.helpers.process_manager import stop_fastapi_server

                stop_fastapi_server(project_root, verbose=False)
            if docker_manager is not None:
                docker_manager.stop_services()
            if prev_admin_email is None:
                os.environ.pop("DANGO_ADMIN_EMAIL", None)
            else:
                os.environ["DANGO_ADMIN_EMAIL"] = prev_admin_email

    def test_metabase_proxy_serves_valid_js(self, project: dict[str, Any]) -> None:
        """Every JS asset referenced by the real /metabase/ proxy page loads as JS, not HTML.

        This is the exact condition 1.0.8-W's Site URL fix addresses: before
        that fix, Metabase's Site URL pointed asset references at the wrong
        place and the proxy would serve back HTML (e.g. a login/error page)
        for what should have been a JS bundle.
        """
        base_url = project["base_url"]
        session = project["session"]

        root_resp = session.get(f"{base_url}/metabase/", timeout=15)
        assert root_resp.status_code == 200, (
            f"GET /metabase/ (through the real FastAPI proxy) failed: "
            f"{root_resp.status_code} {root_resp.text[:500]}"
        )

        # Metabase's HTML sets <base href="/metabase/"> and then references its
        # own JS bundle with a *relative*, no-leading-slash path (e.g.
        # `src="app/dist/app-main.xxx.js"`) — confirmed by inspecting the real
        # HTML returned here while writing this test. Per <base> resolution
        # rules that's relative to /metabase/, not to the site root, so it
        # must be requested as /metabase/app/... A leading-slash form
        # (/app/...) is also handled, matching the dedicated unprefixed
        # /app/{path} proxy route Metabase's own bundle-internal fetches use.
        raw_asset_paths = sorted(
            set(re.findall(r'(?:src|href)="(/?app/[^"?]+\.js)"', root_resp.text))
        )
        assert raw_asset_paths, (
            "No app/*.js asset references found in the /metabase/ page HTML — "
            f"cannot verify asset content-type. HTML snippet: {root_resp.text[:2000]}"
        )
        asset_paths = [p if p.startswith("/") else f"/metabase/{p}" for p in raw_asset_paths]

        for asset_path in asset_paths:
            asset_resp = session.get(f"{base_url}{asset_path}", timeout=15)
            assert asset_resp.status_code == 200, (
                f"GET {asset_path} (through the real FastAPI proxy) failed: "
                f"{asset_resp.status_code}"
            )
            content_type = asset_resp.headers.get("content-type", "")
            assert content_type.startswith(_ASSET_JS_CONTENT_TYPES), (
                f"GET {asset_path} returned Content-Type {content_type!r}, expected one "
                f"starting with {_ASSET_JS_CONTENT_TYPES!r} — this is the exact bug "
                "1.0.8-W fixed (wrong Metabase Site URL -> asset requests served as HTML)."
            )

    def test_second_source_visible_without_manual_sync(self, project: dict[str, Any]) -> None:
        """A second CSV source's table appears via dango sync alone, no manual sync_schema call.

        This is the exact scenario 1.0.8-X's schema-sync race fix addresses:
        the existing per-PR CI test (test_metabase_duckdb.py) only ever adds a
        single fresh database and can't reach this case.
        """
        project_root: Path = project["root"]
        base_url = project["base_url"]
        session = project["session"]
        database_id = project["database_id"]

        # Bridge a Metabase session on this client (idempotent — safe even if
        # test_metabase_proxy_serves_valid_js already ran first).
        bridge_resp = session.get(f"{base_url}/metabase/", timeout=15)
        assert bridge_resp.status_code == 200

        def _table_names() -> list[str]:
            """Return the current table list, or [] if Metabase isn't answering yet.

            Tolerates non-200 and connection-level errors: `dango sync`'s own
            `refresh_metabase_connection()` does a real `docker restart` of
            the Metabase container as part of its automatic post-sync
            refresh, so a request landing exactly while Metabase is
            mid-restart is an expected, benign race for this poll to ride
            out — not a real failure.
            """
            import requests

            try:
                meta_resp = session.get(
                    f"{base_url}/metabase/api/database/{database_id}/metadata", timeout=15
                )
            except requests.RequestException:
                return []
            if meta_resp.status_code != 200:
                return []
            return [t["name"] for t in meta_resp.json().get("tables", [])]

        def _sync_until_table_visible(
            source_name: str, max_sync_attempts: int = 3, poll_timeout_s: int = 45
        ) -> list[str]:
            """Run `dango sync` for source_name, polling for its table to appear.

            A single `dango sync` call is expected to make the table visible
            through its own automatic post-sync Metabase refresh alone — no
            manual sync_schema call is ever made here, and this loop makes NO
            direct login/POST /api/session calls of its own: only the
            metadata GET below (which reuses this test's already-bridged
            session cookie through the FastAPI proxy, not a fresh Metabase
            login) and whatever `dango sync` triggers internally.

            History: 1.0.8-Q11 (`dango/visualization/metabase.py`,
            `refresh_metabase_connection()`) fixed the actual root cause this
            test exists to guard against — a still-shutting-down old Metabase
            process could answer `/api/health` with 200 for several seconds
            after `docker restart`'s SIGTERM, before a real gap where nothing
            is listening, so `sync_metabase_schema()`'s single login attempt
            right after could fail even though the restart had genuinely
            succeeded. `refresh_metabase_connection()` now waits out that gap
            itself via a bounded internal login-readiness poll.

            An earlier version of *this* test's own retry loop additionally
            polled `POST /api/session` directly (every 2s, up to 45s, before
            each of up to 4 sync retries) to work around the Q11 bug before it
            was fixed. On this machine `docker restart` -> healthy takes
            ~15-20s, so that polling accumulated far more login attempts than
            intended and tripped Metabase's own login-throttle lockout
            (escalating to 1000+ second lockout windows) — a second, separate
            bug in this test file itself, found live-verifying the Q11 fix
            (see BUGS-FOUND.md). With Q11 fixing the actual race, this test no
            longer needs its own login-readiness probing at all: a bounded
            retry of `dango sync` (a legitimate real-world recovery action) is
            enough, and the metadata-poll below never touches Metabase's login
            endpoint.
            """
            names: list[str] = []
            for attempt in range(1, max_sync_attempts + 1):
                result = _sync_source(project_root, source_name)
                assert source_name in result.get("success_sources", []), (
                    f"{source_name} sync (attempt {attempt}) did not report success: {result}"
                )

                deadline = time.monotonic() + poll_timeout_s
                while time.monotonic() < deadline:
                    names = _table_names()
                    if source_name in names:
                        return names
                    time.sleep(3)
            pytest.fail(
                f"Table {source_name!r} never appeared via "
                f"/api/database/{database_id}/metadata after {max_sync_attempts} "
                f"sync attempt(s). Tables seen: {names}"
            )

        # First source: add, sync (retrying sync itself if needed — see
        # _sync_until_table_visible), confirm visible.
        _write_csv_source(project_root, "csvsrc1")
        names_after_first = _sync_until_table_visible("csvsrc1")
        assert "csvsrc1" in names_after_first

        # Second, different source: add, sync — do NOT call sync_schema
        # manually. Only whatever `dango sync` itself triggers automatically
        # (dlt_runner's post-sync refresh_metabase_connection() +
        # sync_metabase_schema()) should make this visible.
        _write_csv_source(project_root, "csvsrc2")
        names_after_second = _sync_until_table_visible("csvsrc2")
        assert "csvsrc2" in names_after_second
