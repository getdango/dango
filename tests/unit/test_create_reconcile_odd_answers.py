"""tests/unit/test_create_reconcile_odd_answers.py

After a lost droplet-create answer, a name lookup that answers 200 with a non-JSON or oddly shaped body
still ends in the "may exist; check the DigitalOcean console" error, and only the error type is logged.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from dango.exceptions import CloudError
from dango.platform.cloud.digitalocean import DigitalOceanClient

pytestmark = pytest.mark.unit

_REAL_HTTPX_CLIENT = httpx.Client
_BODY: dict[str, Any] = {"name": "dango-proj", "region": "sgp1", "size": "s", "image": "ubuntu"}

_ODD_ANSWERS = {
    "html": httpx.Response(200, text="<html>captive portal</html>"),
    "empty": httpx.Response(200, content=b""),
    "list": httpx.Response(200, json=[{"name": "dango-proj"}]),
    "null": httpx.Response(200, json=None),
    "string": httpx.Response(200, json="ok"),
    "droplets-null": httpx.Response(200, json={"droplets": None}),
    "droplets-string": httpx.Response(200, json={"droplets": "abc"}),
    "droplets-numbers": httpx.Response(200, json={"droplets": [1, 2]}),
}


@pytest.fixture
def lookup_answer(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The create POST answers 502; every droplet lookup answers with the chosen response."""

    def install(answer: httpx.Response) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(502, json={"message": "upstream error"})
            return answer

        def factory(*_args: Any, **kwargs: Any) -> httpx.Client:
            return _REAL_HTTPX_CLIENT(
                transport=httpx.MockTransport(handle), timeout=kwargs.get("timeout")
            )

        monkeypatch.setattr("dango.platform.cloud.digitalocean.httpx.Client", factory)

    monkeypatch.setattr("dango.platform.cloud.digitalocean.time.sleep", lambda _s: None)
    monkeypatch.setattr("dango.platform.cloud.create_reconcile.time.sleep", lambda _s: None)
    return install


@pytest.mark.parametrize("name", list(_ODD_ANSWERS))
def test_an_odd_lookup_answer_still_says_the_droplet_may_exist(
    lookup_answer: Any, name: str
) -> None:
    lookup_answer(_ODD_ANSWERS[name])
    with pytest.raises(CloudError, match=r"may exist; check the DigitalOcean console"):
        DigitalOceanClient(token="test-token").create_droplet(**_BODY)


def test_only_the_exception_type_is_logged(lookup_answer: Any) -> None:
    lookup_answer(httpx.Response(200, text="<html>secret-looking body</html>"))
    with patch("dango.platform.cloud.create_reconcile._logger") as logger:
        with pytest.raises(CloudError):
            DigitalOceanClient(token="test-token").create_droplet(**_BODY)
    logger.warning.assert_called_once_with(
        "create_lookup_failed", kind="droplet", error_type="JSONDecodeError"
    )
