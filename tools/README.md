# Channel scan host tools

These Python scripts drive the channel scan vendor HCI commands on the
`bt_rcp_freertos` RCP. The commands are documented in
[../CHANNEL_SCAN_COMMANDS.md](../CHANNEL_SCAN_COMMANDS.md). Building and
flashing are covered in [../CHANNEL_SCAN_RCP.md](../CHANNEL_SCAN_RCP.md).

## Setup

You need Python 3.8 or newer.

```sh
cd tools
python3 -m venv .venv && . .venv/bin/activate    # optional
pip install -r requirements.txt
```

The only third-party package is `pyserial`. On Windows, `windows-curses` is
also installed for the TUI.

**Serial port:**

| Platform | Port |
|---|---|
| Raspberry Pi / Linux | `/dev/ttyACM0` |
| macOS | `/dev/tty.usbmodem…` |
| Windows | `COMn` |

On Linux, add yourself to the `dialout` group if opening the port fails with a
permission error: `sudo usermod -aG dialout $USER`, then log out and back in.

**Flow control:** the defaults are 115200 8N1 with no flow control, which
matches the BRD2601B. Pass `--rtscts` on kits that have both RTS and CTS
wired and have hardware flow control enabled in the firmware.

**Port sharing:** `rcp_channel_scan.py`, `rcp_channel_scan_tui.py` and
`test_rcp.py` open the serial port directly. They can't run while BlueZ
(`btattach`) or another host stack has the port. Use `test_bluez.py` for the
BlueZ case.

## Scripts

| Script | What it does |
|---|---|
| `rcp_channel_scan.py` | Command-line client and the shared `Rcp` class |
| `rcp_channel_scan_tui.py` | Live terminal viewer (curses) |
| `test_rcp.py` | Hardware tests over raw HCI |
| `test_bluez.py` | Hardware tests with BlueZ owning the controller (Linux) |
| `capture_json.py` | Scratch script: records 20 s of reports as JSON |

### `rcp_channel_scan.py`: command-line client

```sh
python3 rcp_channel_scan.py -p PORT info                 # version, channels, state, report_seq
python3 rcp_channel_scan.py -p PORT start                # start with the firmware defaults
python3 rcp_channel_scan.py -p PORT start --passes 10 --averaging-us 256 \
                                          --interval-ms 1000 --threshold -75
python3 rcp_channel_scan.py -p PORT read                 # print the latest report once
python3 rcp_channel_scan.py -p PORT watch                # start, then print a table per new report
python3 rcp_channel_scan.py -p PORT watch --chscan       # one CSV line per report
python3 rcp_channel_scan.py -p PORT stop
python3 rcp_channel_scan.py -p PORT bootloader           # reboot into the XMODEM bootloader
python3 rcp_channel_scan.py -p PORT update app.gbl       # bootloader + upload + run
python3 rcp_channel_scan.py -p PORT update app.gbl --in-bootloader   # device already at the bootloader menu
```

**Common options:**
- `-b BAUD` (default 115200)
- `--rtscts`
- `--no-reset`: skip the HCI_Reset sent on connect

**`watch` options:**
- `--poll-ms` (default 200)
- `--no-start`: watch a scan that's already running, without restarting it

The `--chscan` line format is:

```
CHSCAN,<version>,<passes>,<n>,<min>,<avg>,<max>,<busy>,...   (n groups of 4)
```

**Using it from Python:** other scripts import this module.

```python
import rcp_channel_scan as r
rcp = r.Rcp("/dev/ttyACM0", 115200, False)   # port, baud, rtscts
rcp.reset(); rcp.start(passes=20)
info = rcp.info()                             # dict with report_seq, num_channels, ...
seq, passes, stats = rcp.read_report()        # stats: 40 x (min, avg, max, busy)
rcp.stop()
```

### `rcp_channel_scan_tui.py`: terminal viewer

```sh
python3 rcp_channel_scan_tui.py -p PORT                       # live, controls the scan
python3 rcp_channel_scan_tui.py -p PORT --floor -115 --ceil -50
python3 rcp_channel_scan_tui.py --source chscan -p PORT       # view CHSCAN lines from a serial port
python3 rcp_channel_scan_tui.py --source demo                 # synthetic data, no hardware
```

The TUI works over SSH (use `ssh -t`). It needs a terminal of at least 80×24
and uses 256 colors when the terminal supports them.

**Options:**

| Option | Default | Meaning |
|---|---|---|
| `--threshold` | −71 | Busy threshold, in dBm |
| `--floor` / `--ceil` | −110 / −20 | Bottom and top of the dBm scale |
| `--poll-ms` | 100 | How often the TUI polls the RCP |
| `--no-start` | off | Don't send Start on connect |
| `--no-reset` | off | Don't send HCI_Reset on connect |

**What the graphs show:**
- **Top graph (energy):** a solid bar for the average, light `░` shading for the
  min–max band, a red dashed line for the busy threshold, magenta `▔` for peak
  hold and dim yellow edges for Wi-Fi channels.
- **Middle graph:** busy %.
- **Bottom:** a waterfall of past reports.

Bursty traffic shows up mostly in busy % and in the `░` band. A narrower scale
(`--floor -115 --ceil -50`) makes the band easier to see.

**Keys:**

| Key | Action |
|---|---|
| space | Start/stop the scan |
| `+` / `-` | Passes per report |
| `[` / `]` | Averaging time |
| `i` | Cycle the report interval |
| `t` / `T` | Busy threshold −/+ 1 dB |
| ← / → | Select channel |
| `v` | Waterfall shows max, avg or min |
| `w` | Wi-Fi overlay |
| `h` | Peak hold |
| `c` | Clear history and peaks |
| `p` | Pause |
| `q` | Quit |

### `test_rcp.py`: hardware tests over raw HCI

```sh
python3 test_rcp.py -p PORT basic      # vendor commands, parameter checks, HCI_Reset behavior
python3 test_rcp.py -p PORT coexist    # LE scan with and without the channel scan running
python3 test_rcp.py -p PORT connect --name "My Peripheral" --secs 30
python3 test_rcp.py -p PORT all --name "My Peripheral"
```

`connect` and `all` need a connectable advertiser nearby, found by its
advertised name (`--name`, default "Blinky Example"). The script exits with a
non-zero code if any check fails, so you can use it in CI or in scripts.

### `test_bluez.py`: BlueZ tests (Linux)

Use this to check the RCP while BlueZ owns it.

```sh
sudo btattach -B /dev/ttyACM0 -S 115200 -P h4 &    # kit appears as hciN
python3 test_bluez.py --hci hci1 --peer AA:BB:CC:DD:EE:FF --secs 30
sudo pkill -x btattach                             # give the port back to the other scripts
```

The script sends the vendor commands through `sudo -n hcitool -i hciN cmd 0x3f …`
and uses `bluetoothctl` for discovery and connections. It needs:
- BlueZ, with `hcitool` and `bluetoothctl` installed
- sudo without a password prompt for these tools (see the sudoers line in
  [../CHANNEL_SCAN_RCP.md](../CHANNEL_SCAN_RCP.md#bluez))
- `--peer` set to the address of a connectable device. The default is a
  specific test board, so change it.

### `capture_json.py`: scratch capture

This script records about 20 s of reports and prints them as a JSON list, one
40-channel list of `[min, avg, max, busy]` per report. The port is hardcoded to
`/dev/ttyACM0`, so edit it before you use it.

```sh
python3 capture_json.py > capture.json
```
