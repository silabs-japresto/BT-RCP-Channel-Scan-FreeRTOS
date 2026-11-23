# Channel scan vendor HCI commands

This project adds five vendor-specific HCI commands to the Silicon Labs
Bluetooth HCI RCP. With them, a host can measure how busy the 2.4 GHz band is
on each of the 40 BLE RF channels while the controller keeps working as a
normal Bluetooth controller.

For the host tools, the build steps, and the test results, see
[CHANNEL_SCAN_RCP.md](CHANNEL_SCAN_RCP.md).

## What it does

The scan repeatedly sweeps the 40 BLE RF channels. RF channel *k* is centered
on 2402 + 2·*k* MHz, so channel 0 is 2402 MHz and channel 39 is 2480 MHz.

On each channel the radio takes a hardware RSSI average over a short window
(128 µs by default). It does that without transmitting or decoding anything. A
group of sweeps (20 by default) makes one **report**. A report holds four
numbers for each channel:

| Field | Meaning |
|---|---|
| `min_dbm` | Quietest sample: roughly the noise floor |
| `avg_dbm` | Mean of the samples, in dBm |
| `max_dbm` | Strongest sample: the biggest burst seen |
| `busy_pct` | Share of samples above the busy threshold (−71 dBm by default, the Bluetooth AFH default) |

Busy % and max respond best to bursty traffic such as Wi-Fi frames or
BLE advertising, because most samples land between bursts and pull the average
toward the noise floor. Uses include:
- picking quiet channels
- checking a site for Wi-Fi interference
- deciding which channels to leave out of a channel map

The scan starts idle at boot. The host starts and stops it.

## How the commands fit into the RCP

### Transport

Vendor commands are ordinary HCI command packets with OGF 0x3F. They use the
same UART H4 link (115200 8N1) and the same controller command path as standard
HCI commands. The controller answers each one with a normal HCI Command
Complete event. Because of that, they work both from a raw serial script and
through a host stack such as BlueZ (`hcitool cmd 0x3f <ocf>`).

The opcode is `(OGF << 10) | OCF`:

| Opcode | OGF | OCF | Name |
|---|---|---|---|
| 0xFF00 | 0x3F | 0x300 | Get Info |
| 0xFF01 | 0x3F | 0x301 | Start |
| 0xFF02 | 0x3F | 0x302 | Stop |
| 0xFF03 | 0x3F | 0x303 | Read Report |
| 0xFF04 | 0x3F | 0x304 | Enter Bootloader |

Example: Get Info over raw H4, followed by its Command Complete event.

```
Host → controller:  01  00 FF  00
                    │   │      └ parameter length 0
                    │   └ opcode 0xFF00, little endian
                    └ H4 command packet

Controller → host:  04  0E  0A  01  00 FF  00  01 28 01 05 00 0A
                    │   │   │   │   │      │   └ return parameters
                    │   │   │   │   │      └ status 0x00 (success)
                    │   │   │   │   └ opcode 0xFF00
                    │   │   │   └ Num_HCI_Command_Packets
                    │   │   └ event parameter length
                    │   └ Command Complete
                    └ H4 event packet
```

The return parameters in that example are:
- format version 1
- 40 channels
- running
- `report_seq` = 5
- 10 channels per read

### Firmware wiring

The base RCP example already has the controller, the HCI UART transport and
FreeRTOS. The scan adds three pieces on top of that.

1. **Vendor command handler.** `app_init()` calls `channel_scan_init()`, which
   registers `hci_vendor_handler()` with `sl_btctrl_hci_register_handler()`
   (from `sl_btctrl_hci_handler.h`). The controller offers every incoming HCI
   command to this handler before its own command table.
   - For 0xFF00–0xFF04, the handler reads the parameters with
     `sl_btctrl_hci_message_get_parameters()`, fills in the reply with
     `sl_btctrl_hci_message_set_response()` and returns `true`.
   - For any other opcode it returns `false`. The controller then handles the
     command as usual, including "Unknown HCI Command" for vendor opcodes it
     doesn't know.

   The handler never blocks. It copies shared state with interrupts briefly
   masked. Start, Stop and Enter Bootloader only record a request and wake the
   scan task.

2. **A second RAIL handle.** The `sl_rail_util_init` component (instance
   `inst0`, BLE 1 Mbps PHY) gives the scan its own radio handle. The
   `rail_lib_multiprotocol` library lets that handle share the radio with the
   Bluetooth link layer. RAIL's scheduler decides who gets the radio:
   - Each measurement asks for the radio at priority 250 with a 20 ms slip
     time.
   - The Bluetooth controller always uses a higher priority (a lower number).
   - So advertising, scanning and connection events always come first. The
     scan only fills in the gaps.

   A measurement that gets preempted or doesn't finish within 50 ms is
   dropped, and the sweep moves on.

3. **Scan task.** A FreeRTOS task (`chscan_task`, 256 words, priority 20) runs
   the sweeps:
   - It calls `sl_rail_start_average_rssi()` for each channel.
   - It waits for a task notification from `sl_rail_util_on_event()`, which
     handles the `RSSI_AVERAGE_DONE` and `SCHEDULER_STATUS` events.
   - It adds each sample to the running min, max, sum and busy count for that
     channel.

   When a report is complete, the task copies it into a published buffer and
   increments `report_seq`. Read Report always serves that published copy, so
   the host never sees a half-finished sweep. While idle, or between reports in
   interval mode, the task blocks, so the device can sleep.

Files:
- `channel_scan.c`: the handler, the task and the RAIL callback.
- `channel_scan.h`: opcodes and the report layout.
- `channel_scan_config.h`: default values.

Project setup in the `.slcp` file:
- Components: `bootloader_interface`, `rail_lib_multiprotocol`,
  `sl_rail_util_init` (`inst0`).
- `SL_RAIL_UTIL_INIT_PROTOCOL_INST0_DEFAULT = SL_RAIL_UTIL_PROTOCOL_BLE_1MBPS`.
- The VCOM flow-control setting. It's off because the BRD2601B has no RTS line.

### RAIL APIs used

The scan makes only a handful of RAIL calls, all on its own inst0 handle. The
Bluetooth controller's handle is never touched. Details of each call are in the
[RAIL API reference](https://docs.silabs.com/rail/latest/rail-api/).

| Step | API | Where | What it does |
|---|---|---|---|
| Get the handle | `sl_rail_util_get_handle(SL_RAIL_UTIL_HANDLE_INST0)` | Scan task start | Returns the handle that `sl_rail_util_init` (inst0) created at boot with the BLE 1 Mbps PHY. RAIL channel *k* is BLE RF channel *k*. |
| Enable events | `sl_rail_config_events(h, SL_RAIL_EVENTS_ALL, SL_RAIL_EVENT_RSSI_AVERAGE_DONE \| SL_RAIL_EVENT_SCHEDULER_STATUS)` | Scan task start, once | Turns on only the two events the scan needs. The event options in `sl_rail_util_init_inst0_config.h` stay at 0. |
| Start a sample | `sl_rail_start_average_rssi(h, channel, averaging_us, &scheduler_info)` | `measure_channel()`, once per channel per pass | Asks the RAIL scheduler for the radio, tunes to the channel and averages RSSI for `averaging_us` without transmitting. |
| Get notified | `sl_rail_util_on_event(h, events)`, interrupt context | Callback implemented in `channel_scan.c` | `RSSI_AVERAGE_DONE` means the average is ready. `SCHEDULER_STATUS` means the scheduler preempted or refused the sample. Both end the sample. |
| Read the value | `sl_rail_get_average_rssi(h)` | Inside the callback | Returns the average in quarter-dBm (`int16_t`), or `SL_RAIL_RSSI_INVALID`. |
| Release the radio | `sl_rail_yield_radio(h)` | Callback, and the timeout path | Ends the scheduled operation so Bluetooth or the next sample can use the radio. |
| Abort on timeout | `sl_rail_idle(h, SL_RAIL_IDLE_ABORT, true)` | `measure_channel()`, after 50 ms with no event | Cancels an operation that never finished, then yields the radio. |

**Scheduler parameters.** `sl_rail_start_average_rssi()` takes these
`sl_rail_scheduler_info_t` values:

| Field | Value | Effect |
|---|---|---|
| `priority` | 250 (`CHANNEL_SCAN_RAIL_PRIORITY`) | Numerically above every Bluetooth controller priority (lower number = higher priority), so Bluetooth always preempts the scan |
| `slip_time` | 20000 µs (`CHANNEL_SCAN_SLIP_TIME_US`) | How long the sample may wait for the radio before the scheduler gives up |
| `transaction_time` | `averaging_us` | How long the sample holds the radio once it starts |

**One sample, step by step:**

1. The scan task increments a sequence number, clears its pending task
   notification and calls `sl_rail_start_average_rssi()`. If the call fails,
   the task waits one tick and the sample is dropped.
2. The RAIL scheduler runs the average when the radio is free. Bluetooth
   activity can delay it by up to the slip time, or preempt it.
3. `sl_rail_util_on_event()` runs in interrupt context. On `RSSI_AVERAGE_DONE`
   it reads `sl_rail_get_average_rssi()`. Then, for either event, it calls
   `sl_rail_yield_radio()`. It packs the value, a valid flag and the sequence
   number into one word and sends it to the scan task with
   `xTaskNotifyFromISR()`.
4. The task waits up to 50 ms for a notification whose sequence number matches
   the sample. This throws away late results from an earlier sample. On
   timeout it calls `sl_rail_idle(…, SL_RAIL_IDLE_ABORT, …)` and
   `sl_rail_yield_radio()`, and drops the sample.
5. A valid sample updates that channel's running min, max and sum, all in
   quarter-dBm. If the value is above `busy_threshold_dbm × 4`, the channel's
   busy count goes up.

**Units.** RAIL reports RSSI in quarter-dBm. The scan keeps everything in
quarter-dBm and converts to whole dBm (`int8_t`) only when it publishes a
report. A channel that got no valid samples reports `SL_RAIL_RSSI_INVALID_DBM`
in its dBm fields.

**Why a multiprotocol RAIL library.** `rail_lib_multiprotocol` allows more than
one RAIL handle on the radio. The RAIL scheduler then arbitrates between them
by priority. Without it, the scan would need the Bluetooth controller's own
handle and couldn't run while Bluetooth is active.

## Command reference

All multi-byte fields are little endian. Every response starts with the HCI
status byte. On errors the response contains only the status:
- 0x12 Invalid HCI Command Parameters
- 0x1F Unspecified Error

### 0xFF00 Get Info

Reports what the device supports and what it's doing.

- **Parameters:** none.
- **Response (6 bytes after the status):**

| Field | Type | Notes |
|---|---|---|
| `format_version` | u8 | Report format, currently 1 |
| `num_channels` | u8 | 40 |
| `state` | u8 | 0 = idle, 1 = running |
| `report_seq` | u16 | Increments with each finished report. 0 means no report yet. |
| `max_channels_per_read` | u8 | Most channels one Read Report returns (10) |

Hosts can poll `report_seq` cheaply to find out when a new report is ready.

### 0xFF01 Start

Starts the scan, or changes its parameters if it's already running.

- **Parameters:** send either
  - nothing, to use the defaults, or
  - all 9 bytes below.
- **Response:** status only.

| Field | Type | Default | Limits |
|---|---|---|---|
| `passes` | u16 | 20 | ≥ 1. Sweeps per report. |
| `averaging_us` | u16 | 128 | 1–10000. RSSI averaging window per sample. |
| `interval_ms` | u32 | 0 | Idle time between reports. 0 = scan continuously. |
| `busy_threshold_dbm` | i8 | −71 | A sample above this counts as busy |

Any other parameter length, or a value out of range, returns 0x12.

If the scan is already running, the new parameters take effect immediately.
The report in progress is thrown away and a new one starts.

How long a report takes is roughly `passes × 40 × (averaging_us + overhead)`,
and longer while Bluetooth is using the radio. The defaults give about four
reports per second when Bluetooth is idle.

### 0xFF02 Stop

Stops the scan and releases the radio.

- **Parameters:** none.
- **Response:** status only.

The last finished report stays readable. HCI_Reset doesn't stop the scan, so
use this command.

### 0xFF03 Read Report

Reads part of the most recent finished report.

- **Parameters:**

| Field | Type | Notes |
|---|---|---|
| `first_channel` | u8 | 0–39 |
| `count` | u8 | ≥ 1. Capped at 10 and at the last channel. |

- **Response:**

| Field | Type |
|---|---|
| `report_seq` | u16 |
| `passes` | u16 |
| `first_channel` | u8 |
| `count` | u8 (the number actually returned) |
| `count` × stats | `min_dbm` i8, `avg_dbm` i8, `max_dbm` i8, `busy_pct` u8 |

A channel with no valid samples reports `SL_RAIL_RSSI_INVALID_DBM` in all three
dBm fields and 0 busy.

**Paging:** the controller limits a command response to
`SL_BTCTRL_MAX_HCI_RESPONSE_LENGTH` (50 bytes). One response therefore carries
a 6-byte header plus 10 channels × 4 bytes = 46 bytes. To read a full report:
1. Read channels 0, 10, 20 and 30.
2. Check that `report_seq` is the same on all four pages.
3. If it isn't, a new report was published in the middle, so read all four
   pages again.

### 0xFF04 Enter Bootloader

Reboots into the UART XMODEM bootloader so new firmware can be loaded over the
same serial port.

- **Parameters:** none.
- **Response:** status only.

The scan stops. About 50 ms after the Command Complete, so that it has time to
leave the UART, the device calls `bootloader_rebootAndInstall()`. The
bootloader then waits for a GBL upload at 115200 8N1.
`tools/rcp_channel_scan.py update app.gbl` sends this command and does the
whole upload.

## Notes for host developers

- **Enable LE meta events.** Send `HCI_Set_Event_Mask` with bit 61 set and
  `HCI_LE_Set_Event_Mask` before you use standard LE features alongside the
  scan. The vendor commands themselves don't need this.
- **Radio sharing:** the scan never delays Bluetooth traffic, but heavy
  Bluetooth activity slows the scan down. A 100%-duty LE scan leaves it almost
  no radio time.
- **Absolute levels depend on the board.** Compare channels on the same kit by
  their shape, not by absolute dBm.
