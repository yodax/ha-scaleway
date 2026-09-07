"""DataUpdateCoordinators for the Scaleway integration."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import timedelta

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
    MAX_POLL_OFFSET,
    REGIONS,
    ZONES,
)

_LOGGER = logging.getLogger(__name__)


def poll_offset(seed: str) -> timedelta:
    """This install's fixed offset into the poll interval, in [0, MAX_POLL_OFFSET).

    This integration is published through HACS, so without an offset every
    install polls Scaleway on the same schedule relative to its own start.
    Spreading them costs nothing and is simply good manners toward an API we
    don't own.

    Two properties matter, and they pull in opposite directions:

    - **Stable across restarts**, so the schedule stays predictable and
      debuggable, and so a restart loop cannot walk the poll time around the
      hour.
    - **Different across installs**, which is the entire point.

    Hence a hash of the config entry id — and **`hashlib`, not the builtin
    `hash()`**. Python salts string hashing per process (PYTHONHASHSEED), so
    `hash()` looks perfectly deterministic inside any one process and silently
    re-rolls the offset on every Home Assistant restart. Only a cross-process
    test catches that; see tests/test_schedule.py.

    The seed is the **entry id**, not the organization id: entry ids are random
    ULIDs, so two Home Assistant instances watching the same Scaleway
    organization still get different offsets, which is the spreading we want.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / 2**64
    return timedelta(seconds=int(MAX_POLL_OFFSET.total_seconds() * fraction))


class _OffsetPollMixin:
    """Phase-shifts a coordinator's poll schedule by this install's offset.

    DataUpdateCoordinator polls immediately at setup and then every
    `update_interval`. Starting with `interval + offset` and settling onto
    `interval` once that first scheduled poll has happened shifts the whole
    subsequent grid by the offset, without changing the period. The alternative
    — leaving `interval + offset` in place forever — would make the advertised
    "hourly" anything from 60 to 75 minutes depending on the install.

    The switch happens on the *second* call to `_async_update_data`, because
    the first is the immediate setup refresh, which is not on the timer. A
    manual refresh in between collapses the offset early; that costs the
    install nothing but its phase, and the moment of a manual refresh is itself
    unsynchronised across installs, so the spreading survives.
    """

    _base_update_interval: timedelta

    def _init_offset_schedule(self, base: timedelta, seed: str) -> timedelta:
        self._base_update_interval = base
        self._poll_count = 0
        return base + poll_offset(seed)

    def _settle_offset_schedule(self) -> None:
        """Call at the top of `_async_update_data`."""
        self._poll_count += 1
        if self._poll_count == 2:
            # A plain attribute assignment on the coordinator; the setter does
            # not reschedule, so the new value is simply picked up when the
            # refresh finishes and HA arms the next timer.
            self.update_interval = self._base_update_interval


class ScalewayCoordinator(_OffsetPollMixin, DataUpdateCoordinator[dict]):
    """Polls account-level cost, Instance, and Kubernetes status."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: ScalewayApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            # Passed explicitly rather than left to HA's `current_entry`
            # ContextVar: that fallback is deprecated, and it only happens to
            # be populated during async_setup_entry.
            config_entry=entry,
            name=f"{DOMAIN} ({entry.title})",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.update_interval = self._init_offset_schedule(
            DEFAULT_SCAN_INTERVAL, entry.entry_id
        )
        self.client = client
        self.entry = entry

    async def _async_update_data(self) -> dict:
        self._settle_offset_schedule()
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


class ScalewayBucketsCoordinator(_OffsetPollMixin, DataUpdateCoordinator[dict]):
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
            config_entry=entry,
            name=f"{DOMAIN} buckets ({entry.title})",
            update_interval=BUCKET_SCAN_INTERVAL,
        )
        # Same offset as the main coordinator: one config entry, one phase. Two
        # separately derived offsets would spread this install's own two polls
        # apart, which buys nothing — the spreading that matters is between
        # installs.
        self.update_interval = self._init_offset_schedule(BUCKET_SCAN_INTERVAL, entry.entry_id)
        self.client = client
        self.buckets = buckets

    async def _async_update_data(self) -> dict:
        self._settle_offset_schedule()
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
