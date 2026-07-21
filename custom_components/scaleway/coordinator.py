"""DataUpdateCoordinators for the Scaleway integration."""
from __future__ import annotations

import asyncio
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ScalewayApiClient, ScalewayApiError, ScalewayAuthError
from .const import (
    BUCKET_SCAN_INTERVAL,
    CONF_ORGANIZATION_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    REGIONS,
    ZONES,
)

_LOGGER = logging.getLogger(__name__)


class ScalewayCoordinator(DataUpdateCoordinator[dict]):
    """Polls account-level cost, Instance, and Kubernetes status."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: ScalewayApiClient) -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN} ({entry.title})", update_interval=DEFAULT_SCAN_INTERVAL
        )
        self.client = client
        self.entry = entry

    async def _async_update_data(self) -> dict:
        organization_id = self.entry.data[CONF_ORGANIZATION_ID]
        try:
            cost = await self.client.async_get_cost(organization_id)
            instances = await self.client.async_list_instances(ZONES)
            clusters = await self.client.async_list_clusters(REGIONS)
        except ScalewayAuthError as err:
            # ConfigEntryAuthFailed (not UpdateFailed) is what makes HA raise
            # the "reconfigure this integration" repair and start the reauth
            # flow — a revoked or expired Scaleway key is never going to fix
            # itself by retrying.
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except (ScalewayApiError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Error communicating with Scaleway: {err}") from err
        return {"cost": cost, "instances": instances, "clusters": clusters}


class ScalewayBucketsCoordinator(DataUpdateCoordinator[dict]):
    """Polls Object Storage bucket size/object count for opted-in buckets.

    Kept as a separate, much-less-frequent coordinator from
    ScalewayCoordinator because sizing a bucket means paginating every
    object in it: Scaleway exposes no bucket-size endpoint, so there is no
    cheaper way to get this.
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: ScalewayApiClient, buckets: list[dict]
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} buckets ({entry.title})",
            update_interval=BUCKET_SCAN_INTERVAL,
        )
        self.client = client
        self.buckets = buckets

    async def _async_update_data(self) -> dict:
        data: dict[str, dict] = {}
        try:
            for bucket in self.buckets:
                size = await self.client.async_get_bucket_size(bucket["region"], bucket["name"])
                if size is not None:
                    data[f"{bucket['region']}:{bucket['name']}"] = size
        except ScalewayAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except (ScalewayApiError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Error communicating with Scaleway Object Storage: {err}") from err
        return data
