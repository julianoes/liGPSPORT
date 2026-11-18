"""Per-channel reassembly: frames on one channel must not corrupt another.

Regression for a corrupted activity download. While a FIT file
streams on the third channel, the BSC200 keeps sending its 20-byte
AGPS ephemeris request on the control channel. With one shared
buffer, that request was spliced into the FIT data at a notification
boundary. The file kept its expected length but failed its CRC, and
Strava rejected it as corrupt.
"""

from __future__ import annotations

import asyncio
import pathlib
from collections.abc import Callable

import pytest

from ligpsport import file_transfer, framing
from ligpsport.ble import BleakTransport
from ligpsport.bluez import BluezTransport
from ligpsport.client import IgpsportClient
from ligpsport.reassembly import FrameReassembler
from ligpsport.simulator import SimulatedActivityFile, Simulator, SimulatorState
from ligpsport.transport import (
    Channel,
    LoopbackTransport,
    Transport,
    TransportClosed,
    make_loopback_pair,
)

# Verbatim AGPS ephemeris request captured from a BSC200 (PROTOCOL.md
# §2.4). These exact 20 bytes were found in the middle of a corrupted
# FIT download.
_AGPS_REQUEST = bytes.fromhex("030305ff01ffff00ffffffffffffffffffffffb3")

# Notification payload size at the MTU of 247 the BSC200 negotiates.
_NOTIFY_SIZE = 244

# The chunk after which the AGPS request is injected.
_INJECT_AFTER_CHUNK = 20

_FIT_BYTES = (pathlib.Path(__file__).parent / "fixtures" / "activity_bsc200.fit").read_bytes()
_FIT_TIMESTAMP = 1147795610


def _split(data: bytes, chunk: int) -> list[bytes]:
    return [data[i : i + chunk] for i in range(0, len(data), chunk)]


def _download_stream(content: bytes) -> bytes:
    """Build the transmit-complete stream for *content*, as the BSC200 sends it."""
    from ligpsport.proto import file_download_pb2

    pb = file_download_pb2.file_download()
    pb.file_size = len(content)
    pb_bytes = pb.SerializeToString()
    head = bytearray(b"\xff" * framing.HEADER_SIZE)
    head[framing.HDR_TYPE] = framing.TYPE_PB
    head[framing.HDR_SERVICE] = 6  # CYCLING_DATA
    head[framing.HDR_FILE_TAG] = framing.FILE_TAG_TRANSMIT_COMPLETE
    head[framing.HDR_OPERATION] = 4  # FILE_SEND
    head[framing.HDR_HEADER_CRC] = framing.crc8(bytes(head[: framing.HDR_HEADER_CRC]))
    return bytes(head) + len(pb_bytes).to_bytes(4, "big") + pb_bytes + content


def test_interleaved_frame_on_other_channel_stays_out_of_download() -> None:
    stream = _download_stream(_FIT_BYTES)
    reassembler = FrameReassembler()
    out: list[bytes] = []
    for i, chunk in enumerate(_split(stream, _NOTIFY_SIZE)):
        out += reassembler.feed("third", chunk)
        if i == _INJECT_AFTER_CHUNK:
            out += reassembler.feed("control", _AGPS_REQUEST)
    assert out == [_AGPS_REQUEST, stream]


def test_channels_reassemble_independently() -> None:
    a = framing.build_frame(framing.Frame(service=10, operation=2, payload=b"a" * 300))
    b = framing.build_frame(framing.Frame(service=17, operation=2, payload=b"b" * 300))
    reassembler = FrameReassembler()
    out: list[bytes] = []
    for chunk_a, chunk_b in zip(_split(a, 100), _split(b, 100), strict=True):
        out += reassembler.feed("data", chunk_a)
        out += reassembler.feed("fourth", chunk_b)
    assert sorted(out) == sorted([a, b])


def test_malformed_header_only_drops_its_own_channel() -> None:
    stream = _download_stream(b"\x01" * 1000)
    reassembler = FrameReassembler()
    chunks = _split(stream, _NOTIFY_SIZE)
    assert reassembler.feed("third", chunks[0]) == []
    assert reassembler.feed("control", b"\x7f" * framing.HEADER_SIZE) == []
    out: list[bytes] = []
    for chunk in chunks[1:]:
        out += reassembler.feed("third", chunk)
    assert out == [stream]


Feed = Callable[[Channel, bytes], None]


def _bleak() -> tuple[Transport, Feed]:
    transport = BleakTransport(address="00:11:22:33:44:55")
    return transport, transport._on_notify


def _bluez() -> tuple[Transport, Feed]:
    transport = BluezTransport(address="00:11:22:33:44:55")
    return transport, transport._on_chunk


class _NotifyBridge(Transport):
    """Run simulator replies through a real BLE transport's notify path.

    Writes go to the simulator over loopback. Each frame the simulator
    sends back is split into notification-sized chunks and fed into
    *ble*'s notification handler on the channel it was sent on, the
    way bleak / BlueZ would deliver it. Partway through every
    third-channel stream, the AGPS request is fed in on the control
    channel, which is what the BSC200 does.
    """

    def __init__(self, link: LoopbackTransport, ble: Transport, feed: Feed) -> None:
        self._link = link
        self._ble = ble
        self._feed = feed
        self._pump: asyncio.Task[None] | None = None
        self.injected = 0

    async def __aenter__(self) -> _NotifyBridge:
        self._pump = asyncio.create_task(self._run())
        return self

    async def _run(self) -> None:
        try:
            while True:
                channel, wire = await self._link.receive_with_channel()
                for i, chunk in enumerate(_split(wire, _NOTIFY_SIZE)):
                    self._feed(channel, chunk)
                    if channel == "third" and i == _INJECT_AFTER_CHUNK:
                        self._feed("control", _AGPS_REQUEST)
                        self.injected += 1
        except TransportClosed:
            pass

    async def send(self, frame: bytes, *, channel: Channel = "control") -> None:
        await self._link.send(frame, channel=channel)

    async def receive(self) -> bytes:
        return await self._ble.receive()

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
        await self._link.close()
        await self._ble.close()


@pytest.mark.parametrize("make_ble", [_bleak, _bluez], ids=["bleak", "bluez"])
async def test_download_survives_agps_request_mid_stream(
    make_ble: Callable[[], tuple[Transport, Feed]],
) -> None:
    state = SimulatorState(
        activity_files=[
            SimulatedActivityFile(
                timestamp=_FIT_TIMESTAMP,
                file_size=len(_FIT_BYTES),
                user_id="user-42",
                device_id="BSC200-test",
                content=_FIT_BYTES,
            )
        ]
    )
    client_end, peer_end = make_loopback_pair()
    ble, feed = make_ble()
    async with (
        Simulator(peer_end, state),
        _NotifyBridge(client_end, ble, feed) as bridge,
        IgpsportClient(bridge) as client,
    ):
        result = await file_transfer.download_activity(client, timestamp=_FIT_TIMESTAMP)
    assert bridge.injected == 1
    assert result.content == _FIT_BYTES
