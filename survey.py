#!/usr/bin/env python3
"""Local ESP32 survey download and SQLite import. No network services."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
import zlib

class ProtocolError(RuntimeError):
    pass

class Board:
    def __init__(self, port):
        self.port = port

    def send(self, command):
        self.port.write((command + "\n").encode())
        self.port.flush()

    def line(self, timeout=10):
        deadline = time.monotonic() + timeout
        data = bytearray()
        while time.monotonic() < deadline:
            chunk = self.port.read(1)
            if chunk:
                data.extend(chunk)
                if chunk == b"\n":
                    return bytes(data).strip()
                if len(data) > 16384:
                    raise ProtocolError("Oversized protocol line")
        raise ProtocolError("Board response timed out; logs have not been erased")

    def response(self, prefix, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.line(max(0.1, deadline-time.monotonic()))
            if line.startswith(b"ERR "):
                raise ProtocolError(line.decode(errors="replace"))
            if line.startswith(prefix):
                return line
        raise ProtocolError("No matching board response")

    def status(self):
        self.send("status")
        state = json.loads(self.response(b"STATUS ")[7:])
        if state.get("protocol") != 2:
            raise ProtocolError("Upload the supplied protocol-v2 firmware first")
        return state

    def pause(self):
        self.send("pause")
        self.response(b"OK PAUSE")
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            state = self.status()
            if not state["scanning"]:
                return state
            time.sleep(0.2)
        raise ProtocolError("Scan did not stop")

    def sync(self):
        utc_ms = time.time_ns()//1_000_000
        self.send(f"time {utc_ms}")
        return self.response(b"OK TIME ").decode()

    def dump(self):
        self.send("dump")
        header = self.response(b"BEGIN ", timeout=120).split()
        if len(header) != 3:
            raise ProtocolError("Invalid transfer header")
        size, expected = int(header[1]), int(header[2], 16)
        if not 0 <= size <= 64*1024*1024:
            raise ProtocolError("Unexpected transfer size")
        data = bytearray()
        deadline = time.monotonic()+max(30,size/3000)
        while len(data) < size and time.monotonic() < deadline:
            data.extend(self.port.read(min(8192,size-len(data))))
        if len(data) != size:
            raise ProtocolError("Incomplete download; reconnect and retry")
        if self.response(b"END", timeout=10) != b"END":
            raise ProtocolError("Invalid end marker")
        if zlib.crc32(data) != expected:
            raise ProtocolError("Checksum mismatch; reconnect and retry")
        return bytes(data)

    def clear(self, size, crc):
        """Board deletes its log only if it still exports exactly these verified bytes."""
        self.send(f"clear {size} {crc:08x}")
        return self.response(b"OK CLEAR", timeout=120).decode()

    def resume(self):
        self.send("resume")
        self.response(b"OK RESUME")

    def set_interval(self, interval_ms):
        self.send(f"interval {interval_ms}")
        return self.response(b"OK INTERVAL ").decode()


def canonical(record):
    return json.dumps(record,sort_keys=True,separators=(",",":"),ensure_ascii=True)


def validate(r):
    if not isinstance(r,dict) or r.get("v") not in (1,2):
        raise ValueError("Unsupported record version")
    for key in ("device","session","type"):
        if not isinstance(r.get(key),str) or not r[key]:
            raise ValueError(f"Missing {key}")
    if r["v"] == 2 and (type(r.get("record_id")) is not int or r["record_id"] < 1):
        raise ValueError("Invalid record ID")
    if r["type"] == "scan":
        for field in ("scan_id","start_ms","end_ms","result"):
            if type(r.get(field)) is not int:
                raise ValueError(f"Invalid {field}")
        if r["start_ms"] < 0 or r["end_ms"] < r["start_ms"]:
            raise ValueError("Invalid scan interval")
        if not isinstance(r.get("aps"),list):
            raise ValueError("Missing access points")
        if (r["result"] >= 0 and r["result"] != len(r["aps"])) or (r["result"] < 0 and r["aps"]):
            raise ValueError("Scan count mismatch")
        for ap in r["aps"]:
            parts=ap["bssid"].split(":")
            if len(parts)!=6 or any(len(p)!=2 for p in parts):
                raise ValueError("Invalid BSSID")
            bytes.fromhex("".join(parts))
            if type(ap["rssi_dbm"]) is not int or not -127<=ap["rssi_dbm"]<=0:
                raise ValueError("Invalid RSSI")
            if type(ap["channel"]) is not int or not 1<=ap["channel"]<=14:
                raise ValueError("Invalid ESP32-S3 channel")
            ssid=bytes.fromhex(ap.get("ssid_hex",""))
            if len(ssid)>32: raise ValueError("Invalid SSID")
        if r.get("gps") is not None:
            if not isinstance(r["gps"], dict): raise ValueError("Invalid GPS object")
            for coord in ("lat", "lon"):
                if not isinstance(r["gps"].get(coord), (int, float)):
                    raise ValueError(f"Invalid gps.{coord}")
            if not -90 <= r["gps"]["lat"] <= 90 or not -180 <= r["gps"]["lon"] <= 180:
                raise ValueError("Out of range coordinates")
    elif r["type"] == "clock_anchor":
        if type(r.get("ms")) is not int or r["ms"]<0 or type(r.get("utc_ms")) is not int:
            raise ValueError("Invalid clock anchor")
    elif r["type"] in ("gps_lost","gps_regained"):
        if type(r.get("ms")) is not int or r["ms"]<0:
            raise ValueError("Invalid GPS event")
        fix=r.get("last_fix" if r["type"]=="gps_lost" else "fix")
        if not isinstance(fix,dict) or not all(isinstance(fix.get(k),(int,float)) for k in ("lat","lon","age_ms")):
            raise ValueError("Invalid GPS event fix")
    elif r["type"] not in ("boot","scan_error"):
        raise ValueError("Unknown record type")


def import_data(data, directory, source):
    """Keep raw export elsewhere; quarantine bad lines, atomically import good records."""
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    bad=[]; records=[]
    for line_no,line in enumerate(data.splitlines(),1):
        if not line.strip(): continue
        try:
            r=json.loads(line);validate(r);payload=canonical(r)
            identity=(f'{r["device"]}/{r["session"]}/{r["record_id"]}' if r["v"]==2
                      else 'legacy/'+hashlib.sha256(payload.encode()).hexdigest())
            records.append((identity,r,payload))
        except (ValueError,KeyError,TypeError,AttributeError) as exc:
            bad.append({"line":line_no,"reason":str(exc),"raw_hex":line.hex()})
    added=0
    with closing(sqlite3.connect(directory/"survey.sqlite3")) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, device TEXT NOT NULL, session TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, source TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS session_records ON records(device,session)")
        for identity,r,payload in records:
            prior=db.execute("SELECT payload FROM records WHERE id=?",(identity,)).fetchone()
            if prior:
                if prior[0]!=payload: raise ValueError(f"Conflicting record {identity}; import rolled back, raw file retained")
                continue
            db.execute("INSERT INTO records VALUES (?,?,?,?,?,?)",(identity,r["device"],r["session"],r["type"],payload,str(source)))
            added+=1
    return {"added":added,"duplicates":len(records)-added,"rejected":bad}


def persist(data, directory):
    directory=Path(directory);raw=directory/"raw";raw.mkdir(parents=True,exist_ok=True)
    name=time.strftime("%Y%m%dT%H%M%SZ",time.gmtime())+"-"+uuid.uuid4().hex[:12]
    path=raw/(name+".jsonl")
    temporary=path.with_suffix(".partial")
    with open(temporary,"xb") as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    temporary.rename(path)
    report=import_data(data,directory,path)
    report["sha256"]=hashlib.sha256(data).hexdigest()
    report["bytes"]=len(data)
    (raw/(name+".report.json")).write_text(json.dumps(report,indent=2))
    return path,report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["ports","status","collect","start","initialize","import","interval"])
    parser.add_argument("--port",help="Explicit /dev/cu.usbmodem... or /dev/cu.usbserial... path")
    parser.add_argument("--data",type=Path,default=Path(__file__).resolve().parent/"data")
    parser.add_argument("--file",type=Path,help="Existing JSONL export to import")
    parser.add_argument("--interval",type=int,help="Dynamic scan interval in milliseconds (1000 - 3600000)")
    args=parser.parse_args()
    if args.action=="import":
        if not args.file: parser.error("import requires --file")
        path,report=persist(args.file.read_bytes(),args.data)
        print(path);print(json.dumps(report,indent=2));return
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        parser.error("Install requirements.txt in the supplied virtual environment first")
    if args.action=="ports":
        for p in list_ports.comports(): print(p.device,p.description)
        return
    if not args.port: parser.error("Specify --port; use ports to list connected devices")
    # Disable handshake signals before opening to reduce reset risk. Some boards still reset.
    port=serial.Serial(port=None,baudrate=115200,timeout=0.5,write_timeout=10)
    port.dtr=False;port.rts=False;port.port=args.port;port.open()
    with port:
        time.sleep(2);port.reset_input_buffer();board=Board(port)
        state=board.status()
        if args.action=="status": print(json.dumps(state,indent=2));return
        if args.action=="interval":
            if not args.interval: parser.error("interval action requires --interval <ms>")
            res=board.set_interval(args.interval)
            print(res)
            return
        board.pause()
        if args.action=="initialize":
            if state["mounted"]:
                raise ProtocolError("Already initialized. Automatic erasure is deliberately unavailable; export first.")
            if input("This erases the board filesystem. Type ERASE to initialize: ")!="ERASE": return
            board.send("FORMAT ERASE ALL");board.response(b"OK FORMAT",timeout=30)
            board.sync();board.resume();print("Initialized and recording.");return
        if args.action=="start":
            board.sync();board.resume();print("Time synchronized; recording. Keep the board powered.");return
        warning=None
        try: board.sync()
        except ProtocolError as exc:
            warning=str(exc);print("Clock anchor not saved:",warning,"— attempting export anyway.")
        # Capture status AFTER pausing/sync so export can be checked against this session.
        before=board.status();data=board.dump()
        after=board.status()
        if (before["device"],before["session"])!=(after["device"],after["session"]):
            raise ProtocolError("Board restarted during export; retry")
        path,report=persist(data,args.data)
        print("Verified raw export:",path)
        print(f'Imported {report["added"]} new records; {report["duplicates"]} duplicates; {len(report["rejected"])} rejected lines.')
        cleared=False
        if not report["rejected"]:
            # Auto-clear: raw file is saved+fsynced and imported, so the board copy is redundant.
            try: print(board.clear(len(data),zlib.crc32(data)),"— board storage freed.");cleared=True
            except ProtocolError as exc: print("Board NOT cleared:",exc)
        else: print("Rejected lines present; board data retained (NOT cleared). Check the report.")
        if not state["paused"] and (cleared or not after["fault"]):
            board.resume();print("Recording resumed.")
        else: print("Board remains paused; use start when ready (storage faults must be addressed first).")

if __name__=="__main__":
    try: main()
    except (ProtocolError,ValueError,OSError,sqlite3.Error) as exc:
        raise SystemExit(f"ERROR: {exc}. Nothing was automatically erased; check board status before your next survey.")
