#!/usr/bin/env python3
"""Hardware tests for the bt_rcp_channel_scan_freertos RCP over raw H4 UART.

  test_rcp.py -p /dev/ttyACM0 basic            # vendor commands, validation, reset
  test_rcp.py -p /dev/ttyACM0 coexist          # LE scan with and without channel scan
  test_rcp.py -p /dev/ttyACM0 connect --name "Blinky Example" [--secs 30]
  test_rcp.py -p /dev/ttyACM0 all --name "Blinky Example"

The port must not be attached to BlueZ (btattach) while this runs.
Exit code is non-zero if any check fails.
"""
import argparse
import struct
import sys
import time

import rcp_channel_scan as r

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{(': ' + detail) if detail else ''}", flush=True)
    if not cond:
        failures.append(name)


def expect_status(rcp, name, opcode, params, status):
    try:
        rcp.command(opcode, params)
        check(name, False, "unexpected success")
    except r.HciError as e:
        check(name, f"0x{status:02X}" in str(e), str(e))


def drain_events(rcp, secs, handler):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        try:
            code, p = rcp._read_packet()
        except r.HciError:
            continue
        handler(code, p)


def test_basic(rcp):
    ver = rcp.command(0x1001)
    check("HCI Read_Local_Version", len(ver) >= 8,
          f"HCI ver {ver[0]}, mfr 0x{struct.unpack_from('<H', ver, 4)[0]:04X}")
    info = rcp.info()
    check("Get Info", info["version"] == 1 and info["num_channels"] == 40
          and info["max_per_read"] == 10, str(info))

    expect_status(rcp, "Start rejects bad length", r.OP_START, b"\x01", 0x12)
    expect_status(rcp, "Start rejects passes=0", r.OP_START,
                  struct.pack("<HHIb", 0, 128, 0, -71), 0x12)
    expect_status(rcp, "Start rejects averaging 20000 us", r.OP_START,
                  struct.pack("<HHIb", 5, 20000, 0, -71), 0x12)
    expect_status(rcp, "Read rejects channel 40", r.OP_READ_REPORT, bytes([40, 1]), 0x12)
    expect_status(rcp, "Read rejects bad length", r.OP_READ_REPORT, b"\x00", 0x12)
    expect_status(rcp, "Unknown vendor opcode 0xFF05", 0xFF05, b"", 0x01)

    rcp.start(passes=10)
    time.sleep(2)
    a = rcp.info()
    check("Scan running and producing reports", a["running"] and a["report_seq"] > 0, str(a))
    seq, passes, stats = rcp.read_report()
    valid = [s for s in stats if s[1] != -128]
    check("Full report has 40 channels", len(stats) == 40)
    check("Report values plausible", len(valid) >= 38
          and all(-127 <= mn <= av <= mx <= 20 and 0 <= b <= 100 for mn, av, mx, b in valid),
          f"{len(valid)} channels valid, avg range {min(s[1] for s in valid)}..{max(s[1] for s in valid)} dBm")
    rsp = rcp.command(r.OP_READ_REPORT, bytes([35, 10]))
    check("Read clamps count at end of table", rsp[5] == 5, f"count={rsp[5]}")

    rate0 = rcp.info()["report_seq"]
    time.sleep(5)
    rate = (rcp.info()["report_seq"] - rate0) / 5
    check("Continuous report rate (10 passes)", rate > 2, f"{rate:.1f} reports/s")

    rcp.reset()
    check("HCI_Reset leaves scan running", rcp.info()["running"])

    rcp.stop()
    time.sleep(0.3)
    s0 = rcp.info()
    time.sleep(1.5)
    s1 = rcp.info()
    check("Stop freezes reports", not s1["running"] and s0["report_seq"] == s1["report_seq"])

    rcp.start(passes=5, interval_ms=1000)
    s0 = rcp.info()["report_seq"]
    time.sleep(5.5)
    n = rcp.info()["report_seq"] - s0
    check("Interval mode 1000 ms", 4 <= n <= 6, f"{n} reports in 5.5 s")
    rcp.start(passes=10)   # restart with new params while running
    s0 = rcp.info()["report_seq"]
    time.sleep(2)
    check("Start while running applies new params", rcp.info()["report_seq"] - s0 >= 3)
    rcp.stop()


def le_scan(rcp, secs, interval=0x20, window=0x10, name=None):
    rcp.command(0x200B, struct.pack("<BHHBB", 0, interval, window, 0, 0))
    rcp.command(0x200C, bytes([1, 0]))
    found = {}
    count = [0]

    def on_evt(code, p):
        if code != 0x3E or p[0] != 0x02:
            return
        off, n = 2, p[1]
        for _ in range(n):
            atype = p[off + 1]
            addr = bytes(p[off + 2:off + 8])
            dlen = p[off + 8]
            data = p[off + 9:off + 9 + dlen]
            off += 9 + dlen + 1
            count[0] += 1
            i = 0
            while i + 1 < len(data):
                ln = data[i]
                if ln == 0:
                    break
                if data[i + 1] in (0x08, 0x09):
                    found[(addr, atype)] = data[i + 2:i + 1 + ln].decode(errors="replace")
                i += ln + 1
            found.setdefault((addr, atype), None)

    drain_events(rcp, secs, on_evt)
    rcp.command(0x200C, bytes([0, 0]))
    return count[0], found


def test_coexist(rcp):
    for scan_on in (False, True):
        rcp.stop()
        if scan_on:
            rcp.start(passes=10)
        s0 = rcp.info()["report_seq"]
        n, found = le_scan(rcp, 10)
        s1 = rcp.info()["report_seq"]
        print(f"   LE scan 50% duty, channel scan {'on ' if scan_on else 'off'}: "
              f"{n} adv reports, {len(found)} devices, {s1 - s0} channel reports")
        check(f"LE scan works (channel scan {'on' if scan_on else 'off'})", n > 0)
        if scan_on:
            check("Channel scan progresses during LE scan", s1 - s0 > 0)
    rcp.stop()


def fmt(addr):
    return ":".join(f"{b:02X}" for b in addr[::-1])


def test_connect(rcp, name, secs):
    rcp.stop()
    _, found = le_scan(rcp, 5, name=name)
    target = [(a, t) for (a, t), nm in found.items() if nm == name]
    check(f"Found advertiser '{name}'", bool(target))
    if not target:
        return
    addr, atype = target[0]
    print(f"   target {fmt(addr)} (type {atype})")

    # LE Create Connection: 30 ms interval, latency 0, 4 s supervision timeout.
    params = struct.pack("<HHBB6sBHHHHHH", 0x60, 0x30, 0, atype, addr, 0,
                         24, 24, 0, 400, 0, 0)
    rcp.ser.write(struct.pack("<BHB", 1, 0x200D, len(params)) + params)
    handle = [None]
    status = [None]

    def on_conn(code, p):
        if code == 0x3E and p[0] in (0x01, 0x0A) and handle[0] is None:
            status[0] = p[1]
            if p[1] == 0:
                handle[0] = struct.unpack_from("<H", p, 2)[0]

    end = time.monotonic() + 20
    while handle[0] is None and status[0] is None and time.monotonic() < end:
        drain_events(rcp, 0.2, on_conn)
    if handle[0] is None:
        try:
            rcp.command(0x200E)   # LE Create Connection Cancel
        except r.HciError:
            pass
        check("LE connection established", False, f"status {status[0]}")
        return
    check("LE connection established", True, f"handle 0x{handle[0]:04X}")

    rcp.start(passes=10)
    s0 = rcp.info()["report_seq"]
    disconnected = [None]
    acl_rx = [0]

    def on_evt(code, p):
        if code == 0x05 and struct.unpack_from("<H", p, 1)[0] == handle[0]:
            disconnected[0] = p[3]

    # Poll RSSI once a second while the channel scan runs; Read_RSSI fails if
    # the link has dropped.
    rssis = []
    t_end = time.monotonic() + secs
    while time.monotonic() < t_end and disconnected[0] is None:
        try:
            rsp = rcp.command(0x1405, struct.pack("<H", handle[0]))
            rssis.append(struct.unpack_from("<b", rsp, 2)[0])
        except r.HciError as e:
            print("   Read_RSSI:", e)
        drain_events(rcp, 1.0, on_evt)
    s1 = rcp.info()["report_seq"]
    check(f"Connection survives {secs} s of channel scanning", disconnected[0] is None,
          f"disconnect reason 0x{disconnected[0]:02X}" if disconnected[0] is not None else
          f"RSSI {min(rssis)}..{max(rssis)} dBm over {len(rssis)} polls")
    check("Channel scan progresses while connected", s1 - s0 > 0,
          f"{s1 - s0} reports ({(s1 - s0) / secs:.1f}/s)")
    rcp.stop()
    if disconnected[0] is None:
        rcp.ser.write(struct.pack("<BHBHB", 1, 0x0406, 3, handle[0], 0x13))
        drain_events(rcp, 1.0, on_evt)
        check("Disconnect", disconnected[0] is not None)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", required=True)
    ap.add_argument("test", choices=["basic", "coexist", "connect", "all"])
    ap.add_argument("--name", default="Blinky Example")
    ap.add_argument("--secs", type=int, default=30)
    args = ap.parse_args()
    rcp = r.Rcp(args.port, 115200, False)
    rcp.reset()
    # Like a real host stack: enable all events, incl. LE meta events (the
    # controller doesn't deliver LE Connection Complete without this).
    rcp.command(0x0C01, bytes.fromhex("ffffffffffffff3f"))
    rcp.command(0x2001, bytes.fromhex("ff0f000000000000"))
    if args.test in ("basic", "all"):
        test_basic(rcp)
    if args.test in ("coexist", "all"):
        test_coexist(rcp)
    if args.test in ("connect", "all"):
        test_connect(rcp, args.name, args.secs)
    rcp.reset()
    print(f"\n{len(failures)} failure(s)" + (": " + ", ".join(failures) if failures else ""))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
