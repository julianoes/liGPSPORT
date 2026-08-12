"""Tests for the named command registry, running over the simulator."""

from __future__ import annotations

import pytest

from ligpsport.client import IgpsportClient
from ligpsport.commands import (
    COMMANDS,
    DeviceVersion,
    UnknownCommandError,
    get_command,
    list_commands,
    run_named,
)
from ligpsport.simulator import Simulator, SimulatorState
from ligpsport.transport import make_loopback_pair


def test_get_command_unknown_name() -> None:
    with pytest.raises(UnknownCommandError):
        get_command("nope")


def test_every_registered_command_has_a_description() -> None:
    for spec in list_commands():
        assert spec.description.strip()
        assert spec.runner is not None


async def test_run_named_version_against_simulator() -> None:
    state = SimulatorState(
        ble_app_ver=141, hardware_ver=100, protocol_ver=101, compile_time="May 14 2024 11:07:51"
    )
    client_t, peer_t = make_loopback_pair()
    async with Simulator(peer_t, state) as _sim, IgpsportClient(client_t) as client:
        result = await run_named(client, "version", timeout=2.0)

    assert isinstance(result.value, DeviceVersion)
    assert result.value.ble_app_ver == 141
    assert result.value.compile_time == "May 14 2024 11:07:51"

    d = result.to_dict()
    assert d["name"] == "version"
    assert d["value"]["ble_app_ver"] == 141  # type: ignore[index]


def test_destructive_prefixes_is_immutable() -> None:
    # Sanity: the canonical destructive list is a tuple, not a list,
    # so call sites can't accidentally mutate it.
    from ligpsport.commands import DESTRUCTIVE_PREFIXES

    assert isinstance(DESTRUCTIVE_PREFIXES, tuple)


def test_commands_registry_round_trips_through_run_named() -> None:
    # Every registered command resolves through get_command, and the
    # runner is callable. We don't actually run them all (most need a
    # live simulator handler); this just catches typos in the registry.
    for name, spec in COMMANDS.items():
        assert get_command(name) is spec
        assert callable(spec.runner)


def test_next_free_route_id_picks_smallest_unused_slot() -> None:
    from ligpsport.commands import _next_free_route_id

    # Empty device → slot 1.
    assert _next_free_route_id(set()) == 1
    # Slot 1 taken → slot 2.
    assert _next_free_route_id({1}) == 2
    # Gaps are filled before extending the range.
    assert _next_free_route_id({1, 3, 4}) == 2
    # Contiguous run → next after the max.
    assert _next_free_route_id({1, 2, 3}) == 4
    # Non-contiguous existing ids don't perturb the search from 1.
    assert _next_free_route_id({7, 42}) == 1
