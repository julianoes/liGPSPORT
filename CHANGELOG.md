# Changelog

All notable changes to `ligpsport` are documented here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- ``ligpsport-gui``: a small Qt (PySide6) desktop app for Linux and
  macOS. Scans for bike computers on startup; pick one to download
  activities as ``.fit`` files and upload / delete routes (drag and
  drop works too). Install with ``pip install 'ligpsport[gui]'``;
  ``--simulator`` shows two fake devices for trying it without
  hardware. Saved activities are named after the local start time
  from the listing (``20261020T081500.fit``, as in the Android app)
  so the app can tell which ones are already downloaded without
  fetching them.
- ``ligpsport.session.DeviceSession``: a long-lived, self-healing
  connection to one device for interactive front-ends. Retries an
  unanswered ``LIST_GET`` (5 s, 15 s, 25 s): the first one after a
  connect is routinely never answered by a BSC200.
- ``ActivityFile.started_at`` / ``default_filename()``,
  ``RoutePlan.is_active`` / ``kind``, ``BleakTransport.mtu``.
- ``simulator.SimulatedTransport``: a transport with a simulator
  behind it, usable anywhere a BLE transport is.

## [1.5.2] — 2026-08-13

Toolchain release: the strict mypy gate is green for the first time
and now runs in CI. No runtime, wire-protocol or CLI changes.

### Fixed
- `mypy --strict ligpsport/` passes. `nix run .#gen-proto` now also
  emits `*_pb2.pyi` stubs (committed alongside the runtime modules),
  which removes the ~200 `Module has no attribute` errors the
  builder-generated protobuf modules produced, and the 28 genuine
  findings underneath were fixed at the source: protobuf enum fields
  are declared with their generated enum types, `envelope` and
  `commands` state their duck-typed contracts as Protocols,
  `_send_file_use` takes a `Queue[Response]`, and the dead
  `import-not-found` ignores for bleak / dbus_fast are gone.
  This closes the "mypy gating is deferred" known issue carried
  since 0.1.0.

### Added
- `.github/workflows/ci.yml` — runs `nix build .#default` and
  `nix flake check` on every push to `main`, every `v*` tag and
  every pull request. The repo documented this gate from the start
  but had no workflow; PR #1 merged with nothing having built it.

### Documentation
- AGENTS.md §5 documents that the `.pyi` stubs are generated **and**
  committed, and that protobuf enum wrappers are not callable at
  runtime — use the generated `enum_*` constants, not bare ints.

## [1.5.1] — 2026-08-13

Patch release: the BlueZ backend printed an asyncio warning after
otherwise successful transfers. Wire protocol, CLI surface and
library API are unchanged.

### Fixed
- `BluezTransport` no longer surfaces
  "Future exception was never retrieved" after a completed upload.
  dbus-fast's compiled reader sets an `EOFError` on an internal
  future when the DBus socket closes during teardown; with no
  awaiter left, asyncio reported it at GC time — after the transfer
  had already succeeded. The loop's exception handler is now wrapped
  by a filter that drops exactly that case (an `EOFError` raised
  from a `dbus_fast` frame) and delegates everything else.
  Reported and fixed by Julian Oes (#1).
- The filter installs idempotently, so a reconnect or a second
  transport on the same event loop reuses it instead of stacking
  wrappers — the wrapper is never uninstalled (the warning can fire
  after `close()` returns, during `asyncio.run` cleanup), so each
  extra layer would otherwise sit in the call path of every
  unrelated loop error for the loop's remaining lifetime.

## [1.5.0] — 2026-05-16

The library shipped two parallel CLI vocabularies for recorded
activities — ``rides``/``get-ride``/``delete-ride``/
``delete-all-rides`` (v1.0–v1.3) and ``list-activities``/
``download-activity``/``del-activity`` (v1.4). Both hit the exact
same CYCLING_DATA wire ops, so carrying both forever would be pure
maintenance burden. v1.5 keeps only the activity-named spellings
that match the iGPSPORT app's own vocabulary
(``HistoryActivity`` / ``readActivityFitFile`` / ...).

### Removed (breaking)
- CLI: ``rides``, ``get-ride``, ``delete-ride`` and
  ``delete-all-rides`` are gone. Use ``list-activities``,
  ``download-activity``, ``del-activity`` and the new
  ``del-all-activities`` instead. Wire protocol unchanged — the
  renamed commands hit the exact same CYCLING_DATA ops they
  always did.
- ``ligpsport.commands.RideFile`` / ``RideList`` aliases removed;
  use :class:`commands.ActivityFile` / :class:`commands.ActivityList`.
- ``ligpsport.simulator.SimulatedRideFile`` renamed to
  ``SimulatedActivityFile``; ``SimulatorState.ride_files`` renamed
  to ``activity_files``.
- ``ligpsport.file_transfer.download_cycling_data`` thin wrapper
  removed; call :func:`file_transfer.download_activity` directly
  (returns an :class:`ActivityDownload`; use ``.content`` for the
  FIT bytes).

### Added
- ``del-all-activities`` CLI command — same wire op as the old
  ``delete-all-rides`` (CYCLING_DATA ``ALL_DEL`` op=6); the
  rename brings it in line with the rest of the activity
  vocabulary.

### Fixed
- ``download-all-activities`` now derives each output filename from
  the FIT ``file_id.time_created`` UTC stamp, matching the
  single-file ``download-activity`` path. Earlier it used the
  CYCLING_DATA listing's ``timestamp`` field, which on BSC200
  firmware 2024-05-14 is encoded in local time (CEST) and produced
  filenames 2h apart from the single-file path for the same activity.

### Changed
- ``tests/test_bsc200_live.py`` documents that destructive ops
  (``sim-activity`` / ``del-activity`` / ``del-all-activities``)
  are deliberately excluded from the live smoke suite so the suite
  is safe to re-run repeatedly without manual prep. Destructive
  paths remain covered against the in-tree simulator in
  ``tests/test_activities.py`` and are live-verified once per
  release as a manual checklist item.

### Documentation
- README's "Terminology: rides vs. activities" section is gone —
  with the duplicate vocabulary removed, the note no longer has
  anything to disambiguate. ``docs/PROTOCOL.md`` §7.5 drops its
  "older ``rides`` / ``get-ride`` / ``delete-ride`` names
  preserved as aliases" parenthetical.

## [1.4.0] — 2026-05-16

Activity downloads gain format conversion, bulk pull, and an
automatic filename derivation; the bleak transport stops printing
the per-write ``Using default MTU value`` warning; the v1.3.0
"active-file protection" claim in PROTOCOL.md §7.5 is retracted
after a live retest could not reproduce it.

### Added
- ``sim-activity count=N size=BYTES`` command + library
  :func:`file_transfer.simulate_fit_files`: FACTORY ``SIM_FIT_SET``
  (op 7) per ``reference/factory.proto:92-96`` and
  ``IGPDeviceManager.simulateFitFile``. Marked destructive,
  gated behind ``--allow-destructive-commands``. Simulator
  honours the op and synthesises listing entries; **on a
  BSC200 the firmware acks ``status=0`` but does not actually
  create the files** — wire path verified, firmware behaviour
  documented in PROTOCOL.md §6.9.
- ``download-activity ... type=gpx`` (or ``--type gpx``): the
  downloaded FIT is parsed with ``fitparse`` and re-emitted as
  GPX 1.1 with one ``<trkpt>`` per FIT ``record`` carrying
  lat/lon, altitude, and timestamp. Default remains ``type=fit``
  (raw FIT bytes).
- ``download-all-activities <out-dir> [type=fit|gpx]``: bulk pull
  of every activity on the device. Skips entries whose target
  file already exists (idempotent re-runs). Result dataclass
  :class:`commands.DownloadedActivityList` enumerates both the
  downloaded and skipped paths.
- ``ligpsport.fit_activity``: stdlib + ``fitparse`` module with
  an Activity-FIT reader, a GPX 1.1 writer, and a shared
  ``activity_filename(timestamp, device_model, extension)``
  helper. Filenames follow ``<YYYYMMDDTHHMMSSZ>_<model>.<ext>``
  (UTC) — derived from ``file_id.time_created`` for single
  downloads, from the listing timestamp for bulk.
- ``fitparse`` promoted from a test-only input to a runtime
  dependency in ``pyproject.toml`` and ``flake.nix``.

### Fixed
- ``BleakTransport.open`` now calls ``_acquire_mtu`` on the
  bleak backend rather than the public wrapper (bleak 2.x moved
  the method to ``BleakClient._backend``) and sets
  ``_mtu_size`` directly when acquisition fails. The per-write
  ``UserWarning: Using default MTU value`` from
  ``bleak/backends/bluezdbus/client.py`` is now silenced
  legitimately — verified against a live BSC200 with
  ``command --name bike version``.
- ``_acquire_mtu`` failures no longer get swallowed by a bare
  ``contextlib.suppress(Exception)``; they log at WARNING with
  the exception type and message.

### Documentation
- ``docs/PROTOCOL.md`` §7.5 — **retraction**: the v1.3.0 "active-
  file protection (FILE_DEL only)" claim was wrong. Live retest
  2026-05-16 against firmware 2024-05-14 with a freshly
  recorded ride showed ``FILE_DEL`` acks ``status=0`` and the
  next ``LIST_GET`` returns empty. The earlier "silently kept"
  observation traces back to a probe-heavy debugging session
  that wedged the device's parser via a speculative
  ``file_tag = 0xAA`` write (cf. AGENTS.md §2 guardrail).
- ``docs/PROTOCOL.md`` §6.9 — flesh out FACTORY ``SIM_FIT_SET``
  with the ``sim_fit_message`` payload shape, smali provenance,
  and the BSC200 no-op finding.
- ``docs/PROTOCOL.md`` §7.5 — note the new library / CLI
  surface (``simulate_fit_files``, ``download-activity
  type=gpx``, ``download-all-activities``) and that none of
  them add new wire ops.

### Notes
- Live-verified against ``F7:11:62:07:21:F5`` on firmware
  2024-05-14: ``list-activities`` → ``download-activity ts
  /tmp/x.fit`` (``fit_magic=True``) → ``download-activity ts
  /tmp/x.gpx type=gpx`` (12 ``<trkpt>`` with real GPS) →
  ``download-activity ts /tmp/ type=fit`` (derived filename
  ``20260515T215627Z_BSC200.fit``) → ``download-all-activities
  /tmp/x/ type=gpx`` (entry written, re-run reports skipped) →
  ``del-activity ts`` (``deleted=true``) → ``list-activities``
  empty.

## [1.3.0] — 2026-05-15

Two new commands aimed at managing routes from the CLI:
``list-routes`` and ``del-route``.

The investigation in ``docs/PROTOCOL.md`` §7.4 confirmed that the
BSC200 has no BLE primitive to stop navigation: the protocol
exposes no ``STOP_NAV`` / ``FILE_UNUSE`` opcode, the iGPSPORT
Android app does not offer such a button, and the firmware
silently no-ops every ``FILE_DEL`` / ``FILES_DEL`` aimed at the
route currently in use (status=0 ack, route stays). Inactive
routes delete normally — the protection is specific to the
active id. A speculative ``stop-nav`` command was prototyped and
dropped because it could never actually stop navigation; the
investigation and the table of probed candidates are preserved
in PROTOCOL.md §7.4 for posterity.

### Added
- ``list-routes`` command (:func:`commands._r_list_routes`,
  :class:`commands.RouteSummary` /
  :class:`commands.RouteSummaryList`): compact ``id name [*]``
  listing built on ``ROUTE_PLAN LIST_GET``. The ``*`` marker
  tags the currently-navigated route.
- ``del-route <id>`` command (:func:`commands._r_del_route`,
  :class:`commands.DelRouteResult`): deletes one route plan from
  the device by id (ids come from ``routes`` / ``list-routes``).
  The result distinguishes ``deleted``, ``not_found``, and
  ``was_active`` (the firmware-protected case). Marked
  destructive — requires ``--allow-destructive-commands``.
- :func:`file_transfer.delete_route_plan_files`: library helper
  that emits ``ROUTE_PLAN FILES_DEL`` with the wire format the
  firmware accepts — single merged write on the gen-4 fourth
  channel carrying **both** ``line_id`` (``"<id>.cnx"``) and full
  ``route_plan_info_msg`` records (id, file_type=CNX, name,
  total_distance). Sending only one of the two field sets is
  rejected. Available to scripts that need to delete multiple
  routes in one shot.
- Simulator: ``_handle_route_files_del`` mirrors the BSC200's
  active-route protection so ``del-route`` can be tested
  hermetically against both the success path and the
  firmware-refusal path.
- Four regression tests in ``tests/test_upload_route_plan.py``:
  inactive-route delete works, active-route delete is refused,
  unknown id reports ``not_found``, and the destructive gate is
  enforced.

### Documentation
- ``docs/PROTOCOL.md`` §7.4 — "Deleting routes — and why
  navigation cannot be stopped over BLE": probe table, wire
  format, and ``del-route`` binding.

### Notes
- Live-verified against ``F7:11:62:07:21:F5`` on firmware
  2024-05-14: ``list-routes`` shows id/name with active marker;
  ``del-route`` succeeds on inactive routes and reports
  "currently active" on the navigating one.

## [1.2.1] — 2026-05-15

The v1.2.0 ``nav-status`` command read ``DEV_STATUS.navi_status``,
which is documented in ``dev_status.proto`` but **never populated by
the BSC200 firmware** — the field stays 0 even while the device is
actively navigating. Live-verified against firmware 2024-05-14:
``Navigation: OFF (raw=0)`` while ``Hopfenhhe`` was on the
navigation screen.

The actual signal lives in ``ROUTE_PLAN LIST_GET``: each
``route_plan_info_msg`` in the reply carries a
``ROUTE_PLAN_FILE_STATUS`` byte and the route currently being
navigated is tagged ``enum_USED_STATUS = 1``. The iGPSPORT Android
app uses exactly this mechanism — see
``RoutePlanViewModel.requestUsingRouteID`` in the smali. This
release switches ``nav-status`` (and the related ``routes`` command)
to the same path.

### Fixed
- ``nav-status``: now reads ``ROUTE_PLAN LIST_GET`` and looks for
  ``status == enum_USED_STATUS`` instead of ``DEV_STATUS.navi_status``.
  Live-verified: ``Navigation: ON (route_id=… name=…)`` while
  navigating, ``Navigation: OFF`` otherwise.
- ``routes``: the LIST_GET request now includes the
  ``route_list_get_msg.file_index_start`` /
  ``file_index_end`` range. Without it, the BSC200 silently returns
  an empty list — which is why the v1.2.0 ``routes`` command
  reported zero routes even when the device had several.
- :class:`commands.NavStatus` extended with ``active_route_id`` and
  ``active_route_name`` so callers can identify *which* route is
  active, not just whether navigation is on.

### Changed
- :class:`commands.NavStatus` field rename: ``raw`` removed
  (it was always 0 on real hardware), replaced with
  ``active_route_id: int | None`` and ``active_route_name: str``.
  Pre-1.2.1 callers that read ``.raw`` will need to update.

### Added
- Simulator: ``_handle_route_plan`` returns
  ``SimulatorState.uploaded_routes`` as a ``route_plan_data_msg``
  with each entry tagged USED / UNUSED per ``active_route_id``.
  This makes the upload + start-navigation + verify-active loop
  testable hermetically without a real device.

### Notes
- Live-verified end-to-end against ``F7:11:62:07:21:F5``:
  ``upload-route tmp/test_route.geojson 12345678 start`` →
  ``nav-status`` reports
  ``Navigation: ON (route_id=12345678 name='ligpsport test loop')``.

## [1.2.0] — 2026-05-15

The v1.1.0 ``start_navigation`` flag didn't actually start
navigation on the device — three independent bugs in the wire format
made the BSC200 firmware silently drop the FILE_USE. This release
fixes them and adds a focused ``nav-status`` command so end-to-end
tests can verify the device actually switched into navigation mode.

### Fixed
- **FILE_USE wire format**: send a single merged write of
  (20-byte head ‖ protobuf body) to the ``fourth`` characteristic,
  matching the gen-4 path in ``setRoutePlanFile`` smali +
  ``send$lambda-135``. The previous two-write split (body on
  ``fourth``, header on ``control``) was the gen-3 path and the
  BSC200 firmware silently ignored it. Verified byte-for-byte
  against frames 35405 / 35545 of a captured "Start navigation"
  tap in the iGPSPORT Android app.
- **FILE_USE protobuf**: include the required ``name`` and
  ``total_distance`` fields in the nested ``route_plan_info_msg``.
  The BSC200 firmware validates ``name`` (the live capture shows
  ``str(file_id)`` for unnamed routes) and drops requests that
  omit it. ``upload_route_plan`` plumbs the truncated upload
  filename and ``route.distance_m`` through to the FILE_USE.
- **Default device generation** for ``upload_route_plan`` /
  ``upload_general_file`` raised from 3 to 4 — the BSC200 reports
  ``getGeneration() == 4`` and takes the merged-write path. The
  earlier default of 3 routed FILE_USE through the legacy
  two-channel split.
- **DeviceReturnStatus wire-value mapping**: the WiFi block lives at
  16-23 and the Navigation block at 65-66 (not 7-16 as earlier
  releases mapped them). Sourced from
  ``DeviceReturnStatus.smali``'s constructor calls. Affects every
  ``RouteUploadError`` / ``NavigationStartError`` message — most
  visibly, a FILE_USE for a not-yet-uploaded route returns
  wire byte 0x42 (66), which now decodes to
  ``NavigationRouteDoesNotExist`` rather than the wrong
  ``unknown(66)``.

### Added
- ``nav-status`` CLI command + :class:`commands.NavStatus`
  dataclass: read just the ``navi_status`` byte from
  ``DEV_STATUS``. Returns ``is_navigating=True`` when the device
  is on the navigation screen (after a successful
  ``upload-route ... start`` or any other FILE_USE). Targeted at
  automation and e2e tests that don't need the full status block.
- Simulator: ``_looks_like_route_plan_file_use_merged`` +
  ``_handle_route_use`` updates that recognise the new
  single-write FILE_USE pattern, flip
  ``SimulatorState.navi_status`` to ``DEV_NAVI_STATUS_ON`` on a
  successful activation, and ACK with status=66
  (``NavigationRouteDoesNotExist``) when the requested file_id
  hasn't been uploaded yet. Faithfully mirrors the firmware
  behaviour observed in ``snoop_start.log``.
- Simulator: ``_handle_dev_status`` accepts both the framing-level
  ``OP_GET = 2`` and the proto-level
  ``enum_DEV_STATUS_OPERATE_TYPE_GET = 1`` (and falls back to
  ``msg.op_type``) so the existing ``status`` runner finally works
  against the simulator. Tests for the new ``nav-status`` command
  depend on this.
- Tests: ``test_upload_with_start_navigation_gen4_e2e`` exercises
  the full upload → FILE_USE → ``DEV_STATUS GET`` round-trip
  through the simulator and asserts ``state.navi_status == 1``
  plus ``nav-status`` reporting ``is_navigating=True``;
  ``test_file_use_not_exist_returns_status_66`` covers the
  pre-upload speculative FILE_USE path.
- ``docs/PROTOCOL.md`` §7.2 rewritten with the gen-4 wire-format
  table, the full ``route_plan_info_msg`` field list, the
  corrected status-byte semantics, and a new §7.3 documenting the
  read-side ``DEV_STATUS.navi_status`` path.

### Notes
- There is still no BLE-level "stop navigation" operation; the
  ``DEV_NAVI_STATUS`` enum is read-only and the proto has no
  ``FILE_UNUSE``. For unattended e2e against the real device,
  expect to either rely on a long enough navigation timeout or
  end navigation manually on the bike computer. Simulator tests
  can reset ``state.navi_status`` directly.

## [1.1.0] — 2026-05-15

Upload-and-go: the library can now end the route upload by also
flipping the device into navigation mode, mirroring what the
iGPSPORT Android app does when the user picks "send and use" on a
route. Reverse-engineered from
``IGPDeviceManager.setRoutePlanFile`` and
``RoadBookSearchActivity.useRoutePlan`` / ``sendFileToDevice``.

### Added
- ``upload_route_plan(..., start_navigation=True)`` and
  ``upload_general_file(..., start_navigation=True, generation=...)``
  — after a successful upload, issue a ``ROUTE_PLAN FILE_USE``
  (operate_type=5) carrying the new ``file_id`` and the file
  extension. The device activates the route and switches its UI
  into navigation mode; the iGPSPORT app waits ~5 s here before
  dismissing its progress dialog. Default ``False`` — opt-in, same
  as the app's ``sendOnly = true`` path that skips FILE_USE.
- ``ligpsport.file_transfer.NavigationStartError`` — raised when
  the FILE_USE step is requested but the device returns a non-zero
  ``DeviceReturnStatus``. The upload itself is still considered
  successful (the file landed); the route just isn't active. The
  exception surfaces ``status``, ``status_name`` (e.g.
  ``"NavigationRouteDoesNotExist"``) and ``file_id``.
- ``ligpsport.file_transfer._send_file_use`` — internal helper
  factored out of the existing chunked-path FILE_USE block. The
  same routine now drives the FILE_OPERATION (CNX) path. Carries
  the gen-aware channel choice (``"fourth"`` for gen ≥ 3,
  ``"data"`` otherwise) and the two-channel split (body on the
  data-bearing UART, 20-byte header on control) the smali emits.
- ``upload-route ... start`` CLI flag: appending the bare token
  ``start`` (or ``start=true`` / ``--start``) to an ``upload-route``
  invocation triggers the FILE_USE step. Without it the file
  uploads but the user must pick the route on-device.

  ```sh
  ligpsport command --name bike upload-route trip.gpx 1 start
  ligpsport command --name bike upload-route trip.cnx 7 start
  ```
- ``docs/PROTOCOL.md`` §7.2 — wire format for ``ROUTE_PLAN
  FILE_USE``, the channel split per device generation, and notes
  on the absence of a "stop navigation" inverse op.
- Simulator: ``Simulator._absorb_general_upload_chunk`` reassembles
  multi-write FILE_OPERATION ADD streams on the ``fourth`` channel
  by reading the head + 4-byte pb-size prefix + protobuf
  ``file_size`` field, then records the upload in
  ``state.uploaded_routes`` and acks on the FILE_OPERATION service.
  This makes the new ``test_file_operation_upload_starts_navigation``
  hermetic end-to-end test possible.

### Changed
- ``upload_route_plan``'s chunked path now respects
  ``start_navigation`` in addition to the existing ``send_file_use``
  parameter (both trigger FILE_USE; ``start_navigation`` further
  promotes a refusal into ``NavigationStartError``). Existing
  callers that relied on ``send_file_use=True`` keep working
  unchanged.

## [1.0.0] — 2026-05-15

Route upload lands on the BSC200. The library now performs the full
GPX/geoJSON → CNX conversion locally and ships the result via the
FILE_OPERATION ADD path the iGPSPORT Android app uses internally — no
iGPSPORT cloud round-trip required.

### Added
- ``ligpsport.cnx`` — stdlib-only GPX/geoJSON → CNX encoder for
  iGPSPORT's proprietary route format. The ``<Tracks>`` second-
  difference delta encoding is derived from LudvvigB/GPXtoCNXConverter
  (Apache 2.0; attribution in ``NOTICE`` / ``LICENSES/``); the XML
  wrapper matches a btsnoop-captured cloud upload byte-for-byte
  (fixture: ``tests/fixtures/cnx_cloud_capture.cnx``). GPX waypoints
  carry through to the CNX ``<Points>`` list as on-device POIs.
- ``ligpsport.file_transfer.upload_general_file`` — implements the
  FILE_OPERATION ADD wire protocol (service 21, ``file_tag = 0xaa``,
  20-byte head + 4-byte BE size + ``general_file_operation``
  protobuf + raw file bytes), documented in ``docs/PROTOCOL.md``
  §7.1.2. This is the upload path the Android app uses for CNX
  route uploads.
- ``ligpsport.file_transfer.upload_route_plan`` — high-level route
  upload entry point. Accepts a :class:`routes.RouteData` plus a
  target file format; converts GPX/geoJSON to CNX automatically
  before dispatching to ``upload_general_file``. Also implements the
  legacy ROUTE_PLAN / FILE_SEND chunked path (per
  ``IGPDeviceManager.sendRoutePlanFile`` in the smali) for callers
  who want to test newer iGPSPORT firmwares — with the device's
  ``DeviceReturnStatus`` decoded into human-readable error strings
  on :class:`RouteUploadError`, ``FILE_USE`` commit, generation-
  aware data-channel selection, and an end-to-end hermetic test in
  ``tests/test_upload_route_plan.py``.
- ``upload-route`` CLI command:
  ``ligpsport command --name bike upload-route <path> [file_id]
  [format=gpx|fit|cnx]``. Defaults to converting GPX/geoJSON inputs
  to CNX locally (the BSC200's only accepted format). Accepts
  ``.cnx`` bytes verbatim for callers who already hold a
  cloud-converted file.
- ``ligpsport.routes`` — stdlib-only GPX and geoJSON parsing plus a
  canonical GPX serialiser. Handles tracks, routes, and waypoints
  from GPX; bare LineString, MultiLineString, Feature, and
  FeatureCollection from geoJSON.
- ``ligpsport.fit_course`` — minimal Garmin FIT Course encoder
  (stdlib only). Emits a protocol-2.0 FIT file with
  ``file_id.type=Course``, one lap, N records (position_lat/long in
  semicircles, distance in cm, altitude in 0.2 m units). Verified
  by round-tripping through ``fitparse``. Note: the BSC200
  rejects FIT with the same ``DataError`` it returns for GPX, so
  FIT is only useful against iGPSPORT models with looser firmware
  validation — the path stays in the tree for sanity-checking new
  devices.
- ``ligpsport.bluez.BluezTransport`` — second BLE backend that
  speaks to BlueZ via DBus (``dbus-fast``). Bypasses bleak's
  abstraction and uses ``AcquireWrite`` / ``AcquireNotify`` to
  obtain Unix file descriptors plus the *negotiated* ATT MTU. On
  BSC200 this raises the per-write payload from 23 bytes (bleak's
  BlueZ default) to 247. Select via the CLI's ``--backend bluez``
  flag, or instantiate directly. Linux-only.
- Multi-channel ``Transport`` API: ``send(frame, *, channel=...)``
  accepts a three-valued literal (``"control"`` / ``"data"`` /
  ``"fourth"``). Both BLE backends and ``LoopbackTransport`` route
  writes to the right Nordic-UART characteristic. Default
  ``"control"`` preserves the previous behaviour for every read
  command.
- ``IgpsportClient.open_subscription`` / ``close_subscription`` —
  eager subscription registration to close the race between
  subscriber setup and the peer's first frame. The previous
  ``subscribe()`` async generator is now a thin wrapper.
- ``docs/CAPTURE.md`` — Android HCI snoop log and Linux btmon
  recipes for capturing BLE traffic, plus the Wireshark filters
  used to extract the frame vectors in ``docs/PROTOCOL.md``, and
  the MAC-anonymisation procedure for the published capture.
- ``docs/btsnoop_hci.log`` — the route-upload capture used to
  reverse-engineer FILE_OPERATION ADD, filtered to the BSC200
  connection handle and MAC-anonymised.
- ``docs/PROTOCOL.md`` §12-§14 — AGPS / ephemeris pre-seeding,
  position-prior injection via FACTORY, and the locale gotcha for
  CNX coordinate emitters.

### Changed
- **Breaking**: ``Transport.send`` now requires keyword-only
  ``channel``; any third-party transport implementation must adopt
  the new signature.
- ``LoopbackTransport`` queue items are now ``(channel, bytes)``
  tuples. The new ``receive_with_channel`` method exposes the tag
  to multi-channel handlers (the simulator's route-upload paths);
  ``receive`` still returns only bytes to match the BLE transports.

### Known issues
- mypy ``--strict`` reports ~160 false-positive ``attr-defined``
  errors against the protobuf-generated modules. ``ruff`` and the
  test suite pass cleanly; mypy gating is deferred until the proto
  modules ship hand-written ``.pyi`` stubs.

## [0.1.0] — 2026-05-12

Initial reverse-engineering pass against the iGPSPORT BSC200.

### Added
- Repository skeleton: Nix flake, pyproject, lint/format/type/test gate.
- Protobuf compilation pipeline (`nix run .#gen-proto`) producing
  `ligpsport/proto/*_pb2.py` from `reference/*.proto`.
- 20-byte header + CRC8 framing codec.
- `LoopbackTransport` and in-process simulator for hermetic tests.
- Async `BleakTransport` and BLE discovery.
- BLE pairing flow with persistent credential storage.
- Read commands: device info, version, status, config, sensors,
  rides list, routes list, capabilities.
- File transfer: rides download, route/map/theme/firmware upload.
- Write commands and destructive-op gating.
- Real-time data stream (`DEV_STATUS`, `REAL_TIME_TRACE`, `INS`).
- CLI: `discover`, `pair`, `creds`, `command`.
