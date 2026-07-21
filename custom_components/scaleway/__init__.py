"""The Scaleway integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ScalewayApiClient
from .const import CONF_ACCESS_KEY, CONF_BUCKETS, CONF_SECRET_KEY, DATA_BACKUP_AGENT_LISTENERS, DOMAIN
from .coordinator import ScalewayBucketsCoordinator, ScalewayCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Scaleway from a config entry (one per Organization)."""
    session = async_get_clientsession(hass)
    client = ScalewayApiClient(session, entry.data[CONF_ACCESS_KEY], entry.data[CONF_SECRET_KEY])

    coordinator = ScalewayCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    buckets = entry.options.get(CONF_BUCKETS, [])
    buckets_coordinator = ScalewayBucketsCoordinator(hass, entry, client, buckets)
    if buckets:
        await buckets_coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "main": coordinator,
        "buckets": buckets_coordinator,
    }
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # The backup platform's agent list is derived from loaded config entries
    # (see backup.py's async_get_backup_agents) — nudge it to re-derive that
    # list whenever this entry's state changes (e.g. reload after the backup
    # bucket is changed in options), rather than only at HA startup.
    def _notify_backup_listeners() -> None:
        for listener in hass.data.get(DATA_BACKUP_AGENT_LISTENERS, []):
            listener()

    entry.async_on_unload(entry.async_on_state_change(_notify_backup_listeners))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options (bucket selection) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded
