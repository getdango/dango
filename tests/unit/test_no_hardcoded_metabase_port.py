"""tests/unit/test_no_hardcoded_metabase_port.py

Architecture test (1.0.13-T5 / C2): every ``localhost:3000`` / ``127.0.0.1:3000`` in ``dango/``
is pinned to a classified allowlist. Host-side code must derive the Metabase URL from
``resolve_metabase_url`` or the configured ``metabase_port``. Do not add an entry to make a
failure pass: a new entry needs a classification and the coordinator's agreement.
"""

import re
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parents[2] / "dango"
_PATTERN = re.compile(r"(localhost|127\.0\.0\.1):3000")
_SUFFIXES = {".py", ".js", ".html", ".j2", ".sh", ".yml", ".yaml", ".md", ".toml", ".json"}

_FALLBACK = (
    "fallback when metadata has no metabase_url (metabase.yml always carries it once set up)"
)
_CLOUD = "runs on the cloud server host; cloud projects use the template's default mapping"

# repo-relative path -> (exact expected count, reason)
_ALLOWED: dict[str, tuple[int, str]] = {
    "cli/commands/dashboard.py": (1, "comment only"),
    "cli/commands/deploy_provision.py": (1, _CLOUD),
    "cli/commands/metabase_cmd.py": (1, _FALLBACK),
    "cli/commands/remote_repair.py": (4, _CLOUD),
    "config/models.py": (1, "the metabase_port field default and description"),
    "platform/common/metabase_admin_repair.py": (1, _FALLBACK),
    "platform/common/metabase_credential_migration.py": (1, _FALLBACK),
    "platform/common/metabase_link.py": (1, _FALLBACK),
    "platform/common/startup.py": (2, "a comment and a fallback when metadata has no URL"),
    "templates/docker-compose.yml.j2": (
        1,
        "container-internal healthcheck; 3000 is fixed in-container",
    ),
    "visualization/metabase.py": (
        13,
        "parameter defaults/docstrings/fallbacks; every in-repo caller passes a URL or reads metabase.yml",
    ),
    "web/routes/config.py": (1, "load-failure fallback response"),
    "web/routes/metabase_proxy.py": (1, _FALLBACK),
    "web/static/js/app.js": (
        2,
        "dead code: openMetabase and its credentials form (no backing route)",
    ),
}


def _scan() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in _PACKAGE.rglob("*"):
        if not path.is_file() or not (
            path.suffix in _SUFFIXES or path.name.startswith("Dockerfile")
        ):
            continue
        if path.name.endswith(".min.js") or path.name.startswith("tailwind"):
            continue
        if "__pycache__" in path.parts:
            continue
        hits = len(_PATTERN.findall(path.read_text(encoding="utf-8", errors="ignore")))
        if hits:
            counts[path.relative_to(_PACKAGE).as_posix()] = hits
    return counts


def test_no_unclassified_hardcoded_metabase_port():
    counts = _scan()
    unlisted = {f: n for f, n in counts.items() if f not in _ALLOWED}
    assert not unlisted, (
        f"hard-coded Metabase port 3000 in non-allowlisted files: {unlisted}. "
        "Use resolve_metabase_url(project_root) or the configured metabase_port."
    )
    changed = {
        f: (counts[f], _ALLOWED[f][0])
        for f in counts
        if f in _ALLOWED and counts[f] != _ALLOWED[f][0]
    }
    assert not changed, f"hit count differs from pinned count (actual, pinned): {changed}"
    stale = sorted(f for f in _ALLOWED if f not in counts)
    assert not stale, f"allowlist entries with no remaining hits: {stale}"
