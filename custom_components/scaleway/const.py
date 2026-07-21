"""Constants for the Scaleway integration."""
from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from homeassistant.util.hass_dict import HassKey

DOMAIN = "scaleway"

CONF_ACCESS_KEY = "access_key"
CONF_SECRET_KEY = "secret_key"
CONF_ORGANIZATION_ID = "organization_id"
CONF_BUCKETS = "buckets"
CONF_BACKUP_BUCKET = "backup_bucket"
CONF_BACKUP_PREFIX = "backup_prefix"

DEFAULT_SCAN_INTERVAL = timedelta(minutes=10)
# Sizing a bucket means paginating every object in it (Scaleway has no
# cheaper "bucket size" endpoint), so buckets are polled far
# less often than cost/instance/cluster status.
BUCKET_SCAN_INTERVAL = timedelta(hours=1)

# S3 multipart part-size floor (5 MiB is the hard S3 minimum; 20 MiB matches
# the reference S3-Compatible integration to avoid excessive part counts on
# large backups). Each part is buffered in memory before upload.
BACKUP_MULTIPART_MIN_PART_SIZE_BYTES = 20 * 2**20
# How long a listed backup set is trusted before re-listing the bucket.
BACKUP_CACHE_TTL = 300

DATA_BACKUP_AGENT_LISTENERS: HassKey[list[Callable[[], None]]] = HassKey(
    f"{DOMAIN}.backup_agent_listeners"
)

# There's no API to enumerate which localities an account has resources in —
# the Instance/Kubernetes APIs are zoned/regional and must be queried per
# locality explicitly. This is Scaleway's full public zone/region list.
REGIONS = ["fr-par", "nl-ams", "pl-waw"]
ZONES = [
    "fr-par-1", "fr-par-2", "fr-par-3",
    "nl-ams-1", "nl-ams-2", "nl-ams-3",
    "pl-waw-1", "pl-waw-2", "pl-waw-3",
]
