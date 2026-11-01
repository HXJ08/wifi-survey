import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('anchors', ROOT / 'anchors.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

T0 = 1_793_400_000_000  # arbitrary UTC ms
BASE_LAT, BASE_LON = 43.4710, -80.5390


def mac(place, i):
    return f'00:11:{place:02x}:00:00:{i:02x}'


def ap(bssid, rssi=-60, ssid=''):
    return dict(bssid=bssid, rssi_dbm=rssi, channel=6, ssid_hex=ssid.encode().hex())


def scan(sid, aps, t=None, session='s1', start_ms=None, gps=None, rid=None):
    r = dict(v=2, type='scan', device='d', session=session, record_id=rid or sid, scan_id=sid,
             start_ms=start_ms if start_ms is not None else sid * 60000, end_ms=0, result=len(aps), aps=aps, gps=gps)
    if t is not None:
        r['utc_ms'] = t
    return r


def place_aps(place, n=8):
    return [ap(mac(place, i)) for i in range(n)]


class Classify(unittest.TestCase):
    def test_fixed_router_is_anchor(self):
        scans = [scan(i, place_aps(1)) for i in range(6)]
        c = m.classify(scans)
        self.assertEqual(c[mac(1, 0).upper()]['class'], 'anchor')

    def test_carried_hotspot_is_mobile(self):
        # The same device shows up in five unrelated places: its neighbours keep changing.
        companion = ap('00:aa:bb:cc:dd:ee', -40, 'Rye-Bread')
        scans = [scan(i, place_aps(p) + [companion]) for i, p in enumerate([1, 2, 3, 4, 5, 6])]
        c = m.classify(scans)
        self.assertEqual(c['00:AA:BB:CC:DD:EE']['class'], 'mobile')
        self.assertIn('neighbours keep changing', c['00:AA:BB:CC:DD:EE']['reasons'][0])
        self.assertEqual(c[mac(1, 0).upper()]['class'], 'unknown')  # only one sighting

    def test_router_seen_while_walking_past_stays_anchor(self):
        # Neighbours shift along a street, but the next-door router is always there too.
        router, neighbour = ap('00:ee:00:00:00:01'), ap('00:ee:00:00:00:02')
        scans = [scan(i, place_aps(p) + [router, neighbour]) for i, p in enumerate([1, 2, 3, 4, 5, 6])]
        info = m.classify(scans)['00:EE:00:00:00:01']
        self.assertEqual(info['class'], 'anchor')
        self.assertIn('passing by', info['reasons'][0])

    def test_randomized_mac_and_hotspot_name(self):
        printer = ap('66:6c:80:fb:4c:c5', ssid='DIRECT-c5-HP LaserJet')
        scans = [scan(i, place_aps(1) + [printer, ap('00:99:99:99:99:99', ssid="Jack's iPhone")]) for i in range(5)]
        scans.append(scan(9, place_aps(1) + [ap('02:12:34:56:78:9a')]))
        c = m.classify(scans)
        self.assertEqual(c['00:99:99:99:99:99']['class'], 'mobile')
        self.assertIn('hotspot', c['00:99:99:99:99:99']['reasons'][0])
        self.assertEqual(c['66:6C:80:FB:4C:C5']['class'], 'anchor')   # randomized MAC, but stays put
        self.assertEqual(c['02:12:34:56:78:9A']['class'], 'mobile')   # randomized MAC seen once

    def test_seen_too_far_apart_is_mobile(self):
        bus = ap('00:bb:00:00:00:01', -55, ssid='Transit')
        gps_a = dict(lat=BASE_LAT, lon=BASE_LON, hdop=1.2, age_ms=0)
        gps_b = dict(lat=BASE_LAT + 0.01, lon=BASE_LON, hdop=1.2, age_ms=0)  # ~1.1 km north
        scans = [scan(i, place_aps(1) + [bus], gps=gps_a) for i in range(3)]
        scans += [scan(10 + i, place_aps(1) + [bus], gps=gps_b) for i in range(3)]
        c = m.classify(scans)
        self.assertEqual(c['00:BB:00:00:00:01']['class'], 'mobile')
        self.assertIn('m apart', c['00:BB:00:00:00:01']['reasons'][0])

    def test_faint_far_sightings_do_not_make_a_router_mobile(self):
        router = '00:dd:00:00:00:01'
        near = dict(lat=BASE_LAT, lon=BASE_LON, hdop=1.2, age_ms=0)
        far = dict(lat=BASE_LAT, lon=BASE_LON + 0.004, hdop=1.2, age_ms=0)  # ~320 m east
        scans = [scan(i, place_aps(1) + [ap(router, -60)], gps=near) for i in range(3)]
        scans += [scan(10 + i, place_aps(1) + [ap(router, -92)], gps=far) for i in range(3)]
        self.assertEqual(m.classify(scans)['00:DD:00:00:00:01']['class'], 'anchor')

    def test_router_that_moved(self):
        router = ap('00:cc:00:00:00:01')
        before = [scan(i, place_aps(1) + [router], t=T0 + i * 60000) for i in range(5)]
        after = [scan(10 + i, place_aps(2) + [router], t=T0 + 86_400_000 + i * 60000, rid=10 + i) for i in range(5)]
        c = m.classify(before + after)
        info = c['00:CC:00:00:00:01']
        self.assertEqual(info['class'], 'moved')
        self.assertEqual(info['moved_at_utc_ms'], T0 + 86_400_000)

    def test_stale_gps_is_not_a_label(self):
        self.assertIsNone(m.fresh_gps(scan(1, [], gps=dict(lat=1, lon=2, hdop=1.0, age_ms=600000))))
        self.assertIsNone(m.fresh_gps(scan(1, [], gps=dict(lat=1, lon=2, hdop=7.0))))
        self.assertEqual(m.fresh_gps(scan(1, [], gps=dict(lat=1, lon=2, hdop=1.0, age_ms=0))), (1.0, 2.0))


class Doorway(unittest.TestCase):
    def events(self, lost_ms, regained_ms):
        fix = dict(lat=BASE_LAT, lon=BASE_LON, age_ms=20000)
        return {('d', 's1'): [dict(type='gps_lost', ms=lost_ms, last_fix=fix),
                              dict(type='gps_regained', ms=regained_ms, fix=dict(fix, age_ms=0))]}

    def test_pins_scans_near_the_door_then_stops(self):
        door, deep = place_aps(1), place_aps(9)
        scans = [scan(1, door, start_ms=100_000), scan(2, door[:7], start_ms=160_000),
                 scan(3, deep, start_ms=220_000), scan(4, door, start_ms=280_000)]
        labels = m.doorway_labels(scans, self.events(100_000, 900_000))
        self.assertEqual(sorted(labels), [0, 1, 3])  # scan 3 is deep inside; scan 4 matches the exit
        self.assertEqual(labels[0][:2], (BASE_LAT, BASE_LON))
        self.assertEqual(labels[0][3], m.DOORWAY_WEIGHT)

    def test_short_flicker_is_ignored(self):
        scans = [scan(1, place_aps(1), start_ms=100_000)]
        self.assertEqual(m.doorway_labels(scans, self.events(100_000, 130_000)), {})


class Loading(unittest.TestCase):
    def test_overlapping_exports_are_deduplicated(self):
        rows = [scan(i, place_aps(1)) for i in range(3)]
        with tempfile.TemporaryDirectory() as d:
            for name, part in (('a.jsonl', rows[:2]), ('b.jsonl', rows)):
                Path(d, name).write_text(''.join(json.dumps(r) + '\n' for r in part))
            self.assertEqual(len(m.load_records([d])), 3)


if __name__ == '__main__':
    unittest.main()
