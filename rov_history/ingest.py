"""Importa todos os tipos decodificáveis e conserva os arquivos originais."""
from collections import defaultdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import time

from .model import clean, dumps, flatten


def fingerprint(path):
    with Path(path).open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def timestamp(value):
    try:
        value = float(value)
        return value if 946684800 <= value <= time.time() + 86400 else None
    except (ValueError, TypeError):
        return None


def log_records(path):
    opener = gzip.open if path.suffix.lower() == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as file:
        for index, line in enumerate(file):
            line = line.rstrip("\r\n")
            if not line:
                continue
            ts = None
            match = re.match(r"^(\d{4}-\d\d-\d\d[T ][\d:.]+Z?)\s+", line)
            if match:
                try:
                    text = match[1]
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    # Sem indicação de fuso, conserva o texto e não presume UTC.
                    if parsed.tzinfo is not None:
                        ts = parsed.timestamp()
                except ValueError:
                    pass
            severity = re.search(r"\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL)\b", line)
            category = re.search(r"\b(system-[\w-]+|netstat|platform|serial-ports):\s*(.*)", line)
            data = {"text": line, "level": severity[1] if severity else "LOG"}
            name = "LOG_TEXT"
            if category:
                name = category[1].upper().replace("-", "_")
                try:
                    data["value"] = json.loads(category[2])
                except json.JSONDecodeError:
                    data["value"] = category[2]
            yield ts, name, "service", data, {}


def mav_records(path, kind):
    from pymavlink import mavutil
    conn = mavutil.mavlink_connection(str(path), dialect="ardupilotmega", robust_parsing=True)
    try:
        while True:
            msg = conn.recv_match()
            if msg is None:
                break
            name = msg.get_type()
            if name == "BAD_DATA":
                yield None, "BAD_DATA", "decoder", {"reason": str(msg)}, {}
                continue
            data = clean(msg.to_dict())
            data.pop("mavpackettype", None)
            units = {}
            if kind == "bin":
                origin = "dataflash"
                fmt = getattr(msg, "fmt", None)
                if fmt and fmt.units:
                    units = dict(zip(fmt.columns, fmt.units))
                if fmt and fmt.instance_field:
                    origin += ":" + str(data.get(fmt.instance_field, "?"))
            else:
                origin = f"{msg.get_srcSystem()}:{msg.get_srcComponent()}"
            if name == "NAMED_VALUE_FLOAT":
                name += ":" + str(data.get("name", ""))
            yield timestamp(getattr(msg, "_timestamp", None)), name, origin, data, units
    finally:
        conn.close()


class Summary:
    def __init__(self):
        self.value = {"records": 0, "bad_data": 0, "clock_issues": [], "statistics": {}, "statistics_observed": {}, "metrics": {}, "armed_periods": [], "observed_seconds": 0, "armed_seconds": 0, "gaps": 0}
        self.last_ts = None
        self.segment = 0
        self.heartbeat = None
        self.period = None

    def add(self, ts, name, origin, data, seq, kind, catalog):
        self.value["records"] += 1
        if name == "BAD_DATA":
            self.value["bad_data"] += 1
        if ts is not None:
            if self.last_ts is not None and (ts < self.last_ts - 2 or ts - self.last_ts > 300):
                self.segment += 1
                if len(self.value["clock_issues"]) < 100:
                    self.value["clock_issues"].append({"sequence": seq, "before": self.last_ts, "after": ts, "reason": "Salto de relógio ou lacuna maior que 5 minutos"})
            self.last_ts = ts
        if name in ("PARAM_VALUE", "PARM"):
            param = data.get("param_id", data.get("Name", ""))
            if isinstance(param, str) and param.startswith("STAT_") and origin in ("1:1", "dataflash"):
                self.value["statistics"][param] = data.get("param_value", data.get("Value"))
                self.value.setdefault("statistics_observed", {})[param] = {"value": self.value["statistics"][param], "timestamp": ts}
        if name == "HEARTBEAT" and origin == "1:1":
            base_mode = data.get("base_mode", 0)
            if isinstance(base_mode, dict):
                base_mode = base_mode.get("bits", 0)
            armed = bool(base_mode & 128)
            previous = self.heartbeat
            dt = ts - previous[0] if previous and ts is not None and previous[0] is not None else None
            contiguous = dt is not None and 0 <= dt <= 5 and previous[2] == self.segment
            if contiguous:
                self.value["observed_seconds"] += dt
                if previous[1] and armed:
                    self.value["armed_seconds"] += dt
                    if self.period:
                        self.period["observed_seconds"] += dt
            elif previous:
                self.value["gaps"] += 1
                self.finish_period("Lacuna ou relógio descontínuo")
            if armed and self.period is None:
                self.period = {"start": ts, "end": ts, "observed_seconds": 0, "open": True, "segment": self.segment}
            if self.period:
                self.period["end"] = ts
            if not armed:
                self.finish_period("Desarmamento observado")
            self.heartbeat = (ts, armed, self.segment)
            self.value["active_armed_period"] = self.period.copy() if self.period else None
        self.value["last_segment"] = self.segment
        selected = {"SYS_STATUS": ("voltage_battery", "current_battery", "load"), "BATTERY_STATUS": ("current_consumed", "energy_consumed", "temperature", "battery_remaining"), "BAT": ("Volt", "Curr", "CurrTot", "EnrgTot"), "SCALED_PRESSURE": ("press_abs", "temperature"), "SCALED_PRESSURE2": ("press_abs", "temperature"), "GLOBAL_POSITION_INT": ("relative_alt",)}
        if origin == "1:1" or kind == "bin":
            for field in selected.get(name, ()):
                if field not in data:
                    continue
                measure = catalog.measure(kind, name, field, data[field], data)
                if not measure["valid"] or not isinstance(measure["value"], (float, int)):
                    continue
                value = measure["value"]
                metric = self.value["metrics"].setdefault(name + "." + field, {"min": value, "max": value, "first": value, "last": value, "unit": measure["unit"]})
                metric["min"] = min(metric["min"], value)
                metric["max"] = max(metric["max"], value)
                metric["last"] = value
        return self.segment

    def finish_period(self, reason):
        if self.period:
            self.period["reason"] = reason
            self.period["open"] = reason != "Desarmamento observado"
            self.value["armed_periods"].append(self.period)
            self.period = None
            self.value["active_armed_period"] = None


def import_file(store, source, source_key=None, progress=None, display_name=None):
    source = Path(source)
    if not source.is_file():
        raise ValueError("Arquivo não encontrado")
    suffix = source.suffix.lower()
    kind = {".bin": "bin", ".tlog": "tlog", ".log": "log", ".gz": "log"}.get(suffix)
    if not kind:
        raise ValueError("Use .tlog, .bin, .log ou .gz. MCAP requer adaptador adicional.")
    sha = fingerprint(source)
    source_key = source_key or "sha256:" + sha
    display_name = display_name or source.name
    originals = store.directory / "originals"
    originals.mkdir(exist_ok=True)
    destination = originals / (sha + suffix)
    if not destination.exists():
        shutil.copyfile(source, destination)
    summary = Summary()
    with store.write_lock, store.db() as db:
        old = db.execute("SELECT * FROM files WHERE source_key=?", (source_key,)).fetchone()
        if old and old["sha256"] == sha:
            return {"id": old["id"], "duplicate": True}
        # Arquivos locais idênticos não são importados duas vezes com nomes diferentes.
        duplicate = db.execute("SELECT id FROM files WHERE sha256=?", (sha,)).fetchone()
        if duplicate:
            return {"id": duplicate["id"], "duplicate": True}
        if old:
            file_id = old["id"]
            db.execute("DELETE FROM records WHERE file_id=?", (file_id,))
            db.execute("DELETE FROM events WHERE file_id=?", (file_id,))
            db.execute("DELETE FROM streams WHERE file_id=?", (file_id,))
            db.execute("UPDATE files SET name=?,kind=?,path=?,sha256=?,size=?,imported_at=?,status='ready' WHERE id=?", (display_name, kind, str(destination.resolve()), sha, source.stat().st_size, time.time(), file_id))
        else:
            file_id = db.execute("INSERT INTO files(source_key,name,kind,path,sha256,size,imported_at) VALUES(?,?,?,?,?,?,?)", (source_key, display_name, kind, str(destination.resolve()), sha, source.stat().st_size, time.time())).lastrowid
        streams = {}
        batch = []
        records = log_records(destination) if kind == "log" else mav_records(destination, kind)
        for seq, (ts, name, origin, data, units) in enumerate(records):
            data = clean(data)
            key = (name, origin)
            if key not in streams:
                stream_id = db.execute("INSERT INTO streams(file_id,type,origin,count,fields,first_ts,last_ts) VALUES(?,?,?,0,'[]',?,?)", (file_id, name, origin, ts, ts)).lastrowid
                streams[key] = {"id": stream_id, "count": 0, "fields": set(), "last": ts, "units": {}}
            info = streams[key]
            info["count"] += 1
            info["fields"].update(flatten(data))
            info["last"] = ts
            info["units"].update(units)
            segment = summary.add(ts, name, origin, data, seq, kind, store.catalog)
            batch.append((file_id, info["id"], seq, ts, segment, dumps(data)))
            event = None
            severity = "INFO"
            if name in ("STATUSTEXT", "MSG"):
                event = str(data.get("text", data.get("Message", "")))
                severity = str(data.get("severity", "INFO"))
            elif name in ("ERR", "EV", "ARM", "MODE", "BAD_DATA"):
                event = dumps(data)
            elif kind == "log" and data.get("level") in ("WARN", "WARNING", "ERROR", "CRITICAL"):
                event = data.get("text", "")
                severity = data["level"]
            if event is not None:
                db.execute("INSERT INTO events(file_id,ts,seq,severity,type,text) VALUES(?,?,?,?,?,?)", (file_id, ts, seq, severity, name, event))
            if len(batch) >= 3000:
                db.executemany("INSERT INTO records(file_id,stream_id,seq,ts,segment,data) VALUES(?,?,?,?,?,?)", batch)
                batch.clear()
                if progress:
                    progress(source.name, seq + 1)
        if batch:
            db.executemany("INSERT INTO records(file_id,stream_id,seq,ts,segment,data) VALUES(?,?,?,?,?,?)", batch)
        summary.finish_period("Fim do arquivo ou sessão ainda aberta")
        summary.value["field_units"] = {str(info["id"]): info["units"] for info in streams.values() if info["units"]}
        summary.value["kind"] = kind
        for info in streams.values():
            db.execute("UPDATE streams SET count=?,fields=?,last_ts=? WHERE id=?", (info["count"], dumps(sorted(info["fields"])), info["last"], info["id"]))
        db.execute("UPDATE files SET summary=? WHERE id=?", (dumps(summary.value), file_id))
    return {"id": file_id, "duplicate": False, "records": summary.value["records"]}
