"""End-to-end setup/unload tests against real Home Assistant machinery.

These go through `hass.config_entries.async_setup` rather than constructing
coordinators directly, because the behaviours that matter here — a reauth flow
appearing, an entry reloading when its options change, sensors existing at all —
are all produced by HA, not by this integration in isolation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scaleway import async_unload_entry
from custom_components.scaleway.api import (
    ScalewayApiClient,
    ScalewayApiError,
    ScalewayAuthError,
)
from custom_components.scaleway.const import (
    CONF_ACCESS_KEY,
    CONF_BUCKETS,
    CONF_ORGANIZATION_ID,
    CONF_SECRET_KEY,
    DOMAIN,
)

ORG = "11111111-1111-4111-8111-111111111111"

COST = {
    "total": 9.45,
    "gross": 9.45,
    "discount": 0.0,
    "currency": "EUR",
    "by_category": {"Object Storage": 9.45},
}


def _client() -> AsyncMock:
    client = AsyncMock(spec=ScalewayApiClient)
    client.async_get_cost.return_value = COST
    client.async_list_instances.return_value = []
    client.async_list_clusters.return_value = []
    client.async_get_bucket_size.return_value = {"size_bytes": 5_000_000_000, "object_count": 3}
    return client


@pytest.fixture
def api() -> AsyncMock:
    client = _client()
    with patch(
        "custom_components.scaleway.ScalewayApiClient", return_value=client
    ):
        yield client


def _entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Scaleway (11111111)",
        entry_id="01KY2Q7SV1FY74NVS14SR6HG62",
        unique_id=ORG,
        data={
            CONF_ACCESS_KEY: "SCWXXXXXXXXXXXXXXXXX",
            CONF_SECRET_KEY: "00000000-0000-4000-8000-000000000000",
            CONF_ORGANIZATION_ID: ORG,
        },
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return result


async def test_setup_creates_the_cost_sensors(hass: HomeAssistant, api: AsyncMock) -> None:
    entry = _entry(hass)

    assert await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    state = hass.states.get("sensor.scaleway_11111111_cost_this_period_excl_vat")
    assert state is not None
    assert state.state == "9.45"
    assert state.attributes["unit_of_measurement"] == "EUR"


async def test_the_total_sensor_exposes_gross_and_discount(
    hass: HomeAssistant, api: AsyncMock
) -> None:
    """With a discount live, the state is net and the parts are visible.

    Reproduces the real shape found on the verification account: gross 13.16,
    a 25% organization-wide discount of 3.29, invoiced 9.87.
    """
    api.async_get_cost.return_value = {
        "total": 9.87,
        "gross": 13.16,
        "discount": 3.29,
        "currency": "EUR",
        "by_category": {"Compute": 13.16},
    }
    entry = _entry(hass)

    assert await _setup(hass, entry)

    state = hass.states.get("sensor.scaleway_11111111_cost_this_period_excl_vat")
    assert state.state == "9.87"
    assert state.attributes["gross"] == 13.16
    assert state.attributes["discount"] == 3.29
    assert state.attributes["excludes_vat"] is True


async def test_the_category_sensor_stays_gross(hass: HomeAssistant, api: AsyncMock) -> None:
    """Deliberately not prorated — so it does not sum to the total."""
    api.async_get_cost.return_value = {
        "total": 9.87,
        "gross": 13.16,
        "discount": 3.29,
        "currency": "EUR",
        "by_category": {"Compute": 13.16},
    }
    entry = _entry(hass)

    assert await _setup(hass, entry)

    assert hass.states.get("sensor.scaleway_11111111_cost_compute").state == "13.16"


async def test_bucket_sensors_appear_only_for_selected_buckets(
    hass: HomeAssistant, api: AsyncMock
) -> None:
    entry = _entry(hass, options={CONF_BUCKETS: [{"name": "alpha", "region": "nl-ams"}]})

    assert await _setup(hass, entry)

    # Reported in bytes, displayed in gigabytes: buckets here run to several GB
    # and a raw byte count is unreadable on a dashboard.
    size = hass.states.get("sensor.alpha_nl_ams_size")
    assert float(size.state) == 5.0
    assert size.attributes["unit_of_measurement"] == "GB"
    assert hass.states.get("sensor.alpha_nl_ams_object_count").state == "3"


async def test_an_expired_key_starts_exactly_one_reauth_flow(
    hass: HomeAssistant, api: AsyncMock
) -> None:
    """The key carries an expiry date, so this is a when, not an if."""
    api.async_get_cost.side_effect = ScalewayAuthError("401")
    entry = _entry(hass)

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = [
        f
        for f in hass.config_entries.flow.async_progress()
        if f["handler"] == DOMAIN and f["context"]["source"] == "reauth"
    ]
    assert len(flows) == 1


async def test_a_transient_error_retries_instead_of_asking_for_credentials(
    hass: HomeAssistant, api: AsyncMock
) -> None:
    """A network blip must not put a credentials prompt in front of the user."""
    api.async_get_cost.side_effect = ScalewayApiError("500")
    entry = _entry(hass)

    await _setup(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.config_entries.flow.async_progress() == []


async def test_unload_removes_the_entry_data(hass: HomeAssistant, api: AsyncMock) -> None:
    entry = _entry(hass)
    await _setup(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_ha_does_not_unload_an_entry_that_never_loaded(
    hass: HomeAssistant, api: AsyncMock
) -> None:
    """Pins the HA behaviour that bounds `async_unload_entry`'s contract.

    `async_unload_entry` uses `hass.data.get(DOMAIN, {}).pop(..., None)` rather
    than a bare `hass.data[DOMAIN].pop(...)`. An earlier version of this test
    claimed that guard was load-bearing on the failed-setup path. It is not:
    `ConfigEntry.async_unload` returns early for any entry that is not LOADED
    (2026.9.1, `if self.state is not ConfigEntryState.LOADED: ... return True`),
    so the integration's unload is never called and the KeyError is
    unreachable that way. The guard stays anyway — an exception here turns the
    entry FAILED_UNLOAD and blocks the reloads that options changes and
    credential swaps depend on — but the claim had to go.

    This asserts the real behaviour, so that if HA ever starts calling unload
    on a failed entry, the assumption breaks loudly here.
    """
    api.async_get_cost.side_effect = ScalewayAuthError("401")
    entry = _entry(hass)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_ERROR

    with patch(
        "custom_components.scaleway.async_unload_entry", wraps=async_unload_entry
    ) as unload:
        assert await hass.config_entries.async_unload(entry.entry_id)

    assert unload.await_count == 0
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_unload_is_idempotent_for_a_loaded_entry(
    hass: HomeAssistant, api: AsyncMock
) -> None:
    """The path that IS reachable: a loaded entry, unloaded twice."""
    entry = _entry(hass)
    await _setup(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)

    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_changing_options_reloads_the_entry(hass: HomeAssistant, api: AsyncMock) -> None:
    """Bucket selection only takes effect through a reload."""
    entry = _entry(hass)
    await _setup(hass, entry)
    assert hass.states.get("sensor.alpha_nl_ams_size") is None

    hass.config_entries.async_update_entry(
        entry, options={CONF_BUCKETS: [{"name": "alpha", "region": "nl-ams"}]}
    )
    await hass.async_block_till_done()

    assert float(hass.states.get("sensor.alpha_nl_ams_size").state) == 5.0
