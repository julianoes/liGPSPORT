"""Tests for BleakTransport's MTU-fragment reassembly logic.

We don't have a real BLE adapter in the test environment, but the
reassembly loop is pure-Python and can be exercised by feeding it
notification chunks directly. The point is to catch off-by-one and
header-skew bugs before they manifest as silent corruption in the
field.
"""

from __future__ import annotations

import pytest

from ligpsport.ble import BleakTransport
from ligpsport.framing import Frame, build_frame
from ligpsport.transport import TransportClosed


def _split(data: bytes, chunk: int) -> list[bytes]:
    return [data[i : i + chunk] for i in range(0, len(data), chunk)]


async def test_single_chunk_frame_reassembles() -> None:
    transport = BleakTransport(address="00:11:22:33:44:55")
    wire = build_frame(Frame(service=10, operation=2, payload=b"hello"))
    # Inject as if it came from the notify callback in one piece.
    transport._on_notify("control", bytearray(wire))
    received = await transport.receive()
    assert received == wire


async def test_multi_chunk_frame_reassembles() -> None:
    transport = BleakTransport(address="00:11:22:33:44:55")
    wire = build_frame(Frame(service=17, operation=2, payload=b"\x00" * 200))
    for chunk in _split(wire, chunk=20):
        transport._on_notify("control", bytearray(chunk))
    received = await transport.receive()
    assert received == wire


async def test_two_back_to_back_frames_reassemble_separately() -> None:
    transport = BleakTransport(address="00:11:22:33:44:55")
    first = build_frame(Frame(service=10, operation=2, payload=b"first"))
    second = build_frame(Frame(service=17, operation=2, payload=b"second"))
    combined = first + second
    for chunk in _split(combined, chunk=11):
        transport._on_notify("control", bytearray(chunk))
    assert await transport.receive() == first
    assert await transport.receive() == second


async def test_close_signals_receivers() -> None:
    transport = BleakTransport(address="00:11:22:33:44:55")
    await transport.close()
    with pytest.raises(TransportClosed):
        await transport.receive()
