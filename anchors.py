#!/usr/bin/env python3
"""Access-point classification and doorway pinning for wifi-survey.

Inspired by how phone Wi-Fi Positioning Systems keep their router maps clean, every
access point (BSSID) is classified as:

  anchor   fixed router: always seen with the same neighbours, in one small area
  mobile   moves with people: hotspot-style name, heard strongly from places too far apart,
           seen with constantly changing neighbours (e.g. a phone hotspot you carry), or a
           randomized MAC that was not seen enough to prove it stays put
  moved    was fixed in one place, then fixed somewhere else (only the newest place is used)
  unknown  too few sightings to judge (kept, but low confidence)

It also turns GPS entry/exit events (firmware 3.2) into weak position labels: scans just
after `gps_lost` are pinned near `last_fix` while they still look like the doorway scan.
Short lost/regained flickers at building edges are ignored.

Pure Python (no third-party packages). Data never leaves this machine.
"""
import argparse
import json
import math
import random
import re
from collections import defaultdict
from pathlib import Path

# --- Tunables (documented in README) ---
MIN_SIGHTINGS = 4             # sightings needed before judging an AP
MOBILE_NEIGHBOUR_JACCARD = 0.08   # median neighbour overlap below this => neighbours keep changing...
MOBILE_TOP_COMPANION = 0.5    # ...and no single neighbour is seen with it this often => it moves around
                              # (walking past a fixed router also changes neighbours, but a nearby
                              #  router stays a constant companion; a carried phone has none)
MAX_ANCHOR_SPREAD_M = 200.0   # a fixed router is not heard *strongly* from points further apart
STRONG_RSSI = -80             # only strong sightings count for spread (faint ones carry 300 m+ outdoors)
MOVED_CROSS_JACCARD = 0.05    # neighbours before vs after a move barely overlap...
MOVED_SIDE_JACCARD = 0.15     # ...while each side on its own is consistent
GPS_MAX_AGE_MS = 5000         # older fixes are stale (indoors) and never used as labels
GPS_MAX_HDOP = 2.5
MIN_INDOOR_MS = 60000         # lost->regained shorter than this is edge flicker, not an entry
DOORWAY_MATCH_JACCARD = 0.3   # keep pinning while scans still share this much with the doorway scan
DOORWAY_RADIUS_M = 40.0       # uncertainty of a doorway-pinned scan
DOORWAY_WEIGHT = 0.3          # training weight vs a real GPS fix (1.0)

HOTSPOT_SSID = re.compile(
    r"\b(iphone|ipad|android|galaxy|pixel|oneplus|redmi|xiaomi|huawei|hotspot|mifi|jetpack|moto)\b"
    r"|'s (phone|iphone|ipad|pixel|galaxy)\b", re.IGNORECASE)  # not DIRECT-: mostly printers/TVs, which stay put


# ---------- loading ----------

def iter_files(inputs):
    for inp in inputs:
        p = Path(inp)
        yield from (sorted(p.glob("*.jsonl")) if p.is_dir() else [p])


def load_records(inputs):
    """Load records from JSONL files/directories, de-duplicated by device/session/record_id.

    Exports overlap when the board is not cleared, so the same scan can appear in many
    files; counting it once matters for every statistic below.
    """
    seen, records = set(), []
    for f in iter_files(inputs):
        for line in Path(f).read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = (r.get("device"), r.get("session"), r.get("record_id")) if r.get("record_id") else line
            if key in seen:
                continue
            seen.add(key)
            records.append(r)
    return records


def split_records(records):
    scans = [r for r in records if r.get("type") == "scan" and r.get("result", 0) >= 0]
    anchors = defaultdict(list)
    events = defaultdict(list)
    for r in records:
        key = (r.get("device"), r.get("session"))
        if r.get("type") == "clock_anchor":
            anchors[key].append(r)
        elif r.get("type") in ("gps_lost", "gps_regained"):
            events[key].append(r)
    return scans, anchors, events


def scan_time(scan, anchors):
    """UTC ms of a scan: recorded utc_ms, else nearest clock anchor in the same session."""
    if scan.get("utc_ms"):
        return scan["utc_ms"]
    aa = anchors.get((scan.get("device"), scan.get("session")))
    if not aa:
        return None
    a = min(aa, key=lambda x: abs(x["ms"] - scan["start_ms"]))
    return a["utc_ms"] + scan["start_ms"] - a["ms"]


def fresh_gps(scan):
    """(lat, lon) only for a fresh, good-quality fix; stale indoor fixes are rejected."""
    g = scan.get("gps")
    if not isinstance(g, dict) or "lat" not in g or "lon" not in g:
        return None
    if g.get("hdop", 0) > GPS_MAX_HDOP or g.get("age_ms", 0) > GPS_MAX_AGE_MS:
        return None
    return float(g["lat"]), float(g["lon"])


# ---------- geometry / sets ----------

def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371000.0 * math.asin(math.sqrt(min(1.0, a)))


def jaccard(a, b):
    u = len(a | b)
    return len(a & b) / u if u else 0.0


def is_laa(bssid):
    try:
        return bool(int(bssid.split(":")[0], 16) & 0x02)
    except ValueError:
        return False


def ssid_of(ap):
    try:
        return bytes.fromhex(ap.get("ssid_hex", "")).decode("utf-8", "replace")
    except ValueError:
        return ""


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2) if n else 0.0


def pair_overlaps(sets, rng, max_pairs=300):
    """Jaccard overlap of neighbour sets over (sampled) pairs of sightings."""
    n = len(sets)
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    if len(pairs) > max_pairs:
        pairs = rng.sample(pairs, max_pairs)
    return [jaccard(sets[i], sets[j]) for i, j in pairs]


# ---------- doorway pinning ----------

def doorway_labels(scans, events):
    """Weak labels {scan_index: (lat, lon, radius_m, weight)} from GPS entry/exit events.

    Entry: scans after gps_lost are pinned to last_fix while they still share
    DOORWAY_MATCH_JACCARD of their networks with the doorway scan. Exit: scans just before
    gps_regained are pinned to the regained fix the same way, walking backwards.
    Lost->regained stretches shorter than MIN_INDOOR_MS are flicker and ignored.
    """
    by_session = defaultdict(list)
    for i, s in enumerate(scans):
        by_session[(s.get("device"), s.get("session"))].append(i)
    labels = {}
    for key, evs in events.items():
        idx = sorted(by_session.get(key, []), key=lambda i: scans[i]["start_ms"])
        if not idx:
            continue
        evs = sorted(evs, key=lambda e: e["ms"])
        for k, ev in enumerate(evs):
            if ev["type"] != "gps_lost":
                continue
            nxt = next((e for e in evs[k + 1:] if e["type"] == "gps_regained"), None)
            end_ms = nxt["ms"] if nxt else float("inf")
            if end_ms - ev["ms"] < MIN_INDOOR_MS:
                continue  # edge flicker, not a real entry
            fix = ev["last_fix"]
            inside = [i for i in idx if ev["ms"] <= scans[i]["start_ms"] < end_ms]
            _pin(scans, inside, (fix["lat"], fix["lon"]), labels)
            if nxt:
                f2 = nxt["fix"]
                _pin(scans, list(reversed(inside)), (f2["lat"], f2["lon"]), labels)
    return labels


def _pin(scans, ordered, where, labels):
    if not ordered:
        return
    door = {a["bssid"].upper() for a in scans[ordered[0]]["aps"]}
    for i in ordered:
        if jaccard(door, {a["bssid"].upper() for a in scans[i]["aps"]}) < DOORWAY_MATCH_JACCARD:
            break  # walked deeper into the building: stop pinning
        labels.setdefault(i, (where[0], where[1], DOORWAY_RADIUS_M, DOORWAY_WEIGHT))


# ---------- classification ----------

def classify(scans, anchors=None, events=None, seed=20261031):
    """Classify every BSSID. Returns {bssid: {"class", "reasons", ...stats}}."""
    anchors = anchors or {}
    rng = random.Random(seed)
    sightings = defaultdict(list)  # bssid -> [(time, neighbour set, position or None, ssid)]
    for i, s in enumerate(scans):
        bssids = {a["bssid"].upper() for a in s["aps"] if not is_laa(a["bssid"])}
        pos = fresh_gps(s)
        t = scan_time(s, anchors)
        for a in s["aps"]:
            b = a["bssid"].upper()
            sightings[b].append((t, bssids - {b}, pos, ssid_of(a), a["rssi_dbm"]))

    result = {}
    for b, sig in sightings.items():
        ssid = next((x[3] for x in reversed(sig) if x[3]), "")
        info = {"ssid": ssid, "sightings": len(sig), "reasons": []}
        result[b] = info
        if ssid and HOTSPOT_SSID.search(ssid):
            info.update({"class": "mobile"}); info["reasons"].append(f"hotspot-style name '{ssid}'")
            continue

        moved = _moved_split(sig, rng)
        if moved:
            info.update({"class": "moved", "moved_at_utc_ms": moved})
            info["reasons"].append("neighbours changed completely at one point in time; using newest location only")
            sig = [x for x in sig if x[0] is not None and x[0] >= moved]

        spread = _spread_m([x[2] for x in sig if x[2] and x[4] >= STRONG_RSSI])
        info["gps_spread_m"] = round(spread, 1) if spread is not None else None
        if spread is not None and spread > MAX_ANCHOR_SPREAD_M:
            info.update({"class": "mobile"}); info["reasons"].append(f"heard strongly {spread:.0f} m apart (too far for one fixed router)")
            continue

        overlap = median(pair_overlaps([x[1] for x in sig], rng)) if len(sig) >= 2 else None
        info["neighbour_overlap"] = round(overlap, 3) if overlap is not None else None
        companion = _top_companion([x[1] for x in sig])
        info["top_companion"] = round(companion, 3)
        if info.get("class") == "moved":
            continue
        if len(sig) < MIN_SIGHTINGS:
            if is_laa(b):  # phones rotate randomized MACs; unproven ones are not trusted
                info["class"] = "mobile"; info["reasons"].append(f"randomized MAC seen only {len(sig)} times")
            else:
                info["class"] = "unknown"; info["reasons"].append(f"only {len(sig)} sightings")
        elif overlap < MOBILE_NEIGHBOUR_JACCARD and companion < MOBILE_TOP_COMPANION:
            info["class"] = "mobile"
            info["reasons"].append(f"neighbours keep changing (median overlap {overlap:.2f}, "
                                   f"no constant companion: best {companion:.0%})")
        else:
            info["class"] = "anchor"
            if overlap < MOBILE_NEIGHBOUR_JACCARD:
                info["reasons"].append(f"seen while passing by (constant companion {companion:.0%} of the time)")
            if is_laa(b):
                info["reasons"].append("randomized MAC but stays put (e.g. printer or smart speaker)")
    return result


def _top_companion(neighbour_sets):
    """Share of sightings in which the most frequent neighbour is also present."""
    counts = defaultdict(int)
    for n in neighbour_sets:
        for b in n:
            counts[b] += 1
    return max(counts.values()) / len(neighbour_sets) if counts else 0.0


def _spread_m(points):
    """Distance from the median point to the furthest sighting (robust to one bad fix)."""
    if len(points) < 2:
        return None
    lat = median([p[0] for p in points]); lon = median([p[1] for p in points])
    d = sorted(haversine_m(lat, lon, p[0], p[1]) for p in points)
    return d[min(len(d) - 1, int(0.9 * len(d)))] * 2  # ~diameter, ignoring the worst 10%


def _moved_split(sig, rng):
    """Earliest UTC time after which the AP's neighbours are a different, consistent set."""
    timed = sorted((x for x in sig if x[0] is not None), key=lambda x: x[0])
    if len(timed) < 2 * MIN_SIGHTINGS:
        return None
    best = None
    for k in range(MIN_SIGHTINGS, len(timed) - MIN_SIGHTINGS + 1):
        before, after = timed[:k], timed[k:]
        cross = jaccard(set().union(*(x[1] for x in before)), set().union(*(x[1] for x in after)))
        if cross >= MOVED_CROSS_JACCARD:
            continue
        if (median(pair_overlaps([x[1] for x in before], rng)) >= MOVED_SIDE_JACCARD and
                median(pair_overlaps([x[1] for x in after], rng)) >= MOVED_SIDE_JACCARD):
            if best is None or cross < best[0]:
                best = (cross, after[0][0])
    return best[1] if best else None


def summary(classes):
    counts = defaultdict(int)
    for v in classes.values():
        counts[v["class"]] += 1
    return dict(sorted(counts.items()))


def main():
    p = argparse.ArgumentParser(description="Classify access points as anchor / mobile / moved / unknown.")
    p.add_argument("inputs", nargs="+", help="JSONL exports or directories (duplicates across files are merged)")
    p.add_argument("--out", default="analysis/ap-classes.json", help="Where to write the per-AP report")
    a = p.parse_args()
    scans, anchors, events = split_records(load_records(a.inputs))
    classes = classify(scans, anchors, events)
    pinned = doorway_labels(scans, events)
    out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(classes, indent=2, sort_keys=True) + "\n")
    print(f"{len(scans)} unique scans, {len(classes)} access points: {summary(classes)}")
    print(f"GPS-labelled scans: {sum(1 for s in scans if fresh_gps(s))}, doorway-pinned scans: {len(pinned)}")
    mobile = sorted(((v['sightings'], b, v) for b, v in classes.items() if v['class'] == 'mobile'), reverse=True)[:10]
    for n, b, v in mobile:
        print(f"  mobile {b} '{v['ssid']}' x{n}: {'; '.join(v['reasons'])}")
    print(f"Saved per-AP report to {out}")


if __name__ == "__main__":
    main()
