#pragma once
// Lightweight NMEA-0183 parser (RMC + GGA, GP/GN/GB/BD talkers) for ATGM336H / u-blox style modules.
// Also extracts UTC time from RMC so a GPS fix can anchor the clock without Wi-Fi.
#include <Arduino.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

class NmeaGps {
public:
  bool valid = false;            // last RMC/GGA said "fix"
  double lat = 0.0, lon = 0.0;
  float alt_m = 0.0f, hdop = 99.9f;
  int satellites = 0;            // satellites used (from GGA), updated even without a fix
  uint64_t lastFixMs = 0;        // device ms of last valid position
  uint64_t lastSentenceMs = 0;   // device ms of last checksum-valid sentence (receiver alive?)
  uint64_t utcMs = 0;            // UTC ms from last valid RMC
  uint64_t utcAtMs = 0;          // device ms when utcMs was received

  void encode(char c) {
    if (c == '\r' || c == '\n') {
      if (len > 6 && buf[0] == '$' && checksumOk()) parse();
      len = 0; return;
    }
    if (len < sizeof(buf) - 1) { buf[len++] = c; buf[len] = 0; } else len = 0;
  }
  bool alive(uint64_t now) const { return lastSentenceMs && now - lastSentenceMs < 5000; }
  bool hasQualityFix(uint64_t now, float maxHdop = 2.5f, uint64_t maxAgeMs = 3000) const {
    return valid && lastFixMs && now - lastFixMs <= maxAgeMs && hdop <= maxHdop;
  }
  bool hasFreshUtc(uint64_t now) const { return utcMs && now - utcAtMs < 2000; }

  // "gps" stays null unless the fix is fresh and good, so bad/indoor fixes never pollute coordinates.
  // "gnss" always reports receiver state so indoor/no-fix periods are visible in the data.
  String json(uint64_t now) const {
    char out[200];
    String s;
    if (hasQualityFix(now)) {
      snprintf(out, sizeof(out), "\"gps\":{\"lat\":%.7f,\"lon\":%.7f,\"alt_m\":%.1f,\"hdop\":%.2f,\"sats\":%d}",
               lat, lon, alt_m, hdop, satellites);
      s = out;
    } else s = "\"gps\":null";
    if (!alive(now)) return s + ",\"gnss\":{\"present\":false}";
    long long age = lastFixMs ? (long long)(now - lastFixMs) : -1;
    snprintf(out, sizeof(out), ",\"gnss\":{\"present\":true,\"fix\":%s,\"sats\":%d,\"hdop\":%.2f,\"fix_age_ms\":%lld}",
             valid ? "true" : "false", satellites, hdop, age);
    return s + out;
  }

private:
  char buf[128];
  size_t len = 0;
  static uint64_t ms() { return esp_timer_get_time() / 1000ULL; }
  static int hex(char c) { return c >= '0' && c <= '9' ? c - '0' : c >= 'A' && c <= 'F' ? c - 'A' + 10 : c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1; }
  bool checksumOk() const {
    const char *star = strchr(buf, '*');
    if (!star || star - buf + 3 > (int)len) return false;
    uint8_t x = 0; for (const char *p = buf + 1; p < star; p++) x ^= (uint8_t)*p;
    int hi = hex(star[1]), lo = hex(star[2]);
    return hi >= 0 && lo >= 0 && x == (hi << 4 | lo);
  }
  static double coord(const char *v, const char *h) {
    if (!v || !*v) return NAN;
    double raw = atof(v); int d = (int)(raw / 100.0);
    double deg = d + (raw - d * 100.0) / 60.0;
    return (h && (*h == 'S' || *h == 'W')) ? -deg : deg;
  }
  static int split(char *line, char **t, int maxT) {
    int n = 0; t[n++] = line;
    for (; *line && n < maxT; line++) if (*line == ',' || *line == '*') { *line = 0; t[n++] = line + 1; }
    return n;
  }
  static int64_t daysFromCivil(int y, unsigned m, unsigned d) {
    y -= m <= 2; const int era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = (unsigned)(y - era * 400);
    const unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return (int64_t)era * 146097 + (int64_t)doe - 719468;
  }
  void parse() {
    const char *type = buf + 3;  // skip "$xx"
    char tmp[128]; strncpy(tmp, buf, sizeof(tmp)); tmp[sizeof(tmp) - 1] = 0;
    char *t[20]; int n = split(tmp, t, 20);
    uint64_t now = ms(); lastSentenceMs = now;
    if (strncmp(type, "RMC", 3) == 0 && n >= 10) {
      bool active = t[2][0] == 'A';
      if (active) {
        double la = coord(t[3], t[4]), lo = coord(t[5], t[6]);
        if (!isnan(la) && !isnan(lo)) { lat = la; lon = lo; valid = true; lastFixMs = now; }
        // UTC: t[1]=hhmmss.sss, t[9]=ddmmyy
        if (strlen(t[1]) >= 6 && strlen(t[9]) == 6) {
          int hh = (t[1][0]-'0')*10 + t[1][1]-'0', mi = (t[1][2]-'0')*10 + t[1][3]-'0', ss = (t[1][4]-'0')*10 + t[1][5]-'0';
          int frac = t[1][6] == '.' ? (int)(atof(t[1] + 6) * 1000) : 0;
          int dd = (t[9][0]-'0')*10 + t[9][1]-'0', mo = (t[9][2]-'0')*10 + t[9][3]-'0', yy = (t[9][4]-'0')*10 + t[9][5]-'0';
          if (mo >= 1 && mo <= 12 && dd >= 1 && dd <= 31 && hh < 24 && mi < 60 && ss < 61) {
            int64_t days = daysFromCivil(2000 + yy, mo, dd);
            utcMs = (uint64_t)(((days * 24 + hh) * 60 + mi) * 60 + ss) * 1000ULL + frac;
            utcAtMs = now;
          }
        }
      } else valid = false;
    } else if (strncmp(type, "GGA", 3) == 0 && n >= 10) {
      int q = atoi(t[6]);
      satellites = atoi(t[7]);
      if (*t[8]) hdop = atof(t[8]);
      if (q > 0) {
        double la = coord(t[2], t[3]), lo = coord(t[4], t[5]);
        if (!isnan(la) && !isnan(lo)) { lat = la; lon = lo; valid = true; lastFixMs = now; }
        if (*t[9]) alt_m = atof(t[9]);
      } else valid = false;
    }
  }
};
