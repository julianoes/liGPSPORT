"""Module-level smoke test for the BlueZ-direct backend.

We can't open a real DBus connection in the unit suite (no adapter,
no D-Bus system bus), but we can exercise the reassembly buffer that
:class:`BluezTransport._on_chunk` shares with the rest of the
codebase. The actual connect path is covered by the live device
tests.
"""

from __future__ import annotations

import pytest

from ligpsport.bluez import BluezTransport
from ligpsport.framing import Frame, build_frame


def _split(data: bytes, chunk: int) -> list[bytes]:
    return [data[i : i + chunk] for i in range(0, len(data), chunk)]


def test_reassembly_single_chunk() -> None:
    transport = BluezTransport(address="00:11:22:33:44:55")
    wire = build_frame(Frame(service=10, operation=2, payload=b"hello"))
    transport._on_chunk("control", wire)
    # The reassembled frame ends up in the inbox without blocking.
    assert transport._inbox.qsize() == 1


def test_reassembly_multi_chunk() -> None:
    transport = BluezTransport(address="00:11:22:33:44:55")
    wire = build_frame(Frame(service=17, operation=2, payload=b"\x00" * 200))
    for chunk in _split(wire, chunk=64):
        transport._on_chunk("control", chunk)
    assert transport._inbox.qsize() == 1


def test_reassembly_two_frames() -> None:
    transport = BluezTransport(address="00:11:22:33:44:55")
    first = build_frame(Frame(service=10, operation=2, payload=b"first"))
    second = build_frame(Frame(service=17, operation=2, payload=b"second"))
    transport._on_chunk("control", first + second)
    assert transport._inbox.qsize() == 2


def test_send_before_open_raises() -> None:
    import asyncio

    from ligpsport.transport import TransportClosed

    transport = BluezTransport(address="00:11:22:33:44:55")
    with pytest.raises(TransportClosed):
        asyncio.run(transport.send(b"\x00" * 20))


def _raise_from(filename: str, exc: BaseException) -> BaseException:
    """Raise *exc* from a frame whose code object lives at *filename*.

    The EOF filter classifies by traceback filename, so the test needs a
    real frame originating in a dbus_fast-looking file. Compiling a tiny
    source with that filename gives us exactly that — a genuine code
    object and traceback, no patching of the module under test.
    """
    code = compile("raise exc", filename, "exec")
    try:
        exec(code, {"exc": exc})
    except BaseException as raised:
        return raised
    raise AssertionError("exec did not raise")


def test_eof_filter_matches_dbus_fast_frame() -> None:
    from ligpsport.bluez import _is_dbus_fast_reader_eof

    exc = _raise_from(
        "/nix/store/abc-python3.13-dbus-fast/lib/python3.13/"
        "site-packages/dbus_fast/aio/message_reader.py",
        EOFError(),
    )
    assert _is_dbus_fast_reader_eof({"exception": exc}) is True


def test_eof_filter_ignores_foreign_eof_and_other_errors() -> None:
    from ligpsport.bluez import _is_dbus_fast_reader_eof

    foreign_eof = _raise_from("/home/user/app/reader.py", EOFError())
    assert _is_dbus_fast_reader_eof({"exception": foreign_eof}) is False

    dbus_value_error = _raise_from(
        "/usr/lib/python3.13/site-packages/dbus_fast/message_bus.py",
        ValueError("boom"),
    )
    assert _is_dbus_fast_reader_eof({"exception": dbus_value_error}) is False

    # A context without an exception (asyncio emits those too).
    assert _is_dbus_fast_reader_eof({"message": "socket closed"}) is False


def test_eof_filter_install_is_idempotent() -> None:
    import asyncio

    from ligpsport.bluez import _install_dbus_eof_filter

    async def _install_twice() -> tuple[object, object]:
        loop = asyncio.get_running_loop()
        _install_dbus_eof_filter(loop)
        first = loop.get_exception_handler()
        _install_dbus_eof_filter(loop)
        return first, loop.get_exception_handler()

    first, second = asyncio.run(_install_twice())
    # Re-opening a transport (or opening a second one) on the same loop
    # must not stack wrappers — each layer would add another call frame
    # for every unrelated loop error.
    assert first is second


def test_eof_filter_delegates_unrelated_errors() -> None:
    import asyncio

    from ligpsport.bluez import _install_dbus_eof_filter

    seen: list[dict[str, object]] = []

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: seen.append(context))
        _install_dbus_eof_filter(loop)
        handler = loop.get_exception_handler()
        assert handler is not None
        handler(loop, {"exception": _raise_from("/home/user/app/x.py", EOFError())})
        handler(loop, {"exception": _raise_from("/x/dbus_fast/aio/reader.py", EOFError())})

    asyncio.run(_run())
    # Only the dbus-fast EOF is swallowed; the foreign one reaches the
    # handler that was installed before us.
    assert len(seen) == 1
