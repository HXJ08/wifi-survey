# Locus

**Find where you are using the Wi-Fi networks around you.**

Locus is an ESP32 survey rig + Mac toolkit that records Wi-Fi fingerprints (BSSID, RSSI, channel, SSID) every few seconds, optionally tags them with GPS, and learns a router map so a board can estimate its location without GPS. Everything stays local: no server, cloud account or SD card.

> **Privacy:** this repo contains code only. Survey data (`data/`, `analysis/`, `models/`) and `secrets.h` are git-ignored. Wi-Fi scans reveal nearby networks and where you have been — do not commit or share them.

## Repo layout

| Path | What it is |
|---|---|
| `firmware/Locus/` | Arduino sketch for ESP32 / ESP32-S3 (compressed flash log, GPS, NTP, OTA) |
| `survey.py` | Mac CLI: initialize, start, status, collect (export + import to SQLite) |
| `fingerprint.py` | Similarity / clustering analysis of scans |
| `geolocate.py` | Train a router map from tagged scans, predict locations, export a C++ lookup header |
| `tools/*.command` | Double-click helpers for macOS (check board, export data) |
| `tests/` | Python tests, end-to-end simulator, native C++ codec test |
| `docs/results/` | Results from **synthetic** test fixtures only |

## Quick start

```sh
git clone https://github.com/HXJ08/locus.git && cd locus
cp firmware/Locus/secrets.example.h firmware/Locus/secrets.h   # then edit it
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

## Verification status

- Compiled successfully with Arduino-ESP32 3.3.12 for generic ESP32-S3, 4 MB flash, default partition, USB CDC enabled.
- Firmware uses 913,045 bytes of program storage and 45,080 bytes of static RAM in that configuration. Dynamic scan/string allocations are additional.
- 15 automated Python tests cover fragmented transfers, checksum failures, truncated transfers, protocol errors, record validation, duplicate imports, conflicting identities, torn records, and the complete simulated pause/sync/download/import/resume workflow.
- The actual C++ storage codec/journal reader passed 1,003 roundtrip cases plus corruption, truncation, recovery and I/O-failure tests under AddressSanitizer and UndefinedBehaviorSanitizer. A 240-scan fixture exported byte-for-byte identically and imported successfully on the Mac.
- Hardware verification on 2026-10-29: uploaded through Arduino IDE to an ESP32-D0WD-V3 (ESP32 Dev Module, 4 MB flash), verified upload hash, initialized storage with user approval, synchronized time, and exported a real scan containing 20 networks. Recording was left active. This is a classic ESP32, not an ESP32-S3. Long-duration operation, storage endurance and abrupt power-loss testing remain outstanding.

## 1. Upload firmware

Copy `firmware/Locus/secrets.example.h` to `secrets.h` and fill in your values, then open `firmware/Locus/Locus.ino` in Arduino IDE with Espressif's Arduino-ESP32 3.x package. Select your exact board and its actual flash size. Select a partition scheme that includes a SPIFFS data partition: LittleFS uses that partition. A scheme without filesystem space will not work.

The generic 4 MB compile configuration is a compile test, not identification of your board. USB CDC settings depend on whether you use native USB or a USB-to-serial bridge. Do not erase flash or change partition layouts on a board containing unexported surveys. Close Serial Monitor before running the downloader.

## 2. Prepare the Mac

In Terminal:

```sh
cd locus
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python survey.py ports
```

Choose the ESP32's `/dev/cu.usbmodem...` or `/dev/cu.usbserial...` device. Do not select Bluetooth or debug-console. Replace PORT below with that full path. Port names may change when reconnecting.

## 3. Initialize a fresh board

```sh
.venv/bin/python survey.py initialize --port PORT
```

This explicitly asks you to type ERASE. Use it only for a fresh filesystem: it destroys filesystem contents. The script refuses initialization when a filesystem is already mounted. A failed mount may also mean the wrong partition configuration or corruption, so do not assume a mount failure means the board is empty. Firmware never formats automatically.

Successful initialization synchronizes time and starts recording.

## 4. Before each walk

```sh
.venv/bin/python survey.py start --port PORT
.venv/bin/python survey.py status --port PORT
```

Check mounted=true, paused=false, fault=false, and available storage. Keep the ESP32 powered after synchronization. Some boards reset when opening their serial port despite handshake suppression; each boot creates a separate session. Synchronize immediately before the walk, avoid further serial connections, and use a power arrangement that does not interrupt power when disconnecting the Mac. Follow your board's power-input instructions.

If a session has no clock anchor, relative ordering and Wi-Fi matching still work, but its absolute date/time is unknown. An anchor saved after a walk can date earlier scans only if the board remained in that same session. Mac clocks and the ESP32 oscillator are not precision timing instruments; clock drift and USB delay remain.

## 5. After each walk

```sh
.venv/bin/python survey.py collect --port PORT
```

The script pauses scans, attempts to store a time anchor, waits for any scan to finish, verifies the export byte count and CRC32, saves and flushes the original file, then imports it. If storage is full, it attempts the export even when the time anchor cannot be saved. A successful download resumes recording only if it was running beforehand and there is no storage fault.

On an error, no logs are automatically erased. The board may remain paused; check status before carrying it again. Retry a failed transfer. Repeated imports deduplicate records by device/session/record ID; differing content with the same identity aborts that import transaction. Original files remain available.

Files default to `data/` beside survey.py; choose another directory with `--data /absolute/path`:

- `raw/<timestamp>-<unique-id>.jsonl`: verified JSONL exported by the board, never intentionally modified by the importer. New compressed flash records are decoded during export; these files are not physical flash images.
- `raw/<same-name>.report.json`: SHA-256, byte count, import counts, rejected line details.
- `survey.sqlite3`: local records table, with original JSON payloads and source-file paths.

Damaged compressed frames are preserved as hexadecimal byte spans in export-only storage_damage records, quarantined while valid records are imported. The firmware resynchronizes to later independently checksummed frames. A torn legacy JSON record may also be quarantined. Read the rejection report. Incomplete local file writes retain a `.partial` extension; these are not considered completed exports. Keep a backup of the data directory. The database can be rebuilt from the raw JSONL exports:

```sh
.venv/bin/python survey.py import --file /absolute/path/to/export.jsonl
```

Legacy version-1 JSONL exports are accepted; legacy live firmware must be upgraded before using the new downloader. Export old firmware manually before any potentially destructive upload.

## 6. When flash fills

The board stops rather than overwriting prior surveys. New surveys use the compact WSL1 journal, with each record independently compressed and checksummed. The two-hour fixture shrank from 752,159 exported JSONL bytes to 204,180 stored bytes (72.85% smaller, 3.68x retention before filesystem overhead). Real compression depends on the observations. Measure growth with `status`; there is no guaranteed multi-day capacity. Existing legacy JSON files remain in place and still consume their original space until explicitly erased after export. The filesystem retains a 32 KB reserve but metadata/fragmentation can still cause earlier errors.

After downloading and checking the export and rejection report, open a serial terminal at 115200 baud with newline endings. Send `pause`, wait for scanning=false in `status`, then `FORMAT ERASE ALL`. This permanently erases the filesystem, creates a fresh session, and leaves recording paused. Close the terminal and run `start` to synchronize time and resume. The downloader intentionally never performs this erasure during collection.

## Data for later training and GPS association

Each scan includes version, device, boot-session ID, record ID, scan ID, start/end monotonic milliseconds, result count, and access points with BSSID, RSSI, channel and SSID encoded as hexadecimal. A null gps field means no GPS measurement exists. Error scans have a negative result; an empty successful scan has result=0. Neither should be treated as a normal populated training fingerprint.

Clock anchors map session-relative milliseconds to Unix UTC milliseconds. The initial approximation is `UTC_scan = UTC_anchor + scan_ms - anchor_ms`; never carry an anchor across sessions. Use multiple consistent anchors to assess drift later. Maintain observed and inferred timestamps separately.

The ESP32-S3 collects 2.4 GHz networks only. Scan duration covers multiple channels, so a scan is a time window rather than one simultaneous measurement. Body placement, orientation and changing routers affect the data. Do not remove hotspot-like names or locally administered addresses during acquisition; learn reliability from repeated observations.

Later, add a GPS receiver to the survey rig, record fresh fixes with receive time, validity and quality, and join those measurements to scan time windows. Do not label indoor observations with stale GPS. Match historical Wi-Fi fingerprints to geographically consistent GPS-tagged reference observations; keep inferred coordinates, uncertainty and model version in separate derived tables. No receiver wiring is specified until its exact module and board pins are known.

## Run software tests

```sh
python3 -m unittest discover -s tests -v
```

## Hardware acceptance checks still required

1. Initialize, start, and collect several scans; confirm real access points and plausible signal readings.
2. Download twice; confirm duplicate handling and byte counts.
3. Remove/reconnect USB during a download; verify retry exports intact logs.
4. Reboot during recording; confirm a new session and preserved valid earlier records.
5. Confirm unplugged operation from your power supply and measure storage growth over a walk.
6. Exercise near-full storage on a disposable test dataset; verify export remains possible and recording stops.

Sources used for API checks:
- https://docs.espressif.com/projects/arduino-esp32/en/latest/api/wifi.html
- https://github.com/espressif/arduino-esp32/blob/master/libraries/LittleFS/src/LittleFS.cpp

## End-to-end Mac workflow simulation

Run `.venv/bin/python tests/simulate.py` from this folder. It generates a deterministic two-hour synthetic survey and drives the production `survey.py` command-line workflow through a simulated serial device. It checks normal/incremental/repeated exports, corrupted and interrupted transfers with retries, a device reboot during export, full-storage status, paused-state preservation, and torn-record quarantine. It writes `simulation-results.json` (a copy is in `docs/results/`); synthetic raw files/databases are isolated in a temporary directory and removed afterward, never mixed into real training data.

Nine scenarios passed. Two hours (240 scans, 15–35 synthetic APs per scan) produced 752,159 bytes. That is the uncompressed export size; the updated firmware stores the same fixture in 204,180 bytes. The Mac workflow simulation does not model compression; the separate native C++ storage test measures it. Measure actual usage and download regularly. The full-storage scenario simulates the device's refusal/status, not actual flash allocation. This simulator exercises the real Mac code but does not run the firmware or simulate RF behaviour, flash wear, or hardware power loss.


## Compact-storage update

Keep `CompactLog.h`, `ExportState.h`, `NmeaGps.h` and `secrets.h` beside `Locus.ino` when opening/uploading the sketch. No new Arduino libraries are required. New logs use `/survey.wsl`; old `/survey.jsonl` logs are exported first and left untouched. Use the same flash size and partition scheme when upgrading, and export existing surveys before flashing. Do not enable erase-all-flash. A subsequent upload and live recording/export check succeeded on an ESP32-D0WD-V3 board. ESP32-S3 remains compile-tested only.

The Mac downloader waits up to 120 seconds for the board to prepare a decoded export. Export size can exceed the compressed on-device size. Storage compression does not drop APs, change RSSI, alter SSIDs, or reduce scan frequency. GPS remains absent until receiver integration.

Native storage tests (from this folder; a C++ compiler is required):

```sh
c++ -std=c++17 -Wall -Wextra -Werror -fsanitize=address,undefined tests/compact_log_test.cpp -o /tmp/locus-codec-test
/tmp/locus-codec-test
```

`docs/results/compact-storage-results.json` contains the measured fixture results. Host tests do not establish ESP32 timing, peak heap usage, real flash endurance, or physical power-loss guarantees.

The live-board test exposed an all-zero startup Wi-Fi MAC response. Device IDs now come directly from ESP.getEfuseMac(), which was verified to return the board's permanent identity. The downloadable package includes this fix.

## Local fingerprint analysis

Install `analysis-requirements.txt` in the same Python environment to use `fingerprint.py`. Example:

```sh
.venv/bin/python -m pip install -r analysis-requirements.txt
.venv/bin/python fingerprint.py data/raw/<export>.jsonl --out analysis/first-survey
```

This exploratory version computes cosine/Jaccard similarities, conservative candidate clusters and match rejection, session-held-out coverage and noise sensitivity. It does not yet provide measured physical-location accuracy or GPS association. Tests for fingerprint analysis require the analysis dependencies.

## Improved Filtering & Geolocation Upgrade

### 1. Robust Fingerprint Clustering

To prevent stationary desks from splitting into multiple false places due to mobile hotspots and faint RF fluctuations, use the noise/LAA-filtered mode:

```sh
.venv/bin/python fingerprint.py data/raw/<export>.jsonl --out analysis/cleaned-survey --filter-laa --min-rssi -82 --method average
```

* `--filter-laa`: Excludes Locally Administered / randomized MAC addresses (e.g., smartphone hotspots, mobile MiFi routers).
* `--min-rssi -82`: Ignores faint background networks below -82 dBm that cause beacon-drop jitter.
* `--method average`: Uses average-linkage clustering, making the matcher resilient to temporary packet drops.

### 2. Supervised WPS Geolocation (`geolocate.py`)

Once you have GPS-tagged scans (or manually anchored places):

```sh
# Train router map from scans with manual or GPS ground truth
.venv/bin/python geolocate.py train data/raw/*.jsonl --tag-all "LAT,LON,My Desk" --export-cpp

# Predict coordinates for new scans without GPS
.venv/bin/python geolocate.py predict data/raw/<export>.jsonl --out predictions.csv
```

* `models/router_map.json`: Mapped fixed routers with estimated coordinates and spread.
* `models/router_map.h`: Embedded C++ header for calculating latitude/longitude directly on the ESP32 without a PC or GPS.

### 3. GPS Receiver Hardware Setup

The updated `Locus.ino` includes zero-dependency NMEA GPS parsing on `SerialGPS` (UART2):
* **GPS TX** -> **ESP32 RX2 (GPIO 16)**
* **GPS RX** -> **ESP32 TX2 (GPIO 17)**
* **VCC** -> **3.3V / 5V** (depending on your module)
* **GND** -> **GND**

GPS coordinates are automatically recorded in each scan only when a high-quality fix (`HDOP <= 2.5`) is active. When no GPS is connected, scans safely record `"gps": null`.

### 4. Dynamic Scan Interval

Adjust the scan rate on the fly over serial without re-flashing:

```sh
.venv/bin/python survey.py interval 5000 --port /dev/cu.usbserial-0001   # 5s interval while walking
.venv/bin/python survey.py interval 30000 --port /dev/cu.usbserial-0001  # 30s interval while stationary
```

## License

MIT — see [LICENSE](LICENSE).
