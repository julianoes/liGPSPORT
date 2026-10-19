"""In-process wire peer used by the test suite.

The simulator is the iGPSPORT-side equivalent of the client: same
framing codec, same envelope router, same protobuf modules. Tests
hand it the peer-end of a :class:`ligpsport.transport.LoopbackTransport`
pair and configure a small in-memory state (device version, activity
list, configuration values). The simulator runs a background read
loop, dispatches incoming frames to per-service handlers, and writes
replies back through the same transport. No protocol detail is
duplicated between client and simulator; both go through
:mod:`ligpsport.framing` and :mod:`ligpsport.envelope`.

Adding a new service handler:

1. Implement an async function ``handle(state, frame, msg) -> Message``
   (or ``-> None`` for one-way operations).
2. Register it in :class:`Simulator._HANDLERS` keyed on the
   ``service_type_index`` value.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Callable, Coroutine
from typing import Final, cast

from google.protobuf.message import Message

from . import client as _client
from . import envelope, framing
from .proto import (
    common_pb2,
    cycling_data_pb2,
    dev_status_pb2,
    dev_ver_info_pb2,
    factory_pb2,
    route_plan_pb2,
    sensor_pb2,
    user_config_pb2,
)
from .transport import (
    Channel,
    LoopbackTransport,
    Transport,
    TransportClosed,
    make_loopback_pair,
)

_LOG = logging.getLogger(__name__)


Handler = Callable[
    ["SimulatorState", framing.Frame, Message],
    Coroutine[None, None, Message | None],
]


@dataclasses.dataclass(slots=True)
class SimulatedActivityFile:
    """One entry in the simulator's recorded-activity file list."""

    timestamp: int
    file_size: int
    user_id: str = ""
    device_id: str = ""
    content: bytes = b""
    """The on-device FIT bytes — served when the client sends FILE_GET.

    Empty by default so existing tests that only exercise LIST_GET
    still work. Tests that exercise the download path populate this
    with enough bytes to match :attr:`file_size`.
    """


@dataclasses.dataclass(slots=True)
class UploadedRouteFile:
    """An accumulated route file received via a multi-chunk upload."""

    file_id: int
    # Wire value of whichever enum the upload path carried:
    # ROUTE_PLAN_FILE_TYPE for the chunked route-plan upload,
    # file_operation_type for the FILE_OPERATION (CNX) upload.
    file_type: int
    name: str
    extension: str
    total_distance: int
    content: bytes
    end_types: list[int]


@dataclasses.dataclass(slots=True)
class SimulatedSensor:
    """One entry in the simulator's paired-sensor list."""

    sensor_type: sensor_pb2.SENSOR_TYPE
    sensor_radio_type: sensor_pb2.SENSOR_RADIO_TYPE = sensor_pb2.enum_SENSOR_RADIO_TYPE_BLE
    sensor_status_type: sensor_pb2.SENSOR_STATUS_TYPE = sensor_pb2.enum_SENSOR_STATUS_TYPE_CONNECTED
    sensor_key: str = ""
    sensor_ble_name: str = ""
    sensor_pwr: int = 0
    wheel_size: int = 0


@dataclasses.dataclass(slots=True)
class SimulatorState:
    """In-memory device state the simulator exposes.

    Fields are populated as new services land. Tests instantiate a
    :class:`Simulator` with a :class:`SimulatorState` carrying just
    the values they care about; defaults match a fresh out-of-the-box
    BSC200.
    """

    main_boot_ver: int = 0x01000000
    main_app_ver: int = 0x01000000
    ble_boot_ver: int = 0x01000000
    ble_app_ver: int = 0x01000000
    hardware_ver: int = 0x01000000
    protocol_ver: int = 101
    compile_time: str = "2026-01-01 00:00:00"

    # Cycling status (DEV_STATUS service).
    cycling_status: dev_status_pb2.DEV_CYCLING_STATUS = dev_status_pb2.DEV_CYCLING_STATUS_FREE
    cycling_start_time: int = 0
    latitude: float = 0.0
    longitude: float = 0.0
    real_time_speed_mm_s: int = 0
    avg_speed_mm_s: int = 0
    riding_time_ms: int = 0
    riding_distance_cm: int = 0
    real_time_cad: int = 0
    real_time_hrm: int = 0
    real_time_power: int = 0
    total_height_m: int = 0
    cur_height_cm: int = 0
    cur_slope: int = 0
    course: int = 0
    wifi_status: dev_status_pb2.DEV_WIFI_STATUS = dev_status_pb2.DEV_WIFI_STATUS_IDLE
    navi_status: dev_status_pb2.DEV_NAVI_STATUS = dev_status_pb2.DEV_NAVI_STATUS_OFF

    # User profile (USER_CONFIG service).
    user_sex: int = 1
    user_weight_g: int = 750  # 75kg
    user_age: int = 30
    user_height_cm: int = 175
    user_wheel_dia_mm: int = 2096
    user_bike_weight_g: int = 80
    user_time_zone_s: int = 0
    user_member_id: str = "ligpsport-sim"

    # Recorded activities (CYCLING_DATA service).
    activity_files: list[SimulatedActivityFile] = dataclasses.field(default_factory=list)

    # Paired sensors (SENSOR service).
    sensors: list[SimulatedSensor] = dataclasses.field(default_factory=list)

    # Completed uploads from `upload_route_plan`. Each entry is the
    # reassembled file plus its decoded metadata.
    uploaded_routes: list[UploadedRouteFile] = dataclasses.field(default_factory=list)

    # file_id of the route most recently committed via FILE_USE (the
    # commit step that mirrors `setRoutePlanFile` in the smali).
    active_route_id: int | None = None

    # The simulator records every received frame for test assertions.
    received: list[framing.Frame] = dataclasses.field(default_factory=list)
    # Destructive-op guardrail: when False, the simulator refuses any
    # write/delete operation with a NACK frame. Tests opt in.
    allow_destructive: bool = False


async def _handle_dev_ver_info(
    state: SimulatorState, frame: framing.Frame, _msg: Message
) -> Message | None:
    if frame.operation != _client.OP_GET:
        _LOG.debug("dev_ver_info: ignoring non-GET operation %d", frame.operation)
        return None
    reply = dev_ver_info_pb2.dev_ver_info_msg()
    reply.operate_type = dev_ver_info_pb2.enum_OPERATE_TYPE_SEND
    v = reply.version_message
    v.main_boot_ver = state.main_boot_ver
    v.main_app_ver = state.main_app_ver
    v.ble_boot_ver = state.ble_boot_ver
    v.ble_app_ver = state.ble_app_ver
    v.hardware_ver = state.hardware_ver
    v.protocol_ver = state.protocol_ver
    v.compile_time = state.compile_time
    return reply


async def _handle_dev_status(
    state: SimulatorState, frame: framing.Frame, msg: Message
) -> Message | None:
    # The framing-level OP_GET (= 2) doesn't match every service's
    # proto enum — DEV_STATUS has its own ``enum_DEV_STATUS_OPERATE_TYPE_GET = 1``.
    # Accept either: the proto's op_type field (the authoritative
    # source the real firmware reads) or the framing operation byte.
    proto_op = msg.op_type if isinstance(msg, dev_status_pb2.dev_status_msg) else 0
    if (
        frame.operation != dev_status_pb2.enum_DEV_STATUS_OPERATE_TYPE_GET
        and frame.operation != _client.OP_GET
        and proto_op != dev_status_pb2.enum_DEV_STATUS_OPERATE_TYPE_GET
    ):
        return None
    reply = dev_status_pb2.dev_status_msg()
    reply.op_type = dev_status_pb2.enum_DEV_STATUS_OPERATE_TYPE_SEND
    reply.dev_cycling_status_msg.dev_cycling_status = state.cycling_status
    reply.dev_cycling_status_msg.cycling_start_time = state.cycling_start_time
    reply.dev_gps_msg.latitude = state.latitude
    reply.dev_gps_msg.longitude = state.longitude
    rt = reply.rt_data_msg
    rt.real_time_speed = state.real_time_speed_mm_s
    rt.avg_speed = state.avg_speed_mm_s
    rt.riding_time = state.riding_time_ms
    rt.riding_distance = state.riding_distance_cm
    rt.real_time_cad = state.real_time_cad
    rt.real_time_hrm = state.real_time_hrm
    rt.real_time_power = state.real_time_power
    rt.total_height = state.total_height_m
    rt.cur_height = state.cur_height_cm
    rt.cur_slope = state.cur_slope
    rt.course = state.course
    reply.wifi_status = state.wifi_status
    reply.navi_status = state.navi_status
    return reply


async def _handle_user_config(
    state: SimulatorState, frame: framing.Frame, _msg: Message
) -> Message | None:
    if frame.operation != user_config_pb2.enum_USER_CONFIG_OPERATE_TYPE_GET:
        return None
    reply = user_config_pb2.user_config_msg()
    reply.user_config_operate_type = user_config_pb2.enum_USER_CONFIG_OPERATE_TYPE_GET
    u = reply.user_config_data_message
    u.sex = state.user_sex
    u.weight = state.user_weight_g
    u.age = state.user_age
    u.height = state.user_height_cm
    u.wheel_dia = state.user_wheel_dia_mm
    u.bike_weight = state.user_bike_weight_g
    u.time_zone = state.user_time_zone_s
    u.member_id = state.user_member_id
    return reply


async def _handle_cycling_data(
    state: SimulatorState, frame: framing.Frame, msg: Message
) -> Message | None:
    """Handle CYCLING_DATA ops: LIST_GET / FILE_GET / FILE_DEL / ALL_DEL.

    The wire-level distinguishing fields:

    * ``LIST_GET`` (op=1, file_tag=0xFF): reply with one
      ``cycling_data_file_flag_msg`` per entry in
      :attr:`SimulatorState.activity_files`.
    * ``FILE_GET`` (op=3, file_tag=0x55): reply with a single
      transmit-complete PbFrame: 20-byte head with
      ``file_tag=0x55`` and ``end_marker=0x03``, then 4-byte BE
      ``pb_size``, a ``file_download`` protobuf carrying
      ``file_size``, then the raw file bytes. The 20-byte head's
      ``payload_size`` field is intentionally bogus on the BSC200
      (the firmware writes 0x07a7 / 1959 regardless of the actual
      stream length); we mirror that quirk so the framing layer's
      transmit-complete path is exercised end-to-end.
    * ``FILE_DEL`` (op=5): drop the matching entry from
      ``activity_files`` and ACK with status=0.
    * ``ALL_DEL`` (op=6): clear ``activity_files`` and ACK with
      status=0.

    Returns ``None`` for ``LIST_NUM_GET`` and ``AUTO_UPLOAD`` ops —
    the simulator doesn't claim to model those today.
    """
    proto_op = (
        msg.cycling_data_operate_type if isinstance(msg, cycling_data_pb2.cycling_data_msg) else 0
    )
    op = proto_op or frame.operation

    if op == cycling_data_pb2.enum_CYCLING_DATA_OPERATE_TYPE_LIST_GET:
        reply = cycling_data_pb2.cycling_data_msg()
        reply.cycling_data_operate_type = cycling_data_pb2.enum_CYCLING_DATA_OPERATE_TYPE_LIST_SEND
        for f in state.activity_files:
            entry = reply.cycling_data_file_flag_msg.add()
            entry.timestamp = f.timestamp
            entry.file_size = f.file_size
            entry.user_id = f.user_id
            entry.device_id = f.device_id
        return reply

    if op == cycling_data_pb2.enum_CYCLING_DATA_OPERATE_TYPE_FILE_GET:
        flags = (
            msg.cycling_data_file_flag_msg
            if isinstance(msg, cycling_data_pb2.cycling_data_msg)
            else ()
        )
        if not flags:
            return None
        timestamp = flags[0].timestamp
        target = next((f for f in state.activity_files if f.timestamp == timestamp), None)
        # The simulator hands the transmit-complete stream back
        # through a sentinel return value the dispatcher recognises;
        # see :meth:`Simulator._handle_one`.
        return _TransmitCompleteReply(target)  # type: ignore[return-value]

    if op == cycling_data_pb2.enum_CYCLING_DATA_OPERATE_TYPE_FILE_DEL:
        if not state.allow_destructive:
            _LOG.warning("simulator: refusing CYCLING_DATA FILE_DEL (allow_destructive=False)")
            return _ConfirmReply(
                service=common_pb2.enum_SERVICE_TYPE_INDEX_CYCLING_DATA,
                operation=op,
                status=6,  # UnsupportedCommand
            )  # type: ignore[return-value]
        flags = (
            msg.cycling_data_file_flag_msg
            if isinstance(msg, cycling_data_pb2.cycling_data_msg)
            else ()
        )
        if flags:
            target_ts = flags[0].timestamp
            state.activity_files = [f for f in state.activity_files if f.timestamp != target_ts]
        return _ConfirmReply(
            service=common_pb2.enum_SERVICE_TYPE_INDEX_CYCLING_DATA,
            operation=op,
            status=0,
        )  # type: ignore[return-value]

    if op == cycling_data_pb2.enum_CYCLING_DATA_OPERATE_TYPE_ALL_DEL:
        if not state.allow_destructive:
            _LOG.warning("simulator: refusing CYCLING_DATA ALL_DEL (allow_destructive=False)")
            return _ConfirmReply(
                service=common_pb2.enum_SERVICE_TYPE_INDEX_CYCLING_DATA,
                operation=op,
                status=6,
            )  # type: ignore[return-value]
        state.activity_files = []
        return _ConfirmReply(
            service=common_pb2.enum_SERVICE_TYPE_INDEX_CYCLING_DATA,
            operation=op,
            status=0,
        )  # type: ignore[return-value]
    return None


@dataclasses.dataclass(slots=True, frozen=True)
class _ConfirmReply:
    """Sentinel: dispatcher should emit a 20-byte TYPE_CONFIRM frame.

    Handler-return signalling channel — the simulator dispatcher
    recognises this and writes a ``ConfirmFrame`` instead of going
    through ``envelope.encode_message``.
    """

    service: int
    operation: int
    status: int = 0


@dataclasses.dataclass(slots=True, frozen=True)
class _TransmitCompleteReply:
    """Sentinel: dispatcher should emit a transmit-complete download stream."""

    target: SimulatedActivityFile | None


async def _handle_route_plan(
    state: SimulatorState, frame: framing.Frame, msg: Message
) -> Message | None:
    """Handle ROUTE_PLAN LIST_GET → return the simulator's uploaded routes.

    The BSC200 firmware uses ``route_plan_info_msg.status`` to flag
    which route is currently being navigated:
    ``enum_USED_STATUS = 1`` for the active route, ``enum_UNUSED_STATUS
    = 2`` for every other entry. The library's ``nav-status`` command
    consumes this — see ``RoutePlanViewModel.requestUsingRouteID`` in
    the smali for the reference implementation. Other ROUTE_PLAN
    operations (LIST_NUM_GET, FILE_DEL, RENAME) aren't simulated yet;
    add them when a test requires them.

    The real BSC200 doesn't strictly validate the framing-level
    operation byte (it'll reply to LIST_GET even when the client
    sets ``frame.operation = OP_GET = 2`` instead of the proto's
    ``enum_ROUTE_PLAN_OPERATE_TYPE_LIST_GET = 1``); we mirror that
    by checking the proto's ``route_plan_operate_type`` field
    (which IS strictly checked) and accepting OP_GET as a fallback
    so existing live-tested callers keep working.
    """
    proto_op = (
        msg.route_plan_operate_type if isinstance(msg, route_plan_pb2.route_plan_data_msg) else 0
    )
    list_get = route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_LIST_GET
    if frame.operation != list_get and frame.operation != _client.OP_GET and proto_op != list_get:
        return None
    reply = route_plan_pb2.route_plan_data_msg()
    reply.route_plan_operate_type = route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_LIST_SEND
    for entry in state.uploaded_routes:
        info = reply.route_plan_info_msg.add()
        info.id = entry.file_id
        info.file_type = cast("route_plan_pb2.ROUTE_PLAN_FILE_TYPE", entry.file_type)
        info.name = entry.name
        info.total_distance = entry.total_distance
        info.status = (
            route_plan_pb2.enum_USED_STATUS
            if state.active_route_id == entry.file_id
            else route_plan_pb2.enum_UNUSED_STATUS
        )
    return reply


def _make_fake_fit(size: int) -> bytes:
    """Build *size* bytes that pass the BSC200's FIT-magic check.

    The real device stores valid FIT files (12-byte header, records,
    CRC footer). Tests don't need that — they only need the magic at
    bytes 8..11 so :class:`ligpsport.commands.DownloadedFile`'s
    ``fit_magic`` flag flips to True. ``data_size`` (bytes 4..7,
    little-endian) is the only other field the magic check care
    about; we fill it in with ``size - 16`` so the header is
    internally consistent for inspectors that look past the magic.
    """
    if size < 12:
        return (b"\x0e\x10\x54\x08" + b"\x00" * 4 + b".FIT")[:size]
    data_size = max(0, size - 16).to_bytes(4, "little")
    header = b"\x0e\x10\x54\x08" + data_size + b".FIT"
    return header + b"\x00" * (size - len(header))


async def _handle_factory(
    state: SimulatorState, frame: framing.Frame, msg: Message
) -> Message | None:
    """Handle FACTORY ops: today only ``SIM_FIT_SET``.

    Mirrors the BSC200's wired-up ``IGPDeviceManager.simulateFitFile``
    behaviour (factory.proto:92-96 + smali ~28008): the device
    appends *num* synthetic FIT files of *size* bytes each to its
    recorded-activity flash, then ACKs with a status byte.

    The simulator does the same — appends to
    :attr:`SimulatorState.activity_files` so the next ``LIST_GET`` /
    ``FILE_GET`` round-trip sees them — but only when
    ``allow_destructive=True``. Without the opt-in the simulator
    refuses with ``status=6`` (UnsupportedCommand), mirroring the
    runtime gate the CLI / library use.
    """
    sim_fit_set = factory_pb2.enum_FACTORY_OPERATE_TYPE_SIM_FIT_SET
    proto_op = msg.factory_operate_type if isinstance(msg, factory_pb2.factory_msg) else 0
    if frame.operation != sim_fit_set and proto_op != sim_fit_set:
        return None
    if not state.allow_destructive:
        _LOG.warning("simulator: refusing FACTORY SIM_FIT_SET (allow_destructive=False)")
        return _ConfirmReply(
            service=common_pb2.enum_SERVICE_TYPE_INDEX_FACTORY,
            operation=sim_fit_set,
            status=6,
        )  # type: ignore[return-value]
    if not isinstance(msg, factory_pb2.factory_msg):
        return None
    count = int(msg.sim_fit_msg.num)
    size = int(msg.sim_fit_msg.size)
    import time as _time

    base = int(_time.time())
    # Stagger timestamps so each synthetic entry is uniquely
    # addressable by FILE_GET / FILE_DEL — the device's flash uses
    # the FIT start-timestamp as the on-wire id.
    used = {entry.timestamp for entry in state.activity_files}
    for i in range(count):
        ts = base + i
        while ts in used:
            ts += 1
        used.add(ts)
        state.activity_files.append(
            SimulatedActivityFile(
                timestamp=ts,
                file_size=size,
                content=_make_fake_fit(size),
            )
        )
    return _ConfirmReply(
        service=common_pb2.enum_SERVICE_TYPE_INDEX_FACTORY,
        operation=sim_fit_set,
        status=0,
    )  # type: ignore[return-value]


async def _handle_sensor(
    state: SimulatorState, frame: framing.Frame, _msg: Message
) -> Message | None:
    if frame.operation != sensor_pb2.enum_SENSOR_OPERATE_TYPE_GET:
        return None
    reply = sensor_pb2.sensor_message()
    reply.sensor_operate_type = sensor_pb2.enum_SENSOR_OPERATE_TYPE_SEND
    for s in state.sensors:
        entry = reply.sensor_data_msg.add()
        entry.sensor_type = s.sensor_type
        entry.sensor_radio_type = s.sensor_radio_type
        entry.sensor_status_type = s.sensor_status_type
        entry.sensor_key = s.sensor_key
        entry.sensor_ble_name = s.sensor_ble_name
        entry.sensor_pwr = s.sensor_pwr
        entry.wheel_size = s.wheel_size
    return reply


class Simulator:
    """Wire-compatible peer driven by an in-memory :class:`SimulatorState`.

    Lifecycle::

        client_t, peer_t = make_loopback_pair()
        sim = Simulator(peer_t)
        async with sim:
            client = IgpsportClient(client_t)
            await client.start()
            ...

    Inside ``async with sim`` a background task reads frames from the
    peer transport and writes replies through the same transport.
    Leaving the context cancels the task and closes the transport.
    """

    # Service → handler. Each handler is awaited; a non-None return
    # value becomes the reply (encoded through the same envelope.encode
    # path the client uses).
    _HANDLERS: Final[dict[int, Handler]] = {
        common_pb2.enum_SERVICE_TYPE_INDEX_DEV_VER_INFO: _handle_dev_ver_info,
        common_pb2.enum_SERVICE_TYPE_INDEX_DEV_STATUS: _handle_dev_status,
        common_pb2.enum_SERVICE_TYPE_INDEX_USER_CONFIG: _handle_user_config,
        common_pb2.enum_SERVICE_TYPE_INDEX_CYCLING_DATA: _handle_cycling_data,
        common_pb2.enum_SERVICE_TYPE_INDEX_SENSOR: _handle_sensor,
        common_pb2.enum_SERVICE_TYPE_INDEX_ROUTE_PLAN: _handle_route_plan,
        common_pb2.enum_SERVICE_TYPE_INDEX_FACTORY: _handle_factory,
    }

    def __init__(self, transport: Transport, state: SimulatorState | None = None):
        self._transport = transport
        self.state = state if state is not None else SimulatorState()
        self._task: asyncio.Task[None] | None = None
        # Pending data-channel chunks waiting for their matching
        # control-channel trailer. Indexed by the channel they
        # arrived on so we don't cross the streams.
        self._pending_chunk: dict[Channel, bytes] = {}
        # In-progress route-plan upload; finalised on end_type=3.
        self._in_progress_route: UploadedRouteFile | None = None
        # In-progress FILE_OPERATION ADD upload (CNX route stream that
        # the BSC200 firmware actually accepts). Unlike the chunked
        # FILE_SEND path this is a single multi-write stream on the
        # fourth channel with no per-chunk trailer; we accumulate
        # until the head's declared payload size is satisfied.
        self._in_progress_general_upload: bytearray | None = None

    async def __aenter__(self) -> Simulator:
        self._task = asyncio.create_task(self._serve(), name="ligpsport-simulator")
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await self._transport.close()

    async def push(self, message: Message, *, operation: int = _client.OP_SEND) -> None:
        """Send an unsolicited frame to the connected client.

        Used to simulate device-initiated events such as periodic
        ``DEV_STATUS`` updates or incoming-call notifications.
        """
        service_type, payload = envelope.encode_message(message)
        frame = framing.Frame(service=service_type, operation=operation, payload=payload)
        await self._transport.send(framing.build_frame(frame))

    async def _serve(self) -> None:
        try:
            while True:
                channel, raw = await self._next_inbound()
                await self._handle_one(channel, raw)
        except TransportClosed:
            _LOG.debug("simulator transport closed; exiting")
        except Exception:
            _LOG.exception("simulator crashed")
            raise

    async def _next_inbound(self) -> tuple[Channel, bytes]:
        """Read the next ``(channel, bytes)`` tuple from the transport.

        Falls back to plain :meth:`Transport.receive` (channel
        defaulted to ``"control"``) when running against a transport
        that doesn't expose channel info — keeps the simulator usable
        with any future transport that doesn't model channels.
        """
        if isinstance(self._transport, LoopbackTransport):
            return await self._transport.receive_with_channel()
        return ("control", await self._transport.receive())

    async def _handle_one(self, channel: Channel, raw: bytes) -> None:
        # Three distinct streams can land on data / fourth channels:
        #
        # (a) ROUTE_PLAN FILE_SEND chunks (legacy gen-3 split):
        #     each write is a complete route_plan_data_msg protobuf,
        #     paired with a 20-byte trailer on control. Starts with
        #     0x08 (field-1 varint tag), no leading head.
        # (b) FILE_OPERATION ADD streams: multi-write stream on
        #     the fourth channel that starts with a 20-byte head
        #     (byte 1 == 0x15 = FILE_OPERATION) and has no per-chunk
        #     trailer. Accumulate until the head's size prefix is
        #     satisfied, then ACK.
        # (c) ROUTE_PLAN merged write (gen-4 BSC200): a single write
        #     of (20-byte head with service=0x07) || protobuf body,
        #     no follow-up trailer. Dispatch to the FILE_USE handler
        #     for op=5 and to the FILES_DEL handler for op=6 (the
        #     two ops the library currently emits this way).
        if channel in ("data", "fourth"):
            if self._in_progress_general_upload is not None or self._looks_like_file_op_head(raw):
                await self._absorb_general_upload_chunk(raw)
                return
            op = self._route_plan_merged_op(raw)
            if op is not None:
                payload_size = (raw[framing.HDR_PAYLOAD_SIZE] << 8) | raw[
                    framing.HDR_PAYLOAD_SIZE + 1
                ]
                body = raw[framing.HEADER_SIZE : framing.HEADER_SIZE + payload_size]
                if op == route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_FILE_USE:
                    await self._handle_route_use(body)
                    return
                if op == route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_FILES_DEL:
                    await self._handle_route_files_del(body)
                    return
            self._pending_chunk[channel] = raw
            return
        # Control-channel writes are usually a full PbFrame. The
        # route-upload trailer is also written here, but it's an
        # *un-paired* 20-byte header whose payload_size points at the
        # raw protobuf already buffered on data/fourth. Disambiguate
        # by length plus the buffered-chunk presence.
        if len(raw) == framing.HEADER_SIZE and self._pending_chunk:
            for buffered_channel, chunk in list(self._pending_chunk.items()):
                match = self._match_route_chunk(chunk, raw)
                if match is not None:
                    op, end_type = match
                    self._pending_chunk.pop(buffered_channel, None)
                    if op == route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_FILE_USE:
                        await self._handle_route_use(chunk)
                    else:
                        await self._handle_route_upload_chunk(chunk, end_type)
                    return
        try:
            frame = framing.parse_frame(raw)
        except framing.FrameError as exc:
            _LOG.warning("simulator dropping malformed frame: %s", exc)
            return
        self.state.received.append(frame)
        try:
            msg = envelope.decode_payload(frame.service, frame.payload)
        except envelope.UnknownServiceError:
            _LOG.warning("simulator dropping unknown service %d", frame.service)
            return
        handler = self._HANDLERS.get(frame.service)
        if handler is None:
            _LOG.debug("no simulator handler for service=%d", frame.service)
            return
        reply = await handler(self.state, frame, msg)
        if reply is None:
            return
        if isinstance(reply, _ConfirmReply):
            # Synthesise a 20-byte TYPE_CONFIRM frame and write it
            # back. Used by the CYCLING_DATA FILE_DEL / ALL_DEL ACKs
            # (PROTOCOL.md §6.4) and any future sentinel-driven
            # responses that don't fit the protobuf reply model.
            await self._transport.send(
                framing.build_frame(
                    framing.Frame(
                        type=framing.TYPE_CONFIRM,
                        service=reply.service,
                        operation=reply.operation,
                        status=reply.status,
                    )
                )
            )
            return
        if isinstance(reply, _TransmitCompleteReply):
            # CYCLING_DATA FILE_GET reply: build the transmit-complete
            # stream the BSC200 sends (PROTOCOL.md §6.4):
            #   [20B head, file_tag=0x55, op=FILE_SEND(4), end=0x03]
            #   [4B BE pb_size]
            #   [file_download protobuf]
            #   [file_size raw FIT bytes]
            await self._send_activity_file(reply.target)
            return
        service_type, payload = envelope.encode_message(reply)
        out_frame = framing.Frame(
            service=service_type,
            operation=_client.OP_SEND,
            payload=payload,
        )
        await self._transport.send(framing.build_frame(out_frame))

    async def _send_activity_file(self, target: SimulatedActivityFile | None) -> None:
        """Emit a transmit-complete CYCLING_DATA FILE_SEND stream for *target*.

        Mirrors the BSC200 firmware's observed wire format byte-for-byte
        (see ``tmp/decode_full.py`` + PROTOCOL.md §6.4). The 20-byte
        head's ``payload_size`` field is intentionally bogus on the
        BSC200 — the embedded ``file_download.file_size`` is
        authoritative. We mirror the quirk so tests exercising the
        client's transmit-complete decoder also exercise the
        framing layer's :func:`framing.transmit_complete_total_size`
        path. *target* may be ``None`` when the client asks for a
        timestamp that doesn't exist on the device; in that case
        the simulator emits a zero-length stream so the client sees
        an explicit "no file" reply.
        """
        from .proto import file_download_pb2

        if target is None:
            file_bytes = b""
            pb_msg = file_download_pb2.file_download()
            pb_msg.file_size = 0
        else:
            file_bytes = target.content
            pb_msg = file_download_pb2.file_download()
            pb_msg.file_size = target.file_size
        pb_bytes = pb_msg.SerializeToString()
        # Bogus payload_size — see PROTOCOL.md §6.4. The real
        # BSC200 writes 0x07a7 (1959). Match it so the framing
        # layer's CRC-relaxation also gets exercised end-to-end.
        bogus_size = 0x07A7
        head = bytearray(framing.HEADER_SIZE)
        head[framing.HDR_TYPE] = framing.TYPE_PB
        head[framing.HDR_SERVICE] = common_pb2.enum_SERVICE_TYPE_INDEX_CYCLING_DATA & 0xFF
        head[framing.HDR_SUB_SERVICE] = 0xFF
        head[framing.HDR_FILE_TAG] = framing.FILE_TAG_TRANSMIT_COMPLETE
        head[framing.HDR_OPERATION] = (
            cycling_data_pb2.enum_CYCLING_DATA_OPERATE_TYPE_FILE_SEND & 0xFF
        )
        head[framing.HDR_SUB_OPERATION] = 0xFF
        head[framing.HDR_RESERVED_6] = 0xFF
        head[framing.HDR_PAYLOAD_SIZE] = (bogus_size >> 8) & 0xFF
        head[framing.HDR_PAYLOAD_SIZE + 1] = bogus_size & 0xFF
        head[framing.HDR_PAYLOAD_CRC] = 0
        head[framing.HDR_END_MARKER] = 0x03  # last chunk
        for off in range(11, 19):
            head[off] = 0xFF
        head[framing.HDR_HEADER_CRC] = framing.crc8(bytes(head[: framing.HDR_HEADER_CRC]))
        wire = bytes(head) + len(pb_bytes).to_bytes(4, "big") + pb_bytes + file_bytes
        await self._transport.send(wire)

    @staticmethod
    def _match_route_chunk(chunk: bytes, header: bytes) -> tuple[int, int] | None:
        """Return ``(operation, end_type)`` if ``(chunk, header)`` is a route trailer.

        Returns None when the header isn't a recognized route-plan
        trailer; the caller then dispatches the header through the
        normal frame path. Validates the trailer is well-formed (type,
        service, declared size, payload CRC, header CRC). Recognized
        operations: ``FILE_SEND`` (chunked upload) and ``FILE_USE``
        (single-frame commit after upload).
        """
        if header[framing.HDR_TYPE] != framing.TYPE_PB:
            return None
        if header[framing.HDR_SERVICE] != common_pb2.enum_SERVICE_TYPE_INDEX_ROUTE_PLAN:
            return None
        op = header[framing.HDR_OPERATION]
        if op not in (
            route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_FILE_SEND,
            route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_FILE_USE,
        ):
            return None
        declared = (header[framing.HDR_PAYLOAD_SIZE] << 8) | header[framing.HDR_PAYLOAD_SIZE + 1]
        if declared != len(chunk):
            return None
        if framing.crc8(chunk) != header[framing.HDR_PAYLOAD_CRC]:
            _LOG.warning("simulator: route-upload chunk failed payload CRC")
            return None
        if framing.crc8(header[: framing.HDR_HEADER_CRC]) != header[framing.HDR_HEADER_CRC]:
            _LOG.warning("simulator: route-upload trailer failed header CRC")
            return None
        return (op, header[framing.HDR_END_MARKER])

    async def _handle_route_upload_chunk(self, chunk: bytes, end_type: int) -> None:
        """Append a route-upload chunk and ACK it.

        Mirrors `checkSendRoutePlanFileIsReceiveFinish` in the smali:
        every chunk gets a ConfirmFrame with status=0. We don't model
        the progress / done-early status codes — they are device-side
        bookkeeping the client already treats as "continue" / "stop".

        Chunks accumulate in :attr:`_in_progress_route` until a chunk
        with ``end_type=3`` arrives; that one finalises the entry and
        appends it to :attr:`SimulatorState.uploaded_routes`.
        """
        msg = route_plan_pb2.route_plan_data_msg()
        msg.ParseFromString(chunk)
        if self._in_progress_route is None:
            info = msg.route_plan_info_msg[0] if msg.route_plan_info_msg else None
            extension = ""
            if msg.line_id:
                parts = msg.line_id[0].rsplit(".", 1)
                if len(parts) == 2:
                    extension = parts[1]
            self._in_progress_route = UploadedRouteFile(
                file_id=info.id if info is not None else 0,
                file_type=info.file_type if info is not None else 0,
                name=info.name if info is not None else "",
                extension=extension,
                total_distance=info.total_distance if info is not None else 0,
                content=b"",
                end_types=[],
            )
        self._in_progress_route.content += msg.file_content
        self._in_progress_route.end_types.append(end_type)
        if end_type == 3:
            self.state.uploaded_routes.append(self._in_progress_route)
            self._in_progress_route = None
        ack = framing.build_frame(
            framing.Frame(
                type=framing.TYPE_CONFIRM,
                service=common_pb2.enum_SERVICE_TYPE_INDEX_ROUTE_PLAN,
                operation=route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_FILE_SEND,
                status=0,
            )
        )
        await self._transport.send(ack)

    @staticmethod
    def _looks_like_file_op_head(raw: bytes) -> bool:
        """Heuristic: does *raw* start with a FILE_OPERATION ADD head?

        FILE_OPERATION upload heads are 20-byte PbFrame heads with
        service=21 (FILE_OPERATION) at offset 1 and file_tag=0xaa at
        offset 3. A ROUTE_PLAN FILE_SEND chunk is a raw protobuf and
        starts with 0x08 (field-1 varint tag), so the two can't
        collide. See docs/PROTOCOL.md §7.1.2.
        """
        return (
            len(raw) >= framing.HEADER_SIZE
            and raw[framing.HDR_TYPE] == framing.TYPE_PB
            and raw[framing.HDR_SERVICE]
            == (common_pb2.enum_SERVICE_TYPE_INDEX_FILE_OPERATION & 0xFF)
            and raw[framing.HDR_FILE_TAG] == 0xAA
        )

    @staticmethod
    def _route_plan_merged_op(raw: bytes) -> int | None:
        """If *raw* starts with a ROUTE_PLAN merged-write head, return its op.

        Gen-4 devices (BSC200) receive certain ROUTE_PLAN operations
        as a single merged write to the fourth characteristic — 20-byte
        head followed by the protobuf body, no control trailer. The
        library uses this pattern for ``FILE_USE`` (op=5) and
        ``FILES_DEL`` (op=6); a head with ROUTE_PLAN service + one of
        those op codes triggers the merged-write dispatch. Other
        ``FILE_SEND`` chunks (legacy gen-3 split) start with a
        protobuf field-1 tag (0x08), not 0x01, and so don't collide.
        """
        if (
            len(raw) < framing.HEADER_SIZE
            or raw[framing.HDR_TYPE] != framing.TYPE_PB
            or raw[framing.HDR_SERVICE] != (common_pb2.enum_SERVICE_TYPE_INDEX_ROUTE_PLAN & 0xFF)
        ):
            return None
        op = raw[framing.HDR_OPERATION]
        if op in (
            route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_FILE_USE,
            route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_FILES_DEL,
        ):
            return int(op)
        return None

    async def _absorb_general_upload_chunk(self, raw: bytes) -> None:
        """Buffer one slice of a FILE_OPERATION ADD upload; finalise on EOF.

        Wire layout (per PROTOCOL.md §7.1.2):
          * 20-byte head (file_tag = 0xaa identifies an upload stream)
          * 4-byte BE ``pb_size`` (length of the protobuf only)
          * ``general_file_operation`` protobuf, which carries
            ``file_size`` as a required field
          * raw ``file_size`` file bytes (CNX for route plans)

        Total expected stream length = ``20 + 4 + pb_size + file_size``.
        We accumulate writes until that's reached, then parse + ACK.
        """
        from .proto import general_file_operation_pb2

        if self._in_progress_general_upload is None:
            self._in_progress_general_upload = bytearray()
        self._in_progress_general_upload.extend(raw)
        buf = self._in_progress_general_upload
        if len(buf) < framing.HEADER_SIZE + 4:
            return  # still waiting for the pb size prefix
        pb_size = int.from_bytes(bytes(buf[framing.HEADER_SIZE : framing.HEADER_SIZE + 4]), "big")
        pb_start = framing.HEADER_SIZE + 4
        pb_end = pb_start + pb_size
        if len(buf) < pb_end:
            return  # still waiting for the protobuf body
        pb_msg = general_file_operation_pb2.general_file_operation()
        pb_msg.ParseFromString(bytes(buf[pb_start:pb_end]))
        total_expected = pb_end + pb_msg.file_size
        if len(buf) < total_expected:
            return  # still waiting for the file bytes
        file_bytes = bytes(buf[pb_end:total_expected])
        self._in_progress_general_upload = None
        entry = UploadedRouteFile(
            file_id=pb_msg.file_id,
            file_type=pb_msg.file_type,
            name=pb_msg.file_name,
            extension=pb_msg.file_extension,
            total_distance=0,
            content=file_bytes,
            end_types=[],
        )
        self.state.uploaded_routes.append(entry)
        ack = framing.build_frame(
            framing.Frame(
                type=framing.TYPE_CONFIRM,
                service=common_pb2.enum_SERVICE_TYPE_INDEX_FILE_OPERATION,
                operation=common_pb2.enum_SERVICE_OPERATE_TYPE_ADD,
                status=0,
            )
        )
        await self._transport.send(ack)

    async def _handle_route_use(self, chunk: bytes) -> None:
        """Handle a FILE_USE commit.

        Mirrors the BSC200 firmware's observed behaviour (from
        ``docs/PROTOCOL.md`` §7.2 + the snoop_start capture):

        * If the requested ``file_id`` is in
          :attr:`SimulatorState.uploaded_routes`, the simulator
          activates it — sets :attr:`active_route_id` and flips
          :attr:`navi_status` to ``DEV_NAVI_STATUS_ON`` (1) — and
          ACKs with ``status=0`` (Success).
        * Otherwise the simulator ACKs with ``status=66``
          (``NavigationRouteDoesNotExist`` — wire byte ``0x42``
          from ``DeviceReturnStatus.smali``). The real device
          returns the same code; the app retries after uploading.
        """
        msg = route_plan_pb2.route_plan_data_msg()
        msg.ParseFromString(chunk)
        info = msg.route_plan_info_msg[0] if msg.route_plan_info_msg else None
        used_id = info.id if info is not None else 0
        found = False
        for entry in self.state.uploaded_routes:
            if entry.file_id == used_id:
                self.state.active_route_id = used_id
                self.state.navi_status = dev_status_pb2.DEV_NAVI_STATUS_ON
                found = True
                break
        ack = framing.build_frame(
            framing.Frame(
                type=framing.TYPE_CONFIRM,
                service=common_pb2.enum_SERVICE_TYPE_INDEX_ROUTE_PLAN,
                operation=route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_FILE_USE,
                status=0 if found else 66,
            )
        )
        await self._transport.send(ack)

    async def _handle_route_files_del(self, chunk: bytes) -> None:
        """Handle a FILES_DEL request — mirror BSC200 active-route protection.

        The captured behaviour (PROTOCOL.md §7.4):

        * The device acknowledges every FILES_DEL with status=0 — there
          is no per-id failure reply.
        * Inactive routes (``status != USED``) named in ``line_id`` /
          ``route_plan_info_msg`` are actually removed from the
          on-device list.
        * The active route (``status == USED``) is silently kept
          even when included in the request — the firmware refuses
          to delete a route that's currently being navigated.

        We mirror both: drop every targeted ``UploadedRouteFile``
        entry from :attr:`SimulatorState.uploaded_routes` *unless*
        it's the active one. ACK with status=0 either way.
        """
        msg = route_plan_pb2.route_plan_data_msg()
        msg.ParseFromString(chunk)
        target_ids: set[int] = set()
        for info in msg.route_plan_info_msg:
            target_ids.add(int(info.id))
        # Some firmwares carry only line_id; pull ids from "<id>.ext"
        # as a fallback so the handler stays compatible.
        for line in msg.line_id:
            stem = line.rsplit(".", 1)[0]
            try:
                target_ids.add(int(stem))
            except ValueError:
                continue

        kept: list[UploadedRouteFile] = []
        for entry in self.state.uploaded_routes:
            if entry.file_id in target_ids and entry.file_id != self.state.active_route_id:
                continue
            kept.append(entry)
        self.state.uploaded_routes = kept

        ack = framing.build_frame(
            framing.Frame(
                type=framing.TYPE_CONFIRM,
                service=common_pb2.enum_SERVICE_TYPE_INDEX_ROUTE_PLAN,
                operation=route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_FILES_DEL,
                status=0,
            )
        )
        await self._transport.send(ack)


class SimulatedTransport(Transport):
    """Client-side transport with a :class:`Simulator` running behind it.

    Drop-in for a BLE transport: entering the context starts the
    simulator on the far end of a loopback pair, leaving it stops the
    simulator. Lets interactive front-ends (``ligpsport-gui
    --simulator``) run end-to-end without hardware. *state* is shared,
    so uploads and deletes persist across reconnects.
    """

    def __init__(self, state: SimulatorState) -> None:
        self._client_end, peer_end = make_loopback_pair()
        self._sim = Simulator(peer_end, state)

    async def __aenter__(self) -> SimulatedTransport:
        await self._sim.__aenter__()
        return self

    async def send(self, frame: bytes, *, channel: Channel = "control") -> None:
        await self._client_end.send(frame, channel=channel)

    async def receive(self) -> bytes:
        return await self._client_end.receive()

    async def close(self) -> None:
        await self._client_end.close()
        await self._sim.__aexit__(None, None, None)
