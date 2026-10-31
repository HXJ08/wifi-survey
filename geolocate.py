#!/usr/bin/env python3
"""
Wi-Fi Geolocation & Positioning System (WPS) Engine.
Learns router positions from GPS-tagged or anchored Wi-Fi scans and predicts
coordinates without GPS using Weighted Centroid Trilateration and k-NN fingerprinting.
"""

import argparse
import csv
import json
import math
from pathlib import Path
import numpy as np

EARTH_RADIUS_METERS = 6371000.0


def haversine_meters(lat1, lon1, lat2, lon2):
    """Calculates great-circle distance between two GPS points in meters."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2.0) ** 2 +
         math.cos(phi1) * math.cos(phi2) * (math.sin(dlambda / 2.0) ** 2))
    return 2.0 * EARTH_RADIUS_METERS * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def is_laa_bssid(bssid_str):
    """Returns True if MAC address has Locally Administered Address (LAA) bit set."""
    try:
        first_byte = int(bssid_str.split(':')[0], 16)
        return bool(first_byte & 0x02)
    except Exception:
        return False


def clean_scan(scan, min_rssi=-82, filter_laa=True, dedup_virtual_bssids=True):
    """Filters noisy, mobile, or duplicate virtual BSSIDs from a scan."""
    cleaned_aps = []
    seen_prefixes = {}

    for ap in scan.get("aps", []):
        rssi = ap.get("rssi_dbm", -100)
        if min_rssi is not None and rssi < min_rssi:
            continue
        bssid = ap.get("bssid", "").upper()
        if filter_laa and is_laa_bssid(bssid):
            continue

        if dedup_virtual_bssids:
            # First 5 bytes identifies the physical radio device
            prefix = ":".join(bssid.split(":")[:5])
            if prefix in seen_prefixes:
                if rssi > seen_prefixes[prefix]["rssi_dbm"]:
                    seen_prefixes[prefix] = ap
                continue
            seen_prefixes[prefix] = ap
        else:
            cleaned_aps.append(ap)

    if dedup_virtual_bssids:
        cleaned_aps = list(seen_prefixes.values())

    return dict(scan, aps=cleaned_aps, result=len(cleaned_aps))


def load_scans(path, min_rssi=-82, filter_laa=True):
    """Loads all valid scan records from a JSONL export."""
    scans = []
    anchors = {}
    path = Path(path)
    if not path.exists():
        return scans, anchors

    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("type") == "clock_anchor":
            anchors.setdefault((r.get("device"), r.get("session")), []).append(r)
        elif r.get("type") == "scan" and r.get("result", 0) >= 0:
            cleaned = clean_scan(r, min_rssi=min_rssi, filter_laa=filter_laa)
            if len(cleaned["aps"]) > 0:
                scans.append(cleaned)
    return scans, anchors


def train_router_map(scans, anchors=None, manual_tags=None):
    """
    Learns physical (lat, lon) coordinates for every fixed Wi-Fi router
    using signal-weighted multilateration from GPS-tagged or anchored scans.
    """
    manual_tags = manual_tags or {}
    router_obs = {}  # bssid -> list of (lat, lon, rssi)

    for s in scans:
        coords = None
        # 1. Direct GPS fix recorded by ESP32
        if s.get("gps") and isinstance(s["gps"], dict):
            g = s["gps"]
            if "lat" in g and "lon" in g and (g.get("hdop", 1.0) <= 2.5):
                coords = (float(g["lat"]), float(g["lon"]))

        # 2. Manual session or scan anchor tag
        if coords is None:
            session = s.get("session")
            if session in manual_tags:
                coords = manual_tags[session]["coords"]
            elif "ALL" in manual_tags:
                coords = manual_tags["ALL"]["coords"]

        if coords is None:
            continue

        lat, lon = coords
        for ap in s.get("aps", []):
            bssid = ap["bssid"].upper()
            rssi = ap["rssi_dbm"]
            router_obs.setdefault(bssid, []).append((lat, lon, rssi, ap.get("ssid_hex", "")))

    router_map = {}
    for bssid, obs in router_obs.items():
        if not obs:
            continue
        # Weight by signal power: w = 10^((RSSI + 100) / 20)
        weights = [10.0 ** ((o[2] + 100.0) / 20.0) for o in obs]
        total_w = sum(weights)
        if total_w <= 0:
            continue

        w_lat = sum(o[0] * w for o, w in zip(obs, weights)) / total_w
        w_lon = sum(o[1] * w for o, w in zip(obs, weights)) / total_w

        # Spread / uncertainty estimate in meters
        distances = [haversine_meters(w_lat, w_lon, o[0], o[1]) for o in obs]
        spread_m = float(np.mean(distances)) if distances else 15.0

        ssid = ""
        try:
            ssid = bytes.fromhex(obs[-1][3]).decode("utf-8", errors="replace")
        except Exception:
            pass

        router_map[bssid] = {
            "lat": round(w_lat, 7),
            "lon": round(w_lon, 7),
            "spread_meters": round(spread_m, 1),
            "observations": len(obs),
            "max_rssi": max(o[2] for o in obs),
            "ssid": ssid
        }

    return router_map


def predict_location(scan, router_map, min_matched_routers=2):
    """
    Predicts (lat, lon) coordinates for a Wi-Fi scan using Weighted Centroid
    Trilateration against the trained router map.
    """
    matched = []
    for ap in scan.get("aps", []):
        bssid = ap["bssid"].upper()
        if bssid in router_map:
            rm = router_map[bssid]
            weight = 10.0 ** ((ap["rssi_dbm"] + 100.0) / 20.0)
            matched.append((rm["lat"], rm["lon"], weight, rm["spread_meters"], ap["rssi_dbm"]))

    if len(matched) < min_matched_routers:
        return {
            "predicted": False,
            "reason": f"Only {len(matched)} mapped routers visible (minimum {min_matched_routers} required)",
            "matched_routers": len(matched)
        }

    total_w = sum(m[2] for m in matched)
    pred_lat = sum(m[0] * m[2] for m in matched) / total_w
    pred_lon = sum(m[1] * m[2] for m in matched) / total_w

    # Estimated error radius is distance to furthest matched router weighted by signal
    err_radius = float(np.average([haversine_meters(pred_lat, pred_lon, m[0], m[1]) for m in matched],
                                   weights=[m[2] for m in matched]))

    # Confidence score 0 - 100
    confidence = min(100.0, max(10.0, (len(matched) * 18.0) - (err_radius * 0.4)))

    return {
        "predicted": True,
        "lat": round(pred_lat, 7),
        "lon": round(pred_lon, 7),
        "accuracy_meters": round(max(8.0, err_radius), 1),
        "confidence_pct": round(confidence, 1),
        "matched_routers": len(matched)
    }


def export_cpp_header(router_map, output_path):
    """
    Exports a lightweight C++ header containing the router lookup table
    for standalone, zero-GPS location determination directly on the ESP32.
    """
    lines = [
        "// Auto-generated by geolocate.py - Wi-Fi Positioning System (WPS) lookup table",
        "// Flash to ESP32 to calculate current (lat, lon) without GPS.",
        "#pragma once",
        "#include <stdint.h>",
        "",
        "struct RouterAnchor {",
        "  uint8_t bssid[6];",
        "  float lat;",
        "  float lon;",
        "};",
        "",
        f"constexpr size_t ROUTER_COUNT = {len(router_map)};",
        "const RouterAnchor ROUTER_MAP[ROUTER_COUNT] = {"
    ]

    for bssid, info in router_map.items():
        parts = [f"0x{p}" for p in bssid.split(":")]
        bssid_bytes = "{" + ", ".join(parts) + "}"
        lines.append(f"  {{{bssid_bytes}, {info['lat']:.7f}f, {info['lon']:.7f}f}}, // {info['ssid']}")

    lines.append("};")
    lines.append("")
    Path(output_path).write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Train command
    p_train = subparsers.add_parser("train", help="Train router map and fingerprint models from survey files")
    p_train.add_argument("inputs", nargs="+", help="JSONL export files or directories")
    p_train.add_argument("--out-dir", default="models", help="Directory to save trained models")
    p_train.add_argument("--tag-all", help="Tag all scans with lat,lon,place_name (e.g. '37.7749,-122.4194,Home Desk')")
    p_train.add_argument("--tag-session", action="append", nargs=2, metavar=("SESSION_ID", "LAT,LON[,NAME]"),
                         help="Tag a specific session with coordinates")
    p_train.add_argument("--export-cpp", action="store_true", help="Also export C++ header for on-device ESP32 inference")

    # Predict command
    p_pred = subparsers.add_parser("predict", help="Predict coordinates for scans using trained models")
    p_pred.add_argument("input", help="JSONL file containing scans to predict")
    p_pred.add_argument("--model", default="models/router_map.json", help="Path to trained router_map.json")
    p_pred.add_argument("--out", help="Optional CSV output file for predictions")

    args = parser.parse_args()

    if args.command == "train":
        all_scans = []
        for inp in args.inputs:
            p = Path(inp)
            files = list(p.glob("*.jsonl")) if p.is_dir() else [p]
            for f in files:
                scans, _ = load_scans(f)
                all_scans.extend(scans)

        manual_tags = {}
        if args.tag_all:
            parts = args.tag_all.split(",")
            lat, lon = float(parts[0]), float(parts[1])
            name = parts[2] if len(parts) > 2 else "Anchored Place"
            manual_tags["ALL"] = {"coords": (lat, lon), "name": name}

        if args.tag_session:
            for sess, val in args.tag_session:
                parts = val.split(",")
                manual_tags[sess] = {"coords": (float(parts[0]), float(parts[1])),
                                     "name": parts[2] if len(parts) > 2 else sess}

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        router_map = train_router_map(all_scans, manual_tags=manual_tags)
        map_path = out_dir / "router_map.json"
        map_path.write_text(json.dumps(router_map, indent=2))

        print(f"Loaded {len(all_scans)} scans across input files.")
        print(f"Successfully mapped {len(router_map)} unique fixed routers.")
        print(f"Saved router map to: {map_path}")

        if args.export_cpp:
            cpp_path = out_dir / "router_map.h"
            export_cpp_header(router_map, cpp_path)
            print(f"Exported ESP32 C++ header: {cpp_path}")

    elif args.command == "predict":
        router_map = json.loads(Path(args.model).read_text())
        scans, _ = load_scans(args.input)
        predictions = []

        for s in scans:
            pred = predict_location(s, router_map)
            pred["session"] = s.get("session")
            pred["scan_id"] = s.get("scan_id")
            pred["start_ms"] = s.get("start_ms")
            predictions.append(pred)

        successful = [p for p in predictions if p["predicted"]]
        print(f"Evaluated {len(predictions)} scans:")
        print(f"  Successfully located: {len(successful)} ({len(successful)*100.0/len(predictions):.1f}%)")
        if successful:
            median_acc = np.median([p["accuracy_meters"] for p in successful])
            print(f"  Median estimated accuracy: {median_acc:.1f} meters")

        if args.out:
            with open(args.out, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["session", "scan_id", "predicted", "lat", "lon", "accuracy_meters", "confidence_pct", "matched_routers", "reason"])
                writer.writeheader()
                for p in predictions:
                    writer.writerow({k: p.get(k, "") for k in writer.fieldnames})
            print(f"Saved predictions to: {args.out}")


if __name__ == "__main__":
    main()
