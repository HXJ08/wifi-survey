import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zlib
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('survey',Path(__file__).parents[1]/'survey.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)

def record(i=1):
    return dict(v=2,type='scan',device='test',session='session',record_id=i,scan_id=i,start_ms=0,end_ms=4000,result=1,aps=[dict(bssid='aa:bb:cc:dd:ee:ff',rssi_dbm=-60,channel=6,ssid_hex='ff00220a')],gps=None)
def encoded(r): return (json.dumps(r)+'\n').encode()

class FakePort:
    def __init__(self,data):self.data=bytearray(data);self.sent=[]
    def read(self,n):
        # Deliberately fragmented transport.
        n=min(n,7,len(self.data));out=bytes(self.data[:n]);del self.data[:n];return out
    def write(self,b):self.sent.append(b);return len(b)
    def flush(self):pass

def transfer(data,crc=None,end=b'\nEND\n'):
    return b'BEGIN '+str(len(data)).encode()+b' '+f'{zlib.crc32(data) if crc is None else crc:08x}'.encode()+b'\n'+data+end

class Tests(unittest.TestCase):
    def test_fragmented_export(self):
        data=encoded(record());port=FakePort(b'boot chatter\n'+transfer(data))
        self.assertEqual(s.Board(port).dump(),data);self.assertEqual(port.sent,[b'dump\n'])
    def test_checksum_failure(self):
        with self.assertRaisesRegex(s.ProtocolError,'Checksum'):s.Board(FakePort(transfer(encoded(record()),crc=0))).dump()
    def test_board_error(self):
        with self.assertRaisesRegex(s.ProtocolError,'NOT_MOUNTED'):s.Board(FakePort(b'ERR NOT_MOUNTED\n')).dump()
    def test_oversized_transfer(self):
        with self.assertRaisesRegex(s.ProtocolError,'size'):s.Board(FakePort(b'BEGIN 999999999 00000000\n')).dump()
    def test_dedup_and_raw_preservation(self):
        with tempfile.TemporaryDirectory() as d:
            data=encoded(record());p,a=s.persist(data,d);q,b=s.persist(data,d)
            self.assertNotEqual(p,q);self.assertEqual(p.read_bytes(),data)
            self.assertEqual(a['added'],1);self.assertEqual(b['duplicates'],1)
    def test_torn_record_quarantined(self):
        with tempfile.TemporaryDirectory() as d:
            data=encoded(record())+b'{"torn":\n'+encoded(record(2))
            p,r=s.persist(data,d);self.assertEqual(r['added'],2);self.assertEqual(len(r['rejected']),1);self.assertEqual(p.read_bytes(),data)
    def test_conflict_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            s.persist(encoded(record()),d);changed=record();changed['end_ms']=5000
            with self.assertRaisesRegex(ValueError,'Conflicting'):s.persist(encoded(record(2))+encoded(changed),d)
            with closing(sqlite3.connect(Path(d)/'survey.sqlite3')) as db:self.assertEqual(db.execute('select count(*) from records').fetchone()[0],1)
    def test_missing_and_invalid_values(self):
        for key,value in [('start_ms',-1),('end_ms',-1),('result',5),('record_id',True),('aps',None)]:
            r=record();r[key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):s.validate(r)
    def test_empty_scan(self):
        r=record();r['aps']=[];r['result']=0;s.validate(r)
    def test_failed_scan(self):
        r=record();r['aps']=[];r['result']=-2;s.validate(r)
    def test_legacy_import(self):
        r=record();r['v']=1;del r['record_id']
        with tempfile.TemporaryDirectory() as d:
            _,a=s.persist(encoded(r),d);_,b=s.persist(encoded(r),d)
            self.assertEqual(a['added'],1);self.assertEqual(b['duplicates'],1)
    def test_partial_serial_line_timeout(self):
        with self.assertRaisesRegex(s.ProtocolError,'timed out'):s.Board(FakePort(b'STAT')).line(timeout=0.01)

class StatefulPort(FakePort):
    def __init__(self):
        super().__init__(b'');self.paused=False;self.data_records=[record()]
    def write(self,b):
        super().write(b);cmd=b.decode().strip()
        if cmd=='status':
            self.data.extend(b'STATUS '+json.dumps(dict(protocol=2,device='test',session='session',paused=self.paused,scanning=False,mounted=True,fault=False)).encode()+b'\n')
        elif cmd=='pause':self.paused=True;self.data.extend(b'OK PAUSE\n')
        elif cmd.startswith('time '):
            self.data_records.append(dict(v=2,type='clock_anchor',device='test',session='session',record_id=2,ms=9000,utc_ms=int(cmd.split()[1])))
            self.data.extend(b'OK TIME session 9000\n')
        elif cmd=='dump':self.data.extend(transfer(b''.join(encoded(r) for r in self.data_records)))
        elif cmd=='resume':self.paused=False;self.data.extend(b'OK RESUME\n')
        else:raise AssertionError(cmd)
        return len(b)

class IntegrationTests(unittest.TestCase):
    def test_complete_workflow(self):
        p=StatefulPort();b=s.Board(p)
        self.assertFalse(b.status()['paused']);b.pause();b.sync();data=b.dump()
        with tempfile.TemporaryDirectory() as d:
            path,report=s.persist(data,d)
            self.assertEqual(report['added'],2)
            self.assertEqual(json.loads(path.read_text().splitlines()[1])['type'],'clock_anchor')
        b.resume();self.assertFalse(b.status()['paused'])
        self.assertFalse(any(b'FORMAT' in cmd for cmd in p.sent))
    def test_truncated_payload(self):
        p=FakePort(b'BEGIN 100 00000000\nabc')
        clock=[0.0]
        original_read=p.read
        def read(n):
            if not p.data:clock[0]+=31
            return original_read(n)
        p.read=read
        with patch.object(s.time,'monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(s.ProtocolError,'Incomplete download'):s.Board(p).dump()
    def test_incorrect_terminator(self):
        with self.assertRaisesRegex(s.ProtocolError,'end marker'):
            s.Board(FakePort(transfer(encoded(record()),end=b'\nEND_WRONG\n'))).dump()

if __name__=='__main__':unittest.main()
