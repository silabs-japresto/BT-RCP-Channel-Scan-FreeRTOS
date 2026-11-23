#!/usr/bin/env python3
"""Host tool for the bt_rcp_channel_scan_freertos vendor HCI commands.

Talks raw H4 HCI over the RCP's UART (115200 8N1). Hardware flow control is
off by default because the BRD2601B VCOM has no RTS line; use --rtscts on kits
that support it.
Requires: pip install pyserial

Examples:
  rcp_channel_scan.py -p /dev/tty.usbmodem0004401234 info
  rcp_channel_scan.py -p PORT start --passes 10 --interval-ms 1000
  rcp_channel_scan.py -p PORT watch            # print a table per new report
  rcp_channel_scan.py -p PORT watch --chscan   # print CHSCAN,... CSV lines
  rcp_channel_scan.py -p PORT stop
  rcp_channel_scan.py -p PORT update app.gbl   # reboot to XMODEM bootloader, upload, run

Do not run this while a Bluetooth host stack (BlueZ, etc.) owns the same port.
"""
import argparse
import struct
import sys
import time

import serial

OP_GET_INFO = 0xFF00
OP_START = 0xFF01
OP_STOP = 0xFF02
OP_READ_REPORT = 0xFF03
OP_ENTER_BOOTLOADER = 0xFF04
OP_RESET = 0x0C03

H4_CMD, H4_ACL, H4_EVT = 0x01, 0x02, 0x04
EVT_CMD_COMPLETE, EVT_CMD_STATUS = 0x0E, 0x0F
NUM_CHANNELS = 40


class HciError(Exception):
    pass


class Rcp:
    def __init__(self, port, baud, rtscts, timeout=2.0):
        self.ser = serial.Serial(port, baud, rtscts=rtscts, timeout=timeout)
        self.timeout = timeout

    def _read_exact(self, n):
        data = self.ser.read(n)
        if len(data) != n:
            raise HciError("timeout reading from controller")
        return data

    def _read_packet(self):
        while True:
            ptype = self._read_exact(1)[0]
            if ptype == H4_EVT:
                code, plen = self._read_exact(2)
                return code, self._read_exact(plen)
            if ptype == H4_ACL:
                _, dlen = struct.unpack("<HH", self._read_exact(4))
                self._read_exact(dlen)   # ignore ACL data
                continue
            # Unknown byte: out of sync, skip it.

    def command(self, opcode, params=b""):
        self.ser.write(struct.pack("<BHB", H4_CMD, opcode, len(params)) + params)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            code, p = self._read_packet()
            if code == EVT_CMD_COMPLETE and len(p) >= 4:
                _, op, status = struct.unpack_from("<BHB", p)
                if op == opcode:
                    if status != 0:
                        raise HciError(f"opcode 0x{opcode:04X} failed, status 0x{status:02X}")
                    return p[4:]
            elif code == EVT_CMD_STATUS and len(p) >= 4:
                status, _, op = struct.unpack_from("<BBH", p)
                if op == opcode and status != 0:
                    raise HciError(f"opcode 0x{opcode:04X} rejected, status 0x{status:02X}")
        raise HciError(f"no Command Complete for 0x{opcode:04X}")

    def reset(self):
        self.ser.reset_input_buffer()
        self.command(OP_RESET)

    def info(self):
        ver, nch, state, seq, per_read = struct.unpack("<BBBHB", self.command(OP_GET_INFO))
        return dict(version=ver, num_channels=nch, running=bool(state),
                    report_seq=seq, max_per_read=per_read)

    def start(self, passes=None, averaging_us=None, interval_ms=None, threshold=None):
        if passes is None and averaging_us is None and interval_ms is None and threshold is None:
            self.command(OP_START)   # firmware defaults
            return
        self.command(OP_START, struct.pack("<HHIb",
                                           passes if passes is not None else 20,
                                           averaging_us if averaging_us is not None else 128,
                                           interval_ms if interval_ms is not None else 0,
                                           threshold if threshold is not None else -71))

    def stop(self):
        self.command(OP_STOP)

    def read_report(self, num_channels=NUM_CHANNELS, per_read=10, retries=5):
        """Read a full report in pages; retry if it changed between pages."""
        for _ in range(retries):
            seq0, passes, stats = None, 0, []
            ok = True
            for first in range(0, num_channels, per_read):
                rsp = self.command(OP_READ_REPORT,
                                   bytes([first, min(per_read, num_channels - first)]))
                seq, passes, f, count = struct.unpack_from("<HHBB", rsp)
                if seq0 is None:
                    seq0 = seq
                elif seq != seq0:
                    ok = False
                    break
                for i in range(count):
                    stats.append(struct.unpack_from("<bbbB", rsp, 6 + 4 * i))
            if ok:
                return seq0, passes, stats
        raise HciError("report kept changing while reading; slow down the scan")


SOH, EOT, ACK, NAK, CAN = 0x01, 0x04, 0x06, 0x15, 0x18


def crc16_xmodem(data):
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def read_until(ser, token, timeout):
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        buf += ser.read(ser.in_waiting or 1)
        if token in buf:
            return buf
    raise HciError(f"bootloader: timeout waiting for {token!r}; got {buf[-200:]!r}")


def xmodem_update(ser, image, verbose=True):
    """Upload a GBL with the UART XMODEM bootloader menu (1 = upload, 2 = run)."""
    ser.reset_input_buffer()
    ser.write(b"\r")
    read_until(ser, b"BL >", 5)
    ser.write(b"1")
    read_until(ser, b"C", 10)              # receiver ready, CRC mode
    blocks = [image[i:i + 128].ljust(128, b"\x1a") for i in range(0, len(image), 128)]
    for n, block in enumerate(blocks, start=1):
        pkt = bytes([SOH, n & 0xFF, 0xFF - (n & 0xFF)]) + block + crc16_xmodem(block).to_bytes(2, "big")
        for _attempt in range(10):
            ser.reset_input_buffer()
            ser.write(pkt)
            rsp = ser.read(1)
            if rsp == bytes([ACK]):
                break
            if rsp == bytes([CAN]):
                raise HciError(f"bootloader cancelled the transfer at block {n}")
        else:
            raise HciError(f"block {n} not acknowledged")
        if verbose and (n % 64 == 0 or n == len(blocks)):
            print(f"  {n}/{len(blocks)} blocks", flush=True)
    ser.write(bytes([EOT]))
    if ser.read(1) != bytes([ACK]):
        raise HciError("EOT not acknowledged")
    out = read_until(ser, b"BL >", 10)
    if b"Serial upload complete" not in out:
        raise HciError(f"upload not accepted: {out!r}")
    ser.write(b"2")                        # run the new application


def ble_channel(rf):
    if rf == 0:
        return 37
    if rf == 12:
        return 38
    if rf == 39:
        return 39
    return rf - 1 if rf < 12 else rf - 2


def print_table(seq, passes, stats):
    print(f"Report {seq} ({passes} passes)")
    print("  RF BLE  MHz   min  avg  max busy")
    for rf, (mn, av, mx, busy) in enumerate(stats):
        print(f"  {rf:2} {ble_channel(rf):3} {2402 + 2 * rf:4} {mn:5}{av:5}{mx:5} {busy:3}%")


def chscan_line(passes, stats):
    # CHSCAN,<version>,<passes>,<n>,{min,avg,max,busy}xN (format version 1).
    fields = [f"CHSCAN,1,{passes},{len(stats)}"]
    fields += [f"{mn},{av},{mx},{busy}" for mn, av, mx, busy in stats]
    return ",".join(fields)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", required=True)
    ap.add_argument("-b", "--baud", type=int, default=115200)
    ap.add_argument("--rtscts", action="store_true", help="enable RTS/CTS flow control")
    ap.add_argument("--no-reset", action="store_true", help="skip HCI_Reset at connect")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    st = sub.add_parser("start")
    st.add_argument("--passes", type=int)
    st.add_argument("--averaging-us", type=int)
    st.add_argument("--interval-ms", type=int)
    st.add_argument("--threshold", type=int, help="busy threshold, dBm")
    sub.add_parser("stop")
    sub.add_parser("read")
    sub.add_parser("bootloader", help="reboot into the UART XMODEM bootloader")
    up = sub.add_parser("update", help="reboot into the bootloader and upload a .gbl")
    up.add_argument("gbl")
    up.add_argument("--in-bootloader", action="store_true",
                    help="device is already at the bootloader menu")
    w = sub.add_parser("watch", help="poll and print each new report")
    w.add_argument("--poll-ms", type=int, default=200)
    w.add_argument("--chscan", action="store_true", help="print CHSCAN CSV lines")
    w.add_argument("--no-start", action="store_true", help="don't send Start first")
    args = ap.parse_args()

    rcp = Rcp(args.port, args.baud, args.rtscts)
    try:
        if args.cmd == "update":
            image = open(args.gbl, "rb").read()
            if not args.in_bootloader:
                rcp.reset()
                rcp.command(OP_ENTER_BOOTLOADER)
                time.sleep(0.5)
            xmodem_update(rcp.ser, image)
            time.sleep(1.0)
            rcp.ser.reset_input_buffer()
            rcp.reset()
            print("updated:", rcp.info())
            return
        if args.cmd == "bootloader":
            rcp.reset()
            rcp.command(OP_ENTER_BOOTLOADER)
            return
        if not args.no_reset:
            rcp.reset()
        info = rcp.info()
        if args.cmd == "info":
            print(info)
        elif args.cmd == "start":
            rcp.start(args.passes, args.averaging_us, args.interval_ms, args.threshold)
        elif args.cmd == "stop":
            rcp.stop()
        elif args.cmd == "read":
            seq, passes, stats = rcp.read_report(info["num_channels"], info["max_per_read"])
            print_table(seq, passes, stats)
        elif args.cmd == "watch":
            if not args.no_start and not info["running"]:
                rcp.start()
            last = info["report_seq"]
            while True:
                seq = rcp.info()["report_seq"]
                if seq != last and seq != 0:
                    seq, passes, stats = rcp.read_report(info["num_channels"],
                                                         info["max_per_read"])
                    last = seq
                    if args.chscan:
                        print(chscan_line(passes, stats), flush=True)
                    else:
                        print_table(seq, passes, stats)
                time.sleep(args.poll_ms / 1000)
    except HciError as e:
        sys.exit(f"error: {e}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
