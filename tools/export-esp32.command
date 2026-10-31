#!/bin/bash
# One-click: export data from the plugged-in ESP32 (runs survey.py collect)
cd "$(dirname "$0")/.."
mkdir -p data; LOG="data/export-log.txt"
{
echo "=== $(date) ==="
if [ ! -x .venv/bin/python ]; then
  echo "Creating .venv..."; python3 -m venv .venv && .venv/bin/python -m pip install -q -r requirements.txt
fi
PY=.venv/bin/python
$PY survey.py ports
PORT=$(ls /dev/cu.usbserial* /dev/cu.usbmodem* /dev/cu.SLAB_USBtoUART* /dev/cu.wchusbserial* 2>/dev/null | head -1)
if [ -z "$PORT" ]; then echo "ERROR: no ESP32 serial port found"; exit 1; fi
echo "Using port: $PORT"
$PY survey.py collect --port "$PORT"
echo "Exit code: $?"
echo "=== done ==="
} 2>&1 | tee -a "$LOG"
