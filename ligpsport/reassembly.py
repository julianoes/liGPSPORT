"""Per-channel reassembly of TX notification chunks into whole frames.

Shared by :class:`ligpsport.ble.BleakTransport` and
:class:`ligpsport.bluez.BluezTransport`. The device notifies on four
TX characteristics, and each one carries its own independent stream
of frames, so each gets its own buffer.

A single shared buffer is not safe. An activity download streams on
the third channel for tens of seconds, and the BSC200 keeps sending
its unsolicited AGPS ephemeris request (PROTOCOL.md §2.4) on the
control channel every ~3 s. Reassembly counts bytes rather than
tracking frames, so with one buffer that 20-byte request lands in the
middle of the FIT data. The file still comes out at the expected
length, but with header bytes where ride data should be. Its CRC
fails and Strava rejects it.
"""

from __future__ import annotations

import logging
from typing import Final

from . import framing, gatt
from .transport import Channel

_LOG = logging.getLogger(__name__)

TX_UUIDS: Final[dict[Channel, str]] = {
    "control": gatt.PRIMARY_TX_UUID,
    "data": gatt.DATA_TX_UUID,
    "third": gatt.THIRD_TX_UUID,
    "fourth": gatt.FOURTH_TX_UUID,
}
"""Channel → notify (TX) characteristic UUID."""


class FrameReassembler:
    """Turn notification chunks into whole frames, one buffer per channel."""

    def __init__(self) -> None:
        self._buf: dict[Channel, bytearray] = {}
        self._expected: dict[Channel, int] = {}

    def feed(self, channel: Channel, data: bytes) -> list[bytes]:
        """Append *data* received on *channel*; return any frames it completed."""
        buf = self._buf.setdefault(channel, bytearray())
        buf.extend(data)
        frames: list[bytes] = []
        while True:
            expected = self._expected.get(channel)
            if expected is None:
                if len(buf) < framing.HEADER_SIZE:
                    return frames  # not enough yet to learn the frame size
                head = bytes(buf[: framing.HEADER_SIZE])
                try:
                    expected = framing.expected_total_size(head)
                    if expected is None:
                        # Transmit-complete download stream (file_tag=0x55).
                        # The head's payload_size is bogus; the real length
                        # comes from the embedded file_download protobuf.
                        expected = framing.transmit_complete_total_size(bytes(buf))
                except framing.FrameError as exc:
                    _LOG.warning("dropping malformed frame on %s: %s", channel, exc)
                    buf.clear()
                    return frames
                if expected is None:
                    return frames  # need more bytes to learn the size
                self._expected[channel] = expected
            if len(buf) < expected:
                return frames  # wait for more chunks
            frames.append(bytes(buf[:expected]))
            del buf[:expected]
            del self._expected[channel]
