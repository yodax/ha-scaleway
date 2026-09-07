"""Config, reauth, reconfigure and options flow tests.

The credential-replacement flows carry the most risk here. Deleting and
re-adding an entry is *not* an equivalent workaround: every sensor's unique_id
and the backup agent's agent_id are derived from `entry_id`, so a fresh entry
renames all the entities and drops the configured backup destination. Both
flows therefore have to update the existing entry in place, and neither may
repoint it at a different Scaleway organization.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scaleway.api import (
    ScalewayApiClient,
    ScalewayApiError,
    ScalewayAuthError,
)
from custom_components.scaleway.const import (
    CONF_ACCESS_KEY,
    CONF_BACKUP_BUCKET,
    CONF_BACKUP_PREFIX,
    CONF_BUCKETS,
    CONF_ORGANIZATION_ID,
    CONF_SECRET_KEY,
    DOMAIN,
)

ORG = "11111111-1111-4111-8111-111111111111"
OTHER_ORG = "22222222-2222-4222-8222-222222222222"
ACCESS_KEY = "SCWXXXXXXXXXXXXXXXXX"
SECRET_KEY = "00000000-0000-4000-8000-000000000000"
NEW_ACCESS_KEY = "SCWYYYYYYYYYYYYYYYYY"
NEW_SECRET_KEY = "99999999-9999-4999-8999-999999999999"

BUCKETS = [
    {"name": "alpha", "region": "nl-ams"},
    {"name": "beta", "region": "fr-par"},
]


@pytest.fixture
def api() -> AsyncMock:
    """Patch the client everywhere the flows construct one."""
    client = AsyncMock(spec=ScalewayApiClient)
    client.async_resolve_organization_id.return_value = ORG
    client.async_list_buckets.return_value = list(BUCKETS)
    with (
        patch("custom_components.scaleway.config_flow.ScalewayApiClient", return_value=client),
        patch("custom_components.scaleway.ScalewayApiClient", return_value=client),
    ):
        client.async_get_cost.return_value = {
            "total": 0.0,
            "gross": 0.0,
            "discount": 0.0,
            "currency": "EUR",
            "by_category": {},
        }
        client.async_list_instances.return_value = []
        client.async_list_clusters.return_value = []
        yield client


def _entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Scaleway (11111111)",
        entry_id="01KY2Q7SV1FY74NVS14SR6HG62",
        unique_id=ORG,
        data={
            CONF_ACCESS_KEY: ACCESS_KEY,
            CONF_SECRET_KEY: SECRET_KEY,
            CONF_ORGANIZATION_ID: ORG,
        },
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


class TestUserFlow:
    async def test_a_valid_key_resolves_its_organization_and_creates_an_entry(
        self, hass: HomeAssistant, api: AsyncMock
    ) -> None:
        """The user is never asked for the organization id — there is no whoami
        endpoint, so it is looked up from the key's principal instead."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_KEY: ACCESS_KEY, CONF_SECRET_KEY: SECRET_KEY}
        )
        assert result["step_id"] == "buckets"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_BUCKETS: ["nl-ams:alpha"]}
        )

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_ORGANIZATION_ID] == ORG
        assert result["options"][CONF_BUCKETS] == [{"name": "alpha", "region": "nl-ams"}]

    async def test_choosing_a_backup_bucket_records_it_with_its_prefix(
        self, hass: HomeAssistant, api: AsyncMock
    ) -> None:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_KEY: ACCESS_KEY, CONF_SECRET_KEY: SECRET_KEY}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_BUCKETS: [],
                CONF_BACKUP_BUCKET: "fr-par:beta",
                CONF_BACKUP_PREFIX: "homeassistant/",
            },
        )

        assert result["options"][CONF_BACKUP_BUCKET] == {"name": "beta", "region": "fr-par"}
        assert result["options"][CONF_BACKUP_PREFIX] == "homeassistant/"

    async def test_no_backup_bucket_leaves_the_option_unset(
        self, hass: HomeAssistant, api: AsyncMock
    ) -> None:
        """An unset backup bucket is what keeps the backup agent from being
        registered at all — it must not become an empty-string bucket."""
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_KEY: ACCESS_KEY, CONF_SECRET_KEY: SECRET_KEY}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_BUCKETS: [], CONF_BACKUP_BUCKET: ""}
        )

        assert CONF_BACKUP_BUCKET not in result["options"]

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ScalewayAuthError("401"), "invalid_auth"),
            (ScalewayApiError("boom"), "cannot_connect"),
            (RuntimeError("???"), "unknown"),
        ],
    )
    async def test_a_bad_key_reshows_the_form_and_recovers_on_retry(
        self, hass: HomeAssistant, api: AsyncMock, exc: BaseException, expected: str
    ) -> None:
        api.async_resolve_organization_id.side_effect = exc
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_KEY: ACCESS_KEY, CONF_SECRET_KEY: "wrong"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": expected}

        api.async_resolve_organization_id.side_effect = None
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_KEY: ACCESS_KEY, CONF_SECRET_KEY: SECRET_KEY}
        )
        assert result["step_id"] == "buckets"

    async def test_the_same_organization_cannot_be_added_twice(
        self, hass: HomeAssistant, api: AsyncMock
    ) -> None:
        _entry(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_KEY: ACCESS_KEY, CONF_SECRET_KEY: SECRET_KEY}
        )

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "already_configured"

    async def test_an_account_with_no_buckets_skips_the_bucket_step(
        self, hass: HomeAssistant, api: AsyncMock
    ) -> None:
        api.async_list_buckets.return_value = []
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_KEY: ACCESS_KEY, CONF_SECRET_KEY: SECRET_KEY}
        )

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["options"][CONF_BUCKETS] == []

    async def test_a_bucket_listing_failure_does_not_lose_the_valid_key(
        self, hass: HomeAssistant, api: AsyncMock
    ) -> None:
        """Object Storage may be unreachable, or the key may lack that scope."""
        api.async_list_buckets.side_effect = ScalewayApiError("s3 down")
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_KEY: ACCESS_KEY, CONF_SECRET_KEY: SECRET_KEY}
        )

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["data"][CONF_ORGANIZATION_ID] == ORG


class TestCredentialReplacement:
    """Reauth and reconfigure share a step; only the trigger differs.

    Reauth is failure-driven — HA starts it, there is no button. Reconfigure is
    the user-initiated path, and exists only because the handler defines
    `async_step_reconfigure`.
    """

    @pytest.mark.parametrize("source", ["reauth", "reconfigure"])
    async def test_credentials_are_replaced_in_place(
        self, hass: HomeAssistant, api: AsyncMock, source: str
    ) -> None:
        """entry_id must survive: every unique_id and the backup agent_id hang
        off it."""
        entry = _entry(hass)

        if source == "reauth":
            result = await entry.start_reauth_flow(hass)
        else:
            result = await entry.start_reconfigure_flow(hass)
        assert result["type"] is FlowResultType.FORM

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ACCESS_KEY: NEW_ACCESS_KEY, CONF_SECRET_KEY: NEW_SECRET_KEY},
        )
        await hass.async_block_till_done()

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == f"{source}_successful"
        assert entry.entry_id == "01KY2Q7SV1FY74NVS14SR6HG62"
        assert entry.data[CONF_ACCESS_KEY] == NEW_ACCESS_KEY
        assert entry.data[CONF_SECRET_KEY] == NEW_SECRET_KEY
        assert entry.unique_id == ORG

    @pytest.mark.parametrize("source", ["reauth", "reconfigure"])
    async def test_a_key_for_another_organization_is_rejected(
        self, hass: HomeAssistant, api: AsyncMock, source: str
    ) -> None:
        """Otherwise the entry would silently show another account's data — and
        its backup agent would point at another account's bucket."""
        entry = _entry(hass)
        api.async_resolve_organization_id.return_value = OTHER_ORG

        if source == "reauth":
            result = await entry.start_reauth_flow(hass)
        else:
            result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ACCESS_KEY: NEW_ACCESS_KEY, CONF_SECRET_KEY: NEW_SECRET_KEY},
        )

        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "wrong_account"
        assert entry.data[CONF_ACCESS_KEY] == ACCESS_KEY
        assert entry.data[CONF_SECRET_KEY] == SECRET_KEY

    @pytest.mark.parametrize("source", ["reauth", "reconfigure"])
    async def test_a_bad_replacement_reshows_the_form_and_recovers(
        self, hass: HomeAssistant, api: AsyncMock, source: str
    ) -> None:
        entry = _entry(hass)
        api.async_resolve_organization_id.side_effect = ScalewayAuthError("401")

        if source == "reauth":
            result = await entry.start_reauth_flow(hass)
        else:
            result = await entry.start_reconfigure_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCESS_KEY: NEW_ACCESS_KEY, CONF_SECRET_KEY: "wrong"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_auth"}

        api.async_resolve_organization_id.side_effect = None
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ACCESS_KEY: NEW_ACCESS_KEY, CONF_SECRET_KEY: NEW_SECRET_KEY},
        )
        await hass.async_block_till_done()

        assert result["type"] is FlowResultType.ABORT
        assert entry.data[CONF_SECRET_KEY] == NEW_SECRET_KEY

    async def test_reconfigure_is_advertised_to_the_frontend(
        self, hass: HomeAssistant, api: AsyncMock
    ) -> None:
        """The "Reconfigure" menu item appears iff the handler defines the step.

        Without it there is no way to rotate a key *before* it expires, since
        HA only ever starts reauth in response to a failure.
        """
        entry = _entry(hass)

        assert entry.supports_reconfigure is True

    @pytest.mark.parametrize("source", ["reauth", "reconfigure"])
    async def test_the_options_are_untouched_by_a_key_swap(
        self, hass: HomeAssistant, api: AsyncMock, source: str
    ) -> None:
        """The configured backup destination has to survive a key rotation."""
        options = {
            CONF_BUCKETS: [{"name": "alpha", "region": "nl-ams"}],
            CONF_BACKUP_BUCKET: {"name": "alpha", "region": "nl-ams"},
            CONF_BACKUP_PREFIX: "homeassistant/",
        }
        entry = _entry(hass, options=options)

        if source == "reauth":
            result = await entry.start_reauth_flow(hass)
        else:
            result = await entry.start_reconfigure_flow(hass)
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ACCESS_KEY: NEW_ACCESS_KEY, CONF_SECRET_KEY: NEW_SECRET_KEY},
        )
        await hass.async_block_till_done()

        assert dict(entry.options) == options


class TestOptionsFlow:
    async def test_selection_is_saved_as_full_bucket_records(
        self, hass: HomeAssistant, api: AsyncMock
    ) -> None:
        entry = _entry(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_BUCKETS: ["nl-ams:alpha"],
                CONF_BACKUP_BUCKET: "fr-par:beta",
                CONF_BACKUP_PREFIX: "ha/",
            },
        )
        await hass.async_block_till_done()

        assert result["data"][CONF_BUCKETS] == [{"name": "alpha", "region": "nl-ams"}]
        assert result["data"][CONF_BACKUP_BUCKET] == {"name": "beta", "region": "fr-par"}

    async def test_clearing_the_backup_bucket_removes_it(
        self, hass: HomeAssistant, api: AsyncMock
    ) -> None:
        """This is how a user turns the backup agent off again."""
        entry = _entry(
            hass,
            options={
                CONF_BUCKETS: [],
                CONF_BACKUP_BUCKET: {"name": "beta", "region": "fr-par"},
                CONF_BACKUP_PREFIX: "ha/",
            },
        )
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_BUCKETS: [], CONF_BACKUP_BUCKET: ""}
        )
        await hass.async_block_till_done()

        assert CONF_BACKUP_BUCKET not in result["data"]

    async def test_an_api_failure_falls_back_to_the_current_selection(
        self, hass: HomeAssistant, api: AsyncMock
    ) -> None:
        """A transient Object Storage failure must not strand the user with an
        empty picker that silently deselects everything they had."""
        entry = _entry(hass, options={CONF_BUCKETS: [{"name": "alpha", "region": "nl-ams"}]})
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        api.async_list_buckets.side_effect = ScalewayApiError("s3 down")

        result = await hass.config_entries.options.async_init(entry.entry_id)

        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_BUCKETS: ["nl-ams:alpha"]}
        )
        await hass.async_block_till_done()

        assert result["data"][CONF_BUCKETS] == [{"name": "alpha", "region": "nl-ams"}]
