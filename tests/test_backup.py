"""Backup agent tests, aimed squarely at the v0.3.0 hardening.

A red-team review found four defects in the first version of this platform, all
reproduced live before being fixed. Each one had the same signature: it looked
like success. A delete that removed nothing and reported OK. A listing that hid
every backup. A timeout that skipped the multipart abort and left billable
orphan parts. A download that read a 5 GB archive into memory.

Nothing in this file existed at the time. These tests are the regression net,
and each names the defect it pins so a future "simplification" fails loudly.
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.backup import AddonInfo, AgentBackup, Folder
from homeassistant.components.backup import BackupAgentError, BackupNotFound
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.scaleway.api import (
    ScalewayApiClient,
    ScalewayApiError,
    ScalewayNotFoundError,
)
from custom_components.scaleway.backup import (
    ScalewayBackupAgent,
    async_get_backup_agents,
)
from custom_components.scaleway.const import (
    BACKUP_MULTIPART_MIN_PART_SIZE_BYTES,
    CONF_ACCESS_KEY,
    CONF_BACKUP_BUCKET,
    CONF_BACKUP_PREFIX,
    CONF_ORGANIZATION_ID,
    CONF_SECRET_KEY,
    DOMAIN,
)

ORG = "11111111-1111-4111-8111-111111111111"
BUCKET = {"name": "alpha", "region": "nl-ams"}


def _backup(backup_id: str = "abc123", size: int = 1024) -> AgentBackup:
    return AgentBackup(
        backup_id=backup_id,
        date="2026-09-07T02:00:00+02:00",
        addons=[AddonInfo(name="Terminal", slug="core_ssh", version="9.0")],
        database_included=True,
        extra_metadata={},
        folders=[Folder.SHARE],
        homeassistant_included=True,
        homeassistant_version="2026.9.1",
        name="Automatic backup 2026.9.1",
        protected=False,
        size=size,
    )


def _entry(hass: HomeAssistant, prefix: str = "ha-scaleway/") -> MockConfigEntry:
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
        options={CONF_BACKUP_BUCKET: BUCKET, CONF_BACKUP_PREFIX: prefix},
    )
    entry.add_to_hass(hass)
    return entry


def _agent(hass: HomeAssistant, prefix: str = "ha-scaleway/") -> tuple[ScalewayBackupAgent, AsyncMock]:
    client = AsyncMock(spec=ScalewayApiClient)
    with patch("custom_components.scaleway.backup.ScalewayApiClient", return_value=client):
        agent = ScalewayBackupAgent(hass, _entry(hass, prefix))
    return agent, client


def _listing(client: AsyncMock, backup: AgentBackup, metadata_key: str) -> None:
    """Wire the client to report exactly one stored backup at `metadata_key`."""
    client.async_list_objects.return_value = [
        {"key": metadata_key, "size": 500},
        {"key": metadata_key.replace(".metadata.json", ".tar"), "size": backup.size},
    ]
    client.async_get_object.return_value = json.dumps(backup.as_dict()).encode()


def _agen(chunks: list[bytes]) -> AsyncIterator[bytes]:
    """A bare async iterator — what `async_open_object_stream` resolves to."""

    async def gen() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    return gen()


async def _stream(chunks: list[bytes]) -> AsyncIterator[bytes]:
    """Awaitable form — what `open_stream` is contractually required to be."""
    return _agen(chunks)


class TestAgentDiscovery:
    async def test_an_entry_without_a_backup_bucket_gets_no_agent(
        self, hass: HomeAssistant
    ) -> None:
        """Opt-in per entry — there is no "the" backup bucket to auto-select."""
        entry = MockConfigEntry(domain=DOMAIN, unique_id=ORG, data={}, options={})
        entry.add_to_hass(hass)

        assert await async_get_backup_agents(hass) == []

    async def test_the_agent_id_is_derived_from_the_entry(self, hass: HomeAssistant) -> None:
        """Which is why credential replacement must never create a new entry —
        the backup location would move with it."""
        agent, _ = _agent(hass)

        assert agent.unique_id == "01KY2Q7SV1FY74NVS14SR6HG62"


class TestKeysComeFromTheListing:
    """DEFECT 1: keys were recomputed from prefix + name + date at delete time.

    Change the prefix in the options flow and the recomputed key stops matching
    the stored one. Because S3 deletes are idempotent, the delete *reported
    success while removing nothing* — silently breaking HA's retention policy
    and letting storage grow forever. Reproduced end-to-end before the fix.
    """

    async def test_delete_targets_the_key_the_listing_returned(
        self, hass: HomeAssistant
    ) -> None:
        backup = _backup()
        # Stored under the OLD prefix; the agent is now configured with a new one.
        old_metadata_key = "old-prefix/Automatic_backup_2026.9.1.metadata.json"
        agent, client = _agent(hass, prefix="new-prefix/")
        _listing(client, backup, old_metadata_key)

        await agent.async_delete_backup(backup.backup_id)

        deleted = [c.args[2] for c in client.async_delete_object.await_args_list]
        assert deleted == [
            "old-prefix/Automatic_backup_2026.9.1.tar",
            old_metadata_key,
        ]
        assert not any(k.startswith("new-prefix/") for k in deleted)

    async def test_download_targets_the_key_the_listing_returned(
        self, hass: HomeAssistant
    ) -> None:
        backup = _backup()
        agent, client = _agent(hass, prefix="new-prefix/")
        _listing(client, backup, "old-prefix/Automatic_backup_2026.9.1.metadata.json")
        client.async_open_object_stream.return_value = _agen([b"tar"])

        await agent.async_download_backup(backup.backup_id)

        assert (
            client.async_open_object_stream.await_args.args[2]
            == "old-prefix/Automatic_backup_2026.9.1.tar"
        )

    async def test_the_archive_is_deleted_before_its_metadata(
        self, hass: HomeAssistant
    ) -> None:
        """Reverse order strands a multi-GB invisible orphan on the first failure;
        this order converges on a clean state when retried."""
        backup = _backup()
        agent, client = _agent(hass)
        _listing(client, backup, "ha-scaleway/b.metadata.json")

        await agent.async_delete_backup(backup.backup_id)

        keys = [c.args[2] for c in client.async_delete_object.await_args_list]
        assert keys[0].endswith(".tar")
        assert keys[1].endswith(".metadata.json")


class TestOneBadSidecarDoesNotHideEverything:
    """DEFECT 2: `AgentBackup.from_dict` sat outside the try.

    It raises KeyError (a foreign file), ValueError (a folder enum from a newer
    HA) or TypeError (JSON that isn't an object) — none of which are
    BackupAgentError and none of which were caught. One stray `.metadata.json`
    made *every* backup invisible. Proved by dropping an unrelated file next to
    a good backup and watching the good one vanish.
    """

    @pytest.mark.parametrize(
        ("bad_content", "why"),
        [
            (b'{"not": "a backup"}', "foreign JSON object -> KeyError"),
            (b'["a", "list"]', "JSON that is not an object -> TypeError"),
            (b"not json at all", "truncated or foreign file -> ValueError"),
            (b"", "zero-byte write -> ValueError"),
        ],
    )
    async def test_a_good_backup_survives_a_bad_neighbour(
        self, hass: HomeAssistant, bad_content: bytes, why: str
    ) -> None:
        good = _backup()
        agent, client = _agent(hass)
        client.async_list_objects.return_value = [
            {"key": "ha-scaleway/bad.metadata.json", "size": 10},
            {"key": "ha-scaleway/good.metadata.json", "size": 500},
        ]
        client.async_get_object.side_effect = [
            bad_content,
            json.dumps(good.as_dict()).encode(),
        ]

        backups = await agent.async_list_backups()

        assert [b.backup_id for b in backups] == [good.backup_id], why

    async def test_an_unknown_folder_from_a_newer_ha_is_skipped_not_fatal(
        self, hass: HomeAssistant
    ) -> None:
        good = _backup()
        future = good.as_dict()
        future["folders"] = ["a_folder_this_ha_has_never_heard_of"]
        future["backup_id"] = "future1"
        agent, client = _agent(hass)
        client.async_list_objects.return_value = [
            {"key": "ha-scaleway/future.metadata.json", "size": 500},
            {"key": "ha-scaleway/good.metadata.json", "size": 500},
        ]
        client.async_get_object.side_effect = [
            json.dumps(future).encode(),
            json.dumps(good.as_dict()).encode(),
        ]

        backups = await agent.async_list_backups()

        assert [b.backup_id for b in backups] == [good.backup_id]

    async def test_a_transport_failure_still_propagates(self, hass: HomeAssistant) -> None:
        """The deliberate asymmetry: a backup we merely FAILED TO READ must not
        be silently omitted, or HA's retention logic gets an incomplete picture
        and may delete the wrong thing."""
        agent, client = _agent(hass)
        client.async_list_objects.return_value = [
            {"key": "ha-scaleway/good.metadata.json", "size": 500}
        ]
        client.async_get_object.side_effect = ScalewayApiError("503")

        with pytest.raises(BackupAgentError):
            await agent.async_list_backups()

    async def test_non_metadata_objects_in_the_bucket_are_ignored(
        self, hass: HomeAssistant
    ) -> None:
        """A shared bucket is an explicitly supported setup (hence the prefix)."""
        good = _backup()
        agent, client = _agent(hass)
        client.async_list_objects.return_value = [
            {"key": "ha-scaleway/holiday-photo.jpg", "size": 100},
            {"key": "ha-scaleway/good.metadata.json", "size": 500},
            {"key": "ha-scaleway/good.tar", "size": 1024},
        ]
        client.async_get_object.return_value = json.dumps(good.as_dict()).encode()

        backups = await agent.async_list_backups()

        assert len(backups) == 1
        assert client.async_get_object.await_count == 1


class TestMultipartAlwaysAborts:
    """DEFECT 3: `asyncio.TimeoutError` is not an `aiohttp.ClientError`.

    It bypassed the transport-error wrapper, escaped every
    `except ScalewayApiError` downstream, and skipped the multipart abort —
    leaving parts that Scaleway bills for and that ListObjectsV2 does not show.
    """

    @staticmethod
    def _big_stream(parts: int = 2):
        size = BACKUP_MULTIPART_MIN_PART_SIZE_BYTES
        return lambda: _stream([b"x" * size for _ in range(parts)])

    async def test_a_multipart_upload_completes(self, hass: HomeAssistant) -> None:
        agent, client = _agent(hass)
        client.async_create_multipart_upload.return_value = "upload-1"
        client.async_upload_part.return_value = ('"etag"', b"\x00" * 16)

        await agent.async_upload_backup(
            open_stream=self._big_stream(2),
            backup=_backup(size=BACKUP_MULTIPART_MIN_PART_SIZE_BYTES * 2),
        )

        assert client.async_upload_part.await_count == 2
        client.async_complete_multipart_upload.assert_awaited_once()
        client.async_abort_multipart_upload.assert_not_awaited()

    @pytest.mark.parametrize(
        "exc",
        [TimeoutError("stalled"), OSError("reset"), ScalewayApiError("500")],
        ids=["timeout", "oserror", "api-error"],
    )
    async def test_any_failure_mid_upload_aborts(
        self, hass: HomeAssistant, exc: BaseException
    ) -> None:
        agent, client = _agent(hass)
        client.async_create_multipart_upload.return_value = "upload-1"
        client.async_upload_part.side_effect = exc

        with pytest.raises(BackupAgentError):
            await agent.async_upload_backup(
                open_stream=self._big_stream(2),
                backup=_backup(size=BACKUP_MULTIPART_MIN_PART_SIZE_BYTES * 2),
            )

        client.async_abort_multipart_upload.assert_awaited_once()

    async def test_cancellation_still_aborts(self, hass: HomeAssistant) -> None:
        """HA shutting down mid-upload is the most likely way to strand parts."""
        import asyncio

        agent, client = _agent(hass)
        client.async_create_multipart_upload.return_value = "upload-1"
        client.async_upload_part.side_effect = asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await agent.async_upload_backup(
                open_stream=self._big_stream(2),
                backup=_backup(size=BACKUP_MULTIPART_MIN_PART_SIZE_BYTES * 2),
            )

        client.async_abort_multipart_upload.assert_awaited_once()

    async def test_a_source_stream_that_fails_is_contract_correct(
        self, hass: HomeAssistant
    ) -> None:
        """The stream HA hands us can fail too; the agent may only raise
        BackupAgentError."""
        agent, client = _agent(hass)

        async def bad_stream():
            async def gen():
                yield b"x"
                raise OSError("source went away")

            return gen()

        with pytest.raises(BackupAgentError):
            await agent.async_upload_backup(open_stream=bad_stream, backup=_backup(size=10))

    async def test_a_chunk_larger_than_the_part_size_flushes_repeatedly(
        self, hass: HomeAssistant
    ) -> None:
        """`while`, not `if` — otherwise the buffer grows without bound."""
        agent, client = _agent(hass)
        client.async_create_multipart_upload.return_value = "upload-1"
        client.async_upload_part.return_value = ('"etag"', b"\x00" * 16)
        one_chunk = BACKUP_MULTIPART_MIN_PART_SIZE_BYTES * 3

        await agent.async_upload_backup(
            open_stream=lambda: _stream([b"x" * one_chunk]),
            backup=_backup(size=one_chunk),
        )

        assert client.async_upload_part.await_count == 3


class TestOrphanCleanup:
    async def test_a_failed_metadata_write_removes_the_archive(
        self, hass: HomeAssistant
    ) -> None:
        """Otherwise the bucket keeps several GB that HA can never see or delete."""
        agent, client = _agent(hass)
        client.async_put_object.side_effect = [None, ScalewayApiError("503")]

        with pytest.raises(BackupAgentError):
            await agent.async_upload_backup(
                open_stream=lambda: _stream([b"tar-bytes"]), backup=_backup(size=9)
            )

        client.async_delete_object.assert_awaited_once()
        assert client.async_delete_object.await_args.args[2].endswith(".tar")

    async def test_a_small_backup_uses_a_single_put(self, hass: HomeAssistant) -> None:
        agent, client = _agent(hass)

        await agent.async_upload_backup(
            open_stream=lambda: _stream([b"tar-bytes"]), backup=_backup(size=9)
        )

        client.async_create_multipart_upload.assert_not_awaited()
        keys = [c.args[2] for c in client.async_put_object.await_args_list]
        assert keys[0].endswith(".tar")
        assert keys[1].endswith(".metadata.json")
        assert all(k.startswith("ha-scaleway/") for k in keys)


class TestLookupAndCaching:
    async def test_an_unknown_backup_id_raises_backup_not_found(
        self, hass: HomeAssistant
    ) -> None:
        agent, client = _agent(hass)
        client.async_list_objects.return_value = []

        with pytest.raises(BackupNotFound):
            await agent.async_get_backup("nope")

    async def test_a_vanished_archive_raises_backup_not_found_on_download(
        self, hass: HomeAssistant
    ) -> None:
        """Listed via its sidecar but the tar is gone — deleted out from under us."""
        backup = _backup()
        agent, client = _agent(hass)
        _listing(client, backup, "ha-scaleway/b.metadata.json")
        client.async_open_object_stream.side_effect = ScalewayNotFoundError("404")

        with pytest.raises(BackupNotFound):
            await agent.async_download_backup(backup.backup_id)

    async def test_the_listing_is_cached_between_calls(self, hass: HomeAssistant) -> None:
        backup = _backup()
        agent, client = _agent(hass)
        _listing(client, backup, "ha-scaleway/b.metadata.json")

        await agent.async_list_backups()
        await agent.async_list_backups()

        assert client.async_list_objects.await_count == 1

    async def test_an_upload_invalidates_the_cache(self, hass: HomeAssistant) -> None:
        """A new backup must be visible immediately, not up to five minutes later."""
        backup = _backup()
        agent, client = _agent(hass)
        _listing(client, backup, "ha-scaleway/b.metadata.json")
        await agent.async_list_backups()

        await agent.async_upload_backup(
            open_stream=lambda: _stream([b"tar"]), backup=_backup("new1", size=3)
        )
        await agent.async_list_backups()

        assert client.async_list_objects.await_count == 2

    async def test_a_delete_invalidates_the_cache(self, hass: HomeAssistant) -> None:
        """A stale cache here would let HA's retention pass act on a backup it
        just removed."""
        backup = _backup()
        agent, client = _agent(hass)
        _listing(client, backup, "ha-scaleway/b.metadata.json")
        await agent.async_list_backups()

        await agent.async_delete_backup(backup.backup_id)
        await agent.async_list_backups()

        assert client.async_list_objects.await_count == 2

    async def test_a_failed_delete_also_invalidates_the_cache(
        self, hass: HomeAssistant
    ) -> None:
        backup = _backup()
        agent, client = _agent(hass)
        _listing(client, backup, "ha-scaleway/b.metadata.json")
        await agent.async_list_backups()
        client.async_delete_object.side_effect = ScalewayApiError("503")

        with pytest.raises(BackupAgentError):
            await agent.async_delete_backup(backup.backup_id)
        await agent.async_list_backups()

        assert client.async_list_objects.await_count == 2

    async def test_only_the_configured_prefix_is_listed(self, hass: HomeAssistant) -> None:
        agent, client = _agent(hass, prefix="ha-scaleway/")
        client.async_list_objects.return_value = []

        await agent.async_list_backups()

        assert client.async_list_objects.await_args.args[2] == "ha-scaleway/"
