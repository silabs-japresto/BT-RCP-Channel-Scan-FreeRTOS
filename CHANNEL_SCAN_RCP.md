# Channel energy scan on the Bluetooth HCI RCP

This project starts from the Simplicity SDK 2026.6.2 `bt_rcp_freertos` example,
retargeted to the BRD2601B xG24 Dev Kit. It adds a 2.4 GHz channel energy scan
that a host controls with vendor-specific HCI commands. Those commands use the
same UART as normal HCI traffic.

The scan uses its own RAIL handle (`sl_rail_util_init` inst0, BLE 1 Mbps PHY,
`rail_lib_multiprotocol`). It takes hardware RSSI averages at scheduler
priority 250, so the Bluetooth link layer always wins the radio.

The commands, their wire format and how they plug into the controller are
described in [CHANNEL_SCAN_COMMANDS.md](CHANNEL_SCAN_COMMANDS.md). This file
covers the host tools, building, flashing and test status.

## Vendor commands (OGF 0x3F), quick reference

| Opcode | Name | Parameters | Response (after status) |
|---|---|---|---|
| 0xFF00 | Get Info | — | version u8, num_channels u8, running u8, report_seq u16, max_channels_per_read u8 |
| 0xFF01 | Start | none (defaults), or passes u16, averaging_us u16, interval_ms u32, busy_threshold i8 | — |
| 0xFF02 | Stop | — | — |
| 0xFF03 | Read Report | first_channel u8, count u8 | report_seq u16, passes u16, first u8, count u8, count × {min i8, avg i8, max i8, busy% u8} |
| 0xFF04 | Enter Bootloader | — | — (reboots into the UART XMODEM bootloader ~50 ms later) |

## Host script

```sh
pip install pyserial
python3 tools/rcp_channel_scan.py -p /dev/tty.usbmodemXXXX info
python3 tools/rcp_channel_scan.py -p /dev/tty.usbmodemXXXX watch            # table per report
python3 tools/rcp_channel_scan.py -p /dev/tty.usbmodemXXXX watch --chscan   # one CSV line per report
python3 tools/rcp_channel_scan.py -p /dev/tty.usbmodemXXXX start --passes 10 --interval-ms 1000
python3 tools/rcp_channel_scan.py -p /dev/tty.usbmodemXXXX stop
python3 tools/rcp_channel_scan.py -p /dev/tty.usbmodemXXXX update app.gbl   # serial firmware update
```

On a Raspberry Pi the kit shows up as `/dev/ttyACM0`.

`--chscan` prints each report as
`CHSCAN,<version>,<passes>,<n>,{min,avg,max,busy}×n`, which is easy to log or
pipe into another tool.

**Flow control:** the BRD2601B VCOM has no RTS line. This build therefore turns
off hardware flow control (in `config/sl_uartdrv_usart_vcom_config.h` and the
`.slcp`), and the script runs without it by default. On kits that have both
lines, turn flow control back on and pass `--rtscts`.

The script opens the serial port itself, so it can't share the port with a host
stack such as BlueZ. With a host stack attached, send the same opcodes through
the stack instead. On Linux, for example, `hcitool -i hci0 cmd 0x3f 0x300`
sends Get Info.

## Terminal UI

`tools/rcp_channel_scan_tui.py` is a curses viewer in the style of btop. It
needs only Python 3 and pyserial, and it works over SSH at 80×24 or larger,
with 256 colors when the terminal supports them. It shows:
- the energy min–max band and average, with the busy-threshold line
- busy %
- a Wi-Fi 1/6/11 overlay
- a waterfall
- a channel cursor and peak hold

You can also start and stop the scan from the TUI and change its parameters.

```sh
python3 tools/rcp_channel_scan_tui.py -p /dev/ttyACM0                 # RCP over HCI
python3 tools/rcp_channel_scan_tui.py --source chscan -p <port>       # read CHSCAN CSV lines
python3 tools/rcp_channel_scan_tui.py --source demo                   # no hardware
```

Keys:

| Key | Action |
|---|---|
| space | Start/stop the scan |
| `+` / `-` | Passes per report |
| `[` / `]` | Averaging time |
| `i` | Interval |
| `t` / `T` | Busy threshold |
| ← / → | Select channel |
| `v` | Waterfall shows max, avg or min |
| `w` | Wi-Fi overlay |
| `h` | Peak hold |
| `c` | Clear |
| `p` | Pause |
| `q` | Quit |

`--floor` and `--ceil` set the dBm scale. The BRD2601B noise floor sits near
−110 dBm, so `--floor -115` helps. Short bursts raise busy % and max much more
than the average, so peak hold (`h`) and the max waterfall (`v`) show activity
best.

## Tests

`tools/test_rcp.py -p PORT basic|coexist|connect|all` runs the hardware checks
and exits with a non-zero code on failure. `connect` needs a connectable
advertiser nearby. Pass its name with `--name` (default "Blinky Example").

Host stacks must enable LE meta events (`HCI_Set_Event_Mask` bit 61 and
`HCI_LE_Set_Event_Mask`). Without them, this controller never sends LE
Connection Complete, even though the link is set up. BlueZ and other stacks do
this at init. Raw-HCI scripts have to do it themselves (`test_rcp.py` does).

### BlueZ

```sh
sudo btattach -B /dev/ttyACM0 -S 115200 -P h4 &     # kit becomes hciN (hci1 on a Pi)
python3 tools/test_bluez.py --hci hci1
sudo pkill -x btattach                              # free the port for the scripts/TUI
```

To run these without a password prompt, create
`/etc/sudoers.d/rcp-channel-scan` with `visudo -f` and add this line:

```
<user> ALL=(root) NOPASSWD: /usr/bin/btattach, /usr/bin/hciconfig, /usr/bin/hcitool, /usr/bin/btmgmt, /usr/bin/pkill -x btattach
```

## Build and flash

Following the Simplicity Studio 6 `.gitignore` recommendations, the repository
doesn't track generated files:
- `autogen/`
- the copied `simplicity_sdk_2026.6.2/`
- the generated CMake files (`*.cmake`, `CMakePresets.json`)

After cloning, open the project in Simplicity Studio 6 (or run `slc generate`
on `bt_rcp_freertos.slcp` with SDK 2026.6.2 installed) to regenerate them.
Then build:

```sh
cd bt_rcp_freertos_cmake
NINJA_EXE_PATH=$(which ninja) cmake --preset project   # SDK-bundled ninja fails on this Mac
cmake --build --preset default_config
```

The kit uses a UART XMODEM bootloader (`bootloader-uart-xmodem` built for the
BRD2601B). The bootloader sits at 0x08000000 and the app at 0x08006000. Flash
the bootloader once over the debugger, then flash the app:

```sh
commander flash --serialno <jlink> <path>/bootloader-uart-xmodem.s37
commander flash --serialno <jlink> bt_rcp_freertos_cmake/build/default_config/bt_rcp_freertos.s37
```

After that, update over serial only:

```sh
commander gbl create app.gbl --app bt_rcp_freertos_cmake/build/default_config/bt_rcp_freertos.s37
python3 tools/rcp_channel_scan.py -p /dev/ttyACM0 update app.gbl
```

**Recovery:** if an update leaves no app that runs, the bootloader stays at its
menu. Run `update app.gbl --in-bootloader` in that case. You can also enter the
bootloader by holding BTN0 (PB2) during reset.

## Test status and open questions

**Not confirmed yet:**
- Which context the vendor HCI handler runs in. The handler only masks
  interrupts briefly and never blocks, so either context is safe.

**BRD2601B on a Mac, 2026-11-13** (no other boards running). These worked:
- all vendor commands
- rejection of invalid parameters
- HCI_Reset not stopping the scan
- stop/start
- interval mode
- LE scanning at the same time as the channel scan
- serial update

**Raspberry Pi 5, Ubuntu 24.04** (kit on `/dev/ttyACM0`, 2026-11-13):
- **Raw HCI:** all 17 basic checks pass, and LE scanning works with the channel
  scan on.
- **Connection:** a 60 s LE connection to a peripheral survived with the channel
  scan running (RSSI −62 to −53 dBm). The scan kept 4.2 reports/s, close to the
  4.4/s idle rate.
- **Serial update:** took 30 s.
- **BlueZ 5.72** (kit attached as `hci1` by `btattach`):
  - All 13 checks in `tools/test_bluez.py` pass.
  - Vendor commands work through `hcitool cmd 0x3f`.
  - LE discovery found 10 devices with the channel scan running.
  - A connection held 30 s with the channel scan running, with vendor commands
    polled during the link. The scan kept 4.3 reports/s.

**Possible overhead on LE scanning:** with the channel scan running, a 5 s LE
scan got about 9–11% fewer advertising reports (124→113 at 100% duty, 73→65 at
50%). That could be ambient variation or real overhead from switching the
radio. A 100%-duty LE scan leaves almost no radio time for the channel scan, as
expected.
