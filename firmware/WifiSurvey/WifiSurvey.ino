// wifi-survey rig — firmware 3.0 (classic ESP32 or ESP32-S3, Arduino-ESP32 3.x)
// Purpose (wifi-survey): continuously log Wi-Fi fingerprints (BSSID, SSID, RSSI, channel) with
// trustworthy UTC time and, when a GPS is attached, coordinates + fix quality. Data stays on the
// board until exported over USB (export-esp32.command / survey.py collect, protocol 2 unchanged).
// New in 3.0: joins the home network for NTP time (anchors every session), OTA upload from
// Arduino IDE, GPS time anchors, GNSS receiver status on every scan, faster retry of failed scans.
// New in 3.1: adaptive interval (30 s moving / 2 min when the strong networks stop changing) and
// "clear <len> <crc>" so the Mac frees storage after a verified export (auto-clear).
// New in 3.2: slower default intervals to save flash (60 s moving / 5 min still) and GPS
// entry/exit events: losing a good fix (gps_lost) marks "went indoors near last_fix"; regaining
// it (gps_regained) marks the exit. Both trigger an immediate doorway scan.
#include <Arduino.h>
#include <WiFi.h>
#include <ArduinoOTA.h>
#include <LittleFS.h>
#include <esp_timer.h>
#include <esp_system.h>
#include <esp_wifi.h>
#include <esp_sntp.h>
#include <sys/time.h>
#include <vector>
#include <algorithm>
#include <iterator>
#include "CompactLog.h"
#include "ExportState.h"
#include "NmeaGps.h"
#include "secrets.h"

#define FW_VERSION "3.2"
const char *HOSTNAME = "wifi-survey";

// Optional GPS (ATGM336H): module TX -> GPIO16, module RX -> GPIO17, 3V3, GND.
HardwareSerial SerialGPS(2);
NmeaGps gps;

uint64_t intervalMs = 60000;                  // moving: scan every 60 s (storage saver)
uint64_t stationaryIntervalMs = 300000;       // still: scan every 5 min
constexpr int STRONG_RSSI = -80;              // only strong APs decide "moving" (weak ones flicker)
constexpr float SAME_PLACE_JACCARD = 0.6f;    // |A∩B|/|A∪B| at or above this = same place
constexpr int STILL_SCANS = 3;                // consecutive same-place scans before slowing down
constexpr uint64_t SCAN_TIMEOUT_MS = 20000;
constexpr uint64_t SCAN_RETRY_MS = 3000;      // retry soon if the radio was busy
constexpr uint64_t WIFI_CONNECT_TIMEOUT_MS = 15000;
constexpr uint64_t WIFI_RETRY_HOME_MS = 60000;      // home SSID seen in last scan
constexpr uint64_t WIFI_RETRY_AWAY_MS = 600000;     // otherwise try every 10 min (SSID may be hidden)
constexpr uint64_t GPS_ANCHOR_EVERY_MS = 1800000;   // GPS clock anchor at most every 30 min
constexpr uint64_t GPS_LOST_MS = 20000;             // good fix gone this long => likely entered a building
constexpr size_t RESERVE_BYTES = 32768;
const char *LOG_PATH = "/survey.wsl";
const char *LEGACY_PATH = "/survey.jsonl";

String session, device, command, timeSource = "none";
bool mounted = false, paused = false, scanning = false, fault = false, overflow = false;
bool timeSet = false, homeSeen = false, wifiTrying = false, wifiUp = false, otaActive = false, otaStarted = false;
uint64_t nextScan = 0, scanStart = 0, wifiTryStart = 0, nextWifiTry = 2000, lastGpsAnchor = 0;
uint32_t scanId = 0, recordId = 0;
volatile bool ntpEvent = false;
std::vector<uint64_t> prevStrong;             // strong BSSIDs of last good scan (sorted)
int stillCount = 0; bool stationary = false;
bool gpsOutdoor = false, scanSoon = false;   // entry/exit state; scanSoon = scan at next chance
uint64_t gpsLostAt = 0;

uint64_t nowMs() { return esp_timer_get_time() / 1000ULL; }
String number(uint64_t n) { char b[24]; snprintf(b, sizeof(b), "%llu", (unsigned long long)n); return String(b); }
void newSession() {
  char b[25];
  snprintf(b, sizeof(b), "%08lx%08lx%08lx", (unsigned long)esp_random(), (unsigned long)esp_random(), (unsigned long)esp_random());
  session = b; recordId = 0; scanId = 0; timeSet = false; timeSource = "none";
}
String hexSSID(const String &s) {
  const char *d = "0123456789abcdef"; String out; out.reserve(s.length() * 2);
  for (size_t i = 0; i < s.length(); i++) { uint8_t b = s[i]; out += d[b >> 4]; out += d[b & 15]; }
  return out;
}
String base(const char *type) {
  return String("{\"v\":2,\"type\":\"") + type + "\",\"device\":\"" + device + "\",\"session\":\"" + session + "\",\"record_id\":" + String(++recordId);
}
void error(const char *code) { Serial.printf("ERR %s\n", code); }
uint32_t crcUpdate(uint32_t crc, const uint8_t *data, size_t n) {
  for (size_t i = 0; i < n; i++) { crc ^= data[i]; for (int j = 0; j < 8; j++) crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320UL : 0); }
  return crc;
}
uint64_t utcNowMs() { struct timeval tv; gettimeofday(&tv, nullptr); return (uint64_t)tv.tv_sec * 1000ULL + tv.tv_usec / 1000; }

bool save(const String &record) {
  if (!mounted || fault || otaActive) return false;
  std::string frame;
  if (!CompactLog::encode(std::string(record.c_str(), record.length()), frame)) { fault = true; error("RECORD_TOO_LARGE"); return false; }
  size_t total = LittleFS.totalBytes(), used = LittleFS.usedBytes();
  if (used > total || total - used < frame.size() + RESERVE_BYTES) { fault = true; error("STORAGE_FULL"); return false; }
  File f = LittleFS.open(LOG_PATH, FILE_APPEND);
  if (!f) { fault = true; error("OPEN_FAILED"); return false; }
  size_t n = f.write((const uint8_t *)frame.data(), frame.size()); f.flush(); f.close();
  if (n != frame.size()) { fault = true; error("SHORT_WRITE"); return false; }
  return true;
}

// Record a clock anchor (device ms <-> UTC ms) and set the system clock for per-scan utc_ms.
bool anchorClock(uint64_t deviceMs, uint64_t utcMs, const char *source, bool setSystemClock) {
  if (setSystemClock) {
    uint64_t utcNow = utcMs + (nowMs() - deviceMs);
    struct timeval tv = {(time_t)(utcNow / 1000), (suseconds_t)((utcNow % 1000) * 1000)};
    settimeofday(&tv, nullptr);
  }
  timeSet = true; timeSource = source;
  return save(base("clock_anchor") + ",\"ms\":" + number(deviceMs) + ",\"utc_ms\":" + number(utcMs) + ",\"source\":\"" + source + "\"}");
}
void onNtpSync(struct timeval *) { ntpEvent = true; }

// ---------- USB export (unchanged protocol 2) ----------
bool emitBytes(ExportState &e, const uint8_t *p, size_t n) {
  e.length += n; e.crc = crcUpdate(e.crc, p, n);
  if (e.send) { size_t sent = 0; while (sent < n) { size_t w = Serial.write(p + sent, n - sent); if (!w) return false; sent += w; } }
  return true;
}
bool emitText(ExportState &e, const std::string &s) { return emitBytes(e, (const uint8_t *)s.data(), s.size()); }
bool exportLogs(ExportState &e) {
  if (LittleFS.exists(LEGACY_PATH)) {
    File old = LittleFS.open(LEGACY_PATH, FILE_READ); if (!old) return false;
    uint8_t bytes[512]; int last = -1;
    while (old.available()) { size_t n = old.read(bytes, sizeof(bytes)); if (!n) return false; last = bytes[n - 1]; if (!emitBytes(e, bytes, n)) return false; delay(1); }
    if (last >= 0 && last != '\n' && !emitText(e, "\n")) return false;
    old.close();
  }
  if (!LittleFS.exists(LOG_PATH)) return true;
  File f = LittleFS.open(LOG_PATH, FILE_READ); if (!f) return false;
  size_t reads = 0;
  bool ok = CompactLog::exportJournal(f.size(), [&](size_t pos, uint8_t *data, size_t n) {
    if ((++reads & 255) == 0) delay(1);
    return f.seek(pos) && f.read(data, n) == n;
  }, [&](const std::string &line) { delay(1); return emitText(e, line); });
  f.close(); return ok;
}

const char *resetReason() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "power_on"; case ESP_RST_SW: return "software"; case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: case ESP_RST_TASK_WDT: case ESP_RST_WDT: return "watchdog"; case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_EXT: return "external"; case ESP_RST_DEEPSLEEP: return "deep_sleep"; default: return "other";
  }
}
void bootRecord() {
  save(base("boot") + ",\"ms\":" + number(nowMs()) + ",\"interval_ms\":" + number(intervalMs) +
       ",\"passive\":true,\"fw\":\"" FW_VERSION "\",\"reset\":\"" + resetReason() + "\"}");
}
void status() {
  Serial.print("STATUS {\"protocol\":2,\"fw\":\"" FW_VERSION "\",\"storage_format\":\"wsl1\",\"device\":\"" + device + "\",\"session\":\"" + session + "\",\"ms\":" + number(nowMs()));
  Serial.printf(",\"mounted\":%s,\"paused\":%s,\"scanning\":%s,\"fault\":%s,\"used\":%u,\"total\":%u,\"interval_ms\":%llu,\"gps_fix\":%s",
                mounted ? "true" : "false", paused ? "true" : "false", scanning ? "true" : "false", fault ? "true" : "false",
                mounted ? (unsigned)LittleFS.usedBytes() : 0, mounted ? (unsigned)LittleFS.totalBytes() : 0,
                (unsigned long long)intervalMs, gps.hasQualityFix(nowMs()) ? "true" : "false");
  Serial.print(String(",\"gps_present\":") + (gps.alive(nowMs()) ? "true" : "false") +
               ",\"wifi\":" + (wifiUp ? "true" : "false") + ",\"ip\":\"" + (wifiUp ? WiFi.localIP().toString() : String("")) +
               "\",\"stationary\":" + (stationary ? "true" : "false") + ",\"time_set\":" + (timeSet ? "true" : "false") + ",\"time_source\":\"" + timeSource + "\"" +
               (timeSet ? ",\"utc_ms\":" + number(utcNowMs()) : String("")) + ",\"ota\":\"" + HOSTNAME + "\"}\n");
}
void dump() {
  paused = true;
  if (scanning) { error("BUSY"); return; }
  if (!mounted) { error("NOT_MOUNTED"); return; }
  ExportState sizing;
  if (!exportLogs(sizing)) { error("READ_FAILED"); return; }
  Serial.printf("BEGIN %u %08lx\n", (unsigned)sizing.length, (unsigned long)(sizing.crc ^ 0xffffffff));
  ExportState sending; sending.send = true;
  if (!exportLogs(sending) || sending.length != sizing.length || sending.crc != sizing.crc) { error("EXPORT_CHANGED"); return; }
  Serial.println("\nEND");
}
void handleCommand(String c) {
  c.trim();
  if (c == "status") status();
  else if (c == "pause") { paused = true; Serial.println("OK PAUSE"); }
  else if (c == "resume") { if (!mounted || fault) { error("STORAGE_UNAVAILABLE"); return; } paused = false; Serial.println("OK RESUME"); }
  else if (c == "dump") dump();
  else if (c == "FORMAT ERASE ALL") {
    if (!paused || scanning) { error("PAUSE_FIRST"); return; }
    LittleFS.end(); mounted = false;
    if (LittleFS.format()) mounted = LittleFS.begin(false);
    fault = !mounted;
    if (mounted) { newSession(); bootRecord(); }
    if (mounted && !fault) Serial.println("OK FORMAT"); else error("FORMAT_FAILED");
  } else if (c.startsWith("time ")) {
    String value = c.substring(5); bool valid = value.length() == 13;
    for (size_t i = 0; i < value.length(); i++) if (!isDigit(value[i])) valid = false;
    if (!valid) { error("INVALID_TIME"); return; }
    uint64_t t = nowMs();
    if (anchorClock(t, strtoull(value.c_str(), nullptr, 10), "serial_host", true)) Serial.println("OK TIME " + session + " " + number(t)); else error("TIME_NOT_SAVED");
  } else if (c.startsWith("clear ")) {
    // Delete the log only if it still exports to exactly the bytes the Mac verified and saved.
    if (!paused || scanning) { error("PAUSE_FIRST"); return; }
    if (!mounted) { error("NOT_MOUNTED"); return; }
    int sp = c.indexOf(' ', 6); if (sp < 0) { error("INVALID_CLEAR"); return; }
    size_t len = strtoul(c.substring(6, sp).c_str(), nullptr, 10);
    uint32_t crc = strtoul(c.substring(sp + 1).c_str(), nullptr, 16);
    ExportState check;
    if (!exportLogs(check)) { error("READ_FAILED"); return; }
    if (check.length != len || (check.crc ^ 0xffffffff) != crc) { error("CLEAR_MISMATCH"); return; }
    size_t before = LittleFS.usedBytes();
    bool ok = (!LittleFS.exists(LOG_PATH) || LittleFS.remove(LOG_PATH)) && (!LittleFS.exists(LEGACY_PATH) || LittleFS.remove(LEGACY_PATH));
    if (!ok) { fault = true; error("CLEAR_FAILED"); return; }
    fault = false;
    Serial.printf("OK CLEAR %u\n", (unsigned)(before - LittleFS.usedBytes()));
  } else if (c.startsWith("interval ")) {
    String value = c.substring(9); value.trim();
    uint64_t val = atoll(value.c_str());
    if (val >= 1000 && val <= 3600000) { intervalMs = val; Serial.println("OK INTERVAL " + String(intervalMs)); }
    else error("INVALID_INTERVAL");
  } else error("UNKNOWN_COMMAND");
}

// ---------- Home Wi-Fi: NTP time + OTA ----------
void setupOta() {
  ArduinoOTA.setHostname(HOSTNAME);
  ArduinoOTA.setPassword(OTA_PASSWORD);
  ArduinoOTA.onStart([]() {
    otaActive = true; paused = true;
    if (scanning) { esp_wifi_scan_stop(); WiFi.scanDelete(); scanning = false; }
  });
  ArduinoOTA.onError([](ota_error_t) { otaActive = false; });
}
void serviceWifi() {
  uint64_t now = nowMs();
  bool connected = WiFi.status() == WL_CONNECTED;
  if (wifiTrying) {
    if (connected) {
      wifiTrying = false; wifiUp = true;
      configTime(0, 0, "pool.ntp.org", "time.google.com", "time.nist.gov");  // UTC; resyncs hourly
      if (!otaStarted) { ArduinoOTA.begin(); otaStarted = true; }
    } else if (now - wifiTryStart > WIFI_CONNECT_TIMEOUT_MS) {
      WiFi.disconnect(false, false); wifiTrying = false;
      nextWifiTry = now + (homeSeen ? WIFI_RETRY_HOME_MS : WIFI_RETRY_AWAY_MS);
    }
    return;
  }
  if (wifiUp && !connected) {  // lost home network (walked away)
    wifiUp = false;
    if (otaStarted) { ArduinoOTA.end(); otaStarted = false; }
    WiFi.disconnect(false, false);
    nextWifiTry = now + WIFI_RETRY_HOME_MS;
    return;
  }
  if (!wifiUp && !scanning && now >= nextWifiTry) {
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    wifiTrying = true; wifiTryStart = now;
  }
}

void serviceClock() {
  uint64_t now = nowMs();
  if (ntpEvent) {  // NTP (re)synchronised the system clock: anchor it
    ntpEvent = false;
    anchorClock(now, utcNowMs(), "ntp", false);
  }
  if (gps.hasFreshUtc(now) && gps.valid && (!timeSet || (timeSource == "gps" && now - lastGpsAnchor > GPS_ANCHOR_EVERY_MS))) {
    lastGpsAnchor = now;
    anchorClock(gps.utcAtMs, gps.utcMs, "gps", timeSource != "ntp");
  }
}

// Last known position (even if stale) with its age, for entry/exit events.
String lastFixJson(uint64_t now) {
  char b[200];
  snprintf(b, sizeof(b), "{\"lat\":%.7f,\"lon\":%.7f,\"alt_m\":%.1f,\"hdop\":%.2f,\"sats\":%d,\"fix_ms\":%llu,\"age_ms\":%llu}",
           gps.lat, gps.lon, gps.alt_m, gps.hdop, gps.satellites, (unsigned long long)gps.lastFixMs,
           (unsigned long long)(now - gps.lastFixMs));
  return String(b);
}

// Inspired by how phone Wi-Fi positioning works indoors: GPS dies at the door, so the last good
// fix is the best anchor for everything seen inside. Scans between gps_lost and gps_regained are
// later joined to last_fix. Loss can also mean a tunnel, vehicle or bag: evidence, not certainty.
void serviceGpsEvents() {
  uint64_t now = nowMs();
  if (!gpsOutdoor && gps.hasQualityFix(now)) {
    gpsOutdoor = true; scanSoon = true;
    if (mounted && !fault && !paused)
      save(base("gps_regained") + ",\"ms\":" + number(now) + ",\"fix\":" + lastFixJson(now) +
           (gpsLostAt ? String(",\"lost_ms\":") + number(gpsLostAt) : String("")) + "}");
  } else if (gpsOutdoor && gps.lastFixMs && now - gps.lastFixMs >= GPS_LOST_MS) {
    gpsOutdoor = false; scanSoon = true; gpsLostAt = now;
    if (mounted && !fault && !paused)
      save(base("gps_lost") + ",\"ms\":" + number(now) + ",\"last_fix\":" + lastFixJson(now) + "}");
  }
}

void serviceScan() {
  uint64_t now = nowMs();
  if (scanning) {
    int n = WiFi.scanComplete();
    if (n == WIFI_SCAN_RUNNING && now - scanStart > SCAN_TIMEOUT_MS) { esp_wifi_scan_stop(); n = WIFI_SCAN_FAILED; }
    if (n == WIFI_SCAN_RUNNING) return;
    String row = base("scan") + ",\"scan_id\":" + String(scanId) + ",\"start_ms\":" + number(scanStart) + ",\"end_ms\":" + number(now) +
                 ",\"result\":" + String(n);
    if (timeSet) row += ",\"utc_ms\":" + number(utcNowMs() - (now - scanStart));  // UTC at scan start
    row += String(",\"wifi_connected\":") + (wifiUp ? "true" : "false") + ",\"aps\":[";
    bool home = false;
    std::vector<uint64_t> strong;
    for (int i = 0; i < n; i++) {
      if (WiFi.RSSI(i) >= STRONG_RSSI) { uint8_t *b = WiFi.BSSID(i); uint64_t k = 0; for (int j = 0; j < 6; j++) k = k << 8 | b[j]; strong.push_back(k); }
      if (i) row += ",";
      String ssid = WiFi.SSID(i);
      if (ssid == WIFI_SSID) home = true;
      row += "{\"bssid\":\"" + WiFi.BSSIDstr(i) + "\",\"rssi_dbm\":" + String(WiFi.RSSI(i)) + ",\"channel\":" + String(WiFi.channel(i)) +
             ",\"ssid_hex\":\"" + hexSSID(ssid) + "\"}";
    }
    row += "]";
    if (n >= 0) {
      std::sort(strong.begin(), strong.end());
      float jac = -1;
      if (!prevStrong.empty() || !strong.empty()) {
        std::vector<uint64_t> both;
        std::set_intersection(prevStrong.begin(), prevStrong.end(), strong.begin(), strong.end(), std::back_inserter(both));
        size_t uni = prevStrong.size() + strong.size() - both.size();
        jac = uni ? (float)both.size() / uni : 1.0f;
      }
      if (jac >= SAME_PLACE_JACCARD) stillCount++; else stillCount = 0;
      stationary = stillCount >= STILL_SCANS;
      prevStrong.swap(strong);
      char m[96]; snprintf(m, sizeof(m), ",\"motion\":{\"jaccard\":%.3f,\"strong\":%u,\"stationary\":%s}", jac, (unsigned)prevStrong.size(), stationary ? "true" : "false");
      row += m;
    }
    row += "," + gps.json(now) + "}";
    if (n >= 0) homeSeen = home;
    save(row); WiFi.scanDelete(); scanning = false;
    if (n < 0) nextScan = now + SCAN_RETRY_MS; else nextScan = scanStart + (stationary ? stationaryIntervalMs : intervalMs);
    if (home && !wifiUp && !wifiTrying && nextWifiTry > now + 5000) nextWifiTry = now + 1000;  // home appeared: connect soon
    return;
  }
  if (mounted && !fault && !paused && !wifiTrying && (now >= nextScan || scanSoon)) {
    scanStart = now; scanId++; scanSoon = false;
    int result = WiFi.scanNetworks(true, true, true, 300);  // async, include hidden, passive, 300 ms/channel
    if (result == WIFI_SCAN_FAILED) {
      save(base("scan_error") + ",\"ms\":" + number(scanStart) + ",\"scan_id\":" + String(scanId) + "}");
      nextScan = scanStart + SCAN_RETRY_MS;
    } else scanning = true;
  }
}

void setup() {
  Serial.begin(115200);
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(HOSTNAME);
  WiFi.setAutoReconnect(false);  // reconnects are scheduled around scans instead
  uint64_t mac = ESP.getEfuseMac(); char macText[18];
  snprintf(macText, sizeof(macText), "%02X:%02X:%02X:%02X:%02X:%02X", unsigned(mac & 255), unsigned((mac >> 8) & 255),
           unsigned((mac >> 16) & 255), unsigned((mac >> 24) & 255), unsigned((mac >> 32) & 255), unsigned((mac >> 40) & 255));
  device = macText; newSession();
  sntp_set_time_sync_notification_cb(onNtpSync);
  SerialGPS.begin(9600, SERIAL_8N1, 16, 17);
  setupOta();
  mounted = LittleFS.begin(false);  // never formats automatically
  if (mounted) bootRecord(); else error("NOT_MOUNTED");
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { if (overflow) error("COMMAND_TOO_LONG"); else handleCommand(command); command = ""; overflow = false; }
    else if (c != '\r' && !overflow) { if (command.length() < 128) command += c; else { overflow = true; command = ""; } }
  }
  while (SerialGPS.available()) gps.encode(SerialGPS.read());
  if (otaStarted) ArduinoOTA.handle();
  if (otaActive) { delay(1); return; }
  serviceWifi();
  serviceClock();
  serviceGpsEvents();
  serviceScan();
  delay(5);
}
