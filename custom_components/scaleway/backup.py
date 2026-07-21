"""Backup platform for the Scaleway integration.

Lets a Scaleway Object Storage bucket be used as an HA backup destination,
via HA's backup-agent platform contract (a `backup.py` module is picked up
automatically — no manifest changes needed).

S3 has no first-class "backup" concept to hang metadata (name, date,
protected, addons, etc.) off of, so each backup is stored as two objects:
`NAME.tar` (the backup itself) and `NAME.metadata.json`
(`AgentBackup.as_dict()`). Listing backups means listing `*.metadata.json`
objects and parsing each one, not the tar files. This is the same on-disk
layout the generic S3-Compatible integration uses
(github.com/PhantomPhoton/S3-Compatible), so backups it already wrote to a
bucket are picked up unchanged.

Two rules keep that scheme safe. Both are deliberate and load-bearing —
an earlier revision got each of them wrong:

1. Object keys for download/delete are derived from **the metadata key the
   listing actually returned**, never recomputed from the configured
   prefix. Recomputing meant that changing the prefix made deletes silently
   target keys that don't exist — and since S3 deletes are idempotent, they
   "succeeded" while removing nothing, quietly breaking retention.
2. A metadata file that can't be parsed is skipped with a warning, never
   allowed to abort the whole listing. One foreign/truncated
   `.metadata.json` in the bucket previously made *every* backup invisible.

Only config entries with a backup bucket configured (see config_flow.py's
"buckets" step / options flow) get an agent — like bucket-size monitoring,
this is opt-in per entry rather than automatic.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from time import time
from typing import Any

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
    suggested_filename,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ScalewayApiClient, ScalewayApiError, ScalewayNotFoundError
from .const import (
    BACKUP_CACHE_TTL,
    BACKUP_MULTIPART_MIN_PART_SIZE_BYTES,
    CONF_ACCESS_KEY,
    CONF_BACKUP_BUCKET,
    CONF_BACKUP_PREFIX,
    CONF_SECRET_KEY,
    DATA_BACKUP_AGENT_LISTENERS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

METADATA_SUFFIX = ".metadata.json"
TAR_SUFFIX = ".tar"


def _contract_errors(message: str):
    """Guarantee an agent method only ever raises BackupAgentError.

    HA's backup manager treats anything else as an unexpected crash. Most
    of the failure paths below already map their own errors, but the source
    stream handed to us by HA can fail too — this is the backstop so that
    never escapes as, say, a bare OSError. CancelledError is BaseException
    and deliberately still propagates, so shutdown isn't swallowed.
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any):
            try:
                return await func(*args, **kwargs)
            except BackupAgentError:
                raise  # already contract-correct (includes BackupNotFound)
            except Exception as err:
                raise BackupAgentError(f"{message}: {err}") from err

        return wrapper

    return decorator


async def async_get_backup_agents(hass: HomeAssistant) -> list[BackupAgent]:
    """Return a backup agent for each loaded entry that has a backup bucket set."""
    entries: list[ConfigEntry] = hass.config_entries.async_loaded_entries(DOMAIN)
    return [
        ScalewayBackupAgent(hass, entry)
        for entry in entries
        if entry.options.get(CONF_BACKUP_BUCKET)
    ]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant,
    *,
    listener: Callable[[], None],
    **kwargs: Any,
) -> Callable[[], None]:
    """Register a listener to be called when agents are added or removed.

    :return: A function to unregister the listener.
    """
    hass.data.setdefault(DATA_BACKUP_AGENT_LISTENERS, []).append(listener)

    @callback
    def remove_listener() -> None:
        hass.data[DATA_BACKUP_AGENT_LISTENERS].remove(listener)
        if not hass.data[DATA_BACKUP_AGENT_LISTENERS]:
            del hass.data[DATA_BACKUP_AGENT_LISTENERS]

    return remove_listener


def _suggested_filenames(backup: AgentBackup, prefix: str) -> tuple[str, str]:
    """Return the (tar, metadata) object keys to store a *new* backup under."""
    base_name = suggested_filename(backup).rsplit(".", 1)[0]
    return f"{prefix}{base_name}{TAR_SUFFIX}", f"{prefix}{base_name}{METADATA_SUFFIX}"


def _tar_key_for_metadata_key(metadata_key: str) -> str:
    """Map a metadata object key to its sibling tar key.

    Derived from the real key rather than rebuilt from the configured
    prefix, so download/delete keep working if the prefix changes.
    """
    return f"{metadata_key[: -len(METADATA_SUFFIX)]}{TAR_SUFFIX}"


@dataclass(frozen=True)
class _StoredBackup:
    """A backup plus the object keys it actually lives at."""

    backup: AgentBackup
    metadata_key: str
    tar_key: str


class ScalewayBackupAgent(BackupAgent):
    """Backup agent that stores backups in one Scaleway Object Storage bucket."""

    domain = DOMAIN

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__()
        backup_bucket = entry.options[CONF_BACKUP_BUCKET]
        self._region: str = backup_bucket["region"]
        self._bucket: str = backup_bucket["name"]
        self._prefix: str = entry.options.get(CONF_BACKUP_PREFIX, "")
        self._client = ScalewayApiClient(
            async_get_clientsession(hass),
            entry.data[CONF_ACCESS_KEY],
            entry.data[CONF_SECRET_KEY],
        )

        self.name = f"{entry.title} — {self._bucket}"
        self.unique_id = entry.entry_id
        self._backup_cache: dict[str, _StoredBackup] = {}
        self._cache_expiration = 0.0

    @_contract_errors("Failed to download backup")
    async def async_download_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[bytes]:
        """Download a backup file as a stream.

        Backups run to several GB, so this streams straight from the
        response rather than materialising the whole archive in memory.
        """
        stored = await self._find_stored_by_id(backup_id)
        try:
            return await self._client.async_open_object_stream(
                self._region, self._bucket, stored.tar_key
            )
        except ScalewayNotFoundError as err:
            raise BackupNotFound(f"Backup {backup_id} not found") from err
        except ScalewayApiError as err:
            raise BackupAgentError(f"Failed to download backup {backup_id}") from err

    @_contract_errors("Failed to upload backup")
    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        **kwargs: Any,
    ) -> None:
        """Upload a backup."""
        tar_key, metadata_key = _suggested_filenames(backup, self._prefix)

        try:
            if backup.size < BACKUP_MULTIPART_MIN_PART_SIZE_BYTES:
                await self._upload_simple(tar_key, open_stream)
            else:
                await self._upload_multipart(tar_key, open_stream)
        except ScalewayApiError as err:
            raise BackupAgentError(f"Failed to upload backup {backup.backup_id}") from err

        # The archive is stored but HA can only see it through its metadata
        # sidecar — if that write fails, remove the archive rather than
        # leaving multiple GB of invisible, billable orphan behind.
        try:
            metadata_content = json.dumps(backup.as_dict()).encode("utf-8")
            await self._client.async_put_object(
                self._region, self._bucket, metadata_key, metadata_content
            )
        except ScalewayApiError as err:
            await self._delete_quietly(tar_key)
            raise BackupAgentError(
                f"Failed to write metadata for backup {backup.backup_id}"
            ) from err

        self._cache_expiration = 0.0

    async def _upload_simple(
        self,
        tar_key: str,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
    ) -> None:
        """Buffer a small backup fully in memory, then PUT it in one request."""
        stream = await open_stream()
        file_data = bytearray()
        async for chunk in stream:
            file_data.extend(chunk)
        await self._client.async_put_object(
            self._region, self._bucket, tar_key, bytes(file_data)
        )

    async def _upload_multipart(
        self,
        tar_key: str,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
    ) -> None:
        """Upload a large backup as fixed-size (except the last) multipart parts."""
        upload_id = await self._client.async_create_multipart_upload(
            self._region, self._bucket, tar_key
        )
        try:
            parts: list[dict] = []
            digests: list[bytes] = []
            part_number = 1
            buffer = bytearray()

            stream = await open_stream()
            async for chunk in stream:
                buffer.extend(chunk)
                # `while`, not `if`: a source yielding chunks larger than the
                # part size would otherwise flush only one part per chunk and
                # let the buffer grow unbounded.
                while len(buffer) >= BACKUP_MULTIPART_MIN_PART_SIZE_BYTES:
                    part_body = bytes(buffer[:BACKUP_MULTIPART_MIN_PART_SIZE_BYTES])
                    del buffer[:BACKUP_MULTIPART_MIN_PART_SIZE_BYTES]
                    etag, digest = await self._client.async_upload_part(
                        self._region, self._bucket, tar_key, upload_id, part_number, part_body
                    )
                    parts.append({"PartNumber": part_number, "ETag": etag})
                    digests.append(digest)
                    part_number += 1

            if buffer:
                etag, digest = await self._client.async_upload_part(
                    self._region, self._bucket, tar_key, upload_id, part_number, bytes(buffer)
                )
                parts.append({"PartNumber": part_number, "ETag": etag})
                digests.append(digest)

            await self._client.async_complete_multipart_upload(
                self._region, self._bucket, tar_key, upload_id, parts, digests
            )
        except BaseException:
            # Any failure at all — including a timeout or HA shutting down
            # mid-upload — must abort, or the uploaded parts keep costing
            # money while being invisible to ordinary bucket listings.
            await self._abort_quietly(tar_key, upload_id)
            raise

    async def _abort_quietly(self, tar_key: str, upload_id: str) -> None:
        """Abort a multipart upload, never raising over the original error."""
        try:
            # Shielded so a cancellation still gets the abort onto the wire.
            await asyncio.shield(
                self._client.async_abort_multipart_upload(
                    self._region, self._bucket, tar_key, upload_id
                )
            )
        except BaseException:  # noqa: BLE001 - cleanup must not mask the real failure
            _LOGGER.warning(
                "Could not abort multipart upload %s for %s; it may still hold "
                "billable parts in the bucket",
                upload_id,
                tar_key,
            )

    async def _delete_quietly(self, key: str) -> None:
        """Delete an object, never raising over the original error."""
        try:
            await self._client.async_delete_object(self._region, self._bucket, key)
        except ScalewayApiError:
            _LOGGER.warning("Could not clean up orphaned object %s", key)

    @_contract_errors("Failed to delete backup")
    async def async_delete_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> None:
        """Delete a backup file (and its metadata sidecar)."""
        stored = await self._find_stored_by_id(backup_id)
        try:
            # Archive first: if this succeeds but the metadata delete fails,
            # a retry is idempotent (the missing archive delete is a no-op)
            # and converges on a clean state. The reverse order would strand
            # the archive as an invisible orphan on the very first failure.
            await self._client.async_delete_object(self._region, self._bucket, stored.tar_key)
            await self._client.async_delete_object(self._region, self._bucket, stored.metadata_key)
        except ScalewayApiError as err:
            self._cache_expiration = 0.0
            raise BackupAgentError(f"Failed to delete backup {backup_id}") from err
        self._cache_expiration = 0.0

    @_contract_errors("Failed to list backups")
    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        """List backups."""
        stored = await self._list_backups()
        return [entry.backup for entry in stored.values()]

    @_contract_errors("Failed to get backup")
    async def async_get_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> AgentBackup:
        """Return a specific backup."""
        return (await self._find_stored_by_id(backup_id)).backup

    async def _find_stored_by_id(self, backup_id: str) -> _StoredBackup:
        backups = await self._list_backups()
        if stored := backups.get(backup_id):
            return stored
        raise BackupNotFound(f"Backup {backup_id} not found")

    async def _list_backups(self) -> dict[str, _StoredBackup]:
        """List backups, using a short-lived cache to avoid re-listing on every call."""
        if time() <= self._cache_expiration:
            return self._backup_cache

        stored: dict[str, _StoredBackup] = {}
        try:
            objects = await self._client.async_list_objects(
                self._region, self._bucket, self._prefix
            )
            metadata_keys = [o["key"] for o in objects if o["key"].endswith(METADATA_SUFFIX)]
            for key in metadata_keys:
                # Transport failures deliberately propagate: silently omitting
                # a backup we simply failed to read would hand HA's retention
                # logic an incomplete picture.
                content = await self._client.async_get_object(self._region, self._bucket, key)
                try:
                    metadata = json.loads(content)
                    if not isinstance(metadata, dict):
                        raise TypeError("metadata is not a JSON object")
                    metadata.setdefault("addons", [])
                    backup = AgentBackup.from_dict(metadata)
                except (ValueError, KeyError, TypeError) as err:
                    # Foreign file, truncated write, or a schema from a newer
                    # HA. Skip it — one unreadable sidecar must not hide every
                    # other backup in the bucket.
                    _LOGGER.warning("Ignoring unreadable backup metadata %s: %s", key, err)
                    continue
                stored[backup.backup_id] = _StoredBackup(
                    backup=backup,
                    metadata_key=key,
                    tar_key=_tar_key_for_metadata_key(key),
                )
        except ScalewayApiError as err:
            raise BackupAgentError("Failed to list backups") from err

        self._backup_cache = stored
        self._cache_expiration = time() + BACKUP_CACHE_TTL
        return self._backup_cache
