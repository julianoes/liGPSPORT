"""DeviceSession end-to-end against the simulator.

Exercises the flows the GUI drives — list activities, download one,
upload a route, list and delete routes — over
:class:`SimulatedTransport`, so every byte goes through the real
framing / envelope / protobuf path on both ends.
"""

from __future__ import annotations

import pathlib

import pytest

from ligpsport import commands
from ligpsport.client import IgpsportClient
from ligpsport.session import DeviceSession
from ligpsport.simulator import (
    SimulatedActivityFile,
    SimulatedTransport,
    SimulatorState,
)
from ligpsport.transport import Channel, Transport

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_FIT = (_REPO_ROOT / "tests" / "fixtures" / "activity_bsc200.fit").read_bytes()


def _session(state: SimulatorState) -> DeviceSession:
    return DeviceSession(
        "SIM", transport_factory=lambda _addr: SimulatedTransport(state), timeout=2.0
    )


def test_activity_file_local_start_and_filename() -> None:
    activity = commands.ActivityFile(timestamp=1129800000, file_size=0, user_id="", device_id="")
    assert activity.started_at.isoformat() == "2025-10-19T09:20:00"
    assert activity.default_filename() == "20251019T092000.fit"


async def test_session_lists_and_downloads_activity(tmp_path: pathlib.Path) -> None:
    state = SimulatorState(
        activity_files=[
            SimulatedActivityFile(timestamp=1129800000, file_size=len(_FIT), content=_FIT)
        ]
    )
    async with _session(state) as session:
        listing = await session.list_activities()
        assert len(listing.files) == 1
        activity = listing.files[0]
        out = tmp_path / activity.default_filename()
        result = await session.download_activity(activity, str(out))

    assert out.read_bytes() == _FIT
    assert result.fit_magic


async def test_session_upload_then_delete_route() -> None:
    state = SimulatorState(allow_destructive=True)
    async with _session(state) as session:
        uploaded = await session.upload_route(str(_REPO_ROOT / "route.geojson"))
        assert uploaded.status == 0
        routes = await session.list_routes()
        assert [r.id for r in routes.routes] == [uploaded.file_id]

        with pytest.raises(commands.DestructiveCommandError):
            await session.delete_route(uploaded.file_id)
        deleted = await session.delete_route(uploaded.file_id, allow_destructive=True)
        assert deleted.deleted
        assert (await session.list_routes()).routes == ()


async def test_session_reconnects_after_close() -> None:
    state = SimulatorState(activity_files=[SimulatedActivityFile(timestamp=1, file_size=1)])
    session = _session(state)
    assert (await session.list_activities()).files
    await session.close()
    assert not session.connected
    assert (await session.list_activities()).files
    await session.close()


class _DropFirstSend(Transport):
    """Simulated link that loses the first request, like a BSC200 after connect."""

    def __init__(self, state: SimulatorState) -> None:
        self._inner = SimulatedTransport(state)
        self.dropped = 0

    async def __aenter__(self) -> _DropFirstSend:
        await self._inner.__aenter__()
        return self

    async def send(self, frame: bytes, *, channel: Channel = "control") -> None:
        if self.dropped == 0:
            self.dropped += 1
            return
        await self._inner.send(frame, channel=channel)

    async def receive(self) -> bytes:
        return await self._inner.receive()

    async def close(self) -> None:
        await self._inner.close()


async def test_session_retries_unanswered_list() -> None:
    state = SimulatorState(activity_files=[SimulatedActivityFile(timestamp=1, file_size=1)])
    link = _DropFirstSend(state)
    session = DeviceSession(
        "SIM", transport_factory=lambda _addr: link, list_attempt_timeouts=(0.2, 2.0)
    )
    async with session:
        assert len((await session.list_activities()).files) == 1
    assert link.dropped == 1


async def test_simulated_transport_is_a_plain_client_peer() -> None:
    async with SimulatedTransport(SimulatorState()) as t, IgpsportClient(t) as client:
        assert isinstance(t, SimulatedTransport)
        assert (await commands.run_named(client, "version")).value is not None
