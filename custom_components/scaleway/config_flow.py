"""Config flow for the Scaleway integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import ScalewayApiClient, ScalewayApiError, ScalewayAuthError
from .const import (
    CONF_ACCESS_KEY,
    CONF_BACKUP_BUCKET,
    CONF_BACKUP_PREFIX,
    CONF_BUCKETS,
    CONF_ORGANIZATION_ID,
    CONF_SECRET_KEY,
    DOMAIN,
    REGIONS,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_KEY): str,
        vol.Required(CONF_SECRET_KEY): TextSelector(
            config=TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

_NO_BACKUP_BUCKET = ""


def _bucket_key(bucket: dict) -> str:
    return f"{bucket['region']}:{bucket['name']}"


def _bucket_choices(buckets: list[dict]) -> dict[str, str]:
    return {_bucket_key(b): f"{b['name']} ({b['region']})" for b in buckets}


def _backup_bucket_choices(buckets: list[dict]) -> dict[str, str]:
    return {_NO_BACKUP_BUCKET: "None", **_bucket_choices(buckets)}


def _bucket_from_key(buckets: list[dict], key: str | None) -> dict | None:
    if not key:
        return None
    return next((b for b in buckets if _bucket_key(b) == key), None)


def _buckets_schema(available_buckets: list[dict], defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_BUCKETS, default=defaults.get(CONF_BUCKETS, [])): cv.multi_select(
                _bucket_choices(available_buckets)
            ),
            vol.Optional(
                CONF_BACKUP_BUCKET, default=defaults.get(CONF_BACKUP_BUCKET, _NO_BACKUP_BUCKET)
            ): vol.In(_backup_bucket_choices(available_buckets)),
            vol.Optional(CONF_BACKUP_PREFIX, default=defaults.get(CONF_BACKUP_PREFIX, "")): cv.string,
        }
    )


class ScalewayConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Scaleway — one entry per Organization."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._available_buckets: list[dict] = []

    async def _async_resolve_organization(
        self, access_key: str, secret_key: str
    ) -> tuple[str | None, str | None]:
        """Validate a key pair. Returns (organization_id, error_key)."""
        client = ScalewayApiClient(async_get_clientsession(self.hass), access_key, secret_key)
        try:
            return await client.async_resolve_organization_id(), None
        except ScalewayAuthError:
            return None, "invalid_auth"
        except ScalewayApiError:
            return None, "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error validating Scaleway API key")
            return None, "unknown"

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}

        if user_input is not None:
            organization_id, error = await self._async_resolve_organization(
                user_input[CONF_ACCESS_KEY], user_input[CONF_SECRET_KEY]
            )
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(organization_id)
                self._abort_if_unique_id_configured()

                self._data = {
                    CONF_ACCESS_KEY: user_input[CONF_ACCESS_KEY],
                    CONF_SECRET_KEY: user_input[CONF_SECRET_KEY],
                    CONF_ORGANIZATION_ID: organization_id,
                }
                client = ScalewayApiClient(
                    async_get_clientsession(self.hass),
                    user_input[CONF_ACCESS_KEY],
                    user_input[CONF_SECRET_KEY],
                )
                try:
                    self._available_buckets = await client.async_list_buckets(REGIONS)
                except ScalewayApiError:
                    self._available_buckets = []
                return await self.async_step_buckets()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start re-authentication when the stored API key stops working.

        Scaleway keys can be revoked or hit their expiry date (they carry
        one), and rotating a key produces a *new* access/secret pair — so
        this asks for both, not just the secret.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect and validate a replacement API key for an existing entry."""
        return await self._async_step_credentials(
            "reauth_confirm", self._get_reauth_entry(), user_input
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Swap in a new API key on demand, without waiting for one to break.

        HA only ever *starts* a reauth flow in response to an auth failure,
        so without this there'd be no way to rotate a key proactively —
        and deleting/re-adding the entry is not an equivalent workaround
        here: every sensor's unique_id and the backup agent's agent_id are
        derived from entry_id, so a fresh entry renames every entity and
        drops the configured backup location.
        """
        return await self._async_step_credentials(
            "reconfigure", self._get_reconfigure_entry(), user_input
        )

    async def _async_step_credentials(
        self,
        step_id: str,
        entry: ConfigEntry,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Shared credential-replacement step for reauth and reconfigure.

        Both update the *existing* entry in place; the only difference is
        what prompted them, which HA reflects in the abort reason.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            organization_id, error = await self._async_resolve_organization(
                user_input[CONF_ACCESS_KEY], user_input[CONF_SECRET_KEY]
            )
            if error:
                errors["base"] = error
            else:
                # The replacement key must belong to the same Organization.
                # Updating an entry in place to point at a different account
                # would silently repoint its sensors — and its backup agent,
                # whose agent_id is derived from this entry — at someone
                # else's data, while HA still shows the old backups.
                await self.async_set_unique_id(organization_id)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_ACCESS_KEY: user_input[CONF_ACCESS_KEY],
                        CONF_SECRET_KEY: user_input[CONF_SECRET_KEY],
                    },
                )

        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA, {CONF_ACCESS_KEY: entry.data.get(CONF_ACCESS_KEY)}
            ),
            errors=errors,
            description_placeholders={
                "organization_id": entry.data.get(CONF_ORGANIZATION_ID, "")
            },
        )

    async def async_step_buckets(self, user_input: dict[str, Any] | None = None):
        # Sizing a bucket means listing every object in it, and a backup
        # destination needs somewhere to write to — both are opt-in rather
        # than auto-selected, so skip the step if there's nothing to choose
        # from at all.
        if not self._available_buckets:
            return self._create_entry([], _NO_BACKUP_BUCKET, "")

        if user_input is not None:
            return self._create_entry(
                user_input.get(CONF_BUCKETS, []),
                user_input.get(CONF_BACKUP_BUCKET, _NO_BACKUP_BUCKET),
                user_input.get(CONF_BACKUP_PREFIX, ""),
            )

        schema = _buckets_schema(self._available_buckets, {})
        return self.async_show_form(step_id="buckets", data_schema=schema)

    def _create_entry(self, selected_keys: list[str], backup_key: str, backup_prefix: str):
        buckets = [b for b in self._available_buckets if _bucket_key(b) in selected_keys]
        options: dict[str, Any] = {CONF_BUCKETS: buckets}
        backup_bucket = _bucket_from_key(self._available_buckets, backup_key)
        if backup_bucket:
            options[CONF_BACKUP_BUCKET] = backup_bucket
            options[CONF_BACKUP_PREFIX] = backup_prefix
        return self.async_create_entry(
            title=f"Scaleway ({self._data[CONF_ORGANIZATION_ID][:8]})",
            data=self._data,
            options=options,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ScalewayOptionsFlow:
        return ScalewayOptionsFlow(config_entry)


class ScalewayOptionsFlow(OptionsFlow):
    """Let the user change monitored buckets and the backup destination."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        session = async_get_clientsession(self.hass)
        client = ScalewayApiClient(
            session,
            self._entry.data[CONF_ACCESS_KEY],
            self._entry.data[CONF_SECRET_KEY],
        )
        try:
            available_buckets = await client.async_list_buckets(REGIONS)
        except ScalewayApiError:
            # Fall back to whatever was already selected, so a transient API
            # failure doesn't strand the user with an empty bucket list.
            available_buckets = list(self._entry.options.get(CONF_BUCKETS, []))
            current_backup_bucket = self._entry.options.get(CONF_BACKUP_BUCKET)
            if current_backup_bucket and current_backup_bucket not in available_buckets:
                available_buckets.append(current_backup_bucket)

        if user_input is not None:
            selected_keys = user_input.get(CONF_BUCKETS, [])
            buckets = [b for b in available_buckets if _bucket_key(b) in selected_keys]
            options: dict[str, Any] = {CONF_BUCKETS: buckets}
            backup_bucket = _bucket_from_key(
                available_buckets, user_input.get(CONF_BACKUP_BUCKET, _NO_BACKUP_BUCKET)
            )
            if backup_bucket:
                options[CONF_BACKUP_BUCKET] = backup_bucket
                options[CONF_BACKUP_PREFIX] = user_input.get(CONF_BACKUP_PREFIX, "")
            return self.async_create_entry(title="", data=options)

        current_backup_bucket = self._entry.options.get(CONF_BACKUP_BUCKET)
        defaults = {
            CONF_BUCKETS: [_bucket_key(b) for b in self._entry.options.get(CONF_BUCKETS, [])],
            CONF_BACKUP_BUCKET: (
                _bucket_key(current_backup_bucket) if current_backup_bucket else _NO_BACKUP_BUCKET
            ),
            CONF_BACKUP_PREFIX: self._entry.options.get(CONF_BACKUP_PREFIX, ""),
        }
        schema = _buckets_schema(available_buckets, defaults)
        return self.async_show_form(step_id="init", data_schema=schema)
