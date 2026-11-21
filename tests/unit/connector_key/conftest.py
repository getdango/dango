"""tests/unit/connector_key/conftest.py

Blocks the network for every connector key-contract test: a connection or lookup attempt fails the test with its target.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.factories.network_guard import blocked_network


@pytest.fixture(autouse=True)
def network_attempts() -> Iterator[list[str]]:
    """The blocked targets of this test; a test that provokes one on purpose clears the list before it ends."""
    with blocked_network() as attempts:
        yield attempts
    assert attempts == [], f"the test reached for the network: {attempts}"
