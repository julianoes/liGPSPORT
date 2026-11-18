"""BlueZ-direct BLE transport backend.

Parallel implementation to :class:`ligpsport.ble.BleakTransport`. Both
satisfy the :class:`ligpsport.transport.Transport` ABC, so the higher
layers (client, simulator, commands) don't care which one is in use.

The motivation for a second backend: bleak wraps BlueZ's DBus API at
a level that doesn't expose **MTU control** in a useful way on Linux.
The BSC200's route-upload path sends ~26 KB of data in one logical
blob; with BlueZ's default 23-byte ATT MTU that becomes 1300+
sequential ATT Write Commands, which the device's input buffer
appears to drop. The iGPSPORT Android app negotiates ~244 bytes via
``ConfigureMTUOperation`` and the BSC200 accepts the upload happily.

This backend bypasses bleak and talks to BlueZ via DBus directly,
using two specific BlueZ features:

* ``org.bluez.GattCharacteristic1.AcquireWrite`` — returns a Unix
  file descriptor plus the *negotiated* MTU. Writes to the FD go
  out as ATT Write Commands sized up to that MTU. No per-write DBus
  round-trip; we just ``os.write(fd, chunk)``.
* ``org.bluez.GattCharacteristic1.AcquireNotify`` — returns a Unix
  file descriptor that delivers raw notification bytes. Subscribing
  this way also bumps the MTU during connection setup.

Selecting between backends:

* ``ligpsport.ble.BleakTransport`` — default; portable across
  Linux / macOS / Windows; uses bleak.
* ``ligpsport.bluez.BluezTransport`` — Linux-only; needs BlueZ
  >= 5.50 (for the Acquire* methods) and DBus access to
  ``org.bluez``. Use this when you need MTU control or when bleak's
  abstraction is getting in the way.

The CLI's ``--backend bluez`` flag flips to this backend.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import TYPE_CHECKING, Any, Final

from . import gatt
from .reassembly import TX_UUIDS, FrameReassembler
from .transport import Channel, Transport, TransportClosed

if TYPE_CHECKING:
    from types import TracebackType


_LOG = logging.getLogger(__name__)

BLUEZ_SERVICE: Final[str] = "org.bluez"
ADAPTER_IFACE: Final[str] = "org.bluez.Adapter1"
DEVICE_IFACE: Final[str] = "org.bluez.Device1"
GATT_SERVICE_IFACE: Final[str] = "org.bluez.GattService1"
GATT_CHAR_IFACE: Final[str] = "org.bluez.GattCharacteristic1"
DBUS_OM_IFACE: Final[str] = "org.freedesktop.DBus.ObjectManager"

# Channel → write characteristic UUID. The Transport ABC accepts three
# channels; we map them onto the same RX UUIDs the iGPSPORT app uses.
# "control" is the primary command channel (the …-8e UART); "data" is
# the bulk-data channel for gen<3 devices (…-9e); "fourth" is the
# bulk-data channel for gen≥3 devices (…-6e). See PROTOCOL.md §7.
_CHANNEL_RX_UUID: Final[dict[Channel, str]] = {
    "control": gatt.PRIMARY_RX_UUID,
    "data": gatt.DATA_RX_UUID,
    "third": gatt.THIRD_RX_UUID,
    "fourth": gatt.FOURTH_RX_UUID,
}


class BluezError(RuntimeError):
    """Raised when BlueZ refuses an operation or a required object is missing."""


async def _wait_writable(loop: asyncio.AbstractEventLoop, fd: int) -> None:
    """Park until *fd* is writable. Lifts the closure trap into a top-level fn."""
    fut: asyncio.Future[None] = loop.create_future()

    def _cb() -> None:
        loop.remove_writer(fd)
        if not fut.done():
            fut.set_result(None)

    loop.add_writer(fd, _cb)
    try:
        await fut
    finally:
        with contextlib.suppress(Exception):
            loop.remove_writer(fd)


def _is_dbus_fast_reader_eof(context: dict[str, object]) -> bool:
    """True when *context* describes the benign dbus-fast reader EOF.

    dbus-fast (>=2.x with the compiled Cython reader) sets an EOFError on
    an internal future when the DBus socket closes during teardown. The
    future has no awaiter at that moment, so asyncio surfaces a
    "Future exception was never retrieved" message at GC time. The
    upload has already succeeded by then, so we filter just this one
    case rather than silencing the loop's whole exception channel.
    """
    exc = context.get("exception")
    if not isinstance(exc, EOFError):
        return False
    tb = exc.__traceback__
    while tb is not None:
        if "dbus_fast" in (tb.tb_frame.f_code.co_filename or ""):
            return True
        tb = tb.tb_next
    return False


_EOF_FILTER_MARK = "_ligpsport_dbus_eof_filter"


def _install_dbus_eof_filter(loop: asyncio.AbstractEventLoop) -> None:
    """Wrap *loop*'s exception handler so the benign dbus-fast EOF is dropped.

    Idempotent: the wrapper is tagged, so reconnecting or running a
    second transport on the same loop reuses the existing filter instead
    of stacking wrappers — every extra layer would sit in the call path
    of every unrelated loop error.

    The wrapper stays installed for the loop's lifetime. The
    "Future exception was never retrieved" message can fire after
    :meth:`BluezTransport.close` returns (during async-with exit /
    ``asyncio.run`` cleanup), so restoring the previous handler eagerly
    would re-introduce the warning we just suppressed.
    """
    prev_handler = loop.get_exception_handler()
    if getattr(prev_handler, _EOF_FILTER_MARK, False):
        return

    def _filter(loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        if _is_dbus_fast_reader_eof(context):
            return
        if prev_handler is None:
            loop.default_exception_handler(context)
        else:
            prev_handler(loop, context)

    setattr(_filter, _EOF_FILTER_MARK, True)
    loop.set_exception_handler(_filter)


class BluezTransport(Transport):
    """BLE transport that drives BlueZ via DBus directly.

    Lifecycle::

        async with BluezTransport(address) as transport:
            client = IgpsportClient(transport)
            ...

    Open / close do the following:

    * ``open()`` — find the adapter, locate the ``Device1`` for
      *address*, ``Connect()`` it, wait for service resolution, find
      the four UART characteristics by UUID, call ``AcquireWrite``
      on the control RX char and ``AcquireNotify`` on every TX char.
      Each Acquire returns a Unix FD and the *negotiated* MTU for
      that endpoint; the largest of those MTUs is what
      :meth:`send` uses as the chunk size.
    * ``close()`` — close every FD, ``Disconnect()`` the device.

    Each TX channel is reassembled in its own buffer (see
    :mod:`ligpsport.reassembly`): an activity download on one channel
    must not absorb an unsolicited frame arriving on another.
    """

    def __init__(self, address: str, *, adapter: str = "hci0") -> None:
        self._address = address
        self._adapter = adapter
        # DBus state (populated in open()).
        self._bus: object | None = None
        self._device_iface: object | None = None
        # Per-channel write FDs (lazy: only "control" is acquired in
        # open(); "data" and "fourth" are acquired on first use to keep
        # the route-upload paths from blocking unrelated callers).
        self._write_fds: dict[Channel, int] = {}
        self._write_mtus: dict[Channel, int] = {}
        # Char paths discovered at connect time and used later for lazy
        # AcquireWrite. None until open() has walked the GATT tree.
        self._chars_by_uuid: dict[str, str] | None = None
        self._notify_fds: list[int] = []
        # Inbox: complete reassembled frames.
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._closed = False
        # Per-channel reassembly — see class docstring.
        self._reassembler = FrameReassembler()

    @property
    def address(self) -> str:
        return self._address

    @property
    def mtu(self) -> int:
        """Negotiated MTU of the control channel (the primary command path)."""
        return self._write_mtus.get("control", 23)

    def channel_mtu(self, channel: Channel) -> int:
        """Negotiated MTU of *channel*, or 23 if the channel is not yet acquired."""
        return self._write_mtus.get(channel, 23)

    def _register_reader(self, fd: int, channel: Channel) -> None:
        """Hook *channel*'s notify *fd* into the asyncio event loop."""
        loop = asyncio.get_running_loop()

        def _on_readable() -> None:
            try:
                # SOCK_SEQPACKET delivers one notification per recv.
                # 4096 is generous for any BSC200 notification (MTU 247).
                data = os.read(fd, 4096)
            except BlockingIOError:
                return
            except OSError as exc:
                _LOG.debug("BlueZ: read on fd=%d errored: %s", fd, exc)
                loop.remove_reader(fd)
                return
            if not data:
                # EOF: the device disconnected or BlueZ tore down the FD.
                _LOG.debug("BlueZ: fd=%d closed by peer", fd)
                loop.remove_reader(fd)
                return
            self._on_chunk(channel, data)

        loop.add_reader(fd, _on_readable)

    async def open(self) -> None:
        """Connect to BlueZ, discover, acquire MTU-aware FDs."""
        # Lazy import: dbus_fast pulls in a binary extension. Code paths
        # that only use LoopbackTransport (tests) should not pay for it.
        from dbus_fast import BusType
        from dbus_fast.aio import MessageBus

        # Drop the benign dbus-fast reader EOFError before it reaches
        # the user. The filter stays installed for the rest of the
        # loop's lifetime — see _install_dbus_eof_filter().
        _install_dbus_eof_filter(asyncio.get_running_loop())

        # negotiate_unix_fd=True is mandatory — AcquireWrite /
        # AcquireNotify return file descriptors over DBus, and without
        # the FD-passing capability negotiated up front, the bus
        # disconnects when the first FD-bearing reply arrives.
        bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        self._bus = bus

        adapter_path = f"/org/bluez/{self._adapter}"
        device_path = f"{adapter_path}/dev_{self._address.replace(':', '_').upper()}"

        # Make sure the device is known to BlueZ. If not, try a short
        # scan to populate it.
        await self._ensure_device_known(bus, adapter_path, device_path)

        device_iface = await self._get_iface(bus, device_path, DEVICE_IFACE)
        self._device_iface = device_iface

        # Connect if not already connected.
        if not await device_iface.get_connected():  # type: ignore[attr-defined]
            await device_iface.call_connect()  # type: ignore[attr-defined]

        # Wait for service resolution. BlueZ exposes the GATT tree
        # asynchronously; ServicesResolved is the canonical "you can
        # start using GATT now" signal.
        await self._wait_services_resolved(device_iface)

        # Walk the GATT tree to find our characteristics by UUID.
        managed = await self._get_managed_objects(bus)
        chars_by_uuid = self._index_chars_by_uuid(managed, device_path)
        self._chars_by_uuid = chars_by_uuid

        # AcquireWrite on the control RX up front — every command needs
        # it. Data / Fourth channels are acquired lazily on first use
        # so we don't pay the round trip for users who never upload.
        await self._acquire_write_channel("control")

        # AcquireNotify on every TX characteristic that exists.
        for channel, tx_uuid in TX_UUIDS.items():
            tx_path = chars_by_uuid.get(tx_uuid)
            if tx_path is None:
                _LOG.debug("BlueZ: skipping unavailable TX %s", tx_uuid)
                continue
            tx_iface = await self._get_iface(bus, tx_path, GATT_CHAR_IFACE)
            try:
                notify_fd, notify_mtu = await tx_iface.call_acquire_notify({})  # type: ignore[attr-defined]
            except Exception as exc:
                _LOG.debug("BlueZ: AcquireNotify on %s failed: %s", tx_path, exc)
                continue
            dup_fd = os.dup(notify_fd)
            os.close(notify_fd)
            self._notify_fds.append(dup_fd)
            _LOG.info(
                "BlueZ: AcquireNotify on %s -> fd=%d mtu=%d",
                tx_path,
                dup_fd,
                notify_mtu,
            )
            # Register the FD with the event loop. BlueZ marks the
            # socket non-blocking, so we use add_reader rather than
            # spawning a thread for a blocking read.
            self._register_reader(dup_fd, channel)

        if not self._notify_fds:
            raise BluezError("no TX notification FDs could be acquired")

    async def __aenter__(self) -> BluezTransport:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Stop reading; remove_reader is idempotent.
        loop = asyncio.get_running_loop()
        for fd in self._notify_fds:
            with contextlib.suppress(Exception):
                loop.remove_reader(fd)
            with contextlib.suppress(OSError):
                os.close(fd)
        self._notify_fds.clear()
        for fd in self._write_fds.values():
            with contextlib.suppress(OSError):
                os.close(fd)
        self._write_fds.clear()
        self._write_mtus.clear()
        # Disconnect the device (best-effort).
        if self._device_iface is not None:
            with contextlib.suppress(Exception):
                await self._device_iface.call_disconnect()  # type: ignore[attr-defined]
            self._device_iface = None
        # Disconnect the bus.
        if self._bus is not None:
            with contextlib.suppress(Exception):
                self._bus.disconnect()  # type: ignore[attr-defined]
            self._bus = None
        # The EOF filter installed by open() is deliberately left in
        # place — see _install_dbus_eof_filter() for why.
        # Wake up any pending receivers.
        await self._inbox.put(None)

    async def send(self, frame: bytes, *, channel: Channel = "control") -> None:
        fd = await self._get_write_fd(channel)
        # AcquireWrite returns the maximum ATT payload size for each
        # write — already net of the ATT header. The kernel's
        # SOCK_SEQPACKET socket truncates anything larger, so we cap
        # at the negotiated MTU directly (not MTU-3).
        chunk = max(self._write_mtus[channel], 20)
        loop = asyncio.get_running_loop()
        for offset in range(0, len(frame), chunk):
            buf = frame[offset : offset + chunk]
            await self._write_buf(loop, fd, buf)

    @staticmethod
    async def _write_buf(loop: asyncio.AbstractEventLoop, fd: int, buf: bytes) -> None:
        """Non-blocking write that waits for writability when EAGAIN."""
        while True:
            try:
                written = os.write(fd, buf)
            except BlockingIOError:
                await _wait_writable(loop, fd)
                continue
            if written == len(buf):
                return
            # Short write — keep going with the rest. Rare for
            # SEQPACKET but harmless.
            buf = buf[written:]

    async def receive(self) -> bytes:
        frame = await self._inbox.get()
        if frame is None:
            raise TransportClosed("BlueZ link closed")
        return frame

    # ---- internal helpers ------------------------------------------

    async def _get_write_fd(self, channel: Channel) -> int:
        """Return the cached write FD for *channel*, acquiring it if needed."""
        if self._closed:
            raise TransportClosed("transport is closed")
        fd = self._write_fds.get(channel)
        if fd is not None:
            return fd
        await self._acquire_write_channel(channel)
        return self._write_fds[channel]

    async def _acquire_write_channel(self, channel: Channel) -> None:
        """``AcquireWrite`` on the characteristic backing *channel*."""
        if self._chars_by_uuid is None or self._bus is None:
            raise TransportClosed("transport not open")
        uuid = _CHANNEL_RX_UUID[channel]
        rx_path = self._chars_by_uuid.get(uuid)
        if rx_path is None:
            raise BluezError(f"{channel} RX characteristic {uuid} not exposed by device")
        rx_iface = await self._get_iface(self._bus, rx_path, GATT_CHAR_IFACE)
        # AcquireWrite returns (FD, MTU). The FD is a SOCK_SEQPACKET
        # socket per BlueZ docs; writes go out as ATT Write Commands
        # sized up to MTU.
        write_fd, write_mtu = await rx_iface.call_acquire_write({})  # type: ignore[attr-defined]
        dup_fd = os.dup(write_fd)
        os.close(write_fd)
        self._write_fds[channel] = dup_fd
        self._write_mtus[channel] = int(write_mtu)
        _LOG.info(
            "BlueZ: AcquireWrite on %s (%s channel) -> fd=%d mtu=%d",
            rx_path,
            channel,
            dup_fd,
            self._write_mtus[channel],
        )

    async def _ensure_device_known(self, bus: object, adapter_path: str, device_path: str) -> None:
        """If BlueZ has never seen *address*, do a brief discovery."""
        managed = await self._get_managed_objects(bus)
        if device_path in managed:
            return
        adapter_iface = await self._get_iface(bus, adapter_path, ADAPTER_IFACE)
        with contextlib.suppress(Exception):
            # SetDiscoveryFilter sometimes refuses unknown keys; this
            # placeholder is best-effort, the real discovery still
            # starts below.
            await adapter_iface.call_set_discovery_filter(  # type: ignore[attr-defined]
                {"DuplicateData": False}
            )
        await adapter_iface.call_start_discovery()  # type: ignore[attr-defined]
        try:
            for _ in range(20):  # up to ~6 seconds
                await asyncio.sleep(0.3)
                managed = await self._get_managed_objects(bus)
                if device_path in managed:
                    return
        finally:
            with contextlib.suppress(Exception):
                await adapter_iface.call_stop_discovery()  # type: ignore[attr-defined]
        raise BluezError(
            f"BlueZ never advertised device {self._address}; is it powered on and in range?"
        )

    async def _wait_services_resolved(self, device_iface: object, *, timeout: float = 15.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if await device_iface.get_services_resolved():  # type: ignore[attr-defined]
                return
            await asyncio.sleep(0.2)
        raise BluezError(f"GATT services not resolved within {timeout}s")

    async def _get_iface(self, bus: object, path: str, iface: str) -> object:
        introspection = await bus.introspect(BLUEZ_SERVICE, path)  # type: ignore[attr-defined]
        proxy = bus.get_proxy_object(BLUEZ_SERVICE, path, introspection)  # type: ignore[attr-defined]
        return proxy.get_interface(iface)

    async def _get_managed_objects(self, bus: object) -> dict[str, dict[str, dict[str, Any]]]:
        introspection = await bus.introspect(BLUEZ_SERVICE, "/")  # type: ignore[attr-defined]
        proxy = bus.get_proxy_object(BLUEZ_SERVICE, "/", introspection)  # type: ignore[attr-defined]
        om = proxy.get_interface(DBUS_OM_IFACE)
        result = await om.call_get_managed_objects()
        return result  # type: ignore[no-any-return]

    @staticmethod
    def _index_chars_by_uuid(
        managed: dict[str, dict[str, dict[str, Any]]], device_path: str
    ) -> dict[str, str]:
        """Walk the ObjectManager tree and return {char_uuid: object_path}.

        Only includes characteristics whose path is rooted under
        *device_path*, so we ignore characteristics from other devices
        the adapter has paired.
        """
        out: dict[str, str] = {}
        for path, ifaces in managed.items():
            if not path.startswith(device_path):
                continue
            char_props = ifaces.get(GATT_CHAR_IFACE)
            if not char_props:
                continue
            uuid_val = char_props.get("UUID")
            if uuid_val is None:
                continue
            # dbus-fast wraps property values in Variant objects whose
            # .value attribute holds the unmarshalled Python value.
            uuid = getattr(uuid_val, "value", uuid_val)
            if isinstance(uuid, str):
                out[uuid.lower()] = path
        return out

    def _on_chunk(self, channel: Channel, data: bytes) -> None:
        """Reassemble one notification from *channel* into whole frames."""
        for frame in self._reassembler.feed(channel, data):
            self._inbox.put_nowait(frame)
