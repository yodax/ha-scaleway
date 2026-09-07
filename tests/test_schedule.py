"""Tests for the per-install poll offset.

This integration is published through HACS, so its poll schedule is not one
machine's business — every install running the same schedule is a thundering
herd against an API we do not own. The offset exists to spread that, and these
tests pin the two properties that make it work: stable across restarts, and
different across installs.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.scaleway.api import ScalewayApiClient
from custom_components.scaleway.const import (
    BUCKET_SCAN_INTERVAL,
    CONF_ACCESS_KEY,
    CONF_ORGANIZATION_ID,
    CONF_SECRET_KEY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_POLL_OFFSET,
)
from custom_components.scaleway.coordinator import (
    ScalewayBucketsCoordinator,
    ScalewayCoordinator,
    poll_offset,
)

ORG = "11111111-1111-4111-8111-111111111111"


class TestScanInterval:
    def test_the_main_interval_is_hourly(self) -> None:
        """Billing moves hourly at best; 10 minutes polled ~6x faster than the data."""
        assert DEFAULT_SCAN_INTERVAL == timedelta(hours=1)

    def test_bucket_sizing_is_no_more_frequent_than_the_main_poll(self) -> None:
        """Sizing a bucket lists every object in it, so it must never be cheaper-looking."""
        assert BUCKET_SCAN_INTERVAL >= DEFAULT_SCAN_INTERVAL


class TestPollOffset:
    def test_offset_is_within_the_configured_bound(self) -> None:
        for n in range(500):
            assert timedelta() <= poll_offset(f"entry-{n}") < MAX_POLL_OFFSET

    def test_same_seed_always_gives_the_same_offset(self) -> None:
        assert poll_offset("01KY2Q7SV1FY74NVS14SR6HG62") == poll_offset(
            "01KY2Q7SV1FY74NVS14SR6HG62"
        )

    def test_offset_is_stable_across_processes(self) -> None:
        """The trap this guards: Python randomises `hash()` per process.

        Using the builtin `hash()` here would look perfectly deterministic —
        every other test in this class would still pass — while silently
        re-rolling each install's offset on every Home Assistant restart.
        Recomputing in subprocesses under different PYTHONHASHSEEDs is the only
        way to catch it. Confirmed by temporarily swapping `hash()` back into
        `poll_offset`: this test fails and no other does.
        """
        seeds = ["entry-a", "entry-b", "01KY2Q7SV1FY74NVS14SR6HG62"]
        expected = [poll_offset(s).total_seconds() for s in seeds]

        script = (
            "import sys;"
            "sys.path.insert(0, %r);"
            "from custom_components.scaleway.coordinator import poll_offset;"
            "print([poll_offset(s).total_seconds() for s in %r])"
            % (str(Path(__file__).parent.parent), seeds)
        )
        results = []
        for seed_env in ("1", "12345"):
            env = {**os.environ, "PYTHONHASHSEED": seed_env}
            proc = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, env=env
            )
            assert proc.returncode == 0, proc.stderr
            results.append(eval(proc.stdout))

        assert results[0] == expected
        assert results[1] == expected

    def test_different_entries_get_different_offsets(self) -> None:
        """The whole point — two installs must not land on the same second."""
        offsets = {poll_offset(f"entry-{n}") for n in range(200)}

        # 15 minutes of whole seconds is 900 buckets; 200 draws should fill most
        # of them rather than clustering.
        assert len(offsets) > 150

    def test_offsets_spread_across_the_whole_window(self) -> None:
        """A broken derivation that always returned a small value would still
        satisfy the bound check above."""
        offsets = [poll_offset(f"entry-{n}").total_seconds() for n in range(500)]
        bound = MAX_POLL_OFFSET.total_seconds()

        for quarter in range(4):
            low, high = bound * quarter / 4, bound * (quarter + 1) / 4
            assert sum(1 for o in offsets if low <= o < high) > 60

    def test_a_real_ulid_entry_id_lands_in_range(self) -> None:
        assert timedelta() <= poll_offset("01KY2Q7SV1FY74NVS14SR6HG62") < MAX_POLL_OFFSET


def _entry(hass: HomeAssistant, entry_id: str = "01KY2Q7SV1FY74NVS14SR6HG62") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Scaleway (11111111)",
        entry_id=entry_id,
        unique_id=ORG,
        data={
            CONF_ACCESS_KEY: "SCWXXXXXXXXXXXXXXXXX",
            CONF_SECRET_KEY: "00000000-0000-4000-8000-000000000000",
            CONF_ORGANIZATION_ID: ORG,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _client() -> AsyncMock:
    client = AsyncMock(spec=ScalewayApiClient)
    client.async_get_cost.return_value = {
        "total": 1.0,
        "gross": 1.0,
        "discount": 0.0,
        "currency": "EUR",
        "by_category": {},
    }
    client.async_list_instances.return_value = []
    client.async_list_clusters.return_value = []
    client.async_get_bucket_size.return_value = {"size_bytes": 1, "object_count": 1}
    return client


class TestOffsetIsAppliedToTheSchedule:
    """The offset phase-shifts the grid; it does not change the period.

    Leaving `interval + offset` in place permanently would make the advertised
    hourly poll anything from 60 to 75 minutes depending on the install, which
    is a worse answer to "when does this update".
    """

    async def test_the_first_scheduled_poll_is_shifted(self, hass: HomeAssistant) -> None:
        entry = _entry(hass)
        coordinator = ScalewayCoordinator(hass, entry, _client())

        offset = poll_offset(entry.entry_id)
        assert coordinator.update_interval == DEFAULT_SCAN_INTERVAL + offset

    async def test_the_setup_refresh_does_not_consume_the_shift(
        self, hass: HomeAssistant
    ) -> None:
        """The immediate refresh at setup is not on the timer.

        Settling on it would arm the next timer at a plain hour and throw the
        phase shift away before it was ever applied.
        """
        entry = _entry(hass)
        coordinator = ScalewayCoordinator(hass, entry, _client())

        await coordinator.async_refresh()

        assert coordinator.update_interval == DEFAULT_SCAN_INTERVAL + poll_offset(entry.entry_id)

    async def test_it_settles_onto_the_plain_interval_afterwards(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(hass)
        coordinator = ScalewayCoordinator(hass, entry, _client())

        await coordinator.async_refresh()
        await coordinator.async_refresh()

        assert coordinator.update_interval == DEFAULT_SCAN_INTERVAL

        await coordinator.async_refresh()
        assert coordinator.update_interval == DEFAULT_SCAN_INTERVAL

    async def test_a_failed_first_poll_still_settles(self, hass: HomeAssistant) -> None:
        """The settle must not hang off success — a failing account would otherwise
        keep the shifted interval forever."""
        from custom_components.scaleway.api import ScalewayApiError

        entry = _entry(hass)
        client = _client()
        coordinator = ScalewayCoordinator(hass, entry, client)
        await coordinator.async_refresh()
        client.async_get_cost.side_effect = ScalewayApiError("boom")

        await coordinator.async_refresh()

        assert coordinator.last_update_success is False
        assert coordinator.update_interval == DEFAULT_SCAN_INTERVAL

    async def test_two_installs_of_the_same_organization_differ(
        self, hass: HomeAssistant
    ) -> None:
        """Seeding from the organization id instead would make these collide —
        which is exactly the case the offset exists for."""
        a = ScalewayCoordinator(hass, _entry(hass, "01KY2Q7SV1FY74NVS14SR6HG62"), _client())
        b = ScalewayCoordinator(hass, _entry(hass, "01M1XERWVWMQKHFAT35E7VWCJY"), _client())

        assert a.update_interval != b.update_interval

    async def test_both_coordinators_of_one_entry_share_a_phase(
        self, hass: HomeAssistant
    ) -> None:
        """One config entry, one offset — no reason to spread an install's own polls."""
        entry = _entry(hass)
        client = _client()
        main = ScalewayCoordinator(hass, entry, client)
        buckets = ScalewayBucketsCoordinator(hass, entry, client, [])

        offset = poll_offset(entry.entry_id)
        assert main.update_interval == DEFAULT_SCAN_INTERVAL + offset
        assert buckets.update_interval == BUCKET_SCAN_INTERVAL + offset

    async def test_the_bucket_coordinator_settles_too(self, hass: HomeAssistant) -> None:
        entry = _entry(hass)
        coordinator = ScalewayBucketsCoordinator(hass, entry, _client(), [])

        await coordinator.async_refresh()
        await coordinator.async_refresh()

        assert coordinator.update_interval == BUCKET_SCAN_INTERVAL

    async def test_the_timer_actually_fires_on_the_shifted_grid(
        self, hass: HomeAssistant, freezer: FrozenDateTimeFactory
    ) -> None:
        """Drives HA's real timer rather than calling async_refresh by hand.

        Every other test in this class inspects `update_interval` or calls
        `async_refresh()` directly, and none of them registers a listener — so
        HA never arms a timer and a regression in `_schedule_refresh`, or in the
        interval it reads, would go unnoticed. This one adds a listener (which
        is what arms the timer), then walks the clock across the first two
        scheduled callbacks and asserts on when the client was actually called.
        """
        entry = _entry(hass)
        client = _client()
        coordinator = ScalewayCoordinator(hass, entry, client)
        offset = poll_offset(entry.entry_id)

        entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
        await coordinator.async_config_entry_first_refresh()
        assert client.async_get_cost.await_count == 1  # the immediate setup poll

        coordinator.async_add_listener(lambda: None)

        # A minute short of the shifted mark: still nothing.
        freezer.tick(DEFAULT_SCAN_INTERVAL + offset - timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert client.async_get_cost.await_count == 1

        # Past it: the first scheduled poll, one offset late.
        freezer.tick(timedelta(minutes=2))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert client.async_get_cost.await_count == 2
        # ...and from here the period is plain, not interval + offset.
        assert coordinator.update_interval == DEFAULT_SCAN_INTERVAL

        # A minute short of a full interval later: still nothing.
        freezer.tick(DEFAULT_SCAN_INTERVAL - timedelta(minutes=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert client.async_get_cost.await_count == 2

        freezer.tick(timedelta(minutes=2))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert client.async_get_cost.await_count == 3

        await coordinator.async_shutdown()

    async def test_a_manual_refresh_before_the_first_timer_collapses_the_shift(
        self, hass: HomeAssistant
    ) -> None:
        """Documented, accepted limitation — pinned so it is a known trade.

        `_settle_offset_schedule` counts calls, and a user-triggered
        `homeassistant.update_entity` in between consumes the second one. The
        install then loses its phase, but re-anchors on the moment of that
        manual refresh, which is itself unsynchronised across installs — so the
        spreading survives even though the specific offset does not.
        """
        entry = _entry(hass)
        coordinator = ScalewayCoordinator(hass, entry, _client())

        entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
        await coordinator.async_config_entry_first_refresh()
        await coordinator.async_refresh()  # stands in for a manual refresh

        assert coordinator.update_interval == DEFAULT_SCAN_INTERVAL

    async def test_a_reload_gives_the_entry_the_same_offset_again(
        self, hass: HomeAssistant
    ) -> None:
        """A reload builds a fresh coordinator; entry_id is unchanged, so the
        offset must be too. If it were re-rolled, a reload loop would walk the
        poll time around the hour."""
        entry = _entry(hass)

        first = ScalewayCoordinator(hass, entry, _client())
        second = ScalewayCoordinator(hass, entry, _client())

        assert first.update_interval == second.update_interval

    async def test_no_daytime_window(self, hass: HomeAssistant) -> None:
        """Deliberately absent, unlike the sibling Trappers integration.

        Cloud cost accrues 24/7 and a server can die at 03:00, so there is no
        hour of the day at which not polling is correct here. This test exists
        so that "add a window like the other one" is a conscious change rather
        than a tidy-up.
        """
        entry = _entry(hass)
        coordinator = ScalewayCoordinator(hass, entry, _client())

        await coordinator.async_refresh()
        await coordinator.async_refresh()

        assert coordinator.update_interval == DEFAULT_SCAN_INTERVAL
        assert not hasattr(coordinator, "_poll_hours")
