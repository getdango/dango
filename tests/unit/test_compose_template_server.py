"""tests/unit/test_compose_template_server.py

Local compose render stays byte-identical; the server render is safe and keeps labels.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from jinja2 import Environment, PackageLoader

from dango.platform.cloud.server_compose import Layout, RenderError, quote_name, render_compose

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PIN_ID = "abcd1234-0000-4000-8000-000000000000"


def local_render(name: str, ident: str = PIN_ID, mb: int = 3000, docs: int = 8081) -> str:
    template = Environment(loader=PackageLoader("dango", "templates")).get_template(
        "docker-compose.yml.j2"
    )
    return template.render(
        project_name=name, project_id=ident, metabase_port=mb, dbt_docs_port=docs
    )


def server_render(name: str, ident: str = PIN_ID, layout: Layout | None = None) -> dict[str, Any]:
    text = render_compose(layout or Layout(), 3000, 8081, quote_name(name), f'"{ident}"')
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)
    return parsed


def test_local_render_is_byte_identical_to_the_previous_output() -> None:
    plain = (FIXTURES / "docker_compose_local_plain.golden.yml").read_text(encoding="utf-8")
    odd = (FIXTURES / "docker_compose_local_odd.golden.yml").read_text(encoding="utf-8")
    # The fixtures end with a newline the template does not emit.
    assert local_render("my-project") + "\n" == plain
    assert local_render("café &<>'#:x", "i-1", 13000, 18081) + "\n" == odd


def test_server_render_uses_absolute_long_bind_syntax_and_nothing_else() -> None:
    layout = Layout()
    data = server_render("my-project", layout=layout)
    mb, docs = data["services"]["metabase"], data["services"]["dbt-docs"]
    assert mb["build"] == {
        "context": "/srv/dango/compose/build",
        "dockerfile": "Dockerfile.metabase",
    }
    binds = [v for v in mb["volumes"] if isinstance(v, dict)]
    assert mb["volumes"][0] == "metabase-data:/metabase-data"
    assert binds == [
        {
            "type": "bind",
            "source": "/srv/dango/project/data",
            "target": "/data",
            "read_only": True,
            "bind": {"create_host_path": False},
        },
        {
            "type": "bind",
            "source": "/srv/dango/metabase/plugins",
            "target": "/app/plugins",
            "bind": {"create_host_path": False},
        },
    ]
    assert len(mb["volumes"]) == 3
    assert docs["volumes"] == [
        {
            "type": "bind",
            "source": "/srv/dango/project/dbt/target",
            "target": "/usr/share/nginx/html",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
    forbidden = {
        "privileged",
        "cap_add",
        "devices",
        "network_mode",
        "pid",
        "security_opt",
        "volumes_from",
        "ipc",
    }
    for service in (mb, docs):
        assert not forbidden & set(service)
    assert set(data["services"]) == {"metabase", "dbt-docs"} and set(data) == {
        "services",
        "volumes",
    }
    assert mb["ports"] == ["127.0.0.1:3000:3000"] and docs["ports"] == ["127.0.0.1:8081:80"]


@pytest.mark.parametrize(
    "name",
    ['a"\n    privileged: true', 'a"b', "a\\b", "a\nb", "a\tb", "a b", "a\x00b", "x" * 201, ""],
)
def test_unquotable_names_are_refused(name: str) -> None:
    with pytest.raises(RenderError):
        quote_name(name)


CORPUS = [
    "my-project",
    "café-数据",
    "a&b<c>d'e",
    "yes",
    "123",
    " leading space",
    "#hash",
    "a:b",
    "{braces}",
    "key: value",
    "- dash",
    "100%",
    "tilde~",
    "a  b",
]


@pytest.mark.parametrize("name", CORPUS)
def test_labels_and_site_name_match_the_old_render_for_every_parsable_name(name: str) -> None:
    old = yaml.safe_load(local_render(name))
    new = server_render(name)
    old_labels = old["volumes"]["metabase-data"]["labels"]
    new_labels = new["volumes"]["metabase-data"]["labels"]
    assert (
        new_labels == old_labels == {"com.dango.project_name": name, "com.dango.project_id": PIN_ID}
    )
    assert (
        new["services"]["metabase"]["environment"]["MB_SITE_NAME"]
        == old["services"]["metabase"]["environment"]["MB_SITE_NAME"]
        == name
    )
    assert new["volumes"]["metabase-data"]["driver"] == "local"
