"""Real BLE transport built on `bleak <https://bleak.readthedocs.io/>`_.

The BleakTransport opens a GATT connection to the BSC200, locates the
primary Nordic-UART service by UUID, subscribes to TX notifications,
and reassembles incoming MTU-sized chunks into whole framing-layer
frames using the ``payload_size`` field from the 20-byte header, with
one buffer per channel (see :mod:`ligpsport.reassembly`).

Outgoing frames are split into MTU-sized chunks the BLE stack can
write in one operation. The BSC200 negotiates an MTU during connect
that is normally well above the typical 23-byte default; the
transport caches the negotiated value and chunks accordingly.

Lifecycle::

    async with BleakTransport(address) as transport:
        client = IgpsportClient(transport)
        await client.start()
        ...

Leaving the context manager disconnects the BLE link and cancels any
pending writes. The transport is not safe to reuse after close.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from . import gatt
from .reassembly import TX_UUIDS, FrameReassembler
from .transport import Channel, Transport, TransportClosed

# Channel → write characteristic UUID. Matches BluezTransport's
# _CHANNEL_RX_UUID; see PROTOCOL.md §7 for the iGPSPORT multi-channel
# rationale.
_CHANNEL_RX_UUID: Final[dict[Channel, str]] = {
    "control": gatt.PRIMARY_RX_UUID,
    "data": gatt.DATA_RX_UUID,
    "third": gatt.THIRD_RX_UUID,
    "fourth": gatt.FOURTH_RX_UUID,
}

if TYPE_CHECKING:
    from types import TracebackType

_LOG = logging.getLogger(__name__)


class BleakTransport(Transport):
    """Async BLE transport for an iGPSPORT device.

    The constructor only records the target address. Call
    :meth:`open` (or use ``async with``) to actually connect, discover
    the GATT service, and subscribe to TX notifications.
    """

    # MTU - 3 (the ATT header overhead). 244 - 3 = 241 is the typical
    # ceiling on a BLE 4.2 / 5.0 stack; the BSC200 negotiates somewhere
    # in this neighbourhood.
    _ATT_HEADER_SIZE = 3
    _DEFAULT_MTU = 23  # BLE LL default; bleak negotiates higher on connect.
    # LE Data Length Extension default; the value the BSC200 negotiates
    # with the iGPSPORT Android app (PROTOCOL.md §1). Used to override
    # bleak's _mtu_size when _acquire_mtu fails to update it.
    _FALLBACK_MTU = 247

    def __init__(self, address: str) -> None:
        self._address = address
        self._client: object | None = None  # bleak.BleakClient at runtime
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        # Reassembly of TX notification chunks, one buffer per channel.
        self._reassembler = FrameReassembler()
        self._closed = False

    @property
    def address(self) -> str:
        return self._address

    async def open(self) -> None:
        """Connect, discover, and start receiving."""
        # Import lazily so a missing bleak doesn't break code that only
        # uses the LoopbackTransport (e.g. test_transport_loopback).
        from bleak import BleakClient

        client = BleakClient(self._address)
        await client.connect()
        # bleak's MTU acquisition is on the backend (BleakClientBlueZDBus
        # in our case); the public BleakClient proxy doesn't expose it.
        # Without this, every write fires bleak's
        # "Using default MTU value" UserWarning from
        # bleak/backends/bluezdbus/client.py.
        # Private bleak internals, probed dynamically — Any is the
        # honest type here; every access below is getattr-guarded.
        backend: Any = getattr(client, "_backend", client)
        acquire_mtu = getattr(backend, "_acquire_mtu", None)
        if acquire_mtu is not None:
            try:
                await acquire_mtu()
                _LOG.info("acquired MTU=%d on %s", self._mtu(), self._address)
            except Exception as exc:
                _LOG.warning(
                    "bleak _acquire_mtu() failed (%s: %s); using fallback MTU",
                    type(exc).__name__,
                    exc,
                )
        else:
            _LOG.info("bleak backend has no _acquire_mtu(); using fallback MTU")
        # If bleak still doesn't know the real MTU, set _mtu_size by
        # hand. The mtu_size getter checks `_mtu_size is None` and
        # otherwise warns + returns 23; setting it silences that path
        # without lying about the link-layer MTU since the kernel
        # fragments writes that exceed the negotiated value.
        if getattr(backend, "_mtu_size", None) in (None, 0):
            backend._mtu_size = self._FALLBACK_MTU
            _LOG.info("forcing _mtu_size=%d as fallback", self._FALLBACK_MTU)
        # Subscribe to TX on every channel. The BSC200 receives on the
        # Control RX (8e) but emits responses on the Data TX (9e), and
        # may use 6e/7e for parallel file/firmware streams. Subscribing
        # to all four covers every observed reply path with no false
        # positives. Each channel is reassembled separately, so the
        # callback is bound to the channel it was registered for.
        for channel, tx_uuid in TX_UUIDS.items():
            with contextlib.suppress(Exception):
                await client.start_notify(tx_uuid, self._notify_callback(channel))
        self._client = client
        _LOG.info("connected to %s; MTU=%d", self._address, self._mtu())

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        client = self._client
        self._client = None
        if client is not None:
            for tx_uuid in TX_UUIDS.values():
                with contextlib.suppress(Exception):
                    await client.stop_notify(tx_uuid)  # type: ignore[attr-defined]
            with _suppress_errors():
                await client.disconnect()  # type: ignore[attr-defined]
        # Wake any pending receivers with TransportClosed.
        await self._inbox.put(None)

    async def __aenter__(self) -> BleakTransport:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def send(self, frame: bytes, *, channel: Channel = "control") -> None:
        if self._client is None:
            raise TransportClosed("transport not open")
        # The BSC200 negotiates a 23-byte BLE LL default MTU on BlueZ /
        # Linux and ignores writes that span more than that. Use the
        # negotiated value minus the ATT header (3 bytes) as the chunk
        # ceiling. Writes go with response=True (acknowledged) — the
        # device drops write-without-response frames for the control
        # channel.
        mtu = self._mtu() - self._ATT_HEADER_SIZE
        if mtu <= 0:
            mtu = self._DEFAULT_MTU - self._ATT_HEADER_SIZE
        rx_uuid = _CHANNEL_RX_UUID[channel]
        for offset in range(0, len(frame), mtu):
            chunk = frame[offset : offset + mtu]
            await self._client.write_gatt_char(  # type: ignore[attr-defined]
                rx_uuid, chunk, response=True
            )

    async def receive(self) -> bytes:
        frame = await self._inbox.get()
        if frame is None:
            raise TransportClosed("BLE link closed")
        return frame

    @property
    def mtu(self) -> int:
        """ATT MTU bleak reports for the open link (23 until connected)."""
        return self._mtu()

    def _mtu(self) -> int:
        if self._client is None:
            return self._DEFAULT_MTU
        try:
            value = int(self._client.mtu_size)  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            return self._DEFAULT_MTU
        return value if value > 0 else self._DEFAULT_MTU

    def _notify_callback(self, channel: Channel) -> Callable[[object, bytearray], None]:
        """Return a bleak notify callback that feeds *channel*'s buffer."""

        def _callback(_char: object, data: bytearray) -> None:
            self._on_notify(channel, data)

        return _callback

    def _on_notify(self, channel: Channel, data: bytes | bytearray) -> None:
        """Reassemble TX chunks from *channel* into whole logical frames."""
        # The notification arrives on the event loop, so put_nowait is
        # safe (and avoids creating a task per frame).
        for frame in self._reassembler.feed(channel, bytes(data)):
            self._inbox.put_nowait(frame)


class _suppress_errors:
    """Tiny context manager that swallows exceptions during shutdown.

    Logged at debug level so failures don't go entirely unseen.
    """

    def __enter__(self) -> _suppress_errors:
        return self

    async def __aenter__(self) -> _suppress_errors:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if exc is not None:
            _LOG.debug("suppressing shutdown error: %s", exc)
            return True
        return False

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return self.__exit__(exc_type, exc, tb)
