"""Coordinator tests — chiefly, which failures ask the user for credentials.

The distinction matters more than it looks. `ConfigEntryAuthFailed` is what
starts Home Assistant's reauth flow and puts a "reconfigure this integration"
repair in front of the user; `UpdateFailed` retries quietly. Getting it the
wrong way round either nags on every network blip, or leaves an expired API key
silently returning nothing.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scaleway.api import (
    ScalewayApiClient,
    ScalewayApiError,
    ScalewayAuthError,
)
from custom_components.scaleway.const import (
    CONF_ACCESS_KEY,
    CONF_ORGANIZATION_ID,
    CONF_SECRET_KEY,
    DOMAIN,
    REGIONS,
    ZONES,
)
from custom_components.scaleway.coordinator import (
    ScalewayBucketsCoordinator,
    ScalewayCoordinator,
)

ORG = "11111111-1111-4111-8111-111111111111"

COST = {
    "total": 9.45,
    "gross": 9.45,
    "discount": 0.0,
    "currency": "EUR",
    "by_category": {"Object Storage": 9.45},
}


def _entry(hass: HomeAssistant) -> MockConfigEntry:
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
    )
    entry.add_to_hass(hass)
    return entry


def _client() -> AsyncMock:
    client = AsyncMock(spec=ScalewayApiClient)
    client.async_get_cost.return_value = COST
    client.async_list_instances.return_value = [
        {"id": "i1", "name": "web", "zone": "fr-par-1", "commercial_type": "DEV1-S",
         "state": "running"}
    ]
    client.async_list_clusters.return_value = []
    return client


class TestMainCoordinator:
    async def test_a_successful_refresh_shapes_the_data(self, hass: HomeAssistant) -> None:
        coordinator = ScalewayCoordinator(hass, _entry(hass), _client())

        await coordinator.async_refresh()

        assert coordinator.last_update_success is True
        assert coordinator.data["cost"] == COST
        assert [i["id"] for i in coordinator.data["instances"]] == ["i1"]
        assert coordinator.data["clusters"] == []

    async def test_the_configured_organization_is_the_one_queried(
        self, hass: HomeAssistant
    ) -> None:
        client = _client()
        coordinator = ScalewayCoordinator(hass, _entry(hass), client)

        await coordinator.async_refresh()

        client.async_get_cost.assert_awaited_once_with(ORG)
        client.async_list_instances.assert_awaited_once_with(ZONES)
        client.async_list_clusters.assert_awaited_once_with(REGIONS)

    async def test_an_auth_error_asks_the_user_for_new_credentials(
        self, hass: HomeAssistant
    ) -> None:
        """Scaleway keys carry an expiry date and can be revoked; retrying
        forever would never fix either."""
        client = _client()
        client.async_get_cost.side_effect = ScalewayAuthError("401")
        coordinator = ScalewayCoordinator(hass, _entry(hass), client)

        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()

    @pytest.mark.parametrize(
        "exc",
        [
            ScalewayApiError("500"),
            aiohttp.ClientError("reset"),
            TimeoutError("slow"),
        ],
        ids=["api-error", "client-error", "timeout"],
    )
    async def test_a_transient_error_retries_quietly(
        self, hass: HomeAssistant, exc: BaseException
    ) -> None:
        """A blip must not put a credentials prompt in front of the user."""
        client = _client()
        client.async_get_cost.side_effect = exc
        coordinator = ScalewayCoordinator(hass, _entry(hass), client)

        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()


class TestBucketsCoordinator:
    async def test_each_selected_bucket_is_keyed_by_region_and_name(
        self, hass: HomeAssistant
    ) -> None:
        client = _client()
        client.async_get_bucket_size.return_value = {"size_bytes": 42, "object_count": 3}
        coordinator = ScalewayBucketsCoordinator(
            hass,
            _entry(hass),
            client,
            [{"name": "alpha", "region": "nl-ams"}, {"name": "beta", "region": "fr-par"}],
        )

        await coordinator.async_refresh()

        assert set(coordinator.data) == {"nl-ams:alpha", "fr-par:beta"}

    async def test_a_deleted_bucket_is_dropped_rather_than_failing_the_refresh(
        self, hass: HomeAssistant
    ) -> None:
        """Deleting a monitored bucket in the console must not take the other
        buckets' sensors down with it."""
        client = _client()
        client.async_get_bucket_size.side_effect = [
            None,
            {"size_bytes": 7, "object_count": 1},
        ]
        coordinator = ScalewayBucketsCoordinator(
            hass,
            _entry(hass),
            client,
            [{"name": "gone", "region": "nl-ams"}, {"name": "beta", "region": "fr-par"}],
        )

        await coordinator.async_refresh()

        assert coordinator.last_update_success is True
        assert set(coordinator.data) == {"fr-par:beta"}

    async def test_no_selected_buckets_is_an_empty_refresh_not_an_error(
        self, hass: HomeAssistant
    ) -> None:
        coordinator = ScalewayBucketsCoordinator(hass, _entry(hass), _client(), [])

        await coordinator.async_refresh()

        assert coordinator.data == {}

    async def test_an_auth_error_also_asks_for_credentials(self, hass: HomeAssistant) -> None:
        """The same key signs S3 requests, so a revoked key breaks both halves."""
        client = _client()
        client.async_get_bucket_size.side_effect = ScalewayAuthError("403")
        coordinator = ScalewayBucketsCoordinator(
            hass, _entry(hass), client, [{"name": "alpha", "region": "nl-ams"}]
        )

        with pytest.raises(ConfigEntryAuthFailed):
            await coordinator._async_update_data()

    async def test_a_transient_error_retries_quietly(self, hass: HomeAssistant) -> None:
        client = _client()
        client.async_get_bucket_size.side_effect = TimeoutError("slow")
        coordinator = ScalewayBucketsCoordinator(
            hass, _entry(hass), client, [{"name": "alpha", "region": "nl-ams"}]
        )

        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
