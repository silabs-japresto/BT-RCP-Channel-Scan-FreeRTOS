#!/usr/bin/env python3
"""BlueZ coexistence tests for the RCP attached with btattach.

Prerequisite (see CHANNEL_SCAN_RCP.md for the sudoers rule):
  sudo btattach -B /dev/ttyACM0 -S 115200 -P h4 &

  test_bluez.py --hci hci1 [--peer 04:87:27:E9:57:0C] [--secs 30]

The vendor commands go through `sudo hcitool -i hciX cmd`, so BlueZ keeps
owning the controller while the tests run. Exit code is non-zero on failure.
"""
import argparse
import re
import struct
import subprocess
import sys
import time

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}{(': ' + detail) if detail else ''}", flush=True)
    if not cond:
        failures.append(name)


def run(*cmd, timeout=20, input_text=None):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text)
    return p.returncode, p.stdout + p.stderr


class HciTool:
    def __init__(self, hci):
        self.hci = hci

    def cmd(self, ocf, params=b""):
        """Send OGF 0x3F command; return (status, return params) from Command Complete."""
        args = ["sudo", "-n", "/usr/bin/hcitool", "-i", self.hci, "cmd", "0x3f", f"0x{ocf:03x}"]
        args += [f"0x{b:02x}" for b in params]
        rc, out = run(*args)
        m = re.search(r"> HCI Event: 0x0e plen \d+\s+((?:[0-9A-Fa-f]{2}\s*)+)", out)
        if rc != 0 or not m:
            raise RuntimeError(f"hcitool failed: {out.strip()}")
        b = bytes.fromhex("".join(m.group(1).split()))
        # Command Complete: num_pkts, opcode (2), status, params
        return b[3], b[4:]

    def info(self):
        st, p = self.cmd(0x300)
        if st != 0:
            raise RuntimeError(f"Get Info status 0x{st:02x}")
        ver, n, running, seq, per = struct.unpack("<BBBHB", p)
        return dict(version=ver, num_channels=n, running=bool(running), report_seq=seq,
                    max_per_read=per)

    def read_report(self):
        for _ in range(5):
            seq0, stats, ok = None, [], True
            for first in range(0, 40, 10):
                st, p = self.cmd(0x303, bytes([first, 10]))
                if st != 0:
                    raise RuntimeError(f"Read Report status 0x{st:02x}")
                seq, passes, f, count = struct.unpack_from("<HHBB", p)
                if seq0 is None:
                    seq0 = seq
                elif seq != seq0:
                    ok = False
                    break
                stats += [struct.unpack_from("<bbbB", p, 6 + 4 * i) for i in range(count)]
            if ok:
                return seq0, passes, stats
        raise RuntimeError("report kept changing")


def controller_addr(hci):
    _, out = run("sudo", "-n", "/usr/bin/hciconfig", hci)
    m = re.search(r"BD Address: ([0-9A-F:]{17})", out)
    return m.group(1) if m else None


def btctl(script, timeout=40):
    """Run bluetoothctl with a list of commands (strings or ('sleep', s))."""
    p = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True)
    for line in script:
        if isinstance(line, tuple):
            time.sleep(line[1])
            continue
        p.stdin.write(line + "\n")
        p.stdin.flush()
        time.sleep(0.3)
    p.stdin.write("quit\n")
    p.stdin.flush()
    out, _ = p.communicate(timeout=timeout)
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\x01|\x02", "", out)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hci", default="hci1")
    ap.add_argument("--peer", default="04:87:27:E9:57:0C", help="address of a connectable peer")
    ap.add_argument("--secs", type=int, default=30)
    args = ap.parse_args()
    h = HciTool(args.hci)
    ctrl = controller_addr(args.hci)
    check(f"{args.hci} present", ctrl is not None, ctrl or "")

    # 1. Vendor commands through BlueZ.
    info = h.info()
    check("Get Info via hcitool", info["version"] == 1 and info["num_channels"] == 40, str(info))
    st, _ = h.cmd(0x301, struct.pack("<HHIb", 0, 128, 0, -71))
    check("Start with passes=0 rejected", st == 0x12, f"status 0x{st:02x}")
    st, _ = h.cmd(0x301, struct.pack("<HHIb", 10, 128, 0, -71))
    check("Start via hcitool", st == 0, f"status 0x{st:02x}")
    time.sleep(2)
    seq, passes, stats = h.read_report()
    check("Read Report via hcitool", len(stats) == 40 and seq > 0,
          f"seq {seq}, {passes} passes, avg {min(s[1] for s in stats)}..{max(s[1] for s in stats)} dBm")

    # 2. BlueZ LE discovery on this controller while the channel scan runs.
    s0 = h.info()["report_seq"]
    out = btctl([f"select {ctrl}", "power on", "scan le", ("sleep", 10), "scan off", "devices"])
    devs = set(re.findall(r"Device ([0-9A-F:]{17})", out))
    s1 = h.info()["report_seq"]
    check("BlueZ LE discovery with channel scan running", len(devs) > 0, f"{len(devs)} devices")
    check("Channel scan progresses during BlueZ discovery", s1 > s0, f"{s1 - s0} reports in ~10 s")
    check("Peer seen by BlueZ", args.peer in devs)

    # 3. BlueZ connection + GATT while the channel scan runs.
    s0 = h.info()["report_seq"]
    out = btctl([f"select {ctrl}", "scan le", ("sleep", 4), "scan off",
                 f"connect {args.peer}", ("sleep", 8), f"info {args.peer}"], timeout=60)
    connected = re.search(r"Connected: yes", out) is not None
    check("BlueZ LE connection to peer", connected)
    names = re.findall(r"Name: (.+)", out)
    if names:
        print(f"   peer name: {names[0].strip()}")
    held = connected
    t_end = time.monotonic() + args.secs
    polls = 0
    while connected and time.monotonic() < t_end:
        time.sleep(5)
        polls += 1
        out = btctl([f"select {ctrl}", f"info {args.peer}"])
        if "Connected: yes" not in out:
            held = False
            break
        h.info()   # vendor command traffic during the connection
    s1 = h.info()["report_seq"]
    if connected:
        check(f"Connection held {args.secs} s with channel scan + vendor polling", held,
              f"{polls} checks")
        check("Channel scan progresses while connected", s1 > s0,
              f"{s1 - s0} reports ({(s1 - s0) / (args.secs + 12):.1f}/s)")
        btctl([f"select {ctrl}", f"disconnect {args.peer}", ("sleep", 2)])
        out = btctl([f"select {ctrl}", f"info {args.peer}"])
        check("Disconnect", "Connected: no" in out)

    st, _ = h.cmd(0x302)
    check("Stop via hcitool", st == 0 and not h.info()["running"])
    print(f"\n{len(failures)} failure(s)" + (": " + ", ".join(failures) if failures else ""))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
