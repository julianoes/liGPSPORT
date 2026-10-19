"""Named-command registry.

The registry is the single source of truth for what library commands
exist. Both the CLI in :mod:`ligpsport.__main__` and library callers
go through :func:`run_named` to invoke a command by name.

A command runs against an open :class:`ligpsport.client.IgpsportClient`.
Each entry declares whether it mutates persistent state on the device
(``destructive=True``) so the CLI's
``--allow-destructive-commands`` gate can refuse risky ops by
default. See ``AGENTS.md`` §2 for the policy.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Final, Protocol, runtime_checkable

from . import client as _client
from . import file_transfer, fit_activity
from .proto import (
    dev_status_pb2,
    dev_ver_info_pb2,
    factory_pb2,
    firmware_pb2,
    route_book_pb2,
    route_plan_pb2,
    sensor_pb2,
    user_config_pb2,
    wifi_pb2,
)

# Service+operation tuples that mutate persistent state on the device.
# The CLI raw-frame escape hatch checks against this list to refuse
# unsafe ops without an explicit override. New commands that touch
# any of these must declare destructive=True so we have two layers of
# defence (the runtime gate and this prefix-check).
DESTRUCTIVE_PREFIXES: Final[tuple[tuple[int, int], ...]] = (
    # CYCLING_DATA delete operations: FILE_DEL=5, ALL_DEL=6.
    (6, 5),
    (6, 6),
    # ROUTE_PLAN file delete & multi-file delete.
    (7, 3),
    (7, 6),
    # FIRMWARE upgrade flows (MCU=3, BLE=5).
    (4, 3),
    (4, 5),
    # FACTORY SN_SET (3) and SIM_FIT_SET (7) — both mutate persistent
    # state in a way the user wouldn't want triggered accidentally.
    (11, 3),
    (11, 7),
)


class CommandError(ValueError):
    """Base class for command-registry errors."""


class UnknownCommandError(CommandError):
    """No command by that name."""


class DestructiveCommandError(CommandError):
    """Caller invoked a destructive command without ``allow_destructive``."""


@dataclasses.dataclass(slots=True, frozen=True)
class CommandResult:
    """Outcome of a named command invocation.

    The ``value`` field is whatever structured result the runner
    returned: usually a dataclass with its own ``to_dict`` /
    ``format`` methods. Plain scalars and strings pass through.
    """

    name: str
    value: object

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "value": _jsonable(self.value)}

    def format(self) -> str:
        return _format(self.value)


@runtime_checkable
class _HasToDict(Protocol):
    """Result dataclasses expose to_dict(); see AGENTS.md §4."""

    def to_dict(self) -> object: ...


@runtime_checkable
class _HasFormat(Protocol):
    """Result dataclasses expose format(); see AGENTS.md §4."""

    def format(self) -> str: ...


def _jsonable(value: object) -> object:
    if isinstance(value, _HasToDict):
        return value.to_dict()
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _format(value: object) -> str:
    if isinstance(value, _HasFormat):
        return value.format()
    if isinstance(value, dict | list):
        return json.dumps(_jsonable(value), indent=2, sort_keys=True)
    return str(value)


Runner = Callable[
    ["CommandSpec", _client.IgpsportClient, Sequence[str], float],
    Awaitable[object],
]


@dataclasses.dataclass(slots=True, frozen=True)
class CommandSpec:
    """One entry in the command registry."""

    name: str
    description: str
    runner: Runner
    destructive: bool = False
    danger: str | None = None

    async def run(
        self,
        client: _client.IgpsportClient,
        args: Sequence[str],
        *,
        timeout: float = 6.0,
        allow_destructive: bool = False,
    ) -> CommandResult:
        if self.destructive and not allow_destructive:
            raise DestructiveCommandError(
                f"command {self.name!r} is destructive ({self.danger}); pass allow_destructive=True"
            )
        value = await self.runner(self, client, tuple(args), timeout)
        return CommandResult(name=self.name, value=value)


# ---------- result dataclasses --------------------------------------------


@dataclasses.dataclass(slots=True, frozen=True)
class DeviceVersion:
    """Decoded payload of a ``DEV_VER_INFO`` SEND reply."""

    main_boot_ver: int
    main_app_ver: int
    ble_boot_ver: int
    ble_app_ver: int
    hardware_ver: int
    protocol_ver: int
    compile_time: str

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        return (
            f"BLE boot ver:     {self.ble_boot_ver}\n"
            f"BLE app ver:      {self.ble_app_ver}\n"
            f"MCU boot ver:     {self.main_boot_ver}\n"
            f"MCU app ver:      {self.main_app_ver}\n"
            f"Hardware ver:     {self.hardware_ver}\n"
            f"Protocol ver:     {self.protocol_ver}\n"
            f"Compile time:     {self.compile_time}"
        )


# ---------- runners --------------------------------------------------------


async def _r_version(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    _args: Sequence[str],
    timeout: float,
) -> DeviceVersion:
    request = dev_ver_info_pb2.dev_ver_info_msg()
    request.operate_type = dev_ver_info_pb2.enum_OPERATE_TYPE_GET
    response = await client.request(request, timeout=timeout)
    msg = response.message
    if not isinstance(msg, dev_ver_info_pb2.dev_ver_info_msg):
        raise CommandError(f"unexpected response message: {type(msg).__name__}")
    v = msg.version_message
    return DeviceVersion(
        main_boot_ver=v.main_boot_ver,
        main_app_ver=v.main_app_ver,
        ble_boot_ver=v.ble_boot_ver,
        ble_app_ver=v.ble_app_ver,
        hardware_ver=v.hardware_ver,
        protocol_ver=v.protocol_ver,
        compile_time=v.compile_time,
    )


@dataclasses.dataclass(slots=True, frozen=True)
class DeviceStatus:
    """Real-time cycling status (decoded ``DEV_STATUS`` SEND payload)."""

    cycling_status: int  # FREE=0 / DOING=1 / PAUSE=2
    cycling_start_time: int
    latitude: float
    longitude: float
    real_time_speed_mm_s: int
    avg_speed_mm_s: int
    riding_time_ms: int
    riding_distance_cm: int
    real_time_cad: int
    real_time_hrm: int
    real_time_power: int
    total_height_m: int
    cur_height_cm: int
    cur_slope: int
    course: int
    wifi_status: int
    navi_status: int

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        # Convert to friendlier units. Speed in km/h, distance in km.
        speed_kmh = self.real_time_speed_mm_s * 3.6 / 1_000_000
        avg_kmh = self.avg_speed_mm_s * 3.6 / 1_000_000
        distance_km = self.riding_distance_cm / 100_000
        time_h = self.riding_time_ms / 3_600_000
        status_name = {0: "free", 1: "doing", 2: "paused"}.get(self.cycling_status, "unknown")
        return (
            f"State:            {status_name}\n"
            f"Speed:            {speed_kmh:.2f} km/h  (avg {avg_kmh:.2f})\n"
            f"Distance:         {distance_km:.3f} km   ({time_h:.2f} h)\n"
            f"Heart rate:       {self.real_time_hrm} bpm\n"
            f"Cadence:          {self.real_time_cad} rpm\n"
            f"Power:            {self.real_time_power} W\n"
            f"Altitude:         {self.cur_height_cm / 100:.1f} m"
            f"  (total +{self.total_height_m} m)\n"
            f"Slope:            {self.cur_slope / 100:.2f}%\n"
            f"Course:           {self.course} deg\n"
            f"GPS:              {self.latitude:.6f}, {self.longitude:.6f}"
        )


@dataclasses.dataclass(slots=True, frozen=True)
class UserConfig:
    """User profile stored on the device."""

    sex: int  # 1 = male, 0 = female
    weight_g: int  # uint, in 100g units on the wire
    age: int
    height_cm: int
    wheel_dia_mm: int
    bike_weight_g: int
    time_zone_s: int
    member_id: str

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        sex_name = {0: "female", 1: "male"}.get(self.sex, str(self.sex))
        return (
            f"Sex:           {sex_name}\n"
            f"Age:           {self.age}\n"
            f"Weight:        {self.weight_g / 10:.1f} kg\n"
            f"Height:        {self.height_cm} cm\n"
            f"Bike weight:   {self.bike_weight_g / 10:.1f} kg\n"
            f"Wheel:         {self.wheel_dia_mm} mm\n"
            f"Time zone:     {self.time_zone_s / 3600:+g} h\n"
            f"Member id:     {self.member_id}"
        )


@dataclasses.dataclass(slots=True, frozen=True)
class ActivityFile:
    """One entry in the device's recorded-activity list."""

    timestamp: int
    file_size: int
    user_id: str
    device_id: str

    @property
    def started_at(self) -> _dt.datetime:
        """Activity start as a naive datetime in the device's local wall-clock.

        The listing timestamp is local time encoded as Garmin-epoch
        seconds (see ``_r_download_all_activities``), so no timezone
        is attached. Matches the companion Android app's reading.
        """
        return _dt.datetime(1989, 12, 31) + _dt.timedelta(seconds=self.timestamp)

    def default_filename(self) -> str:
        """Filename derivable from the listing alone, e.g. ``20261020T081500.fit``.

        Unlike :func:`fit_activity.activity_filename_from_meta` this
        needs no download, so a front-end can tell which activities
        are already saved. Same scheme as the Android app.
        """
        return self.started_at.strftime("%Y%m%dT%H%M%S") + ".fit"

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        import datetime as _dt

        # The device records the timestamp as a uint32 epoch-seconds
        # value rebased on iGPSPORT's "Garmin epoch" (1989-12-31
        # 00:00 UTC), the same epoch the embedded FIT file uses. A
        # naïve ISO render keeps the digits readable in the
        # activity list without committing to a specific timezone.
        epoch = _dt.datetime(1989, 12, 31, tzinfo=_dt.UTC)
        when = epoch + _dt.timedelta(seconds=self.timestamp)
        return (
            f"timestamp={self.timestamp} ({when.isoformat()}) "
            f"size={self.file_size}B "
            f"user={self.user_id!r} device={self.device_id!r}"
        )


@dataclasses.dataclass(slots=True, frozen=True)
class ActivityList:
    """Wrapper around a list of recorded activities."""

    files: tuple[ActivityFile, ...]

    def to_dict(self) -> dict[str, object]:
        return {"files": [f.to_dict() for f in self.files]}

    def format(self) -> str:
        if not self.files:
            return "no recorded activities on device"
        return "\n".join(f.format() for f in self.files)


@dataclasses.dataclass(slots=True, frozen=True)
class SensorList:
    """Wrapper around the SENSOR service's reported sensor list."""

    sensors: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {"sensors": list(self.sensors)}

    def format(self) -> str:
        if not self.sensors:
            return "no sensors paired"
        lines = []
        for s in self.sensors:
            lines.append(", ".join(f"{k}={v}" for k, v in s.items() if v))
        return "\n".join(lines)


async def _r_status(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    _args: Sequence[str],
    timeout: float,
) -> DeviceStatus:
    request = dev_status_pb2.dev_status_msg()
    request.op_type = dev_status_pb2.enum_DEV_STATUS_OPERATE_TYPE_GET
    response = await client.request(request, timeout=timeout)
    msg = response.message
    if not isinstance(msg, dev_status_pb2.dev_status_msg):
        raise CommandError(f"unexpected response message: {type(msg).__name__}")
    cs = msg.dev_cycling_status_msg
    gps = msg.dev_gps_msg
    rt = msg.rt_data_msg
    return DeviceStatus(
        cycling_status=cs.dev_cycling_status,
        cycling_start_time=cs.cycling_start_time,
        latitude=gps.latitude,
        longitude=gps.longitude,
        real_time_speed_mm_s=rt.real_time_speed,
        avg_speed_mm_s=rt.avg_speed,
        riding_time_ms=rt.riding_time,
        riding_distance_cm=rt.riding_distance,
        real_time_cad=rt.real_time_cad,
        real_time_hrm=rt.real_time_hrm,
        real_time_power=rt.real_time_power,
        total_height_m=rt.total_height,
        cur_height_cm=rt.cur_height,
        cur_slope=rt.cur_slope,
        course=rt.course,
        wifi_status=msg.wifi_status,
        navi_status=msg.navi_status,
    )


@dataclasses.dataclass(slots=True, frozen=True)
class NavStatus:
    """Result of the ``nav-status`` command.

    Surfaces whether the device is currently navigating a route and,
    when it is, the file_id / name of the active route. The check
    goes via ``ROUTE_PLAN LIST_GET`` (the iGPSPORT app's own
    mechanism — see ``RoutePlanViewModel.requestUsingRouteID`` in
    the smali): every route on the device carries a
    ``ROUTE_PLAN_FILE_STATUS`` enum and the one currently being
    navigated is tagged ``enum_USED_STATUS = 1``.

    ``DEV_STATUS.navi_status`` is documented in ``dev_status.proto``
    but **the BSC200 firmware never populates it** — the field is
    always 0 on the wire even while the device is actively
    navigating. Verified against firmware 2024-05-14 via a live
    DEV_STATUS GET. The route-list path is what the app uses and
    what the library now uses.
    """

    is_navigating: bool
    active_route_id: int | None
    active_route_name: str

    def to_dict(self) -> dict[str, object]:
        return {
            "is_navigating": self.is_navigating,
            "active_route_id": self.active_route_id,
            "active_route_name": self.active_route_name,
        }

    def format(self) -> str:
        if not self.is_navigating:
            return "Navigation: OFF"
        rid = self.active_route_id if self.active_route_id is not None else "?"
        return f"Navigation: ON  (route_id={rid} name={self.active_route_name!r})"


async def _r_nav_status(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    _args: Sequence[str],
    timeout: float,
) -> NavStatus:
    """Read the route-plan list and report which (if any) route is active.

    Mirrors ``RoutePlanViewModel.requestUsingRouteID`` in the iGPSPORT
    app: send ``ROUTE_PLAN LIST_GET`` with an inclusive index range,
    iterate the returned ``route_plan_info_msg`` entries, and pick
    the one with ``status == enum_USED_STATUS (1)``. The earlier
    implementation read ``DEV_STATUS.navi_status`` instead, but the
    BSC200 firmware doesn't populate that field — it's always 0.

    The ``file_index_start = 0`` / ``file_index_end = 100`` range is
    a sufficient upper bound: the device's ``file_list_support_num_max``
    (queryable via ``LIST_NUM_GET``) is 10 in current BSC200 firmware,
    so any plausible route list fits.
    """
    request = route_plan_pb2.route_plan_data_msg()
    request.route_plan_operate_type = route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_LIST_GET
    # The device returns an empty list if no `route_list_get_msg`
    # range is supplied — verified against BSC200 firmware
    # 2024-05-14. The Android app always sends start/end.
    request.route_list_get_msg.file_index_start = 0
    request.route_list_get_msg.file_index_end = 100
    response = await client.request(request, timeout=timeout)
    msg = response.message
    if not isinstance(msg, route_plan_pb2.route_plan_data_msg):
        raise CommandError(f"unexpected response message: {type(msg).__name__}")
    used = route_plan_pb2.enum_USED_STATUS
    for entry in msg.route_plan_info_msg:
        if entry.status == used:
            return NavStatus(
                is_navigating=True,
                active_route_id=int(entry.id),
                active_route_name=str(entry.name),
            )
    return NavStatus(is_navigating=False, active_route_id=None, active_route_name="")


@dataclasses.dataclass(slots=True, frozen=True)
class RouteSummary:
    """One entry in the ``list-routes`` reply — just identification fields."""

    id: int
    name: str
    is_active: bool

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        marker = " *" if self.is_active else ""
        return f"id={self.id} name={self.name!r}{marker}"


@dataclasses.dataclass(slots=True, frozen=True)
class RouteSummaryList:
    """Result of the ``list-routes`` command — id/name pairs only.

    The fuller ``routes`` command surfaces file_type, total_distance
    and status; ``list-routes`` is the focused variant for scripts
    that just want ``(id, name)`` pairs to drive ``upload-route`` /
    future ``rename-route`` calls.
    """

    entries: tuple[RouteSummary, ...]

    def to_dict(self) -> dict[str, object]:
        return {"entries": [r.to_dict() for r in self.entries]}

    def format(self) -> str:
        if not self.entries:
            return "no routes on device"
        return "\n".join(r.format() for r in self.entries)


async def _fetch_existing_route_ids(
    client: _client.IgpsportClient,
    timeout: float,
) -> set[int]:
    """Query the device for the set of currently-used route plan slot ids.

    Shared by ``upload-route`` (to auto-pick a free slot) and any other
    caller that needs to avoid clobbering an existing route.
    """
    request = route_plan_pb2.route_plan_data_msg()
    request.route_plan_operate_type = route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_LIST_GET
    request.route_list_get_msg.file_index_start = 0
    request.route_list_get_msg.file_index_end = 100
    response = await client.request(request, timeout=timeout)
    msg = response.message
    if not isinstance(msg, route_plan_pb2.route_plan_data_msg):
        raise CommandError(f"unexpected response message: {type(msg).__name__}")
    return {int(r.id) for r in msg.route_plan_info_msg}


def _next_free_route_id(existing: set[int]) -> int:
    """Smallest positive integer not in *existing*."""
    candidate = 1
    while candidate in existing:
        candidate += 1
    return candidate


async def _r_list_routes(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    _args: Sequence[str],
    timeout: float,
) -> RouteSummaryList:
    """Read the route plan list, surface ``(id, name, is_active)`` only.

    Wraps the same ``ROUTE_PLAN LIST_GET`` call as the ``routes``
    command but trims the result to identification fields. Useful as
    the lookup table for the other route commands (``upload-route``
    by id, the future ``rename-route`` / ``delete-route``).
    """
    request = route_plan_pb2.route_plan_data_msg()
    request.route_plan_operate_type = route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_LIST_GET
    request.route_list_get_msg.file_index_start = 0
    request.route_list_get_msg.file_index_end = 100
    response = await client.request(request, timeout=timeout)
    msg = response.message
    if not isinstance(msg, route_plan_pb2.route_plan_data_msg):
        raise CommandError(f"unexpected response message: {type(msg).__name__}")
    used = route_plan_pb2.enum_USED_STATUS
    entries = tuple(
        RouteSummary(
            id=int(r.id),
            name=str(r.name),
            is_active=r.status == used,
        )
        for r in msg.route_plan_info_msg
    )
    return RouteSummaryList(entries=entries)


@dataclasses.dataclass(slots=True, frozen=True)
class DelRouteResult:
    """Outcome of the ``del-route`` command.

    ``deleted`` is True iff a follow-up ``LIST_GET`` confirms the
    target id is gone. The BSC200 firmware protects the active route:
    a ``FILES_DEL`` targeting it acks with status=0 but the route
    stays — see PROTOCOL.md §7.4. ``not_found`` distinguishes
    "device never had this id" from "firmware refused to drop it".
    """

    file_id: int
    name: str
    deleted: bool
    was_active: bool
    not_found: bool
    device_status: int

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        if self.not_found:
            return f"No route with id={self.file_id} on device"
        if self.deleted:
            return f"Deleted route id={self.file_id} name={self.name!r}"
        if self.was_active:
            return (
                f"Refused: route id={self.file_id} name={self.name!r} is "
                "currently active — the BSC200 firmware protects the "
                "navigating route from deletion. End navigation on the "
                "device first, then retry."
            )
        return (
            f"Delete failed: route id={self.file_id} name={self.name!r} "
            f"still present (device status={self.device_status})"
        )


async def _r_del_route(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    args: Sequence[str],
    timeout: float,
) -> DelRouteResult:
    """Delete one route plan by id.

    Usage: ``del-route <id>`` — the id comes from ``routes`` /
    ``list-routes``. Wraps :func:`file_transfer.delete_route_plan_files`
    with a pre/post ``LIST_GET`` so the result can report honestly
    whether the device actually let go of the file.
    """
    if len(args) != 1:
        raise CommandError("del-route takes exactly one argument: <file_id>")
    try:
        file_id = int(args[0])
    except ValueError as exc:
        raise CommandError(f"del-route: file_id must be an integer, got {args[0]!r}") from exc

    list_req = route_plan_pb2.route_plan_data_msg()
    list_req.route_plan_operate_type = route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_LIST_GET
    list_req.route_list_get_msg.file_index_start = 0
    list_req.route_list_get_msg.file_index_end = 100
    pre = await client.request(list_req, timeout=timeout)
    if not isinstance(pre.message, route_plan_pb2.route_plan_data_msg):
        raise CommandError(f"unexpected response message: {type(pre.message).__name__}")

    used = route_plan_pb2.enum_USED_STATUS
    name = ""
    was_active = False
    found = False
    for entry in pre.message.route_plan_info_msg:
        if int(entry.id) == file_id:
            found = True
            name = str(entry.name)
            was_active = entry.status == used
            break
    if not found:
        return DelRouteResult(
            file_id=file_id,
            name="",
            deleted=False,
            was_active=False,
            not_found=True,
            device_status=0,
        )

    device_status = await file_transfer.delete_route_plan_files(
        client,
        [(file_id, name, "cnx")],
        timeout=timeout,
    )

    post = await client.request(list_req, timeout=timeout)
    still_present = False
    if isinstance(post.message, route_plan_pb2.route_plan_data_msg):
        for entry in post.message.route_plan_info_msg:
            if int(entry.id) == file_id:
                still_present = True
                break

    return DelRouteResult(
        file_id=file_id,
        name=name,
        deleted=not still_present,
        was_active=was_active,
        not_found=False,
        device_status=device_status,
    )


async def _r_user(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    _args: Sequence[str],
    timeout: float,
) -> UserConfig:
    request = user_config_pb2.user_config_msg()
    request.user_config_operate_type = user_config_pb2.enum_USER_CONFIG_OPERATE_TYPE_GET
    response = await client.request(request, timeout=timeout)
    msg = response.message
    if not isinstance(msg, user_config_pb2.user_config_msg):
        raise CommandError(f"unexpected response message: {type(msg).__name__}")
    u = msg.user_config_data_message
    return UserConfig(
        sex=u.sex,
        weight_g=u.weight,
        age=u.age,
        height_cm=u.height,
        wheel_dia_mm=u.wheel_dia,
        bike_weight_g=u.bike_weight,
        time_zone_s=u.time_zone,
        member_id=u.member_id,
    )


async def _r_list_activities(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    _args: Sequence[str],
    timeout: float,
) -> ActivityList:
    """List recorded activities on the device.

    Wraps :func:`file_transfer.list_activities`, which sends
    ``CYCLING_DATA LIST_GET`` on the **third UART RX** (``…-7e``) —
    the channel the iGPSPORT app uses (verified against btsnoop
    frame 35365 of ``snoop_start.log``).
    """
    entries = await file_transfer.list_activities(client, timeout=timeout)
    files = tuple(
        ActivityFile(
            timestamp=e.timestamp,
            file_size=e.file_size,
            user_id=e.user_id,
            device_id=e.device_id,
        )
        for e in entries
    )
    return ActivityList(files=files)


async def _r_sensors(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    _args: Sequence[str],
    timeout: float,
) -> SensorList:
    request = sensor_pb2.sensor_message()
    request.sensor_operate_type = sensor_pb2.enum_SENSOR_OPERATE_TYPE_GET
    response = await client.request(request, timeout=timeout)
    msg = response.message
    if not isinstance(msg, sensor_pb2.sensor_message):
        raise CommandError(f"unexpected response message: {type(msg).__name__}")
    sensors: list[dict[str, object]] = [
        {
            "type": s.sensor_type,
            "radio": s.sensor_radio_type,
            "status": s.sensor_status_type,
            "key": s.sensor_key,
            "name": s.sensor_ble_name,
            "battery": s.sensor_pwr,
            "wheel_size_mm": s.wheel_size,
            "crank_length_mm": s.crank_length,
            "rssi": s.sensor_rssi,
            "forbidden": s.sensor_forbidden,
        }
        for s in msg.sensor_data_msg
    ]
    return SensorList(sensors=tuple(sensors))


@dataclasses.dataclass(slots=True, frozen=True)
class FirmwareInfo:
    """Decoded ``FIRMWARE_OPERATE_TYPE_SEND_VERSION`` payload."""

    mcu_firmware_ver: int
    ble_firmware_ver: int
    ble_boot_firmware_ver: int

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        return (
            f"MCU firmware:        {self.mcu_firmware_ver}\n"
            f"BLE firmware:        {self.ble_firmware_ver}\n"
            f"BLE boot firmware:   {self.ble_boot_firmware_ver}"
        )


@dataclasses.dataclass(slots=True, frozen=True)
class WifiStatus:
    """Decoded WIFI status reply."""

    on: bool
    ssid: str
    signal_strength: int

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        return (
            f"WiFi:            {'on' if self.on else 'off'}\n"
            f"SSID:            {self.ssid or '(none)'}\n"
            f"Signal:          {self.signal_strength}"
        )


@dataclasses.dataclass(slots=True, frozen=True)
class RoutePlan:
    """One entry in the route_plan list."""

    id: int
    name: str
    file_type: int  # ROUTE_PLAN_FILE_TYPE (CNX=1, GPX=2, FIT=3, TCX=4, XML=5...)
    total_distance: int
    status: int

    @property
    def is_active(self) -> bool:
        """True for the route the device is currently navigating."""
        return self.status == route_plan_pb2.enum_USED_STATUS

    @property
    def kind(self) -> str:
        return {1: "CNX", 2: "GPX", 3: "FIT", 4: "TCX"}.get(self.file_type, str(self.file_type))

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        kind = {1: "CNX", 2: "GPX", 3: "FIT", 4: "TCX"}.get(self.file_type, str(self.file_type))
        return f"id={self.id} name={self.name!r} type={kind} distance={self.total_distance}m"


@dataclasses.dataclass(slots=True, frozen=True)
class RouteList:
    """List of routes (route_plan service)."""

    routes: tuple[RoutePlan, ...]

    def to_dict(self) -> dict[str, object]:
        return {"routes": [r.to_dict() for r in self.routes]}

    def format(self) -> str:
        if not self.routes:
            return "no routes on device"
        return "\n".join(r.format() for r in self.routes)


@dataclasses.dataclass(slots=True, frozen=True)
class RouteBookEntry:
    """One entry in the route_book list."""

    id: int
    name: str
    status: int

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        used = "used" if self.status == 1 else "unused"
        return f"id={self.id} name={self.name!r} {used}"


@dataclasses.dataclass(slots=True, frozen=True)
class RouteBookList:
    """List of electronic route books (route_book service)."""

    entries: tuple[RouteBookEntry, ...]

    def to_dict(self) -> dict[str, object]:
        return {"entries": [r.to_dict() for r in self.entries]}

    def format(self) -> str:
        if not self.entries:
            return "no route books on device"
        return "\n".join(r.format() for r in self.entries)


async def _r_firmware(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    _args: Sequence[str],
    timeout: float,
) -> FirmwareInfo:
    request = firmware_pb2.firmware_msg()
    request.firmware_operate_type = firmware_pb2.enum_FIRMWARE_OPERATE_TYPE_GET_VERSION
    response = await client.request(request, timeout=timeout)
    msg = response.message
    if not isinstance(msg, firmware_pb2.firmware_msg):
        raise CommandError(f"unexpected response message: {type(msg).__name__}")
    f = msg.firmware_data_msg
    return FirmwareInfo(
        mcu_firmware_ver=f.mcu_firmware_ver,
        ble_firmware_ver=f.ble_firmware_ver,
        ble_boot_firmware_ver=f.ble_boot_firmware_ver,
    )


async def _r_wifi(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    _args: Sequence[str],
    timeout: float,
) -> WifiStatus:
    request = wifi_pb2.wifi_msg()
    request.wifi_operate_type = wifi_pb2.enum_WIFI_OPERATE_TYPE_STATUS_GET
    response = await client.request(request, timeout=timeout)
    msg = response.message
    if not isinstance(msg, wifi_pb2.wifi_msg):
        raise CommandError(f"unexpected response message: {type(msg).__name__}")
    # The device replies with a wifi_data_message; the first entry holds
    # the connection state. status: 1=off, 2=on.
    if not msg.wifi_data_msg:
        return WifiStatus(on=False, ssid="", signal_strength=0)
    d = msg.wifi_data_msg[0]
    return WifiStatus(on=d.status == 2, ssid=d.ssid, signal_strength=d.signal_strength)


async def _r_routes(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    _args: Sequence[str],
    timeout: float,
) -> RouteList:
    request = route_plan_pb2.route_plan_data_msg()
    request.route_plan_operate_type = route_plan_pb2.enum_ROUTE_PLAN_OPERATE_TYPE_LIST_GET
    # Without an inclusive index range the BSC200 returns an empty
    # list — verified live. The Android app's ``RoutePlanViewModel``
    # always supplies start/end. ``file_list_support_num_max`` is 10
    # on current firmware so a 0..100 window is comfortably more
    # than any device's route list will ever hold.
    request.route_list_get_msg.file_index_start = 0
    request.route_list_get_msg.file_index_end = 100
    response = await client.request(request, timeout=timeout)
    msg = response.message
    if not isinstance(msg, route_plan_pb2.route_plan_data_msg):
        raise CommandError(f"unexpected response message: {type(msg).__name__}")
    routes = tuple(
        RoutePlan(
            id=r.id,
            name=r.name,
            file_type=r.file_type,
            total_distance=r.total_distance,
            status=r.status,
        )
        for r in msg.route_plan_info_msg
    )
    return RouteList(routes=routes)


@dataclasses.dataclass(slots=True, frozen=True)
class DownloadedFile:
    """The contents of a file pulled off the device.

    ``file_format`` is "fit" for raw FIT bytes (the historical default
    that keeps old callers working) or "gpx" when the FIT payload was
    converted to GPX before writing.
    """

    path: str
    size_bytes: int
    fit_magic: bool  # True iff the first 12 bytes match the FIT magic header.
    file_format: str = "fit"

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        if self.file_format == "gpx":
            tail = "GPX rendered from FIT"
        else:
            tail = "FIT header verified" if self.fit_magic else "(not a recognised FIT file)"
        return f"wrote {self.size_bytes} bytes -> {self.path}  {tail}"


@dataclasses.dataclass(slots=True, frozen=True)
class DownloadedActivityList:
    """Result of the ``download-all-activities`` command.

    *entries* is the per-file outcome for everything actually written.
    *skipped* is the set of paths that already existed before the run
    and were left alone — the command is idempotent so repeated
    invocations against the same output directory only fetch what's
    missing.
    """

    entries: tuple[DownloadedFile, ...]
    skipped: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "entries": [e.to_dict() for e in self.entries],
            "skipped": list(self.skipped),
        }

    def format(self) -> str:
        lines: list[str] = []
        for entry in self.entries:
            lines.append(entry.format())
        for path in self.skipped:
            lines.append(f"skipped (already present): {path}")
        if not lines:
            return "no recorded activities on device"
        return "\n".join(lines)


async def _r_set_rtc(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    args: Sequence[str],
    timeout: float,
) -> str:
    """Set the device's real-time clock.

    Without arguments uses the current UTC time. Optionally accepts a
    unix epoch seconds value as the first argument.
    """
    import time as _time

    epoch_s: int
    if args:
        try:
            epoch_s = int(args[0])
        except ValueError as exc:
            raise CommandError(f"invalid epoch seconds: {args[0]!r}") from exc
    else:
        epoch_s = int(_time.time())

    request = factory_pb2.factory_msg()
    request.factory_operate_type = factory_pb2.enum_FACTORY_OPERATE_TYPE_RTC_SET
    request.rtc_msg.time = epoch_s
    await client.request(
        request,
        operation=factory_pb2.enum_FACTORY_OPERATE_TYPE_RTC_SET,
        timeout=timeout,
    )
    return f"sent RTC = {epoch_s} (epoch seconds)"


async def _r_set_user(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    args: Sequence[str],
    timeout: float,
) -> str:
    """Set the user profile (weight, age, height, etc.)

    Arguments are ``key=value`` pairs. Recognised keys: ``sex`` (0 or 1),
    ``weight_kg`` (float), ``age`` (int), ``height_cm`` (int),
    ``wheel_dia_mm`` (int), ``bike_weight_kg`` (float),
    ``time_zone_h`` (float), ``member_id`` (string).
    """
    parsed: dict[str, str] = {}
    for arg in args:
        if "=" not in arg:
            raise CommandError(f"expected key=value, got {arg!r}")
        k, _, v = arg.partition("=")
        parsed[k] = v

    request = user_config_pb2.user_config_msg()
    request.user_config_operate_type = user_config_pb2.enum_USER_CONFIG_OPERATE_TYPE_SET
    u = request.user_config_data_message
    if "sex" in parsed:
        u.sex = int(parsed["sex"])
    if "weight_kg" in parsed:
        u.weight = int(float(parsed["weight_kg"]) * 10)
    if "age" in parsed:
        u.age = int(parsed["age"])
    if "height_cm" in parsed:
        u.height = int(parsed["height_cm"])
    if "wheel_dia_mm" in parsed:
        u.wheel_dia = int(parsed["wheel_dia_mm"])
    if "bike_weight_kg" in parsed:
        u.bike_weight = int(float(parsed["bike_weight_kg"]) * 10)
    if "time_zone_h" in parsed:
        u.time_zone = int(float(parsed["time_zone_h"]) * 3600)
    if "member_id" in parsed:
        u.member_id = parsed["member_id"]
    await client.request(
        request,
        operation=user_config_pb2.enum_USER_CONFIG_OPERATE_TYPE_SET,
        timeout=timeout,
    )
    return f"updated user profile ({len(parsed)} field{'s' if len(parsed) != 1 else ''})"


@dataclasses.dataclass(slots=True, frozen=True)
class DelActivityResult:
    """Outcome of the ``del-activity`` command.

    *deleted* is True iff a follow-up ``LIST_GET`` confirms the
    activity is gone. *not_found* distinguishes "the device never
    had this timestamp" from "firmware refused / silently kept it".
    """

    timestamp: int
    deleted: bool
    not_found: bool
    device_status: int

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        if self.not_found:
            return f"No activity with timestamp={self.timestamp} on device"
        if self.deleted:
            return f"Deleted activity timestamp={self.timestamp}"
        return (
            f"Delete failed: activity timestamp={self.timestamp} still "
            f"present (device status={self.device_status})"
        )


async def _r_del_activity(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    args: Sequence[str],
    timeout: float,
) -> DelActivityResult:
    """Delete one recorded activity from the device. **Destructive.**

    Pre-checks the list to distinguish "not on device" from
    "deletion refused", issues
    :func:`file_transfer.delete_activity` (gen-4 merged write on
    the fourth UART with op=FILE_DEL), then re-lists to confirm
    the activity is gone.
    """
    if not args:
        raise CommandError("del-activity takes <timestamp>")
    try:
        timestamp = int(args[0])
    except ValueError as exc:
        raise CommandError(f"invalid timestamp: {args[0]!r}") from exc

    pre = await file_transfer.list_activities(client, timeout=timeout)
    if not any(e.timestamp == timestamp for e in pre):
        return DelActivityResult(
            timestamp=timestamp,
            deleted=False,
            not_found=True,
            device_status=0,
        )
    status = await file_transfer.delete_activity(client, timestamp, timeout=timeout)
    post = await file_transfer.list_activities(client, timeout=timeout)
    still_present = any(e.timestamp == timestamp for e in post)
    return DelActivityResult(
        timestamp=timestamp,
        deleted=not still_present,
        not_found=False,
        device_status=status,
    )


@dataclasses.dataclass(slots=True, frozen=True)
class SimActivityResult:
    """Outcome of the ``sim-activity`` command.

    The device generates *count* synthetic FIT files of *size_bytes*
    each in its recorded-activity flash. *status* is the
    ``DeviceReturnStatus`` ordinal from the ACK frame (0 = Success).
    """

    count: int
    size_bytes: int
    status: int

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        result = "ok" if self.status == 0 else f"error (status={self.status})"
        return (
            f"sim-activity: requested {self.count} file"
            f"{'s' if self.count != 1 else ''} of {self.size_bytes} bytes — {result}"
        )


async def _r_sim_activity(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    args: Sequence[str],
    timeout: float,
) -> SimActivityResult:
    """Ask the device to generate fake FIT files in its activity flash.

    Wraps :func:`file_transfer.simulate_fit_files` (FACTORY service,
    ``SIM_FIT_SET`` op = 7). Accepts ``count=N`` and ``size=BYTES``
    inline-arg pairs; defaults are ``count=1, size=4096`` — one
    small file, fast to clean up with ``del-activity`` or
    ``del-all-activities``.
    """
    count = 1
    size_bytes = 4096
    for arg in args:
        if "=" not in arg:
            raise CommandError(f"expected key=value, got {arg!r}")
        k, _, v = arg.partition("=")
        if k == "count":
            try:
                count = int(v)
            except ValueError as exc:
                raise CommandError(f"sim-activity: count must be an integer, got {v!r}") from exc
        elif k == "size":
            try:
                size_bytes = int(v)
            except ValueError as exc:
                raise CommandError(f"sim-activity: size must be an integer, got {v!r}") from exc
        else:
            raise CommandError(f"sim-activity: unknown key {k!r} (expected count= or size=)")
    if count < 1:
        raise CommandError(f"sim-activity: count must be >= 1, got {count}")
    if size_bytes < 1:
        raise CommandError(f"sim-activity: size must be >= 1, got {size_bytes}")
    status = await file_transfer.simulate_fit_files(
        client,
        count=count,
        size_bytes=size_bytes,
        timeout=max(30.0, timeout),
    )
    return SimActivityResult(count=count, size_bytes=size_bytes, status=status)


async def _r_del_all_activities(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    _args: Sequence[str],
    timeout: float,
) -> str:
    """Delete every recorded activity on the device. **Very destructive.**

    Uses the third UART RX (``…-7e``) and the standard CYCLING_DATA
    head — same wire path as :func:`file_transfer.delete_activity`
    but with op = ``ALL_DEL`` (6). The BSC200 may silently keep an
    activity that's still considered active by the firmware; the
    caller should re-list to verify.
    """
    status = await file_transfer.delete_all_activities(client, timeout=timeout)
    return f"del-all-activities ack status={status}"


@dataclasses.dataclass(slots=True, frozen=True)
class UploadedRoute:
    """Result of an ``upload-route`` invocation."""

    source: str
    name: str
    points: int
    distance_m: int
    file_id: int
    status: int  # 0 = success per the device's ConfirmFrame.

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    def format(self) -> str:
        result = "ok" if self.status == 0 else f"error (status={self.status})"
        return (
            f"uploaded {self.source}\n"
            f"  name:      {self.name}\n"
            f"  points:    {self.points}\n"
            f"  distance:  {self.distance_m / 1000:.2f} km\n"
            f"  file id:   {self.file_id}\n"
            f"  status:    {result}"
        )


async def _r_upload_route(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    args: Sequence[str],
    timeout: float,
) -> UploadedRoute:
    """Upload a route file to the device.

    For ``.gpx`` / ``.geojson``, the file is parsed into a
    :class:`routes.RouteData` and converted to CNX locally (see
    :mod:`ligpsport.cnx`) before the upload — CNX is the only format
    the BSC200 firmware accepts. GPX waypoints carry through to the
    CNX ``<Points>`` list as POIs. For ``.cnx`` (iGPSPORT's
    proprietary format), the bytes are uploaded verbatim — callers
    who hold pre-converted CNX bytes (e.g. fetched from the iGPSPORT
    cloud's ``Routes/DownloadRoutes`` endpoint) push them directly.
    For ``.fit``, the parsed route is re-encoded as a Garmin Course
    FIT file; left in the tree for sanity-checking new devices, but
    BSC200 firmware rejects FIT same as raw GPX.

    An optional ``format=fit|gpx|cnx`` token overrides the default.

    Pass a bare ``start`` (or ``start=true``) to also send the
    ``ROUTE_PLAN FILE_USE`` step after a successful upload — this
    activates the route on the device and starts on-screen
    navigation, mirroring what the iGPSPORT Android app does when
    the user picks "send and use" on a route. Without it, the file
    lands on the device but the user must select it manually.

    See PROTOCOL.md §7 and §7.2.
    """
    import pathlib

    from . import routes as _routes

    if not args:
        raise CommandError("upload-route takes <path> [file_id] [format=gpx|fit|cnx] [start]")
    positional: list[str] = []
    forced_format: str | None = None
    start_navigation = False
    for arg in args:
        if arg.startswith("format="):
            forced_format = arg.split("=", 1)[1].lower()
            continue
        if arg in {"start", "--start", "start=true", "start=yes", "start=1"}:
            start_navigation = True
            continue
        if arg in {"start=false", "start=no", "start=0"}:
            start_navigation = False
            continue
        positional.append(arg)
    if not positional:
        raise CommandError("upload-route takes <path> [file_id] [format=...] [start]")
    path = positional[0]
    file_id: int | None = None
    if len(positional) >= 2:
        try:
            file_id = int(positional[1])
        except ValueError as exc:
            raise CommandError(f"invalid file_id: {positional[1]!r}") from exc
    if file_id is None:
        # No explicit id: pick the smallest unused slot so a fresh
        # upload doesn't clobber an existing route. Pass an explicit
        # file_id to overwrite a specific slot.
        existing = await _fetch_existing_route_ids(client, timeout=timeout)
        file_id = _next_free_route_id(existing)

    p = pathlib.Path(path)
    ext = p.suffix.lower().lstrip(".")
    if forced_format is not None and forced_format not in {"gpx", "fit", "cnx"}:
        raise CommandError(f"format= must be one of gpx|fit|cnx, got {forced_format!r}")
    if ext == "cnx" and forced_format is None:
        raw = p.read_bytes()
        # The protobuf still needs a start coordinate, name and
        # distance — without parsing the CNX content (proprietary)
        # we fall back to zero-coordinates and a name from the
        # filename. The BSC200 doesn't appear to validate these
        # metadata fields against the CNX payload.
        synthetic = _routes.RouteData(name=p.stem, points=())
        status = await file_transfer.upload_route_plan(
            client,
            synthetic,
            file_id=file_id,
            file_extension="cnx",
            timeout=max(30.0, timeout),
            raw_bytes=raw,
            raw_name=p.stem,
            start_navigation=start_navigation,
        )
        return UploadedRoute(
            source=path,
            name=p.stem,
            points=0,
            distance_m=0,
            file_id=file_id,
            status=status,
        )

    try:
        route = _routes.load_route(path)
    except _routes.RouteParseError as exc:
        raise CommandError(str(exc)) from exc
    if not route.points:
        raise CommandError(f"{path!r} contains no usable points")

    # Default for GPX / geoJSON / unknown extensions is CNX (the only
    # format the BSC200 firmware accepts). FIT stays opt-in via the
    # source-file extension or `format=fit`; raw GPX uploads stay
    # behind `format=gpx` for testing newer firmwares.
    wire_format = forced_format or (ext if ext == "fit" else "cnx")
    # CNX uploads can carry GPX-defined waypoints as on-device POIs.
    # geoJSON / FIT inputs have no native POI list the device acts on.
    waypoints = None
    if wire_format == "cnx" and ext in {"gpx", "geojson"}:
        from . import cnx as _cnx

        if ext == "gpx":
            waypoints = _cnx.parse_gpx_waypoints(p.read_bytes())

    status = await file_transfer.upload_route_plan(
        client,
        route,
        file_id=file_id,
        file_extension=wire_format,
        timeout=max(30.0, timeout),
        waypoints=waypoints,
        start_navigation=start_navigation,
    )
    return UploadedRoute(
        source=path,
        name=route.name,
        points=len(route.points),
        distance_m=route.distance_m,
        file_id=file_id,
        status=status,
    )


async def _r_download_activity(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    args: Sequence[str],
    timeout: float,
) -> DownloadedFile:
    """Download a recorded activity by timestamp.

    Accepts an optional ``type=fit|gpx`` / ``--type fit|gpx`` token
    (default ``fit``); when ``gpx``, the FIT bytes are parsed and
    re-rendered to GPX 1.1 via :func:`fit_activity.to_gpx_bytes`
    before writing. An out-path that names an existing directory
    (or ends in ``/``) is filled in with
    :func:`fit_activity.activity_filename_from_meta`.
    """
    import pathlib

    positional: list[str] = []
    file_type = "fit"
    skip_next = False
    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("type="):
            file_type = arg.split("=", 1)[1].lower()
            continue
        if arg == "--type":
            if i + 1 >= len(args):
                raise CommandError("--type requires a value (fit or gpx)")
            file_type = args[i + 1].lower()
            skip_next = True
            continue
        positional.append(arg)
    if file_type not in {"fit", "gpx"}:
        raise CommandError(f"type must be fit or gpx, got {file_type!r}")
    if len(positional) != 2:
        raise CommandError("download-activity takes <timestamp> <out-path> [type=fit|gpx]")
    try:
        timestamp = int(positional[0])
    except ValueError as exc:
        raise CommandError(f"invalid timestamp: {positional[0]!r}") from exc
    out_path = positional[1]

    activity = await file_transfer.download_activity(
        client,
        timestamp=timestamp,
        timeout=max(60.0, timeout * 6),
    )
    fit_data = activity.content
    fit_magic = len(fit_data) >= 12 and fit_data[8:12] == b".FIT"

    out = pathlib.Path(out_path)
    needs_derivation = out_path.endswith("/") or out.is_dir()
    if needs_derivation or file_type == "gpx":
        meta, records = fit_activity.read_activity_fit(fit_data)
        if needs_derivation:
            out = out / fit_activity.activity_filename_from_meta(meta, file_type)
        payload = fit_activity.to_gpx_bytes(meta, records) if file_type == "gpx" else fit_data
    else:
        payload = fit_data

    out.write_bytes(payload)
    return DownloadedFile(
        path=str(out),
        size_bytes=len(payload),
        fit_magic=fit_magic,
        file_format=file_type,
    )


async def _r_download_all_activities(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    args: Sequence[str],
    timeout: float,
) -> DownloadedActivityList:
    """Bulk-download every activity on the device to a directory.

    Lists activities, downloads each one, writes it to
    ``<out-dir>/<derived-name>``. Filenames come from
    :func:`fit_activity.activity_filename_from_meta` so they look the
    same as the iGPSPORT app's own exports (compact-ISO timestamp +
    device model + extension). Existing files are not overwritten —
    they end up in the result's ``skipped`` tuple instead — which
    makes the command safe to re-run after a partial fetch.
    Accepts an inline ``type=fit|gpx`` token (default ``fit``).
    """
    import pathlib

    positional: list[str] = []
    file_type = "fit"
    skip_next = False
    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("type="):
            file_type = arg.split("=", 1)[1].lower()
            continue
        if arg == "--type":
            if i + 1 >= len(args):
                raise CommandError("--type requires a value (fit or gpx)")
            file_type = args[i + 1].lower()
            skip_next = True
            continue
        positional.append(arg)
    if file_type not in {"fit", "gpx"}:
        raise CommandError(f"type must be fit or gpx, got {file_type!r}")
    if len(positional) != 1:
        raise CommandError("download-all-activities takes <out-dir> [type=fit|gpx]")
    out_dir = pathlib.Path(positional[0])
    out_dir.mkdir(parents=True, exist_ok=True)

    entries_listed = await file_transfer.list_activities(client, timeout=timeout)
    downloaded: list[DownloadedFile] = []
    skipped: list[str] = []
    for listing in entries_listed:
        activity = await file_transfer.download_activity(
            client,
            timestamp=listing.timestamp,
            timeout=max(60.0, timeout * 6),
        )
        fit_data = activity.content
        fit_magic = len(fit_data) >= 12 and fit_data[8:12] == b".FIT"
        meta, records = fit_activity.read_activity_fit(fit_data)
        # Derive the filename from the FIT's ``time_created`` — UTC,
        # same as the single-file ``download-activity`` path. Earlier
        # versions of this runner used the CYCLING_DATA listing's
        # ``timestamp`` field instead, which on BSC200 firmware
        # 2024-05-14 turned out to be local time (CEST) encoded as
        # Garmin-epoch seconds: the bulk-derived names landed 2 h
        # apart from the single-file names for the same activity. Two
        # downloads of the same activity should produce identical
        # filenames regardless of which CLI path you took — and they
        # should both be UTC. Verified live 2026-05-16.
        target = out_dir / fit_activity.activity_filename_from_meta(meta, file_type)
        if target.exists():
            skipped.append(str(target))
            continue
        payload = fit_activity.to_gpx_bytes(meta, records) if file_type == "gpx" else fit_data
        target.write_bytes(payload)
        downloaded.append(
            DownloadedFile(
                path=str(target),
                size_bytes=len(payload),
                fit_magic=fit_magic,
                file_format=file_type,
            )
        )
    return DownloadedActivityList(entries=tuple(downloaded), skipped=tuple(skipped))


async def _r_route_books(
    _spec: CommandSpec,
    client: _client.IgpsportClient,
    _args: Sequence[str],
    timeout: float,
) -> RouteBookList:
    request = route_book_pb2.route_book_data_msg()
    # The route_book service has a two-level operate-type: top-level
    # SERVICE_OPERATE_TYPE for GET/SET, plus a ROUTE_BOOK_SUB_OP_TYPE
    # that distinguishes list-num / list-get / use / rename.
    from .proto import common_pb2  # local to avoid widening the top imports.

    request.operate_type = common_pb2.enum_SERVICE_OPERATE_TYPE_GET
    request.sub_operate_type = route_book_pb2.enum_ROUTE_BOOK_GET_SUB_OP_TYPE_LIST_GET
    response = await client.request(
        request,
        operation=common_pb2.enum_SERVICE_OPERATE_TYPE_GET,
        timeout=timeout,
    )
    msg = response.message
    if not isinstance(msg, route_book_pb2.route_book_data_msg):
        raise CommandError(f"unexpected response message: {type(msg).__name__}")
    entries = tuple(
        RouteBookEntry(id=r.id, name=r.name, status=r.status) for r in msg.route_book_infor_msg
    )
    return RouteBookList(entries=entries)


COMMANDS: Final[Mapping[str, CommandSpec]] = {
    "version": CommandSpec(
        name="version",
        description="Read MCU / BLE / hardware versions and firmware compile time.",
        runner=_r_version,
    ),
    "status": CommandSpec(
        name="status",
        description="Read live cycling status (speed, heart rate, cadence, power, GPS).",
        runner=_r_status,
    ),
    "nav-status": CommandSpec(
        name="nav-status",
        description=(
            "Check whether the device is navigating a route. Returns "
            "is_navigating=True plus the active route's id/name. "
            "Reads ROUTE_PLAN LIST_GET and looks for status=USED — "
            "DEV_STATUS.navi_status is unpopulated on the BSC200."
        ),
        runner=_r_nav_status,
    ),
    "list-routes": CommandSpec(
        name="list-routes",
        description=(
            "Compact route listing — returns (id, name, is_active) "
            "for every route on the device. The fuller `routes` "
            "command surfaces file_type / total_distance / status."
        ),
        runner=_r_list_routes,
    ),
    "del-route": CommandSpec(
        name="del-route",
        description=(
            "Delete one route plan from the device by id (`del-route "
            "<id>`; ids come from `routes` / `list-routes`). The active "
            "route is protected by firmware and cannot be deleted "
            "while in use — see PROTOCOL.md §7.4. Destructive."
        ),
        runner=_r_del_route,
        destructive=True,
        danger="Permanently deletes a route plan from the device.",
    ),
    "user": CommandSpec(
        name="user",
        description="Read the user profile stored on the device.",
        runner=_r_user,
    ),
    "list-activities": CommandSpec(
        name="list-activities",
        description=(
            "List recorded activities on the device. Each entry "
            "carries a unix-ish timestamp (the FIT file's start time "
            "on the device's local clock), size in bytes, user_id "
            "and device_id. The timestamp doubles as the file id "
            "for download-activity / del-activity."
        ),
        runner=_r_list_activities,
    ),
    "sensors": CommandSpec(
        name="sensors",
        description="List paired sensors (HRM, cadence, power, radar, ...).",
        runner=_r_sensors,
    ),
    "firmware": CommandSpec(
        name="firmware",
        description="Read MCU/BLE firmware versions (via the FIRMWARE service).",
        runner=_r_firmware,
    ),
    "wifi": CommandSpec(
        name="wifi",
        description="Read WiFi connection status (BSC200 hardware may not support WiFi).",
        runner=_r_wifi,
    ),
    "routes": CommandSpec(
        name="routes",
        description="List route plans stored on the device.",
        runner=_r_routes,
    ),
    "route-books": CommandSpec(
        name="route-books",
        description="List electronic route books stored on the device.",
        runner=_r_route_books,
    ),
    "download-activity": CommandSpec(
        name="download-activity",
        description=(
            "Download a recorded activity file by timestamp. Writes "
            "FIT bytes to <out-path>, or GPX with `type=gpx`. When "
            "<out-path> names an existing directory (or ends in `/`) "
            "the filename is derived from the FIT header. Syntax: "
            "download-activity <timestamp> <out-path> [type=fit|gpx]. "
            "Timestamps come from list-activities."
        ),
        runner=_r_download_activity,
    ),
    "download-all-activities": CommandSpec(
        name="download-all-activities",
        description=(
            "List every recorded activity and download each one into "
            "<out-dir>, deriving the filename from the FIT header "
            "(<compact-iso>_<device>.<ext>). Existing files are kept "
            "and reported as skipped, so re-running only fetches the "
            "ones that aren't there yet. Syntax: download-all-"
            "activities <out-dir> [type=fit|gpx]."
        ),
        runner=_r_download_all_activities,
    ),
    "upload-route": CommandSpec(
        name="upload-route",
        description=(
            "Upload a GPX / geoJSON / CNX / FIT route file (GPX & geoJSON "
            "are converted to CNX locally). Without an explicit file_id, "
            "the smallest unused slot on the device is chosen so existing "
            "routes are not overwritten. Append 'start' to also activate "
            "the route (FILE_USE → starts navigation on the device). "
            "Syntax: upload-route <path> [file_id] [format=gpx|fit|cnx] "
            "[start]"
        ),
        runner=_r_upload_route,
    ),
    "set-rtc": CommandSpec(
        name="set-rtc",
        description="Set the device clock (no args: now; or pass epoch seconds).",
        runner=_r_set_rtc,
    ),
    "set-user": CommandSpec(
        name="set-user",
        description="Set user profile fields: set-user weight_kg=75 age=30 height_cm=180 ...",
        runner=_r_set_user,
    ),
    "del-activity": CommandSpec(
        name="del-activity",
        description=(
            "Delete one recorded activity from the device by "
            "timestamp (`del-activity <timestamp>`; timestamps come "
            "from list-activities). Pre-checks the list to "
            "distinguish 'not on device' from 'firmware kept it', "
            "then re-lists to confirm the delete actually took. "
            "Destructive."
        ),
        runner=_r_del_activity,
        destructive=True,
        danger="Permanently deletes a recorded activity from the device.",
    ),
    "del-all-activities": CommandSpec(
        name="del-all-activities",
        description="Delete every recorded activity. Cannot be undone.",
        runner=_r_del_all_activities,
        destructive=True,
        danger="Erases all recorded activity history. No recovery once issued.",
    ),
    "sim-activity": CommandSpec(
        name="sim-activity",
        description=(
            "Ask the device to generate fake FIT files in its "
            "recorded-activity flash. Useful for re-testing the "
            "delete / download paths without physically riding the "
            "bike. Syntax: sim-activity [count=N] [size=BYTES] "
            "(defaults count=1 size=4096). Destructive."
        ),
        runner=_r_sim_activity,
        destructive=True,
        danger=(
            "Writes synthetic FIT files into the BSC200's recorded-"
            "activity flash. Consumes flash space; clean up with "
            "del-all-activities or del-activity."
        ),
    ),
}


def get_command(name: str) -> CommandSpec:
    spec = COMMANDS.get(name)
    if spec is None:
        raise UnknownCommandError(
            f"unknown command {name!r}; available: {', '.join(sorted(COMMANDS))}"
        )
    return spec


def list_commands() -> list[CommandSpec]:
    return [COMMANDS[name] for name in sorted(COMMANDS)]


async def run_named(
    client: _client.IgpsportClient,
    name: str,
    args: Sequence[str] = (),
    *,
    timeout: float = 6.0,
    allow_destructive: bool = False,
) -> CommandResult:
    spec = get_command(name)
    return await spec.run(client, args, timeout=timeout, allow_destructive=allow_destructive)
