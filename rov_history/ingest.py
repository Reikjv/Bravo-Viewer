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

from .compact import Series, scan_tlog
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


SUBMERGED_METERS = 0.5


class Summary:
    def __init__(self):
        self.value = {"records": 0, "bad_data": 0, "clock_issues": [], "statistics": {}, "statistics_observed": {}, "metrics": {}, "armed_periods": [], "observed_seconds": 0, "armed_seconds": 0, "gaps": 0}
        self.last_ts = None
        self.segment = 0
        self.heartbeat = None
        self.period = None
        self.depth_previous = None

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
        if origin in ("1:1", "dataflash:1"):
            self.depth(ts, name, data)
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

    def depth(self, ts, name, data):
        """Profundidade informada pelo ArduSub; só é exibida com sensor externo registrado."""
        depth = self.value.setdefault("depth", {"external": False, "max": None, "weighted": 0, "seconds": 0, "samples": 0})
        if name == "SCALED_PRESSURE2" or name == "BARO":
            depth["external"] = True
            return
        if name != "GLOBAL_POSITION_INT" or not isinstance(data.get("relative_alt"), (int, float)):
            return
        value = -data["relative_alt"] / 1000
        depth["samples"] += 1
        depth["max"] = value if depth["max"] is None else max(depth["max"], value)
        previous = self.depth_previous
        if previous and ts is not None and previous[0] is not None and previous[2] == self.segment:
            dt = ts - previous[0]
            # Média do tempo submerso: abaixo de 0,5 m e sem lacunas maiores que 5 s.
            if 0 < dt <= 5 and previous[1] > SUBMERGED_METERS:
                depth["seconds"] += dt
                depth["weighted"] += previous[1] * dt
        self.depth_previous = (ts, value, self.segment)

    def finish_period(self, reason):
        if self.period:
            self.period["reason"] = reason
            self.period["open"] = reason != "Desarmamento observado"
            self.value["armed_periods"].append(self.period)
            self.period = None
            self.value["active_armed_period"] = None


def original_records(path, kind, types=None):
    """Mensagens do log original, decodificadas, para inventário e exportação sob demanda.

    types: decodifica só esses tipos no .tlog (os demais vêm sem dados).
    """
    if kind == "tlog":
        yield from scan_tlog(path, decode_all=True, only=types)
        return
    for ts, name, origin, data, _ in (log_records(path) if kind == "log" else mav_records(path, kind)):
        yield ts, name, origin, clean(data)


def read_file(store, destination, kind, progress=None, label=""):
    """Lê o arquivo sem tocar no banco: resumo, inventário, ocorrências e séries.

    Fica fora da trava de escrita; a coleta ao vivo continua durante leituras longas.
    """
    summary = Summary()
    series = Series()
    streams = {}
    events = []
    if kind == "tlog":
        records = ((ts, name, origin, data, {}) for ts, name, origin, data in scan_tlog(destination))
    else:
        records = log_records(destination) if kind == "log" else mav_records(destination, kind)
    for seq, (ts, name, origin, data, units) in enumerate(records):
        if data is not None:
            data = clean(data)
        info = streams.get((name, origin))
        if info is None:
            info = streams[(name, origin)] = {"count": 0, "fields": set(), "first": ts, "last": ts, "units": {}}
        info["count"] += 1
        if data:
            info["fields"].update(flatten(data))
        info["last"] = ts
        info["units"].update(units)
        summary.add(ts, name, origin, data or {}, seq, kind, store.catalog)
        series.add(ts, name, origin, data, kind, store.catalog)
        if data is not None:
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
                events.append((ts, seq, severity, name, event))
        if progress and seq % 50000 == 0:
            progress(label, seq)
    summary.finish_period("Fim do arquivo ou sessão ainda aberta")
    summary.value["kind"] = kind
    return summary, series, streams, events


def import_file(store, source, source_key=None, progress=None, display_name=None, operation_id=None, reference=False):
    """reference=True: o log fica onde o BlueOS o grava (somente leitura); nada é copiado."""
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

    def unchanged(db):
        old = db.execute("SELECT * FROM files WHERE source_key=?", (source_key,)).fetchone()
        if old and old["sha256"] == sha:
            return old, {"id": old["id"], "duplicate": True}
        if old and old["operation_id"] is not None and operation_id is None:
            closed = db.execute("SELECT status FROM operations WHERE id=?", (old["operation_id"],)).fetchone()
            if closed and closed["status"] == "closed":
                # Operações encerradas são congeladas; a versão exportada continua válida.
                return old, {"id": old["id"], "duplicate": True, "frozen": True}
        # Arquivos locais idênticos não são importados duas vezes com nomes diferentes.
        duplicate = db.execute("SELECT id FROM files WHERE sha256=?", (sha,)).fetchone()
        if duplicate:
            return old, {"id": duplicate["id"], "duplicate": True}
        return old, None

    with store.db() as db:
        if unchanged(db)[1]:
            return unchanged(db)[1]
    if reference:
        destination = source
    else:
        originals = store.directory / "originals"
        originals.mkdir(exist_ok=True)
        destination = originals / (sha + suffix)
        if not destination.exists():
            shutil.copyfile(source, destination)
    summary, series, streams, events = read_file(store, destination, kind, progress, source.name)
    with store.write_lock, store.db() as db:
        old, result = unchanged(db)
        if result:
            return result
        if old:
            file_id = old["id"]
            for table in ("records", "events", "streams", "series_points"):
                db.execute(f"DELETE FROM {table} WHERE file_id=?", (file_id,))
            db.execute("UPDATE files SET name=?,kind=?,path=?,sha256=?,size=?,imported_at=?,status='ready' WHERE id=?", (display_name, kind, str(destination.resolve()), sha, source.stat().st_size, time.time(), file_id))
        else:
            file_id = db.execute("INSERT INTO files(source_key,name,kind,path,sha256,size,imported_at) VALUES(?,?,?,?,?,?,?)", (source_key, display_name, kind, str(destination.resolve()), sha, source.stat().st_size, time.time())).lastrowid
        units = {}
        for (name, origin), info in streams.items():
            stream_id = db.execute("INSERT INTO streams(file_id,type,origin,count,fields,first_ts,last_ts) VALUES(?,?,?,?,?,?,?)", (file_id, name, origin, info["count"], dumps(sorted(info["fields"])), info["first"], info["last"])).lastrowid
            if info["units"]:
                units[str(stream_id)] = info["units"]
        db.executemany("INSERT INTO events(file_id,ts,seq,severity,type,text) VALUES(?,?,?,?,?,?)", [(file_id, *event) for event in events])
        db.executemany("INSERT INTO series_points(file_id,metric,bucket,low,high,total,count) VALUES(?,?,?,?,?,?,?)", series.rows(file_id))
        if operation_id is not None:
            # Cópia no PC: a operação é a definida pelo ROV.
            db.execute("UPDATE files SET operation_id=? WHERE id=?", (operation_id, file_id))
        elif not old or old["operation_id"] is None:
            # Arquivo novo entra na operação aberta, salvo quando todos os dados são anteriores a ela.
            current = store.current_operation(db)
            last = max((info["last"] for info in streams.values() if info["last"] is not None), default=None)
            operation = current["id"] if current and (last is None or last >= current["started_at"] - 300) else None
            db.execute("UPDATE files SET operation_id=? WHERE id=?", (operation, file_id))
        summary.value["field_units"] = units
        db.execute("UPDATE files SET summary=? WHERE id=?", (dumps(summary.value), file_id))
    store.forget_series()
    return {"id": file_id, "duplicate": False, "records": summary.value["records"]}
