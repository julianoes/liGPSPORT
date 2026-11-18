# iGPSPORT BLE protocol

This document is the **source of truth** for the reverse-engineered
iGPSPORT BLE protocol. Every value below was observed from one of:

* the iGPSPORT Android APK (`iGPSPORT_7.45.03_APKPure.apk`), with
  the protobuf schemas extracted to `reference/*.proto` and the
  decompiled Java/Kotlin under `tmp/jadx-out/sources/`,
* live BLE captures against a real **iGPSPORT BSC200** (cited as
  *BSC200 capture* in section bodies).

Sections that say **TBD** are work-in-progress — they describe what
we expect to find when the next investigation hits, and the field
or class name we'll transcribe from.

## 1. Transport — BLE GATT

The device exposes up to four parallel **Nordic-UART-style** GATT
services. The first one is the primary control channel that carries
the framed protobuf protocol described below; the other three are
secondary channels used for parallel firmware / file streams when
the device's `DeviceInfo.sendFileMtuSize` advertises them.

| Channel  | App class           | Service UUID                                  | TX (notify, device → app)                    | RX (write, app → device)                     |
|----------|---------------------|-----------------------------------------------|----------------------------------------------|----------------------------------------------|
| Control  | `ControlUARTManager`| `6e400001-b5a3-f393-e0a9-e50e24dcca8e`        | `6e400003-b5a3-f393-e0a9-e50e24dcca8e`       | `6e400002-b5a3-f393-e0a9-e50e24dcca8e`       |
| Data     | `UARTManager`       | `6e400001-b5a3-f393-e0a9-e50e24dcca9e`        | `6e400003-b5a3-f393-e0a9-e50e24dcca9e`       | `6e400002-b5a3-f393-e0a9-e50e24dcca9e`       |
| Third    | `ThirdUARTManager`  | `6e400001-b5a3-f393-e0a9-e50e24dcca7e`        | `6e400003-b5a3-f393-e0a9-e50e24dcca7e`       | `6e400002-b5a3-f393-e0a9-e50e24dcca7e`       |
| Fourth   | `FourthUARTManager` | `6e400001-b5a3-f393-e0a9-e50e24dcca6e`        | `6e400003-b5a3-f393-e0a9-e50e24dcca6e`       | `6e400002-b5a3-f393-e0a9-e50e24dcca6e`       |

The control channel (trailing nibble `8e`) carries the **outgoing**
half of the framed protobuf protocol — every command the library
issues writes to its RX characteristic. The BSC200 quirk: it does
**not** reply on the same channel. Instead, replies come back on the
Data channel's TX characteristic (`9e`), and various unsolicited
notifications (e.g. the AGPS ephemeris request, see §2.4) arrive on
the control channel's TX. The library subscribes to TX on all four
channels and reassembles by header, with one reassembly buffer per
channel. The buffers must be separate. An activity download streams
on the third channel for tens of seconds, and the AGPS request keeps
arriving on the control channel during it. With a shared buffer, that
request ends up inside the FIT data.

The third channel (`7e`) carries the CYCLING_DATA service's
list / download / delete traffic on gen-3+ devices (see §6.4 and
§7.5) — the iGPSPORT app's
``IGPDeviceManager.{readActivityList,readActivityFitFile,
deleteActivityFitFile}`` push onto ``thirdQueue`` for
``IGPDevice.getGeneration() >= 3``, which corresponds to the
``…-7e`` characteristic. The library exposes this as
``Channel = "third"`` in :mod:`ligpsport.transport`.

The fourth channel (`6e`) carries the FILE_OPERATION ADD upload
stream (route plans, CNX bytes — §7.1.2) and the gen-4 merged-write
variants of ROUTE_PLAN FILE_USE / FILES_DEL (§7.2 / §7.4).

### Advertising

The BSC200 advertises with a name beginning with one of:

* `BSC` (e.g. `BSC200_xxxxxx`)
* `iGS`
* `iGPSPORT`

The library's BLE scanner filters on this name prefix; the MAC is
incidental.

### MTU

The device negotiates an MTU during connect; the negotiated value is
used as the chunk size for both directions. The protobuf payload of
a single logical message can exceed the MTU, in which case the
framing layer below handles fragmentation. The app reports a per-file
preferred chunk in `DeviceInfo.sendFileMtuSize` (see §6).

## 2. Framing — 20-byte header + CRC8

Every logical message on a channel is a single byte-string consisting
of a **fixed 20-byte header** followed by a **protobuf payload**. The
payload bytes are the wire encoding of one of the per-service messages
defined in `reference/*.proto`.

```
+---------------------- 20 bytes ----------------------+--- N bytes ---+
|                  CommonHead20Bytes                   | protobuf body |
+------------------------------------------------------+---------------+
```

`CommonHead20Bytes` extends `BaseHead20Bytes`; both live under
`com.igpsport.blelib.pbfactory/` in the jadx output. The constructor
sets `totalSize` (header + payload), `totalBufferSize` (payload only),
`protoBufCRC` (CRC8 over the payload), and `totalCRC` (CRC8 over the
header up to but not including `totalCRC`'s own byte).

The header byte layout (offsets, widths, endianness) is transcribed
from `CommonHead20Bytes` in §2.1 below. The CRC8 lookup table and
poly are transcribed from `com.igpsport.blelib.utils.CRC8` in §2.2.

### 2.1 Header byte layout

Transcribed from
`com.igpsport.blelib.pbfactory.BaseHead20Bytes#confirmCommandByteArray`
and `BaseHead20Bytes$Companion#updateCRC` in
`classes4.dex`.

| Offset | Width | Name              | Default | Notes                                                           |
|--------|-------|-------------------|---------|-----------------------------------------------------------------|
| 0      | u8    | `first_command`   | 0x01    | `END_TYPE_PB` constant — every frame begins with 0x01.          |
| 1      | u8    | `service`         | (set)   | Index into `common.proto`'s `service_type_index` enum (0..23). |
| 2      | u8    | `sub_service`     | 0xFF    | Second-tier service byte; used by peripheral protocols.         |
| 3      | u8    | `file_tag`        | 0xFF    | Multipart file-transfer tag; see [`file_tag` values](#file-tag-magic). |
| 4      | u8    | `operation`       | 0xFF    | Main operate-type (per-service enum, e.g. `SET=1`, `GET=2`).    |
| 5      | u8    | `sub_operation`   | 0xFF    | Second-tier operate-type.                                       |
| 6      | u8    | reserved          | 0xFF    | Hard-coded `-1` in the Kotlin source.                            |
| 7-8    | u16   | `payload_size`    | (set)   | **Big-endian**; produced by `StringUtils.unsignedShortToByte2`. |
| 9      | u8    | `payload_crc`     | (set)   | CRC8 of payload bytes only.                                     |
| 10     | u8    | `end_marker`      | 0x01    | `END_TYPE_PB` (0x01) for ordinary frames; `0x02` = "continue" and `0x03` = "last chunk" for chunked file streams (FILE_SEND uploads, FILE_GET downloads). |
| 11-18  | 8×u8  | reserved padding  | 0xFF    | `RESERVED_BYTE_SIZE = 8` filled by a tight `0xFF` loop.          |
| 19     | u8    | `header_crc`      | (set)   | CRC8 of bytes 0..18 (added by `Companion.updateCRC`).           |

### 2.2 CRC8 algorithm

**CRC-8/MAXIM** (also known as CRC-8/DOW, used by Dallas/Maxim 1-Wire
devices). Polynomial 0x31, init 0, **reflected input and output**, no
final XOR. Canonical check value: `crc8(b"123456789") == 0xA1`.

The 256-byte lookup table is verbatim from
`com.igpsport.blelib.utils.CRC8` (the `crc8_tab` static array) and
shipped in `ligpsport/framing.py` as `_CRC8_TABLE`. The
`com.igpsport.blelib.utils.CRC8#calcCrc8` algorithm is a plain
table-driven loop with no special init or post-processing — the
Python `framing.crc8` function matches byte for byte.

<a id="file-tag-magic"></a>**`file_tag` values (offset 3):**

| Value | Name (`framing.FILE_TAG_*`)    | Meaning                                                                         |
|-------|--------------------------------|----------------------------------------------------------------------------------|
| `0xFF` | `DEFAULT`                      | No chunked-file bookkeeping (the common case for read/write commands).           |
| `0xAA` | `FILE_OPERATION_UPLOAD`        | App→device chunked file upload via FILE_OPERATION ADD (§7.1.2). Set by `sendRoutePlanFileSingleChannel`.|
| `0x55` | `TRANSMIT_COMPLETE`            | Device→app multi-burst file download (`TransmitCompleteCommand` in the smali). Used by CYCLING_DATA FILE_GET responses (§7.5); the head's `payload_size` is *not* authoritative — the real length comes from an embedded `file_download` protobuf. |

### 2.3 Fragmentation

Logical messages longer than the BLE MTU are split into MTU-sized
chunks at the GATT layer. The receiver concatenates chunks until it
has `expected_total_size(header)` bytes in hand, then validates
`header_crc` (and `payload_crc` for `TYPE_PB` frames). Chunks are
only ever concatenated with chunks from the same TX characteristic
(§1). The library's `ligpsport.reassembly.FrameReassembler`
implements this, separately for each channel, and is type-aware
(`TYPE_CONFIRM` / `TYPE_REQUEST` always equal `HEADER_SIZE`; only
`TYPE_PB` reads bytes 7-8 as a size field).

### 2.4 Captured frame catalogue

Reference vectors transcribed from BSC200 captures with firmware
"May 14 2024 11:07:51".

* **AGPS ephemeris request** (device → app, unsolicited, repeats
  every ~3 s): hex
  `030305ff01ffff00ffffffffffffffffffffffb3` — type=0x03,
  service=BACK(3), sub_service=EPHEMERIS(5), operation=GET(1),
  status=0, header_crc=0xB3. The app responds by uploading ephemeris
  payload over the BACK service.

* **Device version info GET** (app → device): hex
  `0111ffff02ffff0004ad01ffffffffffffffffae0811100208111002` —
  20-byte header (service=17, operation=2/GET, payload_size=4) +
  4-byte protobuf payload (`service_type=17, operate_type=GET`).

* **Device version info SEND** (device → app, on the `9e` channel
  in response to the GET above): hex
  `0111ffff03ffff00257c01ffffffffffffffffd1` then 37 bytes of
  protobuf payload encoding `version_msg{ble_boot_ver=111,
  ble_app_ver=141, hardware_ver=100, protocol_ver=101,
  compile_time="May 14 2024 11:07:51"}`.

## 3. Service registry

Every logical message starts with a varint field `service_type` (proto
field 1 in every per-service message), drawn from the
`service_type_index` enum in `reference/common.proto`. The receiver
uses this index to pick the proto class for the rest of the payload.

| Index | Name                                                  | Proto file                          |
|-------|-------------------------------------------------------|-------------------------------------|
| 0     | `enum_SERVICE_TYPE_INDEX_NONE`                        | —                                   |
| 1     | `enum_SERVICE_TYPE_INDEX_INS`                         | `ins.proto`                         |
| 2     | `enum_SERVICE_TYPE_INDEX_MAP`                         | `map.proto`                         |
| 3     | `enum_SERVICE_TYPE_INDEX_BACK`                        | `back.proto`                        |
| 4     | `enum_SERVICE_TYPE_INDEX_FIRMWARE`                    | `firmware.proto`                    |
| 5     | `enum_SERVICE_TYPE_INDEX_WIFI`                        | `wifi.proto`                        |
| 6     | `enum_SERVICE_TYPE_INDEX_CYCLING_DATA`                | `cycling_data.proto`                |
| 7     | `enum_SERVICE_TYPE_INDEX_ROUTE_PLAN`                  | `route_plan.proto`                  |
| 8     | `enum_SERVICE_TYPE_INDEX_REAL_TIME_TRACE`             | `real_time_trace.proto`             |
| 9     | `enum_SERVICE_TYPE_INDEX_USER_CONFIG`                 | `user_config.proto`                 |
| 10    | `enum_SERVICE_TYPE_INDEX_BLE`                         | `ble.proto`                         |
| 11    | `enum_SERVICE_TYPE_INDEX_FACTORY`                     | `factory.proto`                     |
| 12    | `enum_SERVICE_TYPE_INDEX_CONFIG`                      | `config.proto`                      |
| 13    | `enum_SERVICE_TYPE_INDEX_DEV_STATUS`                  | `dev_status.proto`                  |
| 14    | `enum_SERVICE_TYPE_INDEX_SENSOR`                      | `sensor.proto`                      |
| 15    | `enum_SERVICE_TYPE_INDEX_TRAINING`                    | `training.proto`                    |
| 16    | `enum_SERVICE_TYPE_INDEX_TEAM_INFO`                   | `team_info.proto`                   |
| 17    | `enum_SERVICE_TYPE_INDEX_DEV_VER_INFO`                | `dev_ver_info.proto`                |
| 18    | `enum_SERVICE_TYPE_INDEX_LANGUAGE`                    | `language_pack.proto`               |
| 19    | `enum_SERVICE_TYPE_INDEX_LOG`                         | `log.proto`                         |
| 20    | `enum_SERVICE_TYPE_INDEX_THEME`                       | `theme.proto`                       |
| 21    | `enum_SERVICE_TYPE_INDEX_FILE_OPERATION`              | `general_file_operation.proto`      |
| 22    | `enum_SERVICE_TYPE_INDEX_MAP_NEW`                     | `map_new.proto`                     |
| 23    | `enum_SERVICE_TYPE_INDEX_ROUTE_BOOK`                  | `route_book.proto`                  |

Each service defines its own `*_OPERATE_TYPE` enum (most have
`NONE / SET / GET / ADD / DEL`, with service-specific extras like
`UPGRADE` for firmware or `SEND` for status).

## 4. Pairing

The BLE GATT bond is established via the standard BLE pairing flow
(no PIN displayed on the BSC200; "Just Works"-style). Once bonded,
the app exchanges a `BLE` service message to claim a `member_id`:

* App sends `ble_msg { ble_operate_type=BOND_REQ,
  ble_data_msg={member_id=<app-account-id>} }` (service index 10).
* Device responds with `ble_msg { ble_operate_type=BOND_INFO,
  ble_data_msg={status=1, member_id=<bound-id>} }`.

`UNBOND` reverses the binding without disturbing the BLE pairing.
`CONNECT_STATUS` is a heartbeat the app can poll.

## 5. Capabilities

After binding, the app reads `device_information.DeviceInfo` (service
index 17, `dev_ver_info` is the version-only sibling). The
`functionTypeList` field is the canonical "what does this device
support" answer; the BSC200's list is what we test against. The
library caches the capability set and feature-gates read/write paths
accordingly.

## 6. Per-service operations

This section covers the operations the library implements today. Every
service follows the same wire shape:

1. App builds the per-service protobuf request, sets the operate-type
   to the desired value (typically ``GET=2`` or ``SET=1``).
2. App writes the resulting PbFrame (20-byte header + serialised
   protobuf) to the Control RX characteristic.
3. Device replies with a PbFrame on the Data TX characteristic whose
   ``operate_type`` is set to the matching response value (typically
   ``SEND=3``).
4. The header's ``service`` and ``operation`` bytes mirror the
   protobuf ``service_type`` and ``operate_type`` (the
   ``DeviceVersionInfoServiceFactory.getMessage`` Kotlin source sets
   both to the same value).

### 6.1 DEV_VER_INFO (service 17)

* **Operation `GET=2`** — request the version block.
* **Response `SEND=3`** — `version_msg` populated with
  `ble_boot_ver`, `ble_app_ver`, `hardware_ver`, `protocol_ver`,
  `compile_time`. The BSC200 omits `main_boot_ver` / `main_app_ver`.

### 6.2 DEV_STATUS (service 13)

* **`GET=1`** → **`SEND=2`** with cycling status, GPS coords, and
  real-time data (speed, cadence, HR, power, altitude, slope,
  course).

### 6.3 USER_CONFIG (service 9)

* **`GET=2`** → response carries the user profile (sex, weight,
  age, height, wheel diameter, bike weight, time zone, member id).
* **`SET=1`** with `user_config_data_message` overwrites the profile.

### 6.4 CYCLING_DATA (service 6) — recorded activities

The BSC200 calls a recorded activity an **Activity** (the Android
app's vocabulary is consistent: ``HistoryActivity``,
``readActivityList``, ``readActivityFitFile``,
``deleteActivityFitFile``). Every activity is stored on the device
as a Garmin **FIT** file keyed by a uint32 *timestamp* — that value
is both the file's filesystem key and the unique handle for every
op below. PROTOCOL.md §7.5 has the wire-level capture details and
the full library binding; this section documents the operation set.

* **`LIST_GET=1`** → **`LIST_SEND=2`** with one
  `cycling_data_file_flag_message` per recorded activity
  (`timestamp`, `file_size`, `user_id`, `device_id`). The request
  carries a ``file_list_get_message`` range
  (``file_index_start`` / ``file_index_end``). The BSC200
  firmware pads the reply with zero entries
  (``timestamp=0, file_size=0``) up to ``file_list_support_num_max``
  (= 20 on current firmware); the library's
  :func:`file_transfer.list_activities` strips them so callers see
  real entries only.
* **`LIST_NUM_GET=8`** → **`LIST_SEND=2`** with a populated
  ``file_list_get_message`` (``file_num``,
  ``file_list_support_num_max``). Used by the iGPSPORT app to
  decide pagination — captured live in
  ``snoop_start.log`` frame 35365.
* **`FILE_GET=3`** identifying the file by ``timestamp`` → device
  streams a single **transmit-complete** PbFrame on the third TX
  (``…-7e``). The 20-byte head carries ``file_tag = 0x55``,
  ``operation = FILE_SEND (4)`` and ``end_marker = 0x03``; its
  ``payload_size`` field is **deliberately bogus** on the BSC200
  (the firmware writes ``0x07A7`` / 1959 regardless of the
  actual stream length). The actual length is the size of an
  embedded ``file_download`` protobuf: the first 4 payload bytes
  are a big-endian ``pb_size``; the next ``pb_size`` bytes are a
  ``file_download`` protobuf carrying the authoritative
  ``file_size``; then ``file_size`` raw bytes of FIT file content.
  Total stream = ``20 + 4 + pb_size + file_size`` bytes. See
  §7.5 for the byte-level layout and the reassembly logic.
* **`FILE_DEL=5`** with a ``cycling_data_file_flag_message``
  carrying ``timestamp`` → device replies with a 20-byte
  ``ConfirmFrame`` on CYCLING_DATA / FILE_DEL whose status byte
  is the ``DeviceReturnStatus`` ordinal (0 = success). Destructive.
* **`ALL_DEL=6`** — wipes every recorded activity — destructive.
* **`AUTO_UPLOAD=7`** is not an upload over BLE — it stores a
  ``cycling_data_auto_upload_message`` carrying the URL the
  device should auto-upload to over **WiFi** once it has
  connected. Configures the device's cloud-upload backend; does
  not transfer file bytes over the BLE link.

There is **no app→device "upload activity"** path in the
protocol. The CYCLING_DATA service has no inverse of FILE_GET;
``FILE_SEND`` is strictly device→app. The iGPSPORT Android app
mirrors this in code — the only ``readActivityFitFile`` /
``deleteActivityFitFile`` methods exist; there is no
``writeActivityFitFile`` or ``sendActivityFitFile`` in
``IGPDeviceManager``. The cloud-side upload (the
``uploadActivityByOss`` call in ``NewApiService``) is an HTTP
upload from the phone to iGPSPORT's OSS bucket *after* the FIT
file has been pulled off the device via FILE_GET; the device
itself only ever emits activity files, never accepts them.

### 6.5 SENSOR (service 14)

* **`GET=1`** → **`SEND=5`** with a `sensor_data_message` per
  paired sensor (HRM / cadence / power / radar / light / ...).

### 6.6 FIRMWARE (service 4)

* **`GET_VERSION=1`** → **`SEND_VERSION=2`** with
  `firmware_data_message` (MCU/BLE/BLE-boot versions).
* **`MCU_UPDATE=3`** / **`BLE_UPDATE=5`** — destructive, gated.

### 6.7 ROUTE_PLAN (service 7)

* **`LIST_GET=1`** → **`LIST_SEND=2`** with
  `route_plan_info_message` entries (id, name, file_type, total
  distance, status).
* **`FILE_SEND=4`** uploads a route file. The chunking pattern
  mirrors CYCLING_DATA.
* **`FILE_DEL=3`** / **`FILES_DEL=6`** — destructive.

### 6.8 ROUTE_BOOK (service 23)

* **`GET=2` + `LIST_GET=0x02`** — note the two-level operate-type:
  the top-level `SERVICE_OPERATE_TYPE` plus a service-specific
  `ROUTE_BOOK_SUB_OP_TYPE`. The BSC200 firmware in scope here may
  not implement this service (the request times out).

### 6.9 FACTORY (service 11)

* **`SN_GET=1`** / **`SN_SEND=2`** — read the device serial number.
* **`MEMORY_GET=5`** / **`BATTARY_GET=6`** — query flash + battery state.
* **`RTC_SET=12`** — set the device clock (epoch seconds). Treated as
  a write but not destructive (no data loss).
* **`SN_SET=3`** and **`SIM_FIT_SET=7`** — destructive, gated.

#### SIM_FIT_SET (op 7) — fake activity generator

`SIM_FIT_SET` carries a `sim_fit_message { num, size }` body
(per `reference/factory.proto:92-96`) and instructs the device
to synthesise `num` activity FIT files of `size` bytes each into
its recorded-activity flash. The iGPSPORT Android app wires the
op up via `IGPDeviceManager.simulateFitFile` (smali line ~28008)
but only ships it behind an internal debug menu.

```
factory_msg {
    factory_operate_type = SIM_FIT_SET (7)
    sim_fit_message {
        num  = <count>
        size = <bytes_per_file>
    }
}
```

The library exposes this as
:func:`file_transfer.simulate_fit_files` and the
`sim-activity` CLI command, both gated by the destructive flag.

**BSC200 firmware behaviour (live-verified 2026-05-16, firmware
2024-05-14):** the device acks the request with ``status=0`` but
**does not actually create any FIT files** — a follow-up
``LIST_GET`` returns the same list as before. The Android app
wires the op up but the BSC200 firmware in scope here silently
no-ops it. Other iGS / BSC variants may honour the op; the
library returns the wire status byte unchanged so callers can
distinguish ack-from-implementation. To populate the activity
list for e2e tests on a BSC200, the user has to physically
record a short activity.

### 6.10 BLE (service 10)

* **`BOND_REQ=2`** with a `ble_data_message` carrying the app's
  `member_id` claims a binding key on the device.
* **`BOND_INFO=1`** / **`UNBOND=4`** / **`CONNECT_STATUS=3`** —
  binding lifecycle messages.

### 6.11 WIFI (service 5)

* **`STATUS_GET=1`** / **`STATUS_SEND=2`** — connection state. The
  BSC200 doesn't expose a working WiFi service (requests time out);
  the protocol is wired in for other iGS models that include WiFi.
* **`CTRL=3`** / **`ASSIGN_SSID=6`** — destructive in the sense that
  they overwrite the device's WiFi credentials.

## 7. File transfer

File transfers are layered on the per-service envelope (cf. §6.4 for
the canonical example: CYCLING_DATA). Each chunk is a PbFrame whose
protobuf payload is the service's container message with the chunk
bytes in its `file_content` field. The 20-byte header carries an
incrementing `file_tag` (offset 3) per chunk.

**Download** (e.g. activity file):

1. App sends ``cycling_data_msg`` with ``FILE_GET=3`` and a
   ``cycling_data_file_flag_message`` identifying the file.
2. Device streams 1..N ``cycling_data_msg`` frames with
   ``FILE_SEND=4`` and the chunk in ``file_content``. The first
   chunk is the reply to the GET; subsequent chunks arrive
   unsolicited on the same service.
3. App accumulates ``file_content`` bytes until the cumulative size
   matches the ``file_size`` reported by the preceding LIST_GET.

**Upload** (e.g. route plan): the route-upload path is **not** a
normal PbFrame. It is a two-characteristic chunked stream with one
ACK per chunk, derived from
``IGPDeviceManager.sendRoutePlanFile`` (smali 24586-24996) and the
chunk-send mechanic in `BaseCommand.run` (smali 1300-1330) plus
``IGPDeviceManager.send`` / ``sendAfterRequestMtu`` / ``send$lambda-135``
(smali 22547-22710 and 3419-3510).

Per chunk the app does this:

1. Build a `route_plan_data_msg` protobuf with
   `service_type=ROUTE_PLAN(7)`,
   `route_plan_operate_type=FILE_SEND(4)`,
   one `route_plan_info_message` (`id`, `file_type`, `name`,
   `total_distance`), `line_id=["<file_id>.<ext>"]`, and
   `file_content=<chunk bytes>`. Serialise to **raw protobuf bytes**
   — call this `sendData`. **No 20-byte header.**
2. Build a 20-byte `confirmData` header
   (`BaseFactory.confirmCommandByteArray`, smali 112-185)
   parameterised for ROUTE_PLAN / FILE_SEND:

   ```
   off  width  field
   0    1      0x01            (END_TYPE_PB literal)
   1    1      0x07            (service = ROUTE_PLAN ordinal)
   2    1      0xFF            (sub_service)
   3    1      0xFF            (byte3 default)
   4    1      0x04            (operation = FILE_SEND.getNumber())
   5    1      0xFF            (sub_operation)
   6    1      0xFF            (reserved)
   7-8  2      BE u16 = len(sendData)
   9    1      CRC8(sendData)
   10   1      endType         (2 for not-last chunk, 3 for the last)
   11-18 8     0xFF padding
   19   1      CRC8(bytes 0..18)
   ```

3. Write `sendData` to the **data-bearing characteristic**:
   - `…-6e` (Fourth UART RX) for generation-3+ devices
     (BSC200, BSC300, iGS520, iGS320 family);
   - `…-9e` (primary UART RX) for generation-1/2 devices.

   The kernel/BlueZ splits this into ATT-MTU-sized writes
   automatically; with the BlueZ-direct backend the negotiated MTU
   is 247, so chunks of any size fit in ⌈n/244⌉ ATT writes.

4. After the `sendData` write completes, write the 20-byte
   `confirmData` to the **control characteristic** (`…-8e`) as a
   single ≤20-byte ATT Write. The app does this only for
   generation 2 or 3 devices; gen-1 / gen-4 paths differ and are
   out of scope here (gen-4 uses `sendRoutePlanFileSingleChannel`,
   not yet implemented).

5. Wait for the device's ACK on **any TX channel** — it arrives as
   a 20-byte frame (`framing.parse_frame` returns it unchanged)
   carrying `service=ROUTE_PLAN(7)`,
   `operation=FILE_SEND(4)`, and the **status byte at offset 7**
   (`ConfirmCommand.<init>(byte[])`, smali 137-138). The status byte
   is the `DeviceReturnStatus` ordinal (`com.igpsport.blelib.DeviceReturnStatus`):

   |   | Code | Meaning                                           |
   | - | ---- | ------------------------------------------------- |
   |   | 0    | Success                                           |
   |   | 1    | DataError (file content rejected by the parser)   |
   |   | 2    | MemoryError                                       |
   |   | 3    | LowBattery                                        |
   |   | 4    | QuantityIsFull / DoneEarly (route limit reached)  |
   |   | 5    | IsBeingUsed                                       |
   |   | 6    | UnsupportedCommand                                |
   |   | 14   | WifiCyclingActivityIsUploading                    |
   |   | 15   | NavigationRouteDeletionFailed                     |
   |   | 16   | NavigationRouteDoesNotExist                       |

   The receive handler treats status 0 + `isLastPack` as upload
   complete and status 4 as "device drained the queue, stop early".
   Anything else surfaces as :class:`RouteUploadError`.

6. After all chunks are ACKed, send a single ``FILE_USE`` command
   (mirrors `setRoutePlanFile`, smali 27391-27430). The protobuf
   has `operate_type=FILE_USE(5)`, the same `line_id` and `info_msg`
   as the chunks but **no `file_content`**, and is wrapped in a
   standard 20-byte PbFrame header (byte 10 = 0x01, the normal
   END_TYPE_PB literal). The header goes on the **control**
   characteristic just like the chunked trailers. The device acks
   with another `(service=ROUTE_PLAN, operation=FILE_USE)` confirm
   frame. ``status=0`` means the device has switched its navigation
   pointer to the new route file.

The filename in the protobuf metadata is clipped per-device. Source:
the `String.hashCode()` switch in `sendRoutePlanFile` smali
24648-24705:

| Device                                | UTF-8 byte limit |
| ------------------------------------- | ----------------:|
| BSC200, BSC300, iGS320 (all variants), iGS630 | 60       |
| iGS520                                | 50               |
| iGS620                                | 28               |
| (fallback for unknown devices)        | 40               |

After truncating to the byte limit the app decodes with
`errors="replace"` and strips replacement chars so the result lands
on a complete codepoint.

The library's :mod:`ligpsport.file_transfer` exposes
:func:`upload_route_plan` that drives this chunked exchange from any
:class:`RouteData` (parsed from GPX or geoJSON). The `--backend
bluez` flag in the CLI is recommended for live uploads — bleak's
default 23-byte MTU forces ~1300 ATT writes for a typical route
where the BlueZ backend uses ~110 at MTU 247.

### 7.1 File-format requirement: CNX is mandatory on the BSC200

The wire protocol above accepts any of the
``ROUTE_PLAN_FILE_TYPE`` enum values (CNX/GPX/FIT/TCX/XML), but the
**BSC200 firmware (BLE app v141, MCU compile date 2024-05-14)
rejects everything except CNX** with persistent ``status=1``
(DataError) on every chunk. The route never lands. Variants
tested:

* ``<trk>``-form GPX with ``file_type=GPX`` — DataError on
  chunk 0.
* ``<rte>``-form GPX with ``file_type=GPX`` — DataError on
  chunk 0.
* GPX bytes with ``file_type=CNX`` (mislabelled) — DataError on
  chunk 0. The device parses the first chunk against a
  CNX-shape expectation; lying about the enum doesn't help.
* Synthesised FIT Course file (proto 2.0, file_id.type=Course,
  course + lap + event start/stop + record stream) with
  ``file_type=FIT`` — DataError on chunk 0. ``ligpsport.fit_course``
  passes ``fitparse`` round-trip, so the FIT is structurally
  valid; the BSC200 firmware simply doesn't accept it.

The rejection matches the reference app exactly: **every** call
to ``IGPDeviceManager.sendRoutePlanFile`` in the Android APK
hardcodes ``file_extension="cnx"``. The three call sites are:

| Class | dex | smali line | first-arg literal |
|-------|-----|------------|-------------------|
| `RoadBookSearchActivity` | c4 | 1548 | `const-string v19, "cnx"` |
| `RoadBookAndSegmentActivity` | c4 | 1652 | `const-string v19, "cnx"` |
| `PlanningRouteDetailActivity` | c5 | 3073 | `const-string v19, "cnx"` |

Exhaustively verified — a binary grep across **all seven**
``classes*.dex`` files of the APK shows zero occurrences of
``sendRoutePlanFile`` or ``IGPDeviceManager`` outside c4 and c5,
and the only other ``"gpx"`` literals (in c4/c5) are UI/file-
picker MIME filters and icon names (e.g. ``ic_gpx_file``,
``not_support_non_gpx``). The user's GPX file never reaches
``sendRoutePlanFile`` as GPX bytes — it goes through OSS
upload + server-side conversion first.

CNX is iGPSPORT's proprietary binary format. The reference Android
app never converts GPX to CNX client-side — when a user picks a
``.gpx`` file via the system file picker, the app
(``RoadBookAndSegmentViewModel.uploadRouteFile``, smali 1929-2017)
uploads the bytes to an Aliyun OSS bucket via
``OssUtil.uploadFile2``. The iGPSPORT cloud generates the CNX
file server-side; the app then fetches the CNX bytes from
``GET /service/mobile/api/Routes/DownloadRoutes`` and sends those
to the BSC200 with ``file_extension="cnx"``. There is no
client-side converter to reverse-engineer.

#### 7.1.1 Two dead ends: GPXtoCNXConverter and bbmodel.Route

We tried two independent local-conversion paths against the live
device (firmware *2024-05-14*, file_id=99) via the ROUTE_PLAN /
FILE_SEND service:

1. **GPXtoCNXConverter** (LudvvigB, Apache 2.0) — XML with
   second-difference ``<Tracks>`` encoding, ``Encode=2``, decimal
   ``<Distance>`` / ``<Ascent>`` / ``<Descent>`` strings, BOM.
   Rejected with status=1 (DataError) on chunk 0/2.
2. **bbmodel.Route** XML — Jackson bean at
   ``com/igpsport/globalapp/devicemodule/bean/bbmodel/Route.smali``,
   ``Reduce=0`` instead of ``Encode``, integer metrics, plain
   doubles in Tracks, no delta encoding. Also rejected.

Conclusion of §7.1.1: it isn't the CNX *content* — it's the
service. Both shapes also failed when sent via ROUTE_PLAN
FILE_SEND, which turns out to be the *wrong* upload path for
BSC200 entirely. See §7.1.2.

#### 7.1.2 The actual upload path: FILE_OPERATION ADD

A btsnoop capture (Android app → BSC200, route id 3130362; the
anonymised capture lives at ``docs/btsnoop_hci.log`` — MACs scrubbed
per ``docs/CAPTURE.md`` §Anonymisation) showed the route upload goes
via **service 21 (FILE_OPERATION) + operate_type 3 (ADD)**, *not*
ROUTE_PLAN / FILE_SEND. The smali
function ``IGPDeviceManager.sendRoutePlanFileSingleChannel``
(c4 line 3753-3964) handles it; the global APK only calls this
path when ``IGPDevice.getGeneration() == 4`` (i.e. iGS630), but
the BSC200 nonetheless accepts it — either the generation check
is outdated or the BSC200 is gen 4 in current firmware.

Wire format, all writes to the **fourth** characteristic
(``…-6e``), no per-chunk ACKs:

```
[20-byte head]
  0  0x01           TYPE_PB
  1  0x15           service = FILE_OPERATION
  2  0xff           sub_service
  3  0xaa           file_tag (magic for chunked file upload)
  4  0x03           operation = ADD
  5  0xff           sub_operation
  6  0xff           reserved
  7  0x00 0x00      size (8) = 0 (real size in the 4-byte prefix below)
  9  0x00           payload CRC (empty payload → 0)
  10 0x01           END_TYPE_PB
  11..18 0xff       reserved
  19 CRC8(0..18)    header CRC
[4 bytes BE]        size of the general_file_operation protobuf
[general_file_operation pb]
  field 1 varint    service_type = 21
  field 2 varint    operate_type = 3 (ADD)
  field 3 varint    file_type    = 2 (ROUTE_PLAN)
  field 4 varint    file_size    (length of the file body that follows)
  field 5 varint    file_id      (any positive int; the cloud uses RouteId)
  field 6 string    file_name    (UTF-8; truncated to device limit)
  field 7 string    file_extension = "cnx"
[file_size bytes]   the raw CNX body
```

The whole thing is split into MTU-3 byte writes (244 with the
negotiated 247 MTU on BSC200; 20 with bleak's default 23) on the
fourth characteristic. After the last write, the device sends a
single notification on the FILE_OPERATION service with
``status=0`` (Success) once it has parsed the route. No
``FILE_USE``-style commit is needed — the route is live as soon
as the device acks.

CNX content shape (from the same capture) — single-line XML,
ASCII (no BOM):

```
<?xml version="1.0" encoding="UTF-8"?>
<Route>
  <Id>3130362</Id>
  <Distance>8062.16</Distance>
  <Duration></Duration>
  <Ascent>181</Ascent>          ← integer metres, NOT 181.00
  <Descent>-200</Descent>
  <Encode>2</Encode>
  <Lang>0</Lang>
  <TracksCount>213</TracksCount>
  <Tracks>48.7561529,9.2263629,55241;170,2171,0;...;</Tracks>
  <Navs/>                       ← no space, no children
  <Points/>                     ← empty when no POIs
  <PointsCount>0</PointsCount>  ← AFTER Points; cloud quirk
</Route>
```

The track encoding is identical to GPXtoCNXConverter — first
record absolute (``lat,lon,ele*100``), second record
``Δlat*1e7, Δlon*1e7, Δele*100``, subsequent records use the
second difference for lat/lon (``ΔΔlat, ΔΔlon``) and the first
difference for elevation (``Δele*100``). The cloud's XML wrapper
differs in four places: no BOM, no pretty-printing, integer
``<Ascent>`` / ``<Descent>``, and the ``<Navs/>`` /
``<Points/>`` / ``<PointsCount>`` ordering quirk.

**Locale gotcha for porters** (observed in the wild, May 2026): the
``<Tracks>`` field uses commas as the *field separator within a
record*. The absolute lat/lon at record 0 **must** be formatted
with a period as the decimal separator — otherwise the first record
becomes ``48,7561529,9,2263629,55241`` (five comma-separated
tokens, not three) and the BSC200's parser falls off the rails.
The symptom on the device is an "ETA / distance to goal" value
that's off by hundreds of kilometres (we saw 693 km for a route
that was actually 9 km long).

Concretely:

* **Python** (``ligpsport.cnx``) is safe by default: f-strings and
  ``%``-format always use ``.`` regardless of ``LC_NUMERIC`` —
  only the ``:n`` format spec is locale-aware, and we don't use it.
* **Kotlin / Java** (``ligpsport-android``'s ``CnxEncoder``)
  is **not** safe by default: ``"%.7f".format(v)`` and
  ``String.format("%.7f", v)`` honour ``Locale.getDefault()``. Pin
  the locale explicitly:
  ```kotlin
  String.format(Locale.ROOT, "%.7f", v)
  ```
* **C# / .NET** has the same trap: pin
  ``CultureInfo.InvariantCulture`` on every ``ToString("F7")``.
* **Go** is safe (``strconv.FormatFloat`` is locale-independent).
* **JavaScript** is safe (``toFixed`` always uses ``.``); but
  ``Intl.NumberFormat`` and ``toLocaleString`` are not.

In short: the same rule that applies to JSON applies here. The
file format is locale-neutral; the *emitter* must be too.

This is what ``ligpsport.cnx.to_cnx_bytes`` emits and what
``ligpsport.file_transfer.upload_general_file`` ships — verified
end-to-end against the live BSC200: file_num bumped from 4 to 5
after a CLI ``upload-route foo.gpx format=cnx`` call.

The fixture at ``tests/fixtures/cnx_cloud_capture.cnx`` is the
captured cloud CNX; the structural assumptions in the encoder
are anchored against it in ``tests/test_cnx.py``.

The reverse-engineered cloud API surface (i.igpsport.com,
HTTPS, retrofit interface ``NewApiService``):

| Method | Path | Purpose |
|--------|------|---------|
| POST   | `/service/mobile/api/Routes/UploadOssGenerateRoutes` | Trigger server-side GPX→CNX conversion. Body is JSON with the OSS object key, route name, description. Returns the assigned ``RouteId``. |
| GET    | `/service/mobile/api/Routes/DownloadRoutes?RouteId=<id>&SupportDifferenceAlgorithm=<0\|2>&supportAuxiliaryPoint=<0\|1>` | Stream the converted CNX bytes. Response body is the raw file (``ResponseBody.bytes()`` in the app). Pass directly to :func:`upload_route_plan` via ``raw_bytes``. |
| (OSS)  | (aliyuncs.com, presigned PUT) | The Aliyun OSS upload step. Credentials are minted server-side via a separate sts/sign endpoint. |

The library's :func:`upload_route_plan` still implements the wire
protocol correctly: chunking, CRCs, endType byte, FILE_USE commit,
and per-chunk ACK handling all match the smali. The
``raw_bytes`` parameter (and the CLI's ``.cnx`` file handling)
let callers who already hold CNX bytes — e.g. fetched from the
endpoint above, or extracted from a working app session — push
them to the device unchanged. The client-side GPX-to-CNX gap is
the blocker for end-to-end uploads, not the BLE transport. A
``ligpsport.cloud`` wrapper around ``UploadOssGenerateRoutes`` +
``DownloadRoutes`` is out of scope here (it would need an
authenticated user session) but is a clean follow-on.

:mod:`ligpsport.file_transfer` also exposes
:func:`download_activity` for the read direction (verified
against the in-tree simulator; the BSC200 has no recorded
activities on hand to verify against the live device).

### 7.2 Starting navigation — ROUTE_PLAN FILE_USE

Uploading a route puts the file on the device's filesystem but does
**not** activate it. To make the device switch to the new route and
enter navigation mode, the iGPSPORT Android app issues a separate
``ROUTE_PLAN FILE_USE`` (operate_type=5) after the upload. The smali
hook is ``IGPDeviceManager.setRoutePlanFile(id, fileType)`` at
``c4 line 27391``; the activity-level caller is
``RoadBookSearchActivity.useRoutePlan`` (and the paired
``sendFileToDevice`` flow that wraps upload + FILE_USE behind a
single "send and use" button). The app waits ~5 s after a successful
FILE_USE — long enough for the on-device UI to transition into the
navigation screen — then dismisses its dialog.

#### Wire format (gen-4 / BSC200)

The BSC200 in current firmware reports ``getGeneration() == 4`` and
takes the **single merged write** branch of the smali's
``setRoutePlanFile``. Earlier docs (and ligpsport v1.0–v1.1.0) had
this wrong: the live wire format is **one** write of (20-byte head ‖
protobuf body) to the fourth characteristic, *not* a body/header
split across two characteristics. Verified end-to-end against a
btsnoop capture of the live "Start navigation" button (frames
35405 / 35545 in ``snoop_start.log``):

```
write_command handle=0x001f (fourth RX, …-6e), 54 bytes:
  20-byte head:
    01 07 ff ff 05 ff ff 00 22 f2 01 ff*8 22
    │  │              │     │  │  │
    │  │              │     │  │  └── header CRC8(bytes 0..18)
    │  │              │     │  └───── END_TYPE_PB
    │  │              │     └──────── payload CRC8(body)
    │  │              └────────────── BE16 payload_size = 34 (0x0022)
    │  │              ^^^^^ status byte unused on request side
    │  │           ^^ operation = FILE_USE (5)
    │  └─ service = ROUTE_PLAN (7)
    └──── TYPE_PB
  34-byte body: route_plan_data_msg protobuf
```

``route_plan_data_msg`` body, captured from a live "Start" tap:

| Field | Value |
|-------|-------|
| ``service_type`` (1) | ``SERVICE_TYPE_INDEX_ROUTE_PLAN`` (7) |
| ``route_plan_operate_type`` (2) | ``FILE_USE`` (5) |
| ``line_id`` (3) | one entry: ``"<file_id>.<ext>"`` (e.g. ``"235679.cnx"``) |
| ``route_plan_info_msg`` (5) | one entry, see below |

Nested ``route_plan_info_msg``:

| Field | Value | Notes |
|-------|-------|-------|
| ``id`` (1) | ``file_id`` | varint — matches the upload's file_id |
| ``file_type`` (2) | ``enum_ROUTE_PLAN_FILE_TYPE_CNX`` (1) | |
| ``name`` (3) | display name | **required** by BSC200 firmware; capture shows ``str(file_id)`` for unnamed routes (e.g. ``"235679"``). Omitting it causes a silent drop on the device side. |
| ``total_distance`` (4) | metres, ``0`` if unknown | the app sends ``0`` for newly-uploaded routes; the device doesn't validate this. |

Channel mapping (per ``setRoutePlanFile`` smali + ``send$lambda-135``):

| Device gen | Write |
|-----------:|-------|
| **4** (BSC200, iGS630) | **Single** merged write (head ‖ body) on ``fourth`` (``…-6e``). ``StringUtils.byteMerger(pair.second, pair.first)`` glues them; ``send$lambda-135`` skips the follow-up control write. |
| 2–3 (legacy iGS520, …) | Two writes: body on the data-bearing UART (``fourth`` for gen 3, ``data`` for gen 2), then the 20-byte head on ``control`` (``…-8e``). |

The device replies with a 20-byte ``TYPE_CONFIRM`` (0x02) frame on
the same ROUTE_PLAN service:

```
02 07 ff ff 05 ff ff <status> ff*11 <crc8>
                    ^^^^^^^^ DeviceReturnStatus wire byte
```

Status decoding (from ``DeviceReturnStatus.smali`` — the wire
values are **not** sequential ordinals; the WiFi block jumps to
16-23 and the Navigation block to 65-66):

| Wire byte | Name | Meaning for FILE_USE |
|----------:|------|----------------------|
| 0 | Success | Route activated; navigation mode on. |
| 1 | DataError | Malformed protobuf (e.g. missing ``name`` field). |
| 5 | IsBeingUsed | Another route is active and locked. |
| 65 | NavigationRouteDeletionFailed | (FILE_DEL-specific; never seen here.) |
| 66 (0x42) | NavigationRouteDoesNotExist | The ``file_id`` isn't on the device. The app's typical flow fires a speculative FILE_USE first; on 66 it uploads via FILE_OPERATION ADD and retries. |

#### Library binding

`ligpsport.file_transfer.upload_route_plan(..., start_navigation=True)`
(and the underlying ``upload_general_file``) issues FILE_USE
automatically after a successful upload, using the gen-4 merged-write
wire format the BSC200 expects. The CLI exposes this as a trailing
``start`` token:

```sh
ligpsport command --name bike upload-route trip.gpx 1 start
ligpsport command --name bike upload-route trip.cnx 7 start
```

Without ``start`` the route is loaded but the user must pick it
manually from the on-device route list. A non-zero FILE_USE status
raises :class:`ligpsport.file_transfer.NavigationStartError`; the
upload itself is still considered successful (the file remains on
the device and can be activated later — either by retrying the
FILE_USE or via the on-device UI).

There is no inverse "stop navigation" command in the protocol. The
``DEV_NAVI_STATUS`` enum has only ``ON`` and ``OFF`` values, and
``route_plan.proto`` has no ``FILE_UNUSE`` / ``STOP_NAV`` op. The
app's UX confirms this: navigation is ended by the user on the
device itself (back-button / "End Navigation" menu). For automated
test cleanup the simulator's
:attr:`ligpsport.simulator.SimulatorState.navi_status` field can be
reset directly — there is no BLE-level analog on the real device.

### 7.3 Checking whether navigation is currently active

``dev_status.proto`` exposes a ``navi_status`` field on
``dev_status_msg`` (``DEV_NAVI_STATUS`` enum — 0 = OFF, 1 = ON),
which looks like the obvious place to check. **The BSC200
firmware never populates it.** A live ``DEV_STATUS GET`` against
firmware 2024-05-14 while the device is actively navigating
"Hopfenhhe" returns ``navi_status = 0`` and ``DEV_STATUS SEND``
notifications also leave it unset. The field exists in the proto
but is dead on the wire.

The iGPSPORT Android app uses a different mechanism, and so does
the library. The signal is in ``ROUTE_PLAN LIST_GET``: every
``route_plan_info_msg`` in the reply carries a
``ROUTE_PLAN_FILE_STATUS`` byte (field 7), and the route currently
being navigated is tagged ``enum_USED_STATUS = 1``; everything
else is ``enum_UNUSED_STATUS = 2``. Reference implementation:
``RoutePlanViewModel.requestUsingRouteID`` in ``smali-c5``, which
iterates the list and picks the entry with ``getStatus() ==
enum_USED_STATUS``.

#### Wire format

The LIST_GET request **must** include a ``route_list_get_msg``
range — the BSC200 silently returns an empty list otherwise. The
range fields are ``file_index_start`` (field 3) and
``file_index_end`` (field 4) on ``file_list_get_message``; the
Android app populates both. ``file_list_support_num_max`` on
current firmware is 10, so any window wider than that is
sufficient. The library uses ``[0, 100]``.

Captured live response (BSC200 with five routes, "Hopfenhhe" active):

```
ROUTE_PLAN LIST_SEND reply, route_plan_info_msg entries:
  id=1778803189 name="e2e-osrm"      type=CNX status=UNUSED
  id=1778803520 name="e2e-osrm"      type=CNX status=UNUSED
  id=1778803567 name="Pelikanschule" type=CNX status=UNUSED
  id=1778803609 name="Hopfenhhe"     type=CNX status=USED     ← active
  id=1778804267 name="e2e-osrm"      type=CNX status=UNUSED
```

#### Library binding

* :func:`ligpsport.commands._r_nav_status` (CLI: ``nav-status``)
  issues a ``ROUTE_PLAN LIST_GET`` with the 0..100 window, scans
  for ``status == enum_USED_STATUS``, and returns
  :class:`commands.NavStatus` with ``is_navigating``,
  ``active_route_id`` and ``active_route_name``. Use this in
  automation and e2e tests.

  Live example::

    $ ligpsport command --name bike nav-status --backend bluez
    Navigation: ON  (route_id=1778803609 name='Hopfenhhe')

* :func:`ligpsport.commands._r_routes` (CLI: ``routes``) uses the
  same ``ROUTE_PLAN LIST_GET`` mechanism; each
  :class:`commands.RoutePlan` entry exposes ``status`` so callers
  can detect the active route themselves.

The simulator's ``_handle_route_plan`` returns the same list
shape: every entry from ``SimulatorState.uploaded_routes`` is
tagged USED if its ``file_id`` matches
``SimulatorState.active_route_id``, UNUSED otherwise. This makes
the "upload + start navigation + verify active" loop hermetic
without a real device.

### 7.4 Deleting routes — and why navigation cannot be stopped over BLE

Inactive routes can be deleted from the device via
``ROUTE_PLAN FILES_DEL`` (op=6). The currently-navigating route
is **firmware-protected**: every BLE candidate we tried left
navigation on. There is no dedicated ``STOP_NAV`` /
``FILE_UNUSE`` opcode in the protocol, the iGPSPORT Android app
does not expose a "stop" button, and the only way to leave the
navigation screen via the app is to power-cycle the BSC200 or
press the device's own *Stop* button. Live-probed against
firmware 2024-05-14 on ``F7:11:62:07:21:F5``:

| Candidate                                    | Wire status   | Effect on active route        |
|----------------------------------------------|---------------|-------------------------------|
| ``FILE_USE`` with ``file_id=0`` name=``"0"`` | ``0`` (ok)    | Switches to id 0; nav stays on |
| ``FILE_USE`` with a bogus ``file_id``        | ``66`` (err)  | None — error returned          |
| ``FILE_DEL`` (op=3) on active route          | ``0`` (ok)    | None — silently no-op'd        |
| ``FILES_DEL`` (op=6) on active route         | ``0`` (ok)    | None — silently no-op'd        |
| ``FILES_DEL`` (op=6) on an inactive route    | ``0`` (ok)    | Route gone from ``LIST_GET``   |

The firmware **acks deletions of the currently-navigating route
with status=0 but does not actually delete it** — a follow-up
``LIST_GET`` shows the route still present and still tagged
``enum_USED_STATUS``. Inactive routes are deleted normally, so
the protection is specific to the active one. This matches the
Android app's UX: it forces users to delete from the device UI
exactly when the route is in use.

#### Wire format

The captured ``IGPDeviceManager.deleteRoutePlanFile`` (smali
line 7419) builds a ``route_plan_data_msg`` with
``operate_type = FILES_DEL`` and **both** sets of fields
populated:

* ``line_id`` list — one ``"<id>.<ext>"`` string per target
* ``route_plan_info_msg`` records — full entries with ``id``,
  ``file_type``, ``name``, ``total_distance``

Sending only ``line_id`` (or only ``route_plan_info_msg``) is
silently no-op'd by the BSC200. Gen-4 devices want the head and
body in a single merged write on the *fourth* characteristic;
gen ≤ 3 devices take body-on-data + header-on-control.

#### Library binding

* :func:`ligpsport.commands._r_del_route` (CLI:
  ``del-route <id>``) deletes one route by id. It does a
  ``LIST_GET`` pre-check to resolve the route's name and
  active-flag, issues ``FILES_DEL``, then re-lists to confirm
  the route is gone. The result is honest:

  - ``deleted=True`` — the device dropped the route.
  - ``not_found=True`` — the id wasn't on the device.
  - ``was_active=True, deleted=False`` — firmware refused because
    the route is currently being navigated. Stop navigation on
    the device, then retry.

  Marked destructive: needs ``--allow-destructive-commands``.

  Live example::

    $ ligpsport command --name bike del-route 24680 \
        --allow-destructive-commands --backend bluez
    Deleted route id=24680 name='ligpsport test loop'

* :func:`ligpsport.file_transfer.delete_route_plan_files` is the
  underlying helper used by ``del-route`` and available to
  scripts that need to delete multiple routes in one shot.

### 7.5 Listing, downloading, and deleting activities

The BSC200 keeps recorded activities as Garmin **FIT** files in
flash under the **CYCLING_DATA** service (§6.4). The iGPSPORT app
exposes them as *Activities*; the library matches that vocabulary
(``list-activities`` / ``download-activity`` / ``del-activity`` /
``del-all-activities``). All four operations share two non-obvious
wire details that differ from the documented enums and from
ROUTE_PLAN's chunked uploads:

1. **They run on the third UART RX (``…-7e``), not the control
   UART** the rest of the library uses. The iGPSPORT app's
   ``IGPDeviceManager.readActivityList`` /
   ``readActivityFitFile`` / ``deleteActivityFitFile`` all push
   onto ``thirdQueue`` for ``IGPDevice.getGeneration() >= 3``
   (the BSC200 reports gen 4). The btsnoop capture in
   ``snoop_start.log`` frame 35365 confirms it: the LIST_NUM_GET
   request hits handle ``0x0019`` — the third RX (``…-7e``) —
   and the reply notification arrives on handle ``0x001b``, the
   matching third TX.
2. **FILE_GET requests set ``file_tag = 0x55`` in the 20-byte
   head.** The smali ``readActivityFitFile`` (c4 line ~1442 for
   gen 4) wraps the freshly-built head in a
   ``TransmitCompleteCommand`` and patches byte 3 (the
   ``file_tag`` slot) to 0x55. Without that magic the BSC200
   silently ignores the request — verified live against firmware
   2024-05-14: every variant we tried with ``file_tag = 0xFF``
   (default), regardless of channel or operation byte, timed
   out with no reply. With ``file_tag = 0x55`` on the third RX
   the device streams the file.

#### Listing — LIST_GET (op 1) and LIST_NUM_GET (op 8)

Wire shape (captured frame 35365, anonymised
``docs/btsnoop_hci.log``):

```
write handle=0x0019 (third RX, …-7e), 24 bytes:
  20-byte head:
    01 06 ff ff 08 ff ff 00 04 e3 01 ff*8 0b
    │  │     │  │              │  │  │
    │  │     │  │              │  │  └── header CRC8(bytes 0..18)
    │  │     │  │              │  └───── END_TYPE_PB
    │  │     │  │              └──────── payload CRC8(body)
    │  │     │  │ ^^^^^ BE16 payload_size = 4
    │  │     │  └─ operation = LIST_NUM_GET (8)
    │  │     └─── file_tag = 0xff (no transmit-complete magic)
    │  └─ service = CYCLING_DATA (6)
    └──── TYPE_PB
  4-byte body: cycling_data_msg{service_type=6, op=8}
```

Notification on handle ``0x001b`` (third TX, ``…-7e``):

```
01 06 ff ff 08 ff ff 00 0a e0 01 ff*8 72
   |              |
   └─ op=LIST_NUM_GET                                    │
                                            │
   <10-byte body: file_list_get_msg{file_num=0, file_list_support_num_max=20}>
```

The library's :func:`ligpsport.file_transfer.list_activities`
sends a full ``LIST_GET`` (op 1) with an inclusive index range
``[0, 100]`` — same idiom as ``ROUTE_PLAN LIST_GET``. The
device populates the reply with every entry plus
``file_list_support_num_max - file_num`` zero-padded
placeholders; the helper strips the padders before returning.

#### Downloading — FILE_GET (op 3), transmit-complete stream

Request (live, ``timestamp = 1147795610``, file size 15572):

```
write on third RX (…-7e), 32 bytes:
  20-byte head:
    01 06 ff 55 03 ff ff 00 0c 6b 01 ff*8 29
    │     │  │  │                       │
    │     │  │  └─ operation = FILE_GET (3)
    │     │  └──── file_tag = 0x55 (TransmitCompleteCommand)
    │     └─────── service = CYCLING_DATA (6)
    └──────────── TYPE_PB
  12-byte body: cycling_data_msg{op=FILE_GET,
                  cycling_data_file_flag_msg=[{timestamp=1147795610}]}
```

Reply (live, captured byte-for-byte in
``tmp/snoop_dl/activity_third.fit``):

```
notification on third TX (…-7e), 20 + 4 + pb_size + file_size bytes
total (= 15639 bytes for a 15572-byte FIT):

  20-byte head:
    01 06 ff 55 04 ff ff 07 a7 00 03 ff*8 22
    │           │     │  │              │
    │           │     │  └─ end_marker = 0x03 (LAST)
    │           │     └──── payload_crc = 0x00  ← zeroed; BSC200 quirk
    │           │   ^^ payload_size = 0x07a7 (1959) — DELIBERATELY BOGUS
    │           └─ operation = FILE_SEND (4)
    │     └─────── file_tag echoed back: 0x55
    └──────────── TYPE_PB

  4-byte BE u32: pb_size  (= 3 for this file)
  pb_size bytes: file_download protobuf, e.g. `08 d4 79`
                 = {file_size = 15572}
  file_size bytes: raw FIT bytes (start with `.FIT` magic at
                 offset 8 of the FIT header)
```

Several BSC200-firmware quirks worth flagging up front:

* **`payload_size` (offsets 7-8) is not the stream length.** The
  firmware writes ``0x07A7`` (1959) regardless of how much data
  follows. The library's framing layer detects ``file_tag = 0x55``
  via :func:`framing.is_transmit_complete_head` and switches
  to a length-from-pbInfo path
  (:func:`framing.transmit_complete_total_size`) instead of
  trusting the head.
* **`payload_crc` (offset 9) is zero.** The
  :func:`framing._parse_pb_frame` validator tolerates a zero
  payload CRC byte specifically for transmit-complete frames.
* **No EOS marker.** The library learns when the stream is
  complete by counting bytes: ``4 + pb_size + file_size``. A
  trailing BACK / WiFi service request (``TYPE_REQUEST`` /
  type=0x03) frame is unrelated periodic device chatter and is
  routed normally through the dispatcher.
* **The `cycling_data_msg` envelope is bypassed.** The payload is
  **not** a protobuf — it's the `[4B size][pbInfo][file bytes]`
  blob above. The client dispatcher in
  :class:`ligpsport.client.IgpsportClient` recognises
  ``file_tag = 0x55`` frames and skips the
  :func:`envelope.decode_payload` call so the raw bytes flow
  through unchanged.

#### Deleting — FILE_DEL (op 5) and ALL_DEL (op 6)

Wire shape (verified end-to-end against the in-tree simulator and
against a live BSC200, firmware 2024-05-14):

```
write on third RX (…-7e), 30 bytes:
  20-byte head:
    01 06 ff ff 05 ff ff 00 0a <crc> 01 ff*8 <hdr-crc>
    │           │
    │           └─ operation = FILE_DEL (5)  | ALL_DEL = 6
    └─ service = CYCLING_DATA (6)
  10-byte body: cycling_data_msg{op=FILE_DEL,
                  cycling_data_file_flag_msg=[{timestamp=<ts>}]}
```

Reply (TYPE_CONFIRM frame on third TX):

```
02 06 ff ff <op> ff ff <status> ff*11 <crc>
                       ^^^^^^^^ DeviceReturnStatus wire byte
```

Both FILE_DEL and ALL_DEL go out on the third UART RX as a single
**merged** ``(head ‖ body)`` write — same wire pattern the smali
calls ``byteMerger(pair.second, pair.first)``. Worth noting: the
Android app's smali ``deleteAllActivityFitFile`` (line 6742+)
doesn't actually call ``byteMerger`` for ALL_DEL — it passes
``sendData=body, confirmData=header`` and relies on the gen-4
``send$lambda-135`` to *skip* the follow-up control-channel
header write, with the result that on gen-4 only the body would
land at the device. Empirically the BSC200 rejects that
(``status=1`` DataError) and accepts the merged write happily;
the iGPSPORT app's gen-4 behaviour here is a latent bug, not the
protocol's intent. Live-verified: a single merged write returns
``status=0`` and the activity list goes empty.

##### FILE_DEL: ack means deleted

A ``FILE_DEL`` request targeting an existing activity returns
``status=0`` and the entry is gone from the next ``LIST_GET``.
Live-verified 2026-05-16 against firmware 2024-05-14: a freshly
recorded activity was listed, ``FILE_DEL`` ack'd with ``status=0``,
and the next ``LIST_GET`` returned an empty list.

| Op | Wire status | Effect on device |
|----|-------------|------------------|
| ``FILE_DEL`` (5) | ``0`` (ok) | Activity removed from LIST_GET |
| ``ALL_DEL`` (6)  | ``0`` (ok) | All activities removed from LIST_GET |

> **Retraction.** Earlier releases (v1.3 and prior) documented an
> "active-file protection" mirroring ROUTE_PLAN's active-route
> protection — i.e., the firmware silently keeping a recently
> recorded activity after a ``FILE_DEL`` ack. The 2026-05-16
> retest could not reproduce it. The "silently kept" observation
> traces back to a probe-heavy debugging session whose
> speculative ``file_tag = 0xAA`` write wedged the device's parser
> (cf. AGENTS.md §2); the device was either returning a
> stale ``LIST_GET`` while still parked or replying with a
> zeroed-out frame from the wedged state. There is **no
> firmware-level active-file protection** for activities — every
> activity listed by ``LIST_GET`` is deletable with ``FILE_DEL``.

The library's :func:`file_transfer.delete_activity` returns the
status byte; the CLI's ``del-activity`` still wraps it with a
pre/post ``LIST_GET`` and surfaces a ``deleted`` boolean in
:class:`commands.DelActivityResult` so callers can tell a wire
ack apart from any future firmware regression that re-introduces
silent denial.

#### No upload — by design

The CYCLING_DATA service has no app→device file-write
operation. The enum's ``FILE_SEND = 4`` is strictly
device→app (the device streams its own FIT files in response to
FILE_GET; there is no symmetrical app→device variant). The
iGPSPORT Android app mirrors this in code: there is no
``writeActivityFitFile`` / ``sendActivityFitFile`` /
``uploadActivityFitFile`` method anywhere in
``IGPDeviceManager``. The cloud-side ``uploadActivityByOss`` in
``NewApiService`` runs after FILE_GET — the phone pulls the FIT
off the device, then HTTP-uploads it to iGPSPORT's OSS bucket;
the device itself only ever emits activity bytes. The
``AUTO_UPLOAD = 7`` op configures the device's *own* WiFi auto-
upload URL; it does not transfer file bytes over BLE.

If a future firmware ever exposes an app→device write path it
would surface in this repo by adding a new op to
``cycling_data.proto`` and a matching method on
``IGPDeviceManager`` — neither exists today.

#### Library / CLI surface

All three CYCLING_DATA wire ops have library entry points and
matching CLI commands, all built on the wire protocol documented
above (no new framing):

* :func:`file_transfer.list_activities` ↔ ``list-activities``
* :func:`file_transfer.download_activity` ↔ ``download-activity``
* :func:`file_transfer.delete_activity` ↔ ``del-activity``
* :func:`file_transfer.delete_all_activities` ↔ ``del-all-activities``

The library also offers two ergonomic wrappers that don't add new
wire ops:

* ``download-activity <ts> <path> [type=fit|gpx]`` — when
  ``type=gpx``, the downloaded FIT is parsed with ``fitparse``
  (see :mod:`ligpsport.fit_activity`) and re-emitted as GPX 1.1
  with one ``<trkpt>`` per FIT ``record`` carrying lat/lon.
  When ``<path>`` is a directory the file lands as
  ``<YYYYMMDDTHHMMSSZ>_<device_model>.<ext>`` (UTC time from the
  FIT ``file_id.time_created``).
* ``download-all-activities <out-dir> [type=fit|gpx]`` — iterates
  ``LIST_GET``, downloads each entry, writes with the derived
  name, and skips entries whose target file already exists.

For destructive e2e testing, ``sim-activity count=N size=BYTES``
exposes FACTORY/SIM_FIT_SET (§6.9) — but as documented there, the
BSC200 firmware in scope here acks the op without materialising
the files. On a BSC200 you still need to physically record a
short activity to populate the activity list.

## 8. Destructive operations

The following ``(service, operation)`` tuples alter persistent state
on the device. They are listed verbatim in
``ligpsport.commands.DESTRUCTIVE_PREFIXES`` and gated behind
``--allow-destructive-commands``:

| Service | Operation | Effect                                                |
|---------|-----------|-------------------------------------------------------|
| 6 (CYCLING_DATA) | 5 (FILE_DEL) | Permanently deletes one recorded activity (§7.5). |
| 6 (CYCLING_DATA) | 6 (ALL_DEL)  | Permanently deletes every recorded activity.   |
| 7 (ROUTE_PLAN)   | 3 (FILE_DEL) | Deletes one route plan from the device.        |
| 7 (ROUTE_PLAN)   | 6 (FILES_DEL)| Deletes multiple route plans in one shot.      |
| 4 (FIRMWARE)     | 3 (MCU_UPDATE) | Initiates an MCU firmware flash.            |
| 4 (FIRMWARE)     | 5 (BLE_UPDATE) | Initiates a BLE firmware flash.            |
| 11 (FACTORY)     | 3 (SN_SET)   | Overwrites the device serial number.           |
| 11 (FACTORY)     | 7 (SIM_FIT_SET) | Generates a fake activity file in flash.    |

These are also refused by the simulator's
``SimulatorState.allow_destructive`` guardrail unless explicitly
opted in by test fixtures (cf. ``AGENTS.md`` §2).

## 9. Credential persistence

The pairing state is stored in
`$XDG_DATA_HOME/ligpsport/credentials.json` (default
`~/.local/share/ligpsport/credentials.json`), `chmod 0600`. The
schema is:

```json
{
  "version": 1,
  "devices": {
    "bike": {
      "address": "AA:BB:CC:DD:EE:FF",
      "name": "BSC200_abcdef",
      "member_id": "ligpsport-7f31a8c2",
      "paired_at": "2026-05-12T20:42:00Z",
      "last_firmware": "1.2.3"
    }
  }
}
```

`member_id` is a per-installation random token the app generates and
the device remembers as its binding key. We mint our own on first
pair and persist it; subsequent connects reuse it.

## 10. Code map

| Module                       | Responsibility                                  |
|------------------------------|-------------------------------------------------|
| `ligpsport.framing`          | 20-byte header + CRC8 codec                     |
| `ligpsport.envelope`         | service-id → proto class routing                |
| `ligpsport.transport`        | TransportInterface + Bleak / Loopback impls     |
| `ligpsport.client`           | high-level async API                            |
| `ligpsport.discovery`        | BLE scan with name filter                       |
| `ligpsport.credentials`      | XDG-compliant credential store                  |
| `ligpsport.commands`         | named-command registry, destructive gating      |
| `ligpsport.capabilities`     | FunctionType helpers                            |
| `ligpsport.file_transfer`    | chunked upload / download                       |
| `ligpsport.simulator`        | in-process wire peer for tests                  |
| `ligpsport.proto.*`          | generated protobuf modules                      |

## 11. Known unknowns

* The BSC200 advertises all four UART services (`6e` / `7e` / `8e` /
  `9e`) but only exchanges control protocol on `8e` (RX) ↔ `9e` (TX).
  The third (`7e`) and fourth (`6e`) channels are both active on
  this firmware: the third UART carries the CYCLING_DATA activity
  ops (LIST_GET, FILE_GET, FILE_DEL — see §7.5), and the fourth
  UART carries FILE_OPERATION uploads and the gen-4 merged
  ROUTE_PLAN writes (§7.1.2, §7.2). What's *still* unknown is
  whether either channel becomes active during a firmware-upgrade
  flow — capture needed.
* The BSC200's `WIFI` (service 5), `ROUTE_BOOK` (service 23), and
  `IND` (service 1, smart notifications) all time out on this firmware
  (`May 14 2024 11:07:51`, `protocol_ver=101`). The library implements
  them anyway because the app does — they're likely conditional on the
  device's capability bits or simply absent on the BSC200 hardware.
* Whether `REAL_TIME_TRACE` (service 8) streaming starts unsolicited
  on connection or requires an explicit subscribe message. The
  app's handling pulls from the same Control TX subscription either
  way, so the library will pick it up once the trigger is observed.
* The exact `general_file_operation` upload handshake order (does the
  device's ConfirmFrame come after each chunk or only after the final
  chunk?). The library currently sends all chunks and waits for one
  ConfirmFrame — this works for short uploads but may need to slow
  down for very large files.
* MCU firmware version (`main_app_ver`, `main_boot_ver`) is missing
  from this BSC200's `DEV_VER_INFO` reply. The `firmware` service
  also returns `mcu_firmware_ver=0`. Whether the BSC200 truly has
  no MCU firmware revision or simply doesn't expose it over BLE is
  unknown.

## 12. AGPS / ephemeris pre-seeding

The BSC200 (and other iGS-series devices) accept an **AGPS / ephemeris
seed** that drops the cold-start time-to-first-fix from ~30–90 s to
~5–10 s. The payload is a u-blox **AssistNow Online** stream
(concatenated UBX-MGA messages — ephemeris + almanac + reference
time/position priors), valid for ~2–4 hours.

This section documents the wire shape, the source, and the trade-off
between the published `back.proto` schema (which describes the
*intended* path) and the **actual** path the production iGPSPORT app
takes (which is just `FILE_OPERATION` with a different `file_type`).

### 12.1 Two paths, only one is real

`reference/back.proto` defines an `ephemeris_data_message` that looks
like the official upload format:

```protobuf
message ephemeris_data_message {
  optional string    file_name = 1;   // e.g. "online_20240514.ubx"
  optional bytes     contents  = 2;   // raw UBX-MGA stream
  optional GPS_TYPE  gps_type  = 3;   // GPS / BD / GLONASS / GALILEO
  optional AGPS_TYPE agps_type = 4;   // ONLINE / ANO_OFFLINE / ALM_OFFLINE
  optional uint32    time      = 5;   // UTC
}
message back_msg {
  service_type      = BACK (3);
  back_service_type = EPHEMERIS (5);
  back_operate_type = SEND (2);
  optional ephemeris_data_message ephemeris_data_msg = 8;
}
```

with the matching enums:

| Enum                | Values                                                |
|---------------------|-------------------------------------------------------|
| `GPS_TYPE`          | INVALID(0), GPS(1), BD(2), GLONASS(3), GALILEO(4)     |
| `AGPS_TYPE`         | INVALID(0), ONLINE(1), ANO_OFFLINE(2), ALM_OFFLINE(3) |
| `BACK_SERVICE_TYPE` | NONE, MAIN, WEATHER, AIR_PRESSURE, ELEVATION, EPHEMERIS=5 |

The proto comment on `file_name` even hedges: *"此命令后期可以作废，
由码表自己根据类型等信息定义文件名"* ("this command can be deprecated
later — the cycle computer will define the file name itself based on
type info"). That hedge held: **the official app does NOT use
`back_msg` / `ephemeris_data_msg` for the upload.** Instead it routes
the upload through `FILE_OPERATION` (service 21), exactly like a CNX
route, with only `file_type` differing.

Cross-referenced from
`IGPDeviceManager.writeEphemerisDataSingleChannel`
(`classes4.dex` line ~6246) and
`DeviceBleManagerHandler.sendAGPS` / `sendOfflineAGPS`
(`classes5.dex` lines ~10127 / ~16335):

| Field               | Value                                                 |
|---------------------|-------------------------------------------------------|
| header `service`    | 21 (`FILE_OPERATION`)                                 |
| header `operation`  | 3 (`ADD`)                                             |
| header `byte[3]`    | `0xAA` (the same upload magic as a CNX route)         |
| pb `file_type`      | 7 (`FILE_TYPE_AGPS`)                                  |
| pb `file_id`        | `GPS_TYPE` enum number (1=GPS, 2=BD, 3=GLO, 4=GAL)    |
| pb `file_name`      | basename split on `.`, e.g. `"online_20240514"`       |
| pb `file_extension` | `"ubx"`                                               |
| pb `file_size`      | length of the contents                                |
| body                | raw UBX-MGA stream                                    |
| channel             | **Fourth** (`6e`) — same chunk transport as CNX upload|

The device's `ConfirmFrame` on success has `status=0`. There is no
multi-chunk handshake distinct from the regular CNX flow — the whole
payload goes out as one logical frame, fragmented at the MTU.

The lingering use of `back_msg` is for the **device-initiated GET
notification** documented in §2.4 (the every-3-second "send me
ephemeris" poll on the control channel). The app replies by triggering
the `FILE_OPERATION` upload above. The `back_msg{operate=SEND}` shape
is never sent on the wire by the production app.

### 12.2 Source: u-blox AssistNow Online

URL (from `DeviceBleManagerHandler.sendAGPS`, `classes5.dex` line 10213):

```
http://online-live1.services.u-blox.com/GetOnlineData.ashx
    ?token=<TOKEN_SUFFIX>
```

where `<TOKEN_SUFFIX>` is the full string `token=<DEVELOPER_TOKEN>&<query…>`
expects to receive. The iGPSPORT app does NOT hardcode the developer
token in the APK; instead it fetches the entire token-suffix string at
startup from its own backend:

```
GET https://prod.en.igpsport.com/service/mobile/api/Config/GetDefaultConfig?type=0

→ {"code":0, "message":"", "data":"<token>&gnss=gps&datatype=eph"}
```

`prod.zh.igpsport.com` is the mainland-China sibling and serves the
same payload. The value is then persisted to
`<filesDir>/default_config.json` and surfaced via
`UserIdentity.getDefaultConfig().getAgpsToken()`.

The literal token observed at the time of this writing is
`Ui8i31HZzkijSxQvrrRGJw` (verified 2026-05-14 against the live u-blox
endpoint: HTTP 200, 2464 B of `application/ubx` named `mgaonline.ubx`,
starting `B5 62 13 40` = MGA-INI-TIME_UTC). The token may rotate, so
clones should fetch fresh — the iGPSport `Config/GetDefaultConfig`
endpoint requires no authentication (no `Authorization` header, no
user account).

The trailing `&gnss=gps&datatype=eph` is also dictated by the iGPSport
backend response — the official app appends it verbatim to the u-blox
URL. The device firmware expects exactly *GPS ephemeris only* (not a
multi-constellation payload).

Typical response is ~2.5 KB and contains:
* one `UBX-MGA-INI-TIME_UTC` (0x13 0x40) — reference time, valid at
  fetch instant.
* one `UBX-MGA-INI-POS_LLH` or `UBX-MGA-INI-POS_XYZ` (0x13 0x00) if
  the backend can geo-IP the requester.
* up to ~30 `UBX-MGA-EPH` messages — one per active GPS satellite,
  each containing the 3-subframe broadcast ephemeris for the next
  ~4 hours.

For a self-hosted clone that wants to avoid the runtime dependency on
iGPSport's backend, register at
https://www.u-blox.com/en/assistnow-service-evaluation-token-request
for a free developer token of your own.

For **offline AGPS** (longer-term almanac data) the app instead hits
`https://offline-live1.services.u-blox.com/GetOfflineData.ashx?token=…`
with the URL itself returned by the iGPSPORT backend
(`GetUrlByTypeUtil.getUrl(10)`, `classes5.dex` line 16462). The
offline file is much larger (~150 KB) and valid for several days; the
app uploads it with `AGPS_TYPE = ANO_OFFLINE` (and `file_id =
GPS_TYPE_GPS = 1`). The wire shape is otherwise identical.

### 12.3 File-name conventions

Reverse-engineered from `DeviceBleManagerHandler.sendAGPS`
(`classes5.dex` lines ~10266–10316):

* **Online**: `online_<YYYY><MM><DD>.ubx`. The app derives the date
  from the response body itself: `bytes[10]` (interpreted unsigned)
  plus 1792 = year, `bytes[12]` = month, `bytes[13]` = day, zero-padded
  to 2 digits. Simpler implementations can substitute today's UTC
  date — the device stores `file_name` for display only; the *contents*
  are what the firmware parses.
* **Offline**: `offline_<YYYY><MM><DD>.ubx`, today's UTC date
  (`yyyyMMdd` per `TimeUtils.date2String`).
* **Almanac-only offline**: `offline_alm_<YYYY><MM><DD>.ubx`.

The protobuf splits this on `.` into `file_name` and
`file_extension="ubx"` per the regular `FILE_OPERATION` schema.

### 12.4 Reference Kotlin implementation (`ligpsport-android`)

* `agps/AgpsClient.kt` — ktor `HttpClient` wrapper that GETs the
  online URL and returns the raw bytes. When no developer token is
  passed it first calls iGPSport's `Config/GetDefaultConfig?type=0`
  to recover one, exactly like the official app.
* `ble/UploadPipeline.kt#seedAgps` — public entry: opens the paired
  BLE transport, fetches AGPS, uploads via
  `FileTransfer.uploadGeneralFile` with `fileType = FILE_OP_TYPE_AGPS (7)`.
* `ble/UploadPipeline.kt#uploadAgpsBestEffort` — internal helper
  piggybacked onto every `uploadGpx` call. If the network fetch fails
  or the device rejects, it logs and returns `null` so the route
  upload still proceeds. Success surfaces as `agps_bytes=<N>` in the
  RESULT line.
* `BuildConfig.AGPS_TOKEN` (sourced from `LIGPSPORT_AGPS_TOKEN` at
  build time) is now optional — when blank, `AgpsClient` falls back
  to iGPSport's backend. Use the override when you have your own
  u-blox AssistNow developer token and want to avoid the runtime
  dependency on `prod.en.igpsport.com`:
  ```sh
  LIGPSPORT_AGPS_TOKEN=YOUR_UBLOX_TOKEN nix run .#build-debug
  ```

### 12.5 Headless test harness

The adb-driven e2e suite exposes a standalone broadcast:

```sh
adb shell am broadcast \
  -n de.syntaxfehler.ligpsport.debug/de.syntaxfehler.ligpsport.cli.AdbCliReceiver \
  -a de.syntaxfehler.ligpsport.action.SEND_AGPS \
  --es req_id "$RANDOM"
```

emits

```
LigpsportAdb: RESULT action=SEND_AGPS req_id=… status=OK
              name=BSC200 mac=… agps_bytes=3014 device_status=0
```

If `LIGPSPORT_AGPS_TOKEN` was unset at build time the broadcast
returns `status=FAIL reason="no AGPS token — set LIGPSPORT_AGPS_TOKEN
at build time (see docs/PROTOCOL.md §10)"`. The e2e runner treats
this failure as non-fatal so CI doesn't depend on a u-blox token.

`PLAN_AND_UPLOAD` and `UPLOAD` RESULT lines gain an `agps_bytes=<N>`
field when the piggybacked seed succeeds. Absence of the field means
the AGPS step was skipped — no token, no network, or device rejection.
The route upload itself succeeds or fails independently.

## 13. Position-prior injection (FACTORY GPS_COORDINATE_SET)

AGPS supplies "which satellite is where in orbit" via UBX-MGA-EPH;
the BSC200's chip still has to figure out **which satellites are
visible from where I am** before it can fix. A receiver that doesn't
know its rough position has to do a sky search; one that has a hint
within ~100 km can drop straight into search of the visible subset.
The official app sends the phone's current fix to the device for
exactly this reason — short message, big TTFF win when paired with
AGPS.

The proto and the operation type are buried in `factory.proto`,
which sounds factory-only. It is not: the production app calls this
from `BroadcastViewModel` (live-tracking group rides) and
`RecordingViewModel` (recording flow) — see the smali list in
§13.4. Despite the FACTORY service number this command is not
destructive; it just hands the receiver a lat/lon hint.

### 13.1 Wire format

A plain PbFrame on the Control channel:

| header field | value                                              |
|--------------|----------------------------------------------------|
| `service`    | 11 (`FACTORY`)                                     |
| `operation`  | 8 (`FACTORY_OPERATE_TYPE_GPS_COORDINATE_SET`)      |
| payload      | `factory_msg{service_type=11, factory_operate_type=8, gps_coordinate_msg={lat, lon}}` |

Total wire size for the body is **24 bytes** of protobuf — the entire
frame fits in one BLE write at any negotiated MTU.

The device replies with a `ConfirmFrame` on the **Data** channel
(matching the BSC200 control/reply split documented in §1), `status=0`
on success.

### 13.2 Proto schemas

`factory.proto` (excerpt):

```protobuf
enum FACTORY_OPERATE_TYPE {
  enum_FACTORY_OPERATE_TYPE_NONE              = 0;
  enum_FACTORY_OPERATE_TYPE_SN_GET            = 1;
  // ...
  enum_FACTORY_OPERATE_TYPE_GPS_COORDINATE_SET = 8;   // 设置GPS坐标
  // ...
}

message gps_coordinate_message {
  optional double latitude  = 1;  // 0 means "no fix"
  optional double longitude = 2;  // 0 means "no fix"
}

message factory_msg {
  required service_type_index   service_type         = 1 [default = enum_SERVICE_TYPE_INDEX_FACTORY];
  required FACTORY_OPERATE_TYPE factory_operate_type = 2 [default = enum_FACTORY_OPERATE_TYPE_NONE];
  // ...
  optional gps_coordinate_message gps_coordinate_msg = 9;
  // ...
}
```

### 13.3 Hand-rolled encoding

```
factory_msg outer:
  field 1 (service_type)         varint = 11  → 08 0B
  field 2 (factory_operate_type) varint = 8   → 10 08
  field 9 (gps_coordinate_msg)   len-delim    → 4A 12 <18 bytes>

gps_coordinate_message inner:
  field 1 (latitude)  fixed64 (double LE)     → 09 <8 bytes>
  field 2 (longitude) fixed64 (double LE)     → 11 <8 bytes>
```

Total payload: 24 bytes. Lat/lon are IEEE-754 doubles in
little-endian — the protobuf spec requires LE for `fixed64`
regardless of platform.

### 13.4 Reverse-engineering provenance

- `IGPDeviceManager.setCoordinate(gps_coordinate_message)` — public
  method at `classes4.dex` line ~26349. Returns a `BaseCommand<Boolean>`,
  routes via `firstQueue` (the Control channel).
- `ManufacturerServiceFactory.getMessage(operationType, …)` —
  `classes4.dex` line ~148. Builds `factory_msg` from the per-field
  options, calls `setMainCommandByte(operationType.number)` to stamp
  the header.
- Call sites in the production app (`grep -rln "setCoordinate"
  tmp/smali-*`):
  - `com.igpsport.globalapp.broadcast.fragment.BroadcastCyclingFragment`
  - `com.igpsport.globalapp.broadcast.model.BroadcastViewModel`
  - `com.igpsport.globalapp.devicemodule.sportsdetail.model.SportsDetailViewModel`
  - `com.igpsport.globalapp.record.fragment.RecordingMapFragment`
  - `com.igpsport.globalapp.record.model.RecordingViewModel`

None of those are factory-test screens — every one is a normal
end-user flow.

### 13.5 Reference Kotlin implementation (`ligpsport-android`)

- `ble/LocationInjector.kt#setCoordinate(transport, lat, lon)` —
  builds the 24-byte protobuf with hand-rolled fixed64 encoding,
  wraps in a `Frame(service=FACTORY, operation=GPS_COORDINATE_SET)`,
  awaits the ConfirmFrame.
- `ble/UploadPipeline.kt#sendCurrentLocation(context)` — public entry:
  resolves the phone's location via
  `MockLocationStore → FusedLocationProviderClient.getCurrentLocation
  → lastLocation`, opens the paired transport, calls
  `LocationInjector.setCoordinate`.
- `ble/UploadPipeline.kt#injectCurrentLocationBestEffort` — internal
  helper piggybacked onto every `uploadGpx` call, sandwiched between
  the AGPS step (§12.4) and the route upload. Failure is silent;
  success surfaces as `seed_lat=` + `seed_lon=` fields on the RESULT
  line.

### 13.6 Headless test harness

The adb-driven e2e suite exposes a standalone broadcast:

```sh
adb shell am broadcast \
  -n de.syntaxfehler.ligpsport.debug/de.syntaxfehler.ligpsport.cli.AdbCliReceiver \
  -a de.syntaxfehler.ligpsport.action.SEND_LOCATION \
  --es req_id "$RANDOM"
```

emits

```
LigpsportAdb: RESULT action=SEND_LOCATION req_id=… status=OK
              name=BSC200 mac=… seed_lat=48.77000 seed_lon=9.18000 device_status=0
```

Use `MOCK_LOCATION --ef lat --ef lon` first if the test rig has no
real GPS fix. `PLAN_AND_UPLOAD` / `UPLOAD` lines gain
`seed_lat=` + `seed_lon=` when the piggybacked injection succeeds;
absence means the step was skipped (no fix or device rejection).
