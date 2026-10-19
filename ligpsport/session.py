"""Long-lived connection to one device, for interactive front-ends.

The CLI opens a BLE link per command. A GUI wants the opposite: connect
once when the user picks a device, then list activities, download,
upload and refresh over the same link. :class:`DeviceSession` owns that
link, serialises operations on it, and reconnects lazily after a
failure.

Everything goes through the command registry; the session adds no
protocol logic of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Final, TypeVar

from . import commands
from .client import IgpsportClient
from .transport import Transport

T = TypeVar("T")

TransportFactory = Callable[[str], Transport]
"""Builds an unopened transport for a device address."""

LIST_ATTEMPT_TIMEOUTS: Final[tuple[float, ...]] = (5.0, 15.0, 25.0)
"""Per-attempt waits for a LIST_GET reply.

The first LIST_GET after a connect is routinely never answered by a
BSC200 — it times out however long you wait, and the same request sent
again is answered within a second. So retry with a short first probe
rather than waiting longer. Same ladder as the Android app
(liGPSPORT-android ``FileTransfer.LIST_ATTEMPT_TIMEOUTS_MS``).
"""


def default_transport_factory(backend: str = "bleak") -> TransportFactory:
    """Return a factory for the named BLE backend (``bleak`` or ``bluez``)."""

    def _factory(address: str) -> Transport:
        if backend == "bluez":
            from .bluez import BluezTransport

            return BluezTransport(address)
        from .ble import BleakTransport

        return BleakTransport(address)

    return _factory


class DeviceSession:
    """A reusable, self-healing connection to one device.

    Operations connect on first use. If one fails for any reason other
    than a :class:`commands.CommandError` (bad arguments, unexpected
    reply), the link is torn down so the next call starts fresh — a
    dropped BLE connection would otherwise wedge every later request.
    """

    def __init__(
        self,
        address: str,
        *,
        transport_factory: TransportFactory | None = None,
        timeout: float = 10.0,
        list_attempt_timeouts: tuple[float, ...] = LIST_ATTEMPT_TIMEOUTS,
    ) -> None:
        self.address = address
        self.timeout = timeout
        self.list_attempt_timeouts = list_attempt_timeouts
        self._factory = transport_factory or default_transport_factory()
        self._stack: contextlib.AsyncExitStack | None = None
        self._transport: Transport | None = None
        self._client: IgpsportClient | None = None
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def mtu(self) -> int | None:
        """ATT MTU of the open link, if the transport reports one."""
        mtu = getattr(self._transport, "mtu", None)
        return mtu if isinstance(mtu, int) else None

    async def open(self) -> None:
        async with self._lock:
            await self._open_locked()

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def __aenter__(self) -> DeviceSession:
        await self.open()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def _open_locked(self) -> IgpsportClient:
        if self._client is not None:
            return self._client
        stack = contextlib.AsyncExitStack()
        try:
            transport = await stack.enter_async_context(self._factory(self.address))
            client = await stack.enter_async_context(IgpsportClient(transport))
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self._transport = transport
        self._client = client
        return client

    async def _close_locked(self) -> None:
        stack, self._stack, self._client, self._transport = self._stack, None, None, None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def _call(self, fn: Callable[[IgpsportClient], Awaitable[T]]) -> T:
        async with self._lock:
            client = await self._open_locked()
            try:
                return await fn(client)
            except commands.CommandError:
                raise
            except Exception:
                await self._close_locked()
                raise

    async def _run(
        self,
        name: str,
        *args: str,
        attempt_timeouts: tuple[float, ...] | None = None,
        allow_destructive: bool = False,
    ) -> object:
        """Run a named command, retrying on timeout per *attempt_timeouts*."""
        timeouts = attempt_timeouts or (self.timeout,)

        async def go(client: IgpsportClient) -> object:
            for attempt, timeout in enumerate(timeouts):
                try:
                    result = await commands.run_named(
                        client, name, args, timeout=timeout, allow_destructive=allow_destructive
                    )
                except TimeoutError:
                    if attempt == len(timeouts) - 1:
                        raise
                    continue
                return result.value
            raise AssertionError("unreachable")

        return await self._call(go)

    async def list_activities(self) -> commands.ActivityList:
        value = await self._run("list-activities", attempt_timeouts=self.list_attempt_timeouts)
        assert isinstance(value, commands.ActivityList)
        return value

    async def list_routes(self) -> commands.RouteList:
        value = await self._run("routes", attempt_timeouts=self.list_attempt_timeouts)
        assert isinstance(value, commands.RouteList)
        return value

    async def download_activity(
        self, activity: commands.ActivityFile, out_path: str
    ) -> commands.DownloadedFile:
        """Download *activity* as FIT to *out_path* (an explicit file path)."""
        value = await self._run("download-activity", str(activity.timestamp), out_path)
        assert isinstance(value, commands.DownloadedFile)
        return value

    async def upload_route(
        self, path: str, *, start_navigation: bool = False
    ) -> commands.UploadedRoute:
        """Upload a GPX / geoJSON / CNX / FIT route into the next free slot."""
        args = [path, "start"] if start_navigation else [path]
        value = await self._run("upload-route", *args)
        assert isinstance(value, commands.UploadedRoute)
        return value

    async def delete_route(
        self, file_id: int, *, allow_destructive: bool = False
    ) -> commands.DelRouteResult:
        """Delete a route plan. Destructive: the caller must confirm with the user."""
        value = await self._run("del-route", str(file_id), allow_destructive=allow_destructive)
        assert isinstance(value, commands.DelRouteResult)
        return value
