"""Unit tests for ScalewayApiClient — no Home Assistant needed here.

HTTP is faked with `tests/fake_session.py` rather than aioresponses; see that
module for why.

All figures below are invented. The *shapes* are taken from real responses on
Michael's account (see CLAUDE.md), but a public repo has no business carrying a
real person's cloud spend, so no amount here is one he was actually billed.
"""
from __future__ import annotations

import pytest
from urllib.parse import unquote, urlsplit

from custom_components.scaleway.api import (
    ScalewayApiClient,
    ScalewayApiError,
    ScalewayAuthError,
    ScalewayIntegrityError,
    ScalewayNotFoundError,
    _is_md5_style_etag,
    _md5,
)
from tests.fake_session import FakeResponse, FakeSession

ACCESS_KEY = "SCWXXXXXXXXXXXXXXXXX"
SECRET_KEY = "00000000-0000-4000-8000-000000000000"
ORG = "11111111-1111-4111-8111-111111111111"

CONSUMPTIONS = "/billing/v2beta1/consumptions"


def client(routes: dict) -> tuple[ScalewayApiClient, FakeSession]:
    session = FakeSession(routes)
    return ScalewayApiClient(session, ACCESS_KEY, SECRET_KEY), session


def money(amount: float) -> dict:
    """Scaleway money: integer units plus nanos, not a float."""
    units = int(amount // 1)
    nanos = round((amount - units) * 1_000_000_000)
    return {"currency_code": "EUR", "units": units, "nanos": nanos}


def line(amount: float, category: str = "Object Storage", product: str = "Standard") -> dict:
    return {"value": money(amount), "category_name": category, "product_name": product}


def consumptions(lines: list[dict], discount: float = 0) -> FakeResponse:
    return FakeResponse(
        json={
            "consumptions": lines,
            "total_count": len(lines),
            "total_discount_untaxed_value": discount,
        }
    )


# --------------------------------------------------------------------- cost


class TestCostAgainstTheInvoice:
    """The sensor's number has to be the number Scaleway invoices.

    Verified live on 2026-09-07 against all 80 invoices on Michael's
    organization: summing `consumptions[]` alone matches the invoice's
    `total_untaxed` in 68 periods and overstates it in the 12 periods
    (2020-11 .. 2021-10) where a 25% rate discount was active. The API hands
    us the difference in the same response, as the `total_discount_untaxed_value`
    trailer, which `async_get_cost` used to discard. See CLAUDE.md.

    FAILING-FIRST: this whole class was run against the pre-fix
    `async_get_cost` before the fix went in. Nine of its cases failed there —
    not only the two obviously about the discount, but also the category,
    zero-discount and empty-period ones, because the fix changed the returned
    dict's shape (it gained `gross` and `discount`) as well as its arithmetic.
    An earlier version of this docstring claimed only two failed and that the
    rest "passed before and after"; that was wrong and is corrected here rather
    than quietly dropped.

    Two of them are mutation-checked as well as failing-first, because failing
    against the old code does not prove they constrain the new code:
    `test_a_fractional_discount_is_not_truncated` fails under
    `int(discount)`, and the sibling `TestConsumptionsPaging` case fails when
    the trailer is read from any page but the first.
    """

    async def test_total_is_net_of_an_active_discount(self) -> None:
        """A rate discount is org-wide and appears ONLY in the trailer.

        Reproduces the real 25% case: gross 12.00, trailer 3.00, invoiced 9.00.
        """
        api, _ = client(
            {
                ("GET", CONSUMPTIONS): [
                    consumptions([line(8.0, "Compute"), line(4.0, "Object Storage")], discount=3.0)
                ]
            }
        )

        cost = await api.async_get_cost(ORG)

        assert cost["total"] == 9.0

    async def test_discount_and_gross_are_reported(self) -> None:
        """Both halves are exposed so the total is explicable, not just smaller."""
        api, _ = client(
            {("GET", CONSUMPTIONS): [consumptions([line(12.0, "Compute")], discount=3.0)]}
        )

        cost = await api.async_get_cost(ORG)

        assert cost["gross"] == 12.0
        assert cost["discount"] == 3.0
        assert cost["total"] == 9.0

    async def test_categories_stay_gross(self) -> None:
        """A rate discount has no category attribution, so it is not prorated.

        Scaleway gives one org-wide number with no per-category breakdown, and
        the discount-mode enum includes non-rate modes (a fixed-amount coupon
        would not split proportionally). Splitting it across categories would
        be invention, so categories stay gross and the total carries the
        discount — which means total != sum(categories) while one is active.
        """
        api, _ = client(
            {
                ("GET", CONSUMPTIONS): [
                    consumptions([line(8.0, "Compute"), line(4.0, "Object Storage")], discount=3.0)
                ]
            }
        )

        cost = await api.async_get_cost(ORG)

        assert cost["by_category"] == {"Compute": 8.0, "Object Storage": 4.0}
        assert cost["total"] != sum(cost["by_category"].values())

    async def test_a_fractional_discount_is_not_truncated(self) -> None:
        """Pins `float(...)`, not `int(...)`, on the trailer.

        Every other test in this class uses a whole-valued discount, so
        replacing `float(discount)` with `int(discount)` left all of them
        passing while production silently overstated a bill by the fractional
        part. 3.29 is the real figure from the verification account's 2021-10
        invoice; int() would report 9.87 as 9.87 + 0.29.
        """
        api, _ = client(
            {("GET", CONSUMPTIONS): [consumptions([line(13.16, "Compute")], discount=3.29)]}
        )

        cost = await api.async_get_cost(ORG)

        assert cost["total"] == 9.87
        assert cost["discount"] == 3.29
        assert cost["gross"] == 13.16

    @pytest.mark.parametrize(
        ("gross", "discount", "expected"),
        [
            (15.86, 3.97, 11.89),
            (13.20, 3.30, 9.90),
            (11.92, 2.98, 8.94),
            (12.71, 3.18, 9.53),
        ],
    )
    async def test_real_invoice_periods_reproduce_exactly(
        self, gross: float, discount: float, expected: float
    ) -> None:
        """Four of the twelve overstating periods, replayed through the client.

        These are the actual (sum, trailer, invoiced) triples read off the
        verification account, so this asserts against Scaleway's own arithmetic
        rather than against a number this code produced.
        """
        api, _ = client(
            {("GET", CONSUMPTIONS): [consumptions([line(gross, "Compute")], discount=discount)]}
        )

        assert (await api.async_get_cost(ORG))["total"] == expected

    async def test_no_discount_leaves_the_total_alone(self) -> None:
        """The 68-of-80 case: the trailer is 0 and the sum is already the invoice."""
        api, _ = client(
            {
                ("GET", CONSUMPTIONS): [
                    consumptions([line(8.0, "Compute"), line(1.45, "Object Storage")], discount=0)
                ]
            }
        )

        cost = await api.async_get_cost(ORG)

        assert cost["total"] == 9.45
        assert cost["discount"] == 0.0
        assert cost["gross"] == 9.45

    async def test_a_missing_trailer_raises_rather_than_defaulting_to_zero(self) -> None:
        """Absence is a schema change, not "no discount" — so it must not be silent.

        The field is present on every real response including when it is zero
        (verified across all 80 billing periods of a real account). Defaulting a
        missing one to 0 is a fail-open: the code reads as fixed while quietly
        restoring the overstatement for exactly the users the subtraction is
        for. Better a loud UpdateFailed and an unavailable sensor.
        """
        api, _ = client(
            {("GET", CONSUMPTIONS): [FakeResponse(json={"consumptions": [line(5.0)]})]}
        )

        with pytest.raises(ScalewayApiError, match="total_discount_untaxed_value"):
            await api.async_get_cost(ORG)

    async def test_an_unparseable_trailer_raises(self) -> None:
        api, _ = client(
            {
                ("GET", CONSUMPTIONS): [
                    FakeResponse(
                        json={
                            "consumptions": [line(5.0)],
                            "total_discount_untaxed_value": "lots",
                        }
                    )
                ]
            }
        )

        with pytest.raises(ScalewayApiError):
            await api.async_get_cost(ORG)

    async def test_an_integer_zero_trailer_is_accepted(self) -> None:
        """The live API sends int 0 and float 3.29 for the same field."""
        api, _ = client(
            {
                ("GET", CONSUMPTIONS): [
                    FakeResponse(
                        json={
                            "consumptions": [line(5.0)],
                            "total_count": 1,
                            "total_discount_untaxed_value": 0,
                        }
                    )
                ]
            }
        )

        assert (await api.async_get_cost(ORG))["total"] == 5.0

    async def test_free_tier_deduction_is_a_line_item_not_a_discount(self) -> None:
        """Free tier and rate discounts are disjoint mechanisms — no double-count.

        Free tier arrives as a negative "Offer deducted" line item inside
        `consumptions[]` (confirmed live: the current period carries one for
        external bandwidth), while a rate discount arrives only in the trailer.
        Subtracting the trailer must not also re-subtract the line item.
        """
        api, _ = client(
            {
                ("GET", CONSUMPTIONS): [
                    consumptions(
                        [
                            line(2.0, "Object Storage", "External Bandwidth"),
                            line(-0.5, "Object Storage", "Outgoing bandwidth free tier"),
                        ],
                        discount=0,
                    )
                ]
            }
        )

        cost = await api.async_get_cost(ORG)

        assert cost["total"] == 1.5
        assert cost["by_category"] == {"Object Storage": 1.5}

    async def test_nanos_are_summed_before_rounding(self) -> None:
        """Rounding each line item first would drift on a long invoice."""
        api, _ = client(
            {("GET", CONSUMPTIONS): [consumptions([line(0.004) for _ in range(10)])]}
        )

        cost = await api.async_get_cost(ORG)

        # Ten lines of 0.004 is 0.04; rounding each to 0.00 first would give 0.
        assert cost["total"] == 0.04

    async def test_currency_comes_from_the_response(self) -> None:
        api, _ = client({("GET", CONSUMPTIONS): [consumptions([line(1.0)])]})

        assert (await api.async_get_cost(ORG))["currency"] == "EUR"

    async def test_an_empty_period_is_zero_not_an_error(self) -> None:
        """A brand-new organization has no consumption yet."""
        api, _ = client({("GET", CONSUMPTIONS): [consumptions([])]})

        cost = await api.async_get_cost(ORG)

        assert cost == {
            "total": 0.0,
            "gross": 0.0,
            "discount": 0.0,
            "currency": "EUR",
            "by_category": {},
        }

    async def test_a_line_item_without_a_category_is_bucketed_not_dropped(self) -> None:
        api, _ = client(
            {
                ("GET", CONSUMPTIONS): [
                    consumptions([{"value": money(2.0), "product_name": "Mystery"}])
                ]
            }
        )

        cost = await api.async_get_cost(ORG)

        assert cost["by_category"] == {"Other": 2.0}
        assert cost["total"] == 2.0

    async def test_the_organization_is_passed_as_a_query_parameter(self) -> None:
        api, session = client({("GET", CONSUMPTIONS): [consumptions([line(1.0)])]})

        await api.async_get_cost(ORG)

        assert session.calls[0].params["organization_id"] == ORG


class TestConsumptionsPaging:
    """/consumptions pages, and reading one page silently undercounts.

    Verified live: with `page_size=1` the endpoint returns `total_count=6` and
    one item, `page` is 1-indexed, and a page past the end returns an empty
    `consumptions[]` *and* a trailer of 0. The verification account never pages
    by default (its largest period had 18 line items), which is exactly why
    this could sit undetected — a larger organization's total would simply come
    out low, with nothing in the response to indicate it.

    FAILING-FIRST: every test in this class was run against the pre-fix
    `async_get_cost`, which issued one unpaged request. `test_all_pages_are_summed`
    reported 15.0 instead of 45.0; `test_a_short_read_raises...` and
    `test_the_page_cap_is_enforced` did not raise at all;
    `test_an_overrun_page_does_not_zero_the_discount` reported the full gross
    with no discount subtracted.
    """

    @staticmethod
    def _paged(items: list[dict], *, total_count: int | None, discount: float = 0.0):
        """Route that pages `items` using the page/page_size the client sends.

        Deliberately driven by the request rather than by a hardcoded slice
        size: a fixture that ignores `page_size` does not establish that
        production sends the size its own termination logic assumes.
        """

        def route(call):
            page = int(call.params["page"])
            size = int(call.params["page_size"])
            chunk = items[(page - 1) * size : page * size]
            body: dict = {"consumptions": chunk}
            # A page past the end carries a zero trailer — verified against the
            # live API. This is what makes "read the trailer from the last
            # response" a silent reintroduction of the whole bug.
            body["total_discount_untaxed_value"] = discount if chunk else 0
            if total_count is not None:
                body["total_count"] = total_count if chunk else 0
            return FakeResponse(json=body)

        return route

    async def test_all_pages_are_summed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("custom_components.scaleway.api.CONSUMPTIONS_PAGE_SIZE", 2)
        items = [
            line(10.0, "Compute"),
            line(5.0, "Compute"),
            line(20.0, "Object Storage"),
            line(7.0, "Object Storage"),
            line(3.0, "Network"),
        ]
        api, session = client({("GET", CONSUMPTIONS): self._paged(items, total_count=5)})

        cost = await api.async_get_cost(ORG)

        assert cost["total"] == 45.0
        assert cost["by_category"] == {"Compute": 15.0, "Object Storage": 27.0, "Network": 3.0}
        assert [c.params["page"] for c in session.calls] == ["1", "2", "3"]

    async def test_the_client_sends_the_page_size_its_logic_assumes(self) -> None:
        """Termination keys off `len(batch) < CONSUMPTIONS_PAGE_SIZE`, which is
        only meaningful if that is the size actually requested."""
        from custom_components.scaleway.api import CONSUMPTIONS_PAGE_SIZE

        api, session = client(
            {("GET", CONSUMPTIONS): self._paged([line(1.0)], total_count=1)}
        )

        await api.async_get_cost(ORG)

        assert session.calls[0].params["page_size"] == str(CONSUMPTIONS_PAGE_SIZE)

    async def test_a_single_short_page_makes_only_one_request(self) -> None:
        """The common case must not cost an extra round trip per poll."""
        api, session = client(
            {("GET", CONSUMPTIONS): self._paged([line(1.0)], total_count=1)}
        )

        await api.async_get_cost(ORG)

        assert len(session.calls) == 1

    async def test_an_overrun_page_does_not_zero_the_discount(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The trailer must come from page 1, never from the last response.

        This needs a response with NO `total_count`, so the loop cannot stop on
        the count and is forced to request a page past the end — the page that
        reports a trailer of 0. With a count present the overrun page is
        unreachable, which is how an earlier version of this test managed to
        pass even when the trailer was re-read on every page.

        MUTATION-CHECKED: moving the trailer read out of the `if page == 1`
        guard leaves every other test in this class passing and fails only this
        one.
        """
        monkeypatch.setattr("custom_components.scaleway.api.CONSUMPTIONS_PAGE_SIZE", 2)
        items = [line(3.0), line(3.0), line(3.0), line(3.0)]
        api, session = client(
            {("GET", CONSUMPTIONS): self._paged(items, total_count=None, discount=3.0)}
        )

        cost = await api.async_get_cost(ORG)

        # Page 3 was requested and came back empty with a zero trailer...
        assert [c.params["page"] for c in session.calls] == ["1", "2", "3"]
        # ...and page 1's discount survived it.
        assert cost["discount"] == 3.0
        assert cost["gross"] == 12.0
        assert cost["total"] == 9.0

    async def test_a_short_read_raises_rather_than_reporting_a_low_total(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """total_count says 5; the server hands back 3. A quiet low bill is
        worse than no bill."""
        monkeypatch.setattr("custom_components.scaleway.api.CONSUMPTIONS_PAGE_SIZE", 2)
        api, _ = client(
            {
                ("GET", CONSUMPTIONS): self._paged(
                    [line(1.0), line(1.0), line(1.0)], total_count=5
                )
            }
        )

        with pytest.raises(ScalewayApiError, match="partial total"):
            await api.async_get_cost(ORG)

    async def test_the_page_cap_is_enforced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An endpoint that never runs out must not loop forever against billing."""
        monkeypatch.setattr("custom_components.scaleway.api.CONSUMPTIONS_PAGE_SIZE", 1)
        monkeypatch.setattr("custom_components.scaleway.api.MAX_CONSUMPTION_PAGES", 3)
        api, session = client(
            {
                ("GET", CONSUMPTIONS): lambda call: FakeResponse(
                    json={
                        "consumptions": [line(1.0)],
                        "total_count": 10_000,
                        "total_discount_untaxed_value": 0,
                    }
                )
            }
        )

        with pytest.raises(ScalewayApiError, match="within 3 pages"):
            await api.async_get_cost(ORG)
        assert len(session.calls) == 3

    async def test_exactly_filling_the_last_allowed_page_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cap must not reject a legitimately complete read that happens to
        land on the final permitted page."""
        monkeypatch.setattr("custom_components.scaleway.api.CONSUMPTIONS_PAGE_SIZE", 2)
        monkeypatch.setattr("custom_components.scaleway.api.MAX_CONSUMPTION_PAGES", 3)
        api, session = client(
            {("GET", CONSUMPTIONS): self._paged([line(1.0)] * 6, total_count=6)}
        )

        assert (await api.async_get_cost(ORG))["total"] == 6.0
        assert len(session.calls) == 3

    async def test_a_response_without_total_count_still_pages_to_the_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the count there is nothing to assert against, but a full page
        still has to be followed."""
        monkeypatch.setattr("custom_components.scaleway.api.CONSUMPTIONS_PAGE_SIZE", 2)
        api, session = client(
            {
                ("GET", CONSUMPTIONS): self._paged(
                    [line(1.0), line(1.0), line(1.0)], total_count=None
                )
            }
        )

        assert (await api.async_get_cost(ORG))["total"] == 3.0
        assert len(session.calls) == 2


# ---------------------------------------------------------------- REST auth


class TestRestErrors:
    @pytest.mark.parametrize("status", [401, 403])
    async def test_auth_statuses_raise_the_auth_error(self, status: int) -> None:
        """ScalewayAuthError is what makes the coordinator start a reauth flow."""
        api, _ = client({("GET", CONSUMPTIONS): [FakeResponse(status=status)]})

        with pytest.raises(ScalewayAuthError):
            await api.async_get_cost(ORG)

    async def test_a_server_error_is_not_an_auth_error(self) -> None:
        """A 500 must stay transient — nagging for credentials over a blip is wrong."""
        api, _ = client({("GET", CONSUMPTIONS): [FakeResponse(status=500, body=b"boom")]})

        with pytest.raises(ScalewayApiError) as err:
            await api.async_get_cost(ORG)
        assert not isinstance(err.value, ScalewayAuthError)

    @pytest.mark.parametrize(
        "exc", [TimeoutError("slow"), OSError("reset")], ids=["timeout", "oserror"]
    )
    async def test_transport_failures_are_wrapped(self, exc: BaseException) -> None:
        """Neither is an aiohttp.ClientError, so both need listing explicitly.

        An unwrapped TimeoutError escapes every `except ScalewayApiError`
        downstream — which is how a v0.2.0 backup upload skipped its multipart
        abort and left billable orphan parts behind.
        """
        api, _ = client({("GET", CONSUMPTIONS): exc})

        with pytest.raises(ScalewayApiError):
            await api.async_get_cost(ORG)


class TestResolveOrganizationId:
    """There is no whoami endpoint; org id comes from the key's principal."""

    async def test_a_user_key_resolves_through_the_users_endpoint(self) -> None:
        api, session = client(
            {
                ("GET", f"/iam/v1alpha1/api-keys/{ACCESS_KEY}"): [
                    FakeResponse(json={"user_id": "u1", "default_project_id": "p1"})
                ],
                ("GET", "/iam/v1alpha1/users/u1"): [FakeResponse(json={"organization_id": ORG})],
            }
        )

        assert await api.async_resolve_organization_id() == ORG
        assert session.paths() == [
            f"/iam/v1alpha1/api-keys/{ACCESS_KEY}",
            "/iam/v1alpha1/users/u1",
        ]

    async def test_an_application_key_resolves_through_the_applications_endpoint(self) -> None:
        """IAM application keys have no user_id — the recommended kind for this."""
        api, _ = client(
            {
                ("GET", f"/iam/v1alpha1/api-keys/{ACCESS_KEY}"): [
                    FakeResponse(json={"application_id": "a1"})
                ],
                ("GET", "/iam/v1alpha1/applications/a1"): [
                    FakeResponse(json={"organization_id": ORG})
                ],
            }
        )

        assert await api.async_resolve_organization_id() == ORG

    async def test_a_key_bound_to_neither_is_an_error(self) -> None:
        api, _ = client(
            {("GET", f"/iam/v1alpha1/api-keys/{ACCESS_KEY}"): [FakeResponse(json={})]}
        )

        with pytest.raises(ScalewayApiError):
            await api.async_resolve_organization_id()

    async def test_a_principal_without_an_organization_is_an_error(self) -> None:
        api, _ = client(
            {
                ("GET", f"/iam/v1alpha1/api-keys/{ACCESS_KEY}"): [
                    FakeResponse(json={"user_id": "u1"})
                ],
                ("GET", "/iam/v1alpha1/users/u1"): [FakeResponse(json={})],
            }
        )

        with pytest.raises(ScalewayApiError):
            await api.async_resolve_organization_id()


class TestZonedAndRegionalListing:
    """Instance is zoned and Kapsule regional; neither has an "all" endpoint."""

    async def test_instances_are_collected_across_zones(self) -> None:
        api, session = client(
            {
                ("GET", "/instance/v1/zones/fr-par-1/servers"): [
                    FakeResponse(
                        json={
                            "servers": [
                                {
                                    "id": "i1",
                                    "name": "web",
                                    "commercial_type": "DEV1-S",
                                    "state": "running",
                                }
                            ]
                        }
                    )
                ],
                ("GET", "/instance/v1/zones/nl-ams-1/servers"): [FakeResponse(json={"servers": []})],
            }
        )

        instances = await api.async_list_instances(["fr-par-1", "nl-ams-1"])

        assert instances == [
            {
                "id": "i1",
                "name": "web",
                "zone": "fr-par-1",
                "commercial_type": "DEV1-S",
                "state": "running",
            }
        ]
        assert len(session.calls) == 2

    async def test_one_failing_zone_does_not_lose_the_others(self) -> None:
        """Most accounts have resources in one zone; the rest 403 or 404."""
        api, _ = client(
            {
                ("GET", "/instance/v1/zones/fr-par-1/servers"): [FakeResponse(status=403)],
                ("GET", "/instance/v1/zones/nl-ams-1/servers"): [
                    FakeResponse(json={"servers": [{"id": "i2", "name": "db"}]})
                ],
            }
        )

        instances = await api.async_list_instances(["fr-par-1", "nl-ams-1"])

        assert [i["id"] for i in instances] == ["i2"]

    async def test_clusters_are_collected_across_regions(self) -> None:
        api, _ = client(
            {
                ("GET", "/k8s/v1/regions/fr-par/clusters"): [
                    FakeResponse(
                        json={
                            "clusters": [
                                {"id": "c1", "name": "kap", "status": "ready", "version": "1.31"}
                            ]
                        }
                    )
                ],
                ("GET", "/k8s/v1/regions/nl-ams/clusters"): [FakeResponse(json={"clusters": []})],
            }
        )

        clusters = await api.async_list_clusters(["fr-par", "nl-ams"])

        assert clusters == [
            {
                "id": "c1",
                "name": "kap",
                "region": "fr-par",
                "status": "ready",
                "version": "1.31",
            }
        ]


# ------------------------------------------------------------ Object Storage

LIST_BUCKETS_NO_NS = b"""<?xml version="1.0" encoding="UTF-8"?>
<ListAllMyBucketsResult>
  <Owner><ID>owner</ID></Owner>
  <Buckets>
    <Bucket><Name>alpha</Name><CreationDate>2026-01-01T00:00:00.000Z</CreationDate></Bucket>
    <Bucket><Name>beta</Name><CreationDate>2026-01-01T00:00:00.000Z</CreationDate></Bucket>
  </Buckets>
</ListAllMyBucketsResult>"""

NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


def list_objects(entries: list[tuple[str, int]], *, truncated: str | None = None) -> bytes:
    contents = "".join(
        f"<Contents><Key>{k}</Key><Size>{s}</Size></Contents>" for k, s in entries
    )
    trunc = (
        f"<IsTruncated>true</IsTruncated><NextContinuationToken>{truncated}</NextContinuationToken>"
        if truncated
        else "<IsTruncated>false</IsTruncated>"
    )
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult {NS}>{contents}{trunc}'
        "</ListBucketResult>"
    ).encode()


class TestBucketListing:
    async def test_list_buckets_parses_a_response_with_no_xml_namespace(self) -> None:
        """Scaleway's ListBuckets omits the xmlns its ListObjectsV2 declares.

        A namespace-qualified findall() silently returns nothing here — no
        error, just zero buckets and an empty picker in the config flow.
        Caught originally by live-testing against the real account.
        """
        api, _ = client({("GET", "/"): [FakeResponse(body=LIST_BUCKETS_NO_NS)]})

        buckets = await api.async_list_buckets(["nl-ams"])

        assert buckets == [
            {"name": "alpha", "region": "nl-ams"},
            {"name": "beta", "region": "nl-ams"},
        ]

    async def test_list_objects_parses_a_namespaced_response(self) -> None:
        """...and the same helpers must still work when the xmlns IS present."""
        api, _ = client(
            {("GET", "/b"): [FakeResponse(body=list_objects([("a.tar", 10), ("b.tar", 20)]))]}
        )

        assert await api.async_list_objects("nl-ams", "b") == [
            {"key": "a.tar", "size": 10},
            {"key": "b.tar", "size": 20},
        ]

    async def test_a_failing_region_does_not_lose_the_others(self) -> None:
        api, _ = client(
            {
                ("GET", "/"): [
                    FakeResponse(status=403),
                    FakeResponse(body=LIST_BUCKETS_NO_NS),
                    FakeResponse(body=LIST_BUCKETS_NO_NS),
                ]
            }
        )

        buckets = await api.async_list_buckets(["fr-par", "nl-ams"])

        assert [b["region"] for b in buckets] == ["nl-ams", "nl-ams"]


class TestBucketSize:
    """No native size endpoint exists, so size means paginating every object."""

    async def test_sizes_and_counts_are_summed_across_pages(self) -> None:
        api, session = client(
            {
                ("GET", "/kroes"): [
                    FakeResponse(body=list_objects([("a", 100), ("b", 200)], truncated="tok1")),
                    FakeResponse(body=list_objects([("c", 300)])),
                ]
            }
        )

        assert await api.async_get_bucket_size("nl-ams", "kroes") == {
            "size_bytes": 600,
            "object_count": 3,
        }
        assert session.calls[1].params["continuation-token"] == "tok1"

    async def test_a_truncated_page_with_no_token_raises(self) -> None:
        """Stopping is forced; reporting what we have is not.

        An earlier version of this test asserted that the partial size (5) was
        returned successfully — which blessed a real defect as correct
        behaviour. A short bucket size reads as a genuine measurement on a
        dashboard and there is nothing to indicate it is incomplete, so it must
        fail loudly instead.
        """
        body = (
            f'<ListBucketResult {NS}><Contents><Key>a</Key><Size>5</Size></Contents>'
            "<IsTruncated>true</IsTruncated></ListBucketResult>"
        ).encode()
        api, session = client({("GET", "/kroes"): [FakeResponse(body=body)]})

        with pytest.raises(ScalewayApiError, match="partial size"):
            await api.async_get_bucket_size("nl-ams", "kroes")
        # Forced to stop, not looping forever on a token that never arrives.
        assert len(session.calls) == 1

    async def test_a_truncated_object_listing_with_no_token_raises(self) -> None:
        """Worse here than for size: a short listing hides backups from HA, and
        HA's retention logic acts on what it can see."""
        body = (
            f'<ListBucketResult {NS}><Contents><Key>a</Key><Size>5</Size></Contents>'
            "<IsTruncated>true</IsTruncated></ListBucketResult>"
        ).encode()
        api, _ = client({("GET", "/kroes"): [FakeResponse(body=body)]})

        with pytest.raises(ScalewayApiError, match="partial listing"):
            await api.async_list_objects("nl-ams", "kroes")

    async def test_a_deleted_bucket_returns_none_rather_than_raising(self) -> None:
        """Selected in options, deleted in the console — not an integration error."""
        api, _ = client({("GET", "/gone"): [FakeResponse(status=404)]})

        assert await api.async_get_bucket_size("nl-ams", "gone") is None

    async def test_an_empty_bucket_is_zero_not_none(self) -> None:
        api, _ = client({("GET", "/empty"): [FakeResponse(body=list_objects([]))]})

        assert await api.async_get_bucket_size("nl-ams", "empty") == {
            "size_bytes": 0,
            "object_count": 0,
        }

    async def test_a_non_numeric_size_is_skipped_not_fatal(self) -> None:
        body = (
            f'<ListBucketResult {NS}><Contents><Key>a</Key><Size>oops</Size></Contents>'
            "<Contents><Key>b</Key><Size>7</Size></Contents>"
            "<IsTruncated>false</IsTruncated></ListBucketResult>"
        ).encode()
        api, _ = client({("GET", "/b"): [FakeResponse(body=body)]})

        assert await api.async_get_bucket_size("nl-ams", "b") == {
            "size_bytes": 7,
            "object_count": 2,
        }


class TestS3ErrorHandling:
    async def test_a_200_with_an_error_document_is_still_an_error(self) -> None:
        """S3 can answer 200 with <Error>; treating it as success records a
        backup that was never assembled."""
        body = b"<Error><Code>InternalError</Code><Message>nope</Message></Error>"
        api, _ = client({("GET", "/b"): [FakeResponse(status=200, body=body)]})

        with pytest.raises(ScalewayApiError, match="InternalError"):
            await api.async_list_objects("nl-ams", "b")

    async def test_malformed_xml_raises_rather_than_returning_nothing(self) -> None:
        api, _ = client({("GET", "/b"): [FakeResponse(body=b"<not xml")]})

        with pytest.raises(ScalewayApiError, match="Malformed"):
            await api.async_list_objects("nl-ams", "b")

    async def test_404_is_a_distinct_exception(self) -> None:
        api, _ = client({("GET", "/b/missing"): [FakeResponse(status=404)]})

        with pytest.raises(ScalewayNotFoundError):
            await api.async_get_object("nl-ams", "b", "missing")

    async def test_403_on_s3_is_an_auth_error(self) -> None:
        api, _ = client({("GET", "/b/x"): [FakeResponse(status=403)]})

        with pytest.raises(ScalewayAuthError):
            await api.async_get_object("nl-ams", "b", "x")

    async def test_deleting_a_missing_object_is_not_an_error(self) -> None:
        """S3 deletes are idempotent and callers rely on that."""
        api, _ = client({("DELETE", "/b/gone"): [FakeResponse(status=404)]})

        await api.async_delete_object("nl-ams", "b", "gone")


class TestSigning:
    async def test_requests_are_signed_with_sigv4(self) -> None:
        api, session = client({("GET", "/b"): [FakeResponse(body=list_objects([]))]})

        await api.async_list_objects("nl-ams", "b")

        headers = session.calls[0].kwargs["headers"]
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=")
        assert "/nl-ams/s3/aws4_request" in headers["Authorization"]
        assert headers["Host"] == "s3.nl-ams.scw.cloud"
        # An empty-body GET still has to carry the SHA-256 of the empty string.
        assert headers["x-amz-content-sha256"] == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    async def test_a_payload_is_hashed_into_the_signature(self) -> None:
        """The signer once only ever handled empty-body GETs."""
        api, session = client({("PUT", "/b/k"): [FakeResponse(headers={"ETag": '"x"'})]})

        try:
            await api.async_put_object("nl-ams", "b", "k", b"hello")
        except ScalewayIntegrityError:
            pass  # ETag mismatch is this test's non-concern.

        import hashlib

        assert session.calls[0].kwargs["headers"]["x-amz-content-sha256"] == (
            hashlib.sha256(b"hello").hexdigest()
        )

    async def test_the_signed_uri_and_the_request_url_cannot_drift(self) -> None:
        """Both come from one quote() call; a mismatch is an instant 403.

        A key needing percent-encoding used to be quoted twice, independently.
        """
        key = "Before upgrade (2026.7)/backup.tar"
        api, session = client({("GET", f"/b/{key}"): [FakeResponse(body=b"")]})

        await api.async_get_object("nl-ams", "b", key)

        # The recorded URL is what got sent; the path it decodes to is what got
        # signed. FakeSession parses the URL, so equality here is the check.
        assert session.calls[0].path == f"/b/{key}"
        # quote() leaves "(" alone but escapes the space, and the same encoded
        # string is what went into the canonical URI that was signed.
        assert "%20" in session.calls[0].url


class TestEtagIntegrity:
    async def test_a_matching_etag_passes(self) -> None:
        payload = b"contents"
        etag = _md5(payload).hexdigest()
        api, _ = client({("PUT", "/b/k"): [FakeResponse(headers={"ETag": f'"{etag}"'})]})

        await api.async_put_object("nl-ams", "b", "k", payload)

    async def test_a_mismatched_etag_fails_the_upload_loudly(self) -> None:
        """A corrupted backup must not be recorded as a good one."""
        api, _ = client({("PUT", "/b/k"): [FakeResponse(headers={"ETag": '"' + "0" * 32 + '"'})]})

        with pytest.raises(ScalewayIntegrityError):
            await api.async_put_object("nl-ams", "b", "k", b"contents")

    async def test_a_non_md5_etag_skips_the_check(self) -> None:
        """SSE-C/KMS buckets return something that is not an MD5 at all."""
        api, _ = client({("PUT", "/b/k"): [FakeResponse(headers={"ETag": '"not-an-md5"'})]})

        await api.async_put_object("nl-ams", "b", "k", b"contents")

    @pytest.mark.parametrize(
        ("etag", "expected"),
        [
            ("d41d8cd98f00b204e9800998ecf8427e", True),
            ("D41D8CD98F00B204E9800998ECF8427E", True),
            ("d41d8cd98f00b204e9800998ecf8427e-2", True),
            ("d41d8cd98f00b204e9800998ecf8427e-x", False),
            ("short", False),
            ("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", False),
        ],
    )
    def test_md5_shaped_etag_detection(self, etag: str, expected: bool) -> None:
        assert _is_md5_style_etag(etag) is expected


class TestMultipart:
    async def test_the_composite_etag_of_the_assembled_object_is_verified(self) -> None:
        parts = [b"a" * 8, b"b" * 4]
        digests = [_md5(p).digest() for p in parts]
        composite = f"{_md5(b''.join(digests)).hexdigest()}-2"
        body = f"<CompleteMultipartUploadResult><ETag>&quot;{composite}&quot;</ETag></CompleteMultipartUploadResult>"
        api, _ = client({("POST", "/b/k"): [FakeResponse(body=body.encode())]})

        await api.async_complete_multipart_upload(
            "nl-ams",
            "b",
            "k",
            "up1",
            [{"PartNumber": 1, "ETag": '"x"'}, {"PartNumber": 2, "ETag": '"y"'}],
            part_digests=digests,
        )

    async def test_a_wrong_composite_etag_fails(self) -> None:
        body = b'<CompleteMultipartUploadResult><ETag>"' + b"0" * 32 + b'-2"</ETag></CompleteMultipartUploadResult>'
        api, _ = client({("POST", "/b/k"): [FakeResponse(body=body)]})

        with pytest.raises(ScalewayIntegrityError):
            await api.async_complete_multipart_upload(
                "nl-ams", "b", "k", "up1", [], part_digests=[_md5(b"a").digest()] * 2
            )

    async def test_a_missing_upload_id_is_an_error(self) -> None:
        api, _ = client({("POST", "/b/k"): [FakeResponse(body=b"<InitiateMultipartUploadResult/>")]})

        with pytest.raises(ScalewayApiError, match="No UploadId"):
            await api.async_create_multipart_upload("nl-ams", "b", "k")

    async def test_abort_swallows_failures(self) -> None:
        """Best effort — the caller is already handling a more interesting error."""
        api, _ = client({("DELETE", "/b/k"): [FakeResponse(status=500)]})

        await api.async_abort_multipart_upload("nl-ams", "b", "k", "up1")


class TestStreamingDownload:
    async def test_the_body_is_streamed_in_chunks(self) -> None:
        """Michael's real backups are 4-5 GB; resp.read() would OOM a Pi."""
        api, _ = client({("GET", "/b/big.tar"): [FakeResponse(body=b"0123456789")]})

        stream = await api.async_open_object_stream("nl-ams", "b", "big.tar", chunk_size=4)

        assert [c async for c in stream] == [b"0123", b"4567", b"89"]

    async def test_a_missing_object_raises_before_iteration_starts(self) -> None:
        """Not part-way through the generator, where callers cannot react."""
        api, _ = client({("GET", "/b/gone.tar"): [FakeResponse(status=404)]})

        with pytest.raises(ScalewayNotFoundError):
            await api.async_open_object_stream("nl-ams", "b", "gone.tar")

    async def test_the_connection_is_released_when_the_stream_ends(self) -> None:
        resp = FakeResponse(body=b"abcd")
        api, _ = client({("GET", "/b/x"): [resp]})

        stream = await api.async_open_object_stream("nl-ams", "b", "x", chunk_size=2)
        [c async for c in stream]

        assert resp.released is True

    async def test_a_stall_mid_stream_becomes_a_scaleway_error(self) -> None:
        """A bare TimeoutError here escapes the backup agent's error contract."""
        resp = FakeResponse(body=b"abcdefgh", stream_fail_after=1)
        api, _ = client({("GET", "/b/x"): [resp]})

        stream = await api.async_open_object_stream("nl-ams", "b", "x", chunk_size=2)

        with pytest.raises(ScalewayApiError):
            [c async for c in stream]
        assert resp.released is True
