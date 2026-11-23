import time, json, rcp_channel_scan as r
rcp = r.Rcp("/dev/ttyACM0", 115200, False); rcp.reset(); rcp.start(passes=20)
reps, last, t = [], 0, time.time() + 20
while time.time() < t:
    s = rcp.info()["report_seq"]
    if s != last: last = s; reps.append(rcp.read_report()[2])
    time.sleep(0.1)
rcp.stop(); print(json.dumps(reps))
