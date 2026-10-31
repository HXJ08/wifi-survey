#!/bin/bash
# Opens the ESP32 once, waits for Wi-Fi/NTP and a scan, then prints status (log: check-log.txt)
cd "$(dirname "$0")/.."
PORT=$(ls /dev/cu.usbserial* /dev/cu.usbmodem* /dev/cu.SLAB_USBtoUART* /dev/cu.wchusbserial* 2>/dev/null | head -1)
.venv/bin/python - "$PORT" <<'PY' 2>&1 | tee data/check-log.txt
import serial,sys,time,json
p=serial.Serial(sys.argv[1],115200,timeout=1)
def status():
    p.reset_input_buffer(); p.write(b"status\n")
    end=time.time()+5
    while time.time()<end:
        l=p.readline().decode(errors="replace").strip()
        if l.startswith("STATUS "): return json.loads(l[7:])
        if l.startswith("ERR"): print(l)
start=time.time()
for t in (5,30,60,90):
    time.sleep(max(0,t-(time.time()-start)))
    print(f"t={t}s", json.dumps(status()), flush=True)
PY
echo "=== done ==="
