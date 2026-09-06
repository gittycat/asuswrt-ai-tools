"""Fixtures.

The test doubles themselves live in helpers.py so that there is exactly one
definition of FakeRouter no matter how pytest imports these modules.
"""

from __future__ import annotations

import os

import pytest

from asusrouter import AsusData
from asusrouter.modules.port_forwarding import AsusPortForwarding, PortForwardingRule

from helpers import FakeRouter, default_data


@pytest.fixture(autouse=True)
def _isolate_router_env():
    """Undo what load_dotenv() does to os.environ.

    monkeypatch cannot restore it: load_dotenv writes the variables itself,
    so a test that loads a .env leaks its values into every test that runs
    after it.
    """
    from asuswrt.router import ROUTER_ENV_NAMES

    saved = {name: os.environ.get(name) for name in ROUTER_ENV_NAMES}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture
def router() -> FakeRouter:
    return FakeRouter()


@pytest.fixture
def pf_router() -> FakeRouter:
    """A router that already has port forwarding rules.

    `pf remove` connects and looks for a match before it checks --confirm, so
    a dry run of it needs a rule to find.
    """
    data = default_data()
    data[AsusData.PORT_FORWARDING] = {
        "state": AsusPortForwarding.ON,
        "rules": [
            PortForwardingRule(
                name="Plex",
                ip_address="192.168.50.20",
                port="32400",
                protocol="TCP",
                ip_external="",
                port_external="32400",
            ),
            PortForwardingRule(
                name="Web",
                ip_address="192.168.50.30",
                port="80",
                protocol="TCP",
                ip_external="",
                port_external="8080",
            ),
        ],
    }
    return FakeRouter(data=data)


@pytest.fixture
def router_that_ignores_writes() -> FakeRouter:
    """Accepts every write and keeps the old value — the country-code case."""
    return FakeRouter(apply_writes=False)
