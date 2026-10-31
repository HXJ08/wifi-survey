"""Deterministic synthetic-board simulation of the production Mac CLI.
Does not execute ESP32 firmware or emulate its radio/flash hardware.
Run with the environment containing pyserial: python tests/simulate.py
"""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import random
import sqlite3
import sys
import tempfile
from unittest.mock import patch
import zlib
import serial

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('survey',ROOT/'survey.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)

class Rig:
    def __init__(self):
        self.session='sim-session-01';self.records=[];self.sequence=0;self.paused=False
        self.fault=False;self.mode='normal';self.buffer=bytearray();self.commands=[]
        self.rng=random.Random(20261029);self.scan_ms=0;self.closed=False
        self.record('boot',ms=0,interval_ms=30000,passive=True)
    def record(self,kind,**values):
        self.sequence+=1
        r=dict(v=2,type=kind,device='SIMULATED-ESP32-S3',session=self.session,record_id=self.sequence,**values)
        self.records.append(r)
    def scans(self,count):
        for i in range(count):
            aps=[];place=(i//20)%5
            n=self.rng.randint(15,35)
            for j in range(n):
                aps.append(dict(bssid=f'02:00:00:{place:02x}:00:{j:02x}',rssi_dbm=self.rng.randint(-95,-35),channel=[1,6,11][j%3],ssid_hex=f'Synthetic place {place} AP {j}'.encode().hex()))
            self.record('scan',scan_id=i+1,start_ms=self.scan_ms,end_ms=self.scan_ms+3900,result=n,aps=aps,gps=None)
            self.scan_ms+=30000
    def payload(self):
        return b''.join((json.dumps(r,separators=(',',':'))+'\n').encode() for r in self.records)
    def open(self):self.closed=False
    def __enter__(self):return self
    def __exit__(self,*args):self.closed=True
    def reset_input_buffer(self):self.buffer.clear()
    def flush(self):pass
    def read(self,n):
        if not self.buffer:raise OSError('Simulated USB unplug / no further bytes')
        # Simulates serial chunks that do not align with JSON records.
        n=min(n,self.rng.randint(1,251),len(self.buffer))
        result=bytes(self.buffer[:n]);del self.buffer[:n];return result
    def write(self,b):
        cmd=b.decode().strip();self.commands.append(cmd)
        if cmd=='status':
            st=dict(protocol=2,device='SIMULATED-ESP32-S3',session=self.session,ms=self.scan_ms,mounted=True,paused=self.paused,scanning=False,fault=self.fault,used=len(self.payload()),total=1441792)
            self.buffer.extend(b'STATUS '+json.dumps(st).encode()+b'\n')
        elif cmd=='pause':self.paused=True;self.buffer.extend(b'OK PAUSE\n')
        elif cmd=='resume':
            assert not self.fault
            self.paused=False;self.buffer.extend(b'OK RESUME\n')
        elif cmd.startswith('time '):
            if self.fault:self.buffer.extend(b'ERR TIME_NOT_SAVED\n')
            else:
                self.record('clock_anchor',ms=self.scan_ms,utc_ms=int(cmd.split()[1]),source='serial_host')
                self.buffer.extend(f'OK TIME {self.session} {self.scan_ms}\n'.encode())
        elif cmd=='dump':
            data=self.payload();crc=zlib.crc32(data)
            if self.mode=='corrupt':data=data[:20]+bytes([data[20]^1])+data[21:]
            self.buffer.extend(f'BEGIN {len(data)} {crc:08x}\n'.encode())
            if self.mode=='unplug':self.buffer.extend(data[:len(data)//2])
            else:self.buffer.extend(data+b'\nEND\n')
            if self.mode=='reboot':self.session='sim-session-reboot'
            if self.mode=='append':self.record('scan_error',ms=self.scan_ms,scan_id=999)
        elif cmd.startswith('clear '):
            _,n,c=cmd.split();data=self.payload()
            if not self.paused:self.buffer.extend(b'ERR PAUSE_FIRST\n')
            elif len(data)==int(n) and zlib.crc32(data)==int(c,16):
                self.records=[];self.fault=False;self.buffer.extend(f'OK CLEAR {len(data)}\n'.encode())
            else:self.buffer.extend(b'ERR CLEAR_MISMATCH\n')
        else:raise AssertionError('Unexpected or destructive command: '+cmd)
        return len(b)


def collect(rig,directory):
    out=io.StringIO()
    with patch.object(serial,'Serial',return_value=rig),patch.object(sys,'argv',['survey.py','collect','--port','SIMULATED','--data',str(directory)]),patch.object(s.time,'sleep'),contextlib.redirect_stdout(out):
        s.main()
    return out.getvalue()

def counts(directory):
    with contextlib.closing(sqlite3.connect(directory/'survey.sqlite3')) as db:
        return dict(db.execute('SELECT kind,count(*) FROM records GROUP BY kind').fetchall())

def run():
    results=[]
    with tempfile.TemporaryDirectory(prefix='wifi-survey-simulation-') as tmp:
        root=Path(tmp);rig=Rig();rig.scans(240)
        # This is two hours of synthetic scans, generated instantly.
        original_scan_bytes=[s.canonical(r) for r in rig.records if r['type']=='scan']
        output=collect(rig,root/'normal');assert counts(root/'normal')['scan']==240
        assert not rig.paused;assert rig.closed;assert rig.records==[]  # auto-cleared
        first_size=len(rig.payload())
        results.append(dict(scenario='Two-hour survey / fragmented serial download',passed=True,scans=240,bytes=first_size))
        collect(rig,root/'normal');assert counts(root/'normal')['scan']==240
        latest=max((root/'normal'/'raw').glob('*.report.json'),key=lambda p:p.stat().st_mtime_ns)
        rep=json.loads(latest.read_text());assert rep['duplicates']==0 and rep['added']==1
        results.append(dict(scenario='Repeated download after auto-clear',passed=True,old_records_reexported=0,new_clock_anchors=1))
        rig.scans(60);collect(rig,root/'normal');assert counts(root/'normal')['scan']==300
        with contextlib.closing(sqlite3.connect(root/'normal'/'survey.sqlite3')) as db:
            saved={row[0] for row in db.execute("SELECT payload FROM records WHERE kind='scan'")}
        assert set(original_scan_bytes)<=saved
        results.append(dict(scenario='Incremental survey download',passed=True,new_scans=60,total_scans=300))
        for mode,expected in [('corrupt',s.ProtocolError),('unplug',OSError),('reboot',s.ProtocolError)]:
            r=Rig();r.scans(30);r.mode=mode;destination=root/mode
            try:collect(r,destination)
            except expected:pass
            else:raise AssertionError('Failure not detected: '+mode)
            assert not list(destination.glob('raw/*.jsonl'))
            assert not (destination/'survey.sqlite3').exists()
            assert len([x for x in r.records if x['type']=='scan'])==30
            r.mode='normal';collect(r,destination)
            assert counts(destination)['scan']==30
            results.append(dict(scenario=mode+' during transfer, then retry',passed=True,invalid_exports_accepted=0,recovered_scans=30))
        r=Rig();r.scans(240);r.fault=True
        output=collect(r,root/'full');assert 'Clock anchor not saved' in output
        assert counts(root/'full')['scan']==240;assert not r.paused and not r.fault and r.records==[]
        assert 'resume' in r.commands
        results.append(dict(scenario='Full device: export, verified clear, recording resumes',passed=True,exported_scans=240))
        r=Rig();r.scans(30);r.mode='append'
        output=collect(r,root/'append');assert 'NOT cleared' in output
        assert len([x for x in r.records if x['type']=='scan'])==30 and counts(root/'append')['scan']==30
        results.append(dict(scenario='Board changed after export: clear refused, nothing deleted',passed=True))
        r=Rig();r.scans(3);r.paused=True;collect(r,root/'paused');assert r.paused
        results.append(dict(scenario='Initially paused device stays paused',passed=True))
        # Simulates firmware isolating an interrupted write at its next boot.
        torn=b'{"v":2,"type":"scan"\n'
        r2=Rig();r2.scans(3);r2.record('clock_anchor',ms=0,utc_ms=1,source='serial_host')
        payload=r2.payload()+torn
        path,report=s.persist(payload,root/'torn')
        assert report['added']==5 and len(report['rejected'])==1 and path.read_bytes()==payload
        results.append(dict(scenario='Torn record quarantine',passed=True,valid_records=5,rejected_lines=1))
        assert all('FORMAT' not in cmd for cmd in rig.commands)
        summary=dict(simulation='Synthetic board responses passed through the production Mac CLI; not a firmware or hardware emulator',seed=20261029,passed=len(results),failed=0,results=results,capacity_estimate=dict(observed_bytes_per_scan_including_overhead=round(first_size/240,1),note='Synthetic 15–35 AP scans. Actual storage depends on observed networks and filesystem overhead.'))
        return summary

if __name__=='__main__':
    result=run()
    dest=ROOT/'simulation-results.json'
    dest.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
