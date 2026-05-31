"""
Tests for the capacity reporting helper.
"""

from unittest.mock import AsyncMock

import pytest

from vmware_ai_ops_agent.reporting import build_capacity_report
from vmware_ai_ops_agent.reporting.capacity import CapacityEntry


def _client(resources, capacity_by_id):
    client = AsyncMock()
    client.list_resources = AsyncMock(return_value=resources)

    async def _get_capacity(resource_id):
        return capacity_by_id[resource_id]

    client.get_capacity_remaining = AsyncMock(side_effect=_get_capacity)
    return client


@pytest.mark.asyncio
async def test_report_parses_and_sorts_by_soonest_exhaustion():
    resources = [
        {"identifier": "host-1", "resourceKey": {"name": "esxi-01"}},
        {"identifier": "host-2", "resourceKey": {"name": "esxi-02"}},
    ]
    capacity = {
        "host-1": {"remaining_capacity": 60.0, "time_remaining": 120},
        "host-2": {"remaining_capacity": 10.0, "time_remaining": 5},
    }
    client = _client(resources, capacity)

    entries = await build_capacity_report(client, resource_kind="HostSystem", limit=10)

    assert [e.resource_name for e in entries] == ["esxi-02", "esxi-01"]
    assert entries[0].time_remaining_days == 5.0
    assert entries[0].remaining_percent == 10.0


@pytest.mark.asyncio
async def test_report_handles_camelcase_keys():
    resources = [{"identifier": "host-1", "resourceKey": {"name": "esxi-01"}}]
    capacity = {"host-1": {"remainingCapacity": 42, "timeRemaining": 30}}
    client = _client(resources, capacity)

    entries = await build_capacity_report(client)

    assert entries[0].remaining_percent == 42.0
    assert entries[0].time_remaining_days == 30.0


@pytest.mark.asyncio
async def test_unknown_time_remaining_sorts_last():
    resources = [
        {"identifier": "host-1", "resourceKey": {"name": "known"}},
        {"identifier": "host-2", "resourceKey": {"name": "unknown"}},
    ]
    capacity = {
        "host-1": {"remaining_capacity": 50.0, "time_remaining": 90},
        "host-2": {},  # no capacity data
    }
    client = _client(resources, capacity)

    entries = await build_capacity_report(client)

    assert entries[-1].resource_name == "unknown"
    assert entries[-1].time_remaining_days is None


@pytest.mark.asyncio
async def test_failed_lookup_degrades_to_empty_entry():
    resources = [{"identifier": "host-1", "name": "esxi-01"}]
    client = AsyncMock()
    client.list_resources = AsyncMock(return_value=resources)
    client.get_capacity_remaining = AsyncMock(side_effect=RuntimeError("boom"))

    entries = await build_capacity_report(client)

    assert len(entries) == 1
    assert isinstance(entries[0], CapacityEntry)
    assert entries[0].resource_name == "esxi-01"
    assert entries[0].remaining_percent is None


@pytest.mark.asyncio
async def test_resources_without_id_are_skipped():
    resources = [
        {"resourceKey": {"name": "no-id"}},
        {"identifier": "host-2", "resourceKey": {"name": "ok"}},
    ]
    capacity = {"host-2": {"remaining_capacity": 50.0, "time_remaining": 90}}
    client = _client(resources, capacity)

    entries = await build_capacity_report(client)

    assert [e.resource_name for e in entries] == ["ok"]
