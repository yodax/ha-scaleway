"""Client for the Scaleway API.

Two distinct API surfaces are used, each verified against a live Scaleway
account:

- The REST control-plane API at ``api.scaleway.com`` (IAM, Billing,
  Instance, Kubernetes) — authenticated via a plain ``X-Auth-Token: <secret
  key>`` header.
- The S3-compatible Object Storage API at ``s3.<region>.scw.cloud`` —
  authenticated via AWS SigV4, using the *same* access/secret keypair as
  S3-style credentials. Scaleway has no native "bucket size" endpoint on
  the control-plane API — confirmed both by testing ``HeadBucket`` against
  a real bucket (returns only region metadata) and by Scaleway's own open,
  unresolved feature request for one. So bucket size/object count are
  computed by paginating ``ListObjectsV2`` and summing ``Size`` across
  every object.

SigV4 signing is implemented by hand rather than pulling in
botocore/aiobotocore, so the integration ships with no Python dependencies
at all (``"requirements": []``) and rides on Home Assistant's own aiohttp
session.

The write-side S3 operations here (PutObject, multipart upload,
DeleteObject) exist for the backup platform (``backup.py``). Backups run to
several GB, so:

- Downloads stream (``async_open_object_stream``) rather than buffering the
  whole object in memory; only small metadata sidecars use
  ``async_get_object``.
- Transfers use a timeout with **no total cap** — a total cap silently
  aborts a large but perfectly healthy transfer. Stalls are caught by
  ``sock_read`` instead.
- Uploads verify the ETag Scaleway returns against a locally computed MD5,
  so a corrupted upload fails loudly instead of masquerading as a good
  backup.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from urllib.parse import quote

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.scaleway.com"
CONSUMPTIONS_PATH = "/billing/v2beta1/consumptions"
# The endpoint pages; see _fetch_consumptions. The cap bounds a runaway loop
# against someone's billing API — 100 x 50 pages is far more line items than any
# plausible organization has (a real one was seen peaking at 18).
CONSUMPTIONS_PAGE_SIZE = 100
MAX_CONSUMPTION_PAGES = 50
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
# Deliberately no `total`: a multi-GB backup over a slow uplink is not an
# error, but a connection that stops producing bytes is — hence sock_read.
TRANSFER_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=120)
DOWNLOAD_CHUNK_SIZE = 2**20

# Errors that mean "the transport failed" rather than "the server said no".
# asyncio.TimeoutError (built-in TimeoutError on 3.11+) is NOT an
# aiohttp.ClientError, so it has to be listed explicitly — otherwise a
# timed-out backup upload escapes as a bare TimeoutError and skips every
# `except ScalewayApiError` cleanup path downstream.
_TRANSPORT_ERRORS = (aiohttp.ClientError, TimeoutError, OSError)


def _local_name(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _find_all(parent: ET.Element, *tags: str) -> list[ET.Element]:
    """Find elements by a path of local tag names, ignoring XML namespaces.

    Verified against a real account that Scaleway's S3 API is inconsistent
    about namespaces: ``ListObjectsV2`` responses declare the standard S3
    xmlns, but ``ListBuckets`` responses omit it entirely. Matching on the
    local tag name only sidesteps that instead of guessing which endpoints
    are namespaced.
    """
    current = [parent]
    for tag in tags:
        nxt: list[ET.Element] = []
        for el in current:
            nxt.extend(child for child in el if _local_name(child.tag) == tag)
        current = nxt
    return current


def _find_text(parent: ET.Element, tag: str) -> str | None:
    for child in parent:
        if _local_name(child.tag) == tag:
            return child.text
    return None


def _parse_xml(body: bytes) -> ET.Element | None:
    """Parse an S3 XML body, raising if it's an <Error> document.

    S3 has a long-standing wart where some operations (notably
    CompleteMultipartUpload) can answer HTTP 200 with an ``<Error>`` body.
    Scaleway was observed returning a proper 4xx for a bad part instead,
    but a backup tool should not depend on that — treating a 200 <Error> as
    success would record a backup that does not actually exist.
    """
    if not body:
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError as err:
        raise ScalewayApiError(f"Malformed XML from Scaleway: {err}") from err
    if _local_name(root.tag) == "Error":
        code = _find_text(root, "Code") or "Unknown"
        message = _find_text(root, "Message") or ""
        raise ScalewayApiError(f"Scaleway returned an error document: {code}: {message}")
    return root


def _md5(data: bytes) -> "hashlib._Hash":
    # Not security-relevant: S3 defines ETag as an MD5, so MD5 is required
    # for interoperability regardless of its cryptographic standing.
    return hashlib.md5(data, usedforsecurity=False)


def _normalise_etag(etag: str | None) -> str | None:
    return etag.strip().strip('"') if etag else None


def _is_md5_style_etag(etag: str) -> bool:
    """Whether an ETag looks like a plain or multipart MD5.

    Buckets using SSE-C/KMS return an ETag that is not an MD5 at all, so
    integrity checks must be skipped rather than failing every upload.
    """
    base, _, suffix = etag.partition("-")
    if len(base) != 32 or any(c not in "0123456789abcdefABCDEF" for c in base):
        return False
    return not suffix or suffix.isdigit()


class ScalewayApiError(Exception):
    """Raised for any non-auth Scaleway API failure."""


class ScalewayAuthError(ScalewayApiError):
    """Raised when the API key is invalid or lacks permission."""


class ScalewayNotFoundError(ScalewayApiError):
    """Raised when a requested object/bucket doesn't exist (HTTP 404)."""


class ScalewayIntegrityError(ScalewayApiError):
    """Raised when an uploaded object's ETag doesn't match what was sent."""


class ScalewayApiClient:
    """Talks to the Scaleway REST and S3-compatible Object Storage APIs."""

    def __init__(self, session: aiohttp.ClientSession, access_key: str, secret_key: str) -> None:
        self._session = session
        self._access_key = access_key
        self._secret_key = secret_key

    # ---------------------------------------------------------------- REST

    async def _rest_get(self, path: str, params: dict | None = None) -> dict:
        try:
            async with self._session.get(
                f"{API_BASE}{path}",
                headers={"X-Auth-Token": self._secret_key},
                params=params,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status in (401, 403):
                    raise ScalewayAuthError(f"{resp.status} calling {path}")
                if resp.status >= 400:
                    raise ScalewayApiError(f"{resp.status} calling {path}: {await resp.text()}")
                return await resp.json()
        except _TRANSPORT_ERRORS as err:
            raise ScalewayApiError(f"Error calling {path}: {err}") from err

    async def async_resolve_organization_id(self) -> str:
        """Resolve the Organization a key belongs to.

        There's no "whoami" endpoint that returns it directly — a key is
        bound to either a user or an IAM application, and organization_id
        has to be looked up from that principal.
        """
        key_info = await self._rest_get(f"/iam/v1alpha1/api-keys/{self._access_key}")

        if user_id := key_info.get("user_id"):
            principal = await self._rest_get(f"/iam/v1alpha1/users/{user_id}")
        elif application_id := key_info.get("application_id"):
            principal = await self._rest_get(f"/iam/v1alpha1/applications/{application_id}")
        else:
            raise ScalewayApiError("API key is not bound to a user or application")

        organization_id = principal.get("organization_id")
        if not organization_id:
            raise ScalewayApiError("Could not resolve organization_id for this API key")
        return organization_id

    async def async_get_cost(self, organization_id: str) -> dict:
        """Return the invoiced cost for the current billing period.

        Verified on 2026-09-07 against all 80 invoices on a real organization:
        this figure equals the invoice's ``total_untaxed`` to the cent in every
        period. Three things make that true, and each of them is a trap:

        1. **The discount trailer must be subtracted.** ``consumptions[]`` is
           gross. An org-wide rate discount or commitment appears *only* as the
           ``total_discount_untaxed_value`` trailer, with no line item and no
           category. Summing the line items alone overstated the bill by 25% for
           the twelve periods a 25% discount was live on the account it was
           verified against.
        2. **The trailer is a bare number, not a units/nanos money object** —
           the only amount in this API that is not. Parsing it with the
           units/nanos arithmetic used for line items silently yields 0.0 and
           puts the overstatement straight back.
        3. **The trailer is only meaningful on the first page.** Requesting a
           page past the end returns an empty ``consumptions[]`` *and* a trailer
           of 0, so reading it from the last page reintroduces the same bug.

        The figure is **ex-VAT**: it matches ``total_untaxed``, not
        ``total_taxed``. See CLAUDE.md for why that distinction is not
        observable on the verification account and must not be "simplified"
        away.

        Free tier is *not* part of the trailer — it arrives as negative "Offer
        deducted" line items inside ``consumptions[]`` (confirmed against a real
        invoice PDF), so subtracting the trailer does not double-count it.
        """
        lines, discount, currency = await self._fetch_consumptions(organization_id)

        gross = 0.0
        by_category: dict[str, float] = {}
        for item in lines:
            value = item.get("value", {})
            amount = value.get("units", 0) + value.get("nanos", 0) / 1_000_000_000
            gross += amount
            category = item.get("category_name") or "Other"
            by_category[category] = by_category.get(category, 0.0) + amount

        # by_category stays GROSS. A rate discount is organization-wide with no
        # category attribution, and the discount-mode enum includes non-rate
        # modes (a fixed-amount coupon would not split proportionally), so
        # allocating it across categories would be invention. The consequence —
        # total != sum(by_category) while a discount is active — is deliberate
        # and is surfaced by the `discount` and `gross` keys.
        return {
            "total": round(gross - discount, 2),
            "gross": round(gross, 2),
            "discount": round(discount, 2),
            "currency": currency,
            "by_category": {k: round(v, 2) for k, v in by_category.items()},
        }

    async def _fetch_consumptions(self, organization_id: str) -> tuple[list[dict], float, str]:
        """Page through the consumption line items for the current period.

        The endpoint pages (``page`` is 1-indexed, ``page_size`` bounded) and
        reports ``total_count``. Reading only the first page silently
        undercounts an organization with more line items than fit in it — the
        sum simply comes out low, with nothing to indicate it. So this pages to
        completion and then asserts the collected count against ``total_count``:
        a short read raises rather than producing a quiet wrong total.
        """
        lines: list[dict] = []
        discount = 0.0
        currency = "EUR"
        total_count: int | None = None

        for page in range(1, MAX_CONSUMPTION_PAGES + 1):
            data = await self._rest_get(
                CONSUMPTIONS_PATH,
                params={
                    "organization_id": organization_id,
                    "page": page,
                    "page_size": CONSUMPTIONS_PAGE_SIZE,
                },
            )

            if page == 1:
                # Always present on a real response, including when it is zero —
                # verified across all 80 billing periods of a real account. So
                # absence is a schema change, not "no discount", and defaulting
                # it to 0 would silently restore the overstatement for exactly
                # the users the subtraction exists for.
                if "total_discount_untaxed_value" not in data:
                    raise ScalewayApiError(
                        "Scaleway consumptions response has no "
                        "total_discount_untaxed_value field; refusing to report a "
                        "cost that may be overstated by an unknown discount"
                    )
                try:
                    discount = float(data["total_discount_untaxed_value"])
                except (TypeError, ValueError) as err:
                    raise ScalewayApiError(
                        f"Unparseable total_discount_untaxed_value: "
                        f"{data['total_discount_untaxed_value']!r}"
                    ) from err
                raw_total = data.get("total_count")
                total_count = int(raw_total) if raw_total is not None else None

            batch = data.get("consumptions", [])
            for item in batch:
                currency = item.get("value", {}).get("currency_code", currency)
            lines.extend(batch)

            if not batch or len(batch) < CONSUMPTIONS_PAGE_SIZE:
                break
            if total_count is not None and len(lines) >= total_count:
                break
        else:
            raise ScalewayApiError(
                f"Scaleway consumptions did not finish paging within "
                f"{MAX_CONSUMPTION_PAGES} pages; refusing to report a partial total"
            )

        if total_count is not None and len(lines) != total_count:
            raise ScalewayApiError(
                f"Scaleway reported {total_count} consumption line items but "
                f"returned {len(lines)}; refusing to report a partial total"
            )

        return lines, discount, currency

    async def async_list_instances(self, zones: list[str]) -> list[dict]:
        """List Instances (servers) across the given zones.

        There's no "all zones" endpoint — Instance is a zoned API, so this
        has to loop a hardcoded zone list (see const.ZONES).
        """
        instances: list[dict] = []
        for zone in zones:
            try:
                data = await self._rest_get(f"/instance/v1/zones/{zone}/servers")
            except ScalewayApiError:
                continue
            for server in data.get("servers", []):
                instances.append(
                    {
                        "id": server["id"],
                        "name": server["name"],
                        "zone": zone,
                        "commercial_type": server.get("commercial_type"),
                        "state": server.get("state"),
                    }
                )
        return instances

    async def async_list_clusters(self, regions: list[str]) -> list[dict]:
        """List Kubernetes (Kapsule) clusters across the given regions."""
        clusters: list[dict] = []
        for region in regions:
            try:
                data = await self._rest_get(f"/k8s/v1/regions/{region}/clusters")
            except ScalewayApiError:
                continue
            for cluster in data.get("clusters", []):
                clusters.append(
                    {
                        "id": cluster["id"],
                        "name": cluster["name"],
                        "region": region,
                        "status": cluster.get("status"),
                        "version": cluster.get("version"),
                    }
                )
        return clusters

    # ------------------------------------------------- Object Storage: read

    async def async_list_buckets(self, regions: list[str]) -> list[dict]:
        """List every bucket the account owns, tagged with its region.

        ListBuckets is scoped to whichever region's S3 endpoint is called —
        there's no global bucket list, so this loops all known regions.
        """
        buckets: list[dict] = []
        for region in regions:
            try:
                _, body = await self._s3_request("GET", region, "/")
                root = _parse_xml(body)
            except ScalewayApiError:
                continue
            if root is None:
                continue
            for bucket_el in _find_all(root, "Buckets", "Bucket"):
                name = _find_text(bucket_el, "Name")
                if name:
                    buckets.append({"name": name, "region": region})
        return buckets

    async def async_get_bucket_size(self, region: str, bucket: str) -> dict | None:
        """Sum object sizes for one bucket via paginated ListObjectsV2.

        Returns None if the bucket no longer exists (e.g. deleted after
        being selected for monitoring). Scaleway has no cheaper way to get
        a bucket's size — see module docstring.
        """
        total_size = 0
        object_count = 0
        continuation_token: str | None = None
        first_page = True
        while True:
            params = {"list-type": "2", "max-keys": "1000"}
            if continuation_token:
                params["continuation-token"] = continuation_token
            try:
                _, body = await self._s3_request("GET", region, f"/{bucket}", params=params)
            except ScalewayNotFoundError:
                if first_page:
                    return None
                break
            first_page = False
            root = _parse_xml(body)
            if root is None:
                break
            for size_el in _find_all(root, "Contents", "Size"):
                try:
                    total_size += int(size_el.text or 0)
                except ValueError:
                    _LOGGER.debug("Ignoring non-numeric Size in %s/%s listing", region, bucket)
                object_count += 1
            is_truncated = (_find_text(root, "IsTruncated") or "false").lower() == "true"
            if not is_truncated:
                break
            continuation_token = _find_text(root, "NextContinuationToken")
            if not continuation_token:
                # Truncated but with nowhere to continue from. Stopping is the
                # only option, but *reporting what we have* would publish a
                # bucket size that is silently short — the sensor would simply
                # read low, with nothing to indicate it. Same reasoning as the
                # consumptions short-read: a visibly missing measurement beats a
                # quietly wrong one.
                raise ScalewayApiError(
                    f"Truncated ListObjectsV2 for {region}/{bucket} with no "
                    f"NextContinuationToken; refusing to report a partial size"
                )
        return {"size_bytes": total_size, "object_count": object_count}

    async def async_list_objects(self, region: str, bucket: str, prefix: str = "") -> list[dict]:
        """List objects (key + size) in a bucket, optionally under a prefix."""
        objects: list[dict] = []
        continuation_token: str | None = None
        while True:
            params = {"list-type": "2", "max-keys": "1000"}
            if prefix:
                params["prefix"] = prefix
            if continuation_token:
                params["continuation-token"] = continuation_token
            _, body = await self._s3_request("GET", region, f"/{bucket}", params=params)
            root = _parse_xml(body)
            if root is None:
                break
            for contents_el in _find_all(root, "Contents"):
                key = _find_text(contents_el, "Key")
                if not key:
                    continue
                try:
                    size = int(_find_text(contents_el, "Size") or 0)
                except ValueError:
                    size = 0
                objects.append({"key": key, "size": size})
            is_truncated = (_find_text(root, "IsTruncated") or "false").lower() == "true"
            if not is_truncated:
                break
            continuation_token = _find_text(root, "NextContinuationToken")
            if not continuation_token:
                # See async_get_bucket_size. Here it matters more: a short
                # listing hides backups from HA, and HA's retention logic acts
                # on what it can see.
                raise ScalewayApiError(
                    f"Truncated ListObjectsV2 for {region}/{bucket} with no "
                    f"NextContinuationToken; refusing to report a partial listing"
                )
        return objects

    async def async_get_object(self, region: str, bucket: str, key: str) -> bytes:
        """Download a *small* object fully into memory.

        Only for metadata sidecars and similar — anything backup-sized must
        use async_open_object_stream instead.
        """
        _, body = await self._s3_request(
            "GET", region, f"/{bucket}/{key}", timeout=TRANSFER_TIMEOUT
        )
        return body

    async def async_open_object_stream(
        self, region: str, bucket: str, key: str, *, chunk_size: int = DOWNLOAD_CHUNK_SIZE
    ) -> AsyncIterator[bytes]:
        """Stream an object without buffering it in memory.

        The response status is checked eagerly (so a missing object raises
        ScalewayNotFoundError here rather than part-way through iteration),
        then the body is handed back as a chunk iterator that releases the
        connection when it finishes or is discarded.
        """
        url, headers = self._prepare_s3("GET", region, f"/{bucket}/{key}", {}, b"")
        try:
            resp = await self._session.get(url, headers=headers, timeout=TRANSFER_TIMEOUT)
        except _TRANSPORT_ERRORS as err:
            raise ScalewayApiError(f"Error streaming {key} from {region}: {err}") from err

        if resp.status >= 400:
            body = await resp.read()
            resp.release()
            self._raise_for_status(resp.status, "GET", f"/{bucket}/{key}", region, body)

        async def _iterate() -> AsyncIterator[bytes]:
            try:
                async for chunk in resp.content.iter_chunked(chunk_size):
                    yield chunk
            except _TRANSPORT_ERRORS as err:
                raise ScalewayApiError(f"Error streaming {key} from {region}: {err}") from err
            finally:
                resp.release()

        return _iterate()

    # ------------------------------------------------ Object Storage: write

    async def async_put_object(self, region: str, bucket: str, key: str, body: bytes) -> None:
        """Upload an object in a single request, verifying the stored ETag."""
        headers, _ = await self._s3_request(
            "PUT", region, f"/{bucket}/{key}", payload=body, timeout=TRANSFER_TIMEOUT
        )
        self._verify_etag(headers.get("ETag"), _md5(body).hexdigest(), f"object {key}")

    async def async_delete_object(self, region: str, bucket: str, key: str) -> None:
        """Delete an object. Deleting a nonexistent key is not an error (S3 semantics)."""
        try:
            await self._s3_request("DELETE", region, f"/{bucket}/{key}")
        except ScalewayNotFoundError:
            pass

    async def async_create_multipart_upload(self, region: str, bucket: str, key: str) -> str:
        """Start a multipart upload, returning its UploadId."""
        _, body = await self._s3_request(
            "POST", region, f"/{bucket}/{key}", params={"uploads": ""}
        )
        root = _parse_xml(body)
        upload_id = _find_text(root, "UploadId") if root is not None else None
        if not upload_id:
            raise ScalewayApiError(f"No UploadId returned for multipart upload of {key}")
        return upload_id

    async def async_upload_part(
        self, region: str, bucket: str, key: str, upload_id: str, part_number: int, body: bytes
    ) -> tuple[str, bytes]:
        """Upload one part, verifying its ETag.

        Returns (etag, md5_digest); the raw digests are needed to verify the
        assembled object's composite ETag once the upload completes.
        """
        headers, _ = await self._s3_request(
            "PUT",
            region,
            f"/{bucket}/{key}",
            params={"partNumber": str(part_number), "uploadId": upload_id},
            payload=body,
            timeout=TRANSFER_TIMEOUT,
        )
        etag = headers.get("ETag")
        if not etag:
            raise ScalewayApiError(f"No ETag returned for part {part_number} of {key}")
        digest = _md5(body)
        self._verify_etag(etag, digest.hexdigest(), f"part {part_number} of {key}")
        return etag, digest.digest()

    async def async_complete_multipart_upload(
        self,
        region: str,
        bucket: str,
        key: str,
        upload_id: str,
        parts: list[dict],
        part_digests: list[bytes] | None = None,
    ) -> None:
        """Finish a multipart upload, verifying the assembled composite ETag."""
        root = ET.Element("CompleteMultipartUpload")
        for part in parts:
            part_el = ET.SubElement(root, "Part")
            ET.SubElement(part_el, "PartNumber").text = str(part["PartNumber"])
            ET.SubElement(part_el, "ETag").text = part["ETag"]
        body = ET.tostring(root, encoding="utf-8")

        # _parse_xml also rejects a 200-with-<Error> body, which would
        # otherwise record a backup that was never actually assembled.
        _, resp_body = await self._s3_request(
            "POST",
            region,
            f"/{bucket}/{key}",
            params={"uploadId": upload_id},
            payload=body,
            timeout=TRANSFER_TIMEOUT,
        )
        result = _parse_xml(resp_body)

        if part_digests:
            expected = f"{_md5(b''.join(part_digests)).hexdigest()}-{len(part_digests)}"
            etag = _find_text(result, "ETag") if result is not None else None
            self._verify_etag(etag, expected, f"assembled object {key}")

    async def async_abort_multipart_upload(
        self, region: str, bucket: str, key: str, upload_id: str
    ) -> None:
        """Best-effort cleanup of a failed multipart upload.

        Left un-aborted, the already-uploaded parts keep consuming billable
        storage and are invisible to ListObjectsV2.
        """
        try:
            await self._s3_request(
                "DELETE", region, f"/{bucket}/{key}", params={"uploadId": upload_id}
            )
        except ScalewayApiError:
            _LOGGER.warning("Failed to abort multipart upload %s for %s", upload_id, key)

    # ------------------------------------------------------------ S3 (core)

    @staticmethod
    def _verify_etag(raw_etag: str | None, expected_md5: str, what: str) -> None:
        """Compare a returned ETag against a locally computed MD5."""
        etag = _normalise_etag(raw_etag)
        if not etag or not _is_md5_style_etag(etag):
            # Server-side-encrypted buckets return a non-MD5 ETag; there is
            # nothing meaningful to compare against, so don't fail the upload.
            _LOGGER.debug("Skipping integrity check for %s (ETag %r not MD5-style)", what, etag)
            return
        if etag.lower() != expected_md5.lower():
            raise ScalewayIntegrityError(
                f"Integrity check failed for {what}: Scaleway stored {etag}, expected {expected_md5}"
            )

    @staticmethod
    def _raise_for_status(
        status: int, method: str, path: str, region: str, body: bytes = b""
    ) -> None:
        if status == 404:
            raise ScalewayNotFoundError(f"404 calling {method} {path} in {region}")
        if status in (401, 403):
            raise ScalewayAuthError(f"{status} calling {method} {path} in {region}")
        if status >= 400:
            raise ScalewayApiError(f"{status} calling {method} {path} in {region}: {body[:500]!r}")

    def _prepare_s3(
        self, method: str, region: str, path: str, params: dict, payload: bytes
    ) -> tuple[str, dict[str, str]]:
        """Build the signed URL + headers for one S3 request."""
        host = f"s3.{region}.scw.cloud"
        # Encoded exactly once here and reused for both the signature and the
        # real request URL, so the two can never drift out of sync — S3's
        # signature check fails hard on any byte-level mismatch.
        canonical_uri = quote(path, safe="/")
        headers, query_string = _sign_s3_request(
            method, host, canonical_uri, params, self._access_key, self._secret_key, region, payload
        )
        url = f"https://{host}{canonical_uri}"
        if query_string:
            url = f"{url}?{query_string}"
        return url, headers

    async def _s3_request(
        self,
        method: str,
        region: str,
        path: str,
        *,
        params: dict | None = None,
        payload: bytes = b"",
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> tuple[Mapping[str, str], bytes]:
        """Low-level signed S3 request. Returns (response_headers, response_body).

        Raises ScalewayNotFoundError on 404, ScalewayAuthError on 401/403,
        ScalewayApiError on any other failure — callers decide whether "not
        found" should be swallowed rather than this method doing it silently.
        """
        url, headers = self._prepare_s3(method, region, path, params or {}, payload)

        try:
            async with self._session.request(
                method,
                url,
                headers=headers,
                data=payload or None,
                timeout=timeout or REQUEST_TIMEOUT,
            ) as resp:
                body = await resp.read()
                resp_headers = resp.headers
                status = resp.status
        except _TRANSPORT_ERRORS as err:
            raise ScalewayApiError(f"Error calling {method} {path} in {region}: {err}") from err

        self._raise_for_status(status, method, path, region, body)
        return resp_headers, body


def _sign_s3_request(
    method: str,
    host: str,
    canonical_uri: str,
    params: dict,
    access_key: str,
    secret_key: str,
    region: str,
    payload: bytes,
) -> tuple[dict[str, str], str]:
    """Sign a request for Scaleway's S3-compatible API (AWS SigV4, service 's3').

    `canonical_uri` must already be percent-encoded (see `_prepare_s3`).
    Returns (headers, query_string) — the query string is built here, sorted
    and percent-encoded, and must be sent byte-for-byte as-is rather than
    re-encoded by the HTTP client, or the signature won't match what the
    server reconstructs.
    """
    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(payload).hexdigest()

    query_string = "&".join(
        f"{quote(str(k), safe='')}={quote(str(v), safe='')}" for k, v in sorted(params.items())
    )
    canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [method, canonical_uri, query_string, canonical_headers, signed_headers, payload_hash]
    )

    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        [algorithm, amz_date, credential_scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(f"AWS4{secret_key}".encode(), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, "s3")
    k_signing = _hmac(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    authorization = (
        f"{algorithm} Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    headers = {
        "Host": host,
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
        "Authorization": authorization,
    }
    return headers, query_string
