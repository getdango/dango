"""tests/factories/sdk_fakes.py

Stand-in modules for third-party SDKs the vendored connectors import (Google Analytics/Ads, Facebook Business),
so a connector imports and runs the same on every machine, with or without the real SDK installed.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class _AutoModule(types.ModuleType):
    """A module whose unknown public attributes are empty placeholder classes (names only imported/annotated)."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.__path__: list[str] = []  # behave as a package so submodules can be imported

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        placeholder = type(name, (), {})
        setattr(self, name, placeholder)
        return placeholder


@contextmanager
def fake_sdk(modules: dict[str, dict[str, Any]], connector_prefix: str) -> Iterator[None]:
    """Install fake modules (dotted name -> explicit attributes), and import `connector_prefix` against them.

    Everything under `connector_prefix` is dropped from `sys.modules` on entry and exit, so the connector is
    imported fresh inside the block and never leaks a fake-bound copy to other tests.
    """
    touched = [
        k for k in sys.modules if k == connector_prefix or k.startswith(connector_prefix + ".")
    ]
    saved = {k: sys.modules.pop(k) for k in touched}
    previous: dict[str, Any] = {}
    names = set(modules)
    for dotted in list(names):
        parts = dotted.split(".")
        names.update(".".join(parts[:i]) for i in range(1, len(parts)))
    try:
        for dotted in sorted(names, key=lambda n: n.count(".")):
            previous[dotted] = sys.modules.get(dotted)
            if dotted == "google" and previous[dotted] is not None:
                continue  # keep the real namespace package, add the fake subpackages under it
            module = _AutoModule(dotted)
            for attr, value in modules.get(dotted, {}).items():
                setattr(module, attr, value)
            sys.modules[dotted] = module
        yield
    finally:
        for key in [
            k for k in sys.modules if k == connector_prefix or k.startswith(connector_prefix + ".")
        ]:
            del sys.modules[key]
        for dotted, old in previous.items():
            if old is None:
                sys.modules.pop(dotted, None)
            else:
                sys.modules[dotted] = old
        sys.modules.update(saved)
