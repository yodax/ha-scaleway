"""Shared fixtures — pulls in Home Assistant's own test harness."""
from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Make custom_components/scaleway loadable as a real HA integration."""
