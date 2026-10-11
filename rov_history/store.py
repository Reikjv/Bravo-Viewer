"""Armazenamento por arquivo/origem; importações se tornam visíveis atomicamente."""
from contextlib import contextmanager
from collections import OrderedDict
import csv
import io
import json
from pathlib import Path
import sqlite3
import threading
import time

from .model import Catalog, category, dumps, flatten


# Operações encerradas guardadas no ROV; valor fixo para todas as instalações.
OPERATION_LIMIT = 20


def operation_summary(files):
    """Resumo da operação sem somar fontes que registram a mesma telemetria.

    Coleta ao vivo e .tlog descrevem o mesmo veículo; tempos e energia usam o grupo
    de formato com maior cobertura observada, nunca a soma entre grupos.
    """
    groups = {}
    for file in files:
        groups.setdefault(file["kind"], []).append(file["summary"])
    def total(items, key):
        return sum(item.get(key, 0) or 0 for item in items)
    timed = max(groups.items(), key=lambda item: total(item[1], "observed_seconds"), default=(None, []))
    result = {"sessions": len(files), "records": sum(f["summary"].get("records", 0) for f in files),
              "time_source": timed[0], "observed_seconds": total(timed[1], "observed_seconds"),
              "armed_seconds": total(timed[1], "armed_seconds"), "gaps": total(timed[1], "gaps"),
              "clock_issues": sum(len(f["summary"].get("clock_issues", [])) for f in files)}
    metrics = [s.get("metrics", {}) for s in timed[1]]
    def pick(names, mode):
        values = [(m[n][mode], m[n]["unit"]) for m in metrics for n in names if n in m]
        if not values:
            return None
        value = min(values) if mode == "min" else max(values)
        return {"value": value[0], "unit": value[1]}
    result["voltage_min"] = pick(("SYS_STATUS.voltage_battery", "BAT.Volt"), "min")
    result["current_max"] = pick(("SYS_STATUS.current_battery", "BAT.Curr"), "max")
    consumed = [m["BATTERY_STATUS.current_consumed"] for m in metrics if "BATTERY_STATUS.current_consumed" in m]
    result["consumption"] = {"value": sum(max(0, c["last"] - c["first"]) for c in consumed), "unit": consumed[0]["unit"]} if consumed else None
    # Profundidade exige sensor externo; sem ele a altitude estimada não representa profundidade.
    depth_groups = {kind: [s["depth"] for s in items if s.get("depth", {}).get("external")] for kind, items in groups.items()}
    depth_kind, depths = max(depth_groups.items(), key=lambda item: sum(d["seconds"] for d in item[1]), default=(None, []))
    maxima = [d["max"] for items in depth_groups.values() for d in items if d.get("max") is not None]
    seconds = sum(d["seconds"] for d in depths)
    result["depth"] = {"available": bool(maxima), "max": max(maxima) if maxima else None,
                       "mean": sum(d["weighted"] for d in depths) / seconds if seconds else None,
                       "submerged_seconds": seconds, "unit": "m", "source": depth_kind}
    return result


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "history.sqlite3"
        self.write_lock = threading.RLock()
        self.catalog = Catalog()
        self._series_cache = OrderedDict()
        self._cache_lock = threading.RLock()
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS files(
                    id INTEGER PRIMARY KEY, source_key TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                    kind TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT, size INTEGER,
                    imported_at REAL, summary TEXT NOT NULL DEFAULT '{}', note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'ready');
                CREATE TABLE IF NOT EXISTS streams(
                    id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    type TEXT NOT NULL, origin TEXT NOT NULL, count INTEGER NOT NULL,
                    fields TEXT NOT NULL, first_ts REAL, last_ts REAL,
                    UNIQUE(file_id,type,origin));
                CREATE TABLE IF NOT EXISTS records(
                    id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    stream_id INTEGER NOT NULL REFERENCES streams(id) ON DELETE CASCADE,
                    seq INTEGER NOT NULL, ts REAL, segment INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS records_stream_seq ON records(stream_id,seq);
                CREATE INDEX IF NOT EXISTS records_file_seq ON records(file_id,seq);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    ts REAL, seq INTEGER, severity TEXT, type TEXT, text TEXT);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_file_id ON events(file_id,id);
                CREATE TABLE IF NOT EXISTS operations(
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL DEFAULT '',
                    client TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL, ended_at REAL, status TEXT NOT NULL DEFAULT 'open',
                    summary TEXT NOT NULL DEFAULT '{}', bundle_id TEXT, downloaded_at REAL);
            """)
            if "operation_id" not in {row["name"] for row in db.execute("PRAGMA table_info(files)")}:
                db.execute("ALTER TABLE files ADD COLUMN operation_id INTEGER")
            db.execute("CREATE INDEX IF NOT EXISTS files_operation ON files(operation_id)")
            if not db.execute("SELECT 1 FROM operations WHERE status='open'").fetchone():
                # Primeira execução com operações: o histórico existente forma a Operação 1.
                number = db.execute("SELECT coalesce(max(id),0)+1 FROM operations").fetchone()[0]
                first = db.execute("SELECT min(imported_at) FROM files").fetchone()[0] if number == 1 else None
                op = db.execute("INSERT INTO operations(name,started_at) VALUES(?,?)", (f"Operação {number}", first or time.time())).lastrowid
                if number == 1:
                    db.execute("UPDATE files SET operation_id=? WHERE operation_id IS NULL", (op,))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=60)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def config(self):
        defaults = {"host": "127.0.0.1", "collect": False, "equipment": "Meu ROV", "poll_seconds": 2}
        with self.db() as db:
            for row in db.execute("SELECT key,value FROM settings"):
                defaults[row["key"]] = json.loads(row["value"])
        return defaults

    @contextmanager
    def snapshot(self):
        with self.db() as db:
            db.execute("BEGIN")
            db.execute("PRAGMA query_only=ON")
            # Primeira leitura fixa a versão do histórico usada por toda a exportação.
            db.execute("SELECT count(*) FROM files").fetchone()
            yield SnapshotStore(self, db)

    def configure(self, updates):
        with self.write_lock, self.db() as db:
            for key, value in updates.items():
                db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, dumps(value)))
        return self.config()

    def current_operation(self, db=None):
        if db is None:
            with self.db() as db:
                return self.current_operation(db)
        return dict(db.execute("SELECT * FROM operations WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone())

    def operations(self):
        files = self.files()
        with self.db() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM operations ORDER BY id DESC")]
        for op in rows:
            op_files = [f for f in files if f["operation_id"] == op["id"]]
            op["summary"] = json.loads(op["summary"]) if op["status"] == "closed" else operation_summary(op_files)
            op["files"] = [f["id"] for f in op_files]
            # Exportações antigas são podadas; o ZIP pode ser gerado novamente.
            op["bundle_ready"] = bool(op["bundle_id"]) and (self.directory / "exports" / f"{op['bundle_id']}.zip").is_file()
        return rows

    def update_operation(self, values):
        fields = {}
        for name, limit in (("name", 100), ("location", 200), ("client", 200), ("notes", 5000)):
            if name in values:
                value = values[name]
                if not isinstance(value, str) or len(value) > limit:
                    raise ValueError("Dados da operação inválidos")
                fields[name] = value.strip()
        if "name" in fields and not fields["name"]:
            raise ValueError("Informe o nome da operação")
        with self.write_lock, self.db() as db:
            op = self.current_operation(db)
            for name, value in fields.items():
                db.execute(f"UPDATE operations SET {name}=? WHERE id=?", (value, op["id"]))
        return self.current_operation()

    def close_operation(self, values=None):
        """Congela a operação aberta com seu resumo e inicia a próxima na mesma transação."""
        if values:
            self.update_operation(values)
        files = self.files()
        with self.write_lock, self.db() as db:
            op = self.current_operation(db)
            now = time.time()
            summary = operation_summary([f for f in files if f["operation_id"] == op["id"]])
            db.execute("UPDATE operations SET status='closed',ended_at=?,summary=? WHERE id=?", (now, dumps(summary), op["id"]))
            number = db.execute("SELECT coalesce(max(id),0)+1 FROM operations").fetchone()[0]
            new = db.execute("INSERT INTO operations(name,started_at) VALUES(?,?)", (f"Operação {number}", now)).lastrowid
        return {"closed": op["id"], "current": new}

    def operation_limit(self):
        return OPERATION_LIMIT

    def expired_operations(self):
        with self.db() as db:
            rows = db.execute("SELECT id FROM operations WHERE status='closed' ORDER BY ended_at DESC, id DESC").fetchall()
        return [row["id"] for row in rows[self.operation_limit():]]

    def delete_operation(self, operation_id, progress=lambda text: None):
        """Remove uma operação encerrada: registros, originais e a própria operação."""
        with self.db() as db:
            op = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not op or op["status"] != "closed":
                raise ValueError("Somente operações encerradas podem ser removidas")
            files = db.execute("SELECT id,path FROM files WHERE operation_id=?", (operation_id,)).fetchall()
        originals = (self.directory / "originals").resolve()
        for index, file in enumerate(files):
            progress(f"Removendo {op['name']}: sessão {index + 1}/{len(files)}")
            # Uma transação por sessão evita bloquear a coleta por muito tempo.
            with self.write_lock, self.db() as db:
                db.execute("DELETE FROM records WHERE file_id=?", (file["id"],))
                db.execute("DELETE FROM events WHERE file_id=?", (file["id"],))
                db.execute("DELETE FROM streams WHERE file_id=?", (file["id"],))
                db.execute("DELETE FROM files WHERE id=?", (file["id"],))
            if file["path"]:
                path = Path(file["path"]).resolve()
                if path.is_relative_to(originals) and path.is_file():
                    path.unlink(missing_ok=True)
        with self.write_lock, self.db() as db:
            db.execute("DELETE FROM operations WHERE id=?", (operation_id,))
        with self._cache_lock:
            self._series_cache.clear()
        return {"id": operation_id, "name": op["name"], "sessions": len(files)}

    def set_operation_bundle(self, operation_id, bundle_id):
        with self.write_lock, self.db() as db:
            db.execute("UPDATE operations SET bundle_id=? WHERE id=?", (bundle_id, operation_id))

    def mark_bundle_downloaded(self, bundle_id):
        with self.write_lock, self.db() as db:
            db.execute("UPDATE operations SET downloaded_at=? WHERE bundle_id=?", (time.time(), bundle_id))

    def files(self):
        with self.db() as db:
            rows = db.execute("SELECT f.*, (SELECT count(*) FROM streams s WHERE s.file_id=f.id) AS stream_count FROM files f ORDER BY imported_at DESC").fetchall()
        return [{**dict(row), "summary": json.loads(row["summary"]), "path": None} for row in rows]

    def file(self, file_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if not row:
            raise ValueError("Arquivo não encontrado")
        return dict(row)

    def streams(self, file_id=None):
        sql = "SELECT s.*,f.name AS file_name,f.kind FROM streams s JOIN files f ON f.id=s.file_id"
        args = []
        if file_id:
            sql += " WHERE file_id=?"
            args.append(file_id)
        with self.db() as db:
            rows = db.execute(sql + " ORDER BY s.type,s.origin,f.id", args).fetchall()
        return [{**dict(row), "fields": json.loads(row["fields"]), "category": category(row["type"])} for row in rows]

    def stream(self, stream_id):
        with self.db() as db:
            row = db.execute("SELECT s.*,f.kind,f.name AS file_name,f.summary AS file_summary,f.imported_at AS revision FROM streams s JOIN files f ON f.id=s.file_id WHERE s.id=?", (stream_id,)).fetchone()
        if not row:
            raise ValueError("Tipo/origem não encontrado")
        result = dict(row)
        result["units"] = json.loads(result.pop("file_summary")).get("field_units", {}).get(str(stream_id), {})
        return result

    def records(self, stream_id, offset=0, limit=100):
        stream = self.stream(stream_id)
        with self.db() as db:
            rows = db.execute("SELECT seq,ts,segment,data FROM records WHERE stream_id=? ORDER BY seq LIMIT ? OFFSET ?", (stream_id, min(limit, 500), offset)).fetchall()
        return {"stream": {**stream, "fields": json.loads(stream["fields"])}, "total": stream["count"], "offset": offset, "rows": [{**dict(row), "data": json.loads(row["data"])} for row in rows]}

    def series(self, stream_id, field, points=700, normalized=True):
        stream = self.stream(stream_id)
        points = min(2000, max(10, points))
        key = (stream_id, stream["revision"], stream["count"], stream["last_ts"], field, points, normalized)
        with self._cache_lock:
            if key in self._series_cache:
                self._series_cache.move_to_end(key)
                return self._series_cache[key]
        result = self._series(stream, field, points, normalized)
        with self._cache_lock:
            self._series_cache[key] = result
            self._series_cache.move_to_end(key)
            while len(self._series_cache) > 8:
                self._series_cache.popitem(last=False)
        return result

    def _series(self, stream, field, points, normalized):
        stream_id = stream["id"]
        armed_indicator = field == "_armed" and stream["type"] == "HEARTBEAT" and stream["origin"] == "1:1"
        if not armed_indicator and field not in json.loads(stream["fields"]):
            raise ValueError("Campo não encontrado nessa origem")
        points = min(2000, max(10, points))
        with self.db() as db:
            bounds = db.execute("SELECT min(seq),max(seq) FROM records WHERE stream_id=?", (stream_id,)).fetchone()
            if bounds[0] is None:
                return {"points": [], "unit": ""}
            # Buckets por ordem preservam saltos de relógio. Min/max mantêm os picos;
            # valores inválidos são lacunas. O gráfico é uma redução, não o CSV.
            width = max(1, (bounds[1] - bounds[0] + 1) // points)
            rows = db.execute("SELECT seq,ts,segment,data FROM records WHERE stream_id=? ORDER BY seq", (stream_id,))
            buckets = {}
            unit = ""
            invalid = 0
            for row in rows:
                data = json.loads(row["data"])
                raw = flatten(data).get(field)
                measure = self.catalog.measure(stream["kind"], stream["type"], field, raw, data)
                if armed_indicator:
                    mode = data.get("base_mode")
                    if isinstance(mode, dict):
                        mode = mode.get("bits")
                    valid = isinstance(mode, int)
                    raw = int(bool(mode & 128)) if valid else None
                    measure = {"value": raw, "unit": "0 desarmado / 1 armado", "valid": valid}
                if stream["kind"] == "bin":
                    measure = {"value": raw, "unit": stream["units"].get(field.split("[", 1)[0], ""), "valid": raw is not None}
                unit = measure["unit"] if normalized or armed_indicator else self.catalog.unit(stream["kind"], stream["type"], field)
                value = measure["value"] if normalized else raw
                if not measure["valid"] or not isinstance(value, (int, float)):
                    value = None
                    invalid += 1
                point = {"seq": row["seq"], "timestamp": row["ts"], "segment": row["segment"], "value": value, "raw": raw}
                key = (row["segment"], (row["seq"] - bounds[0]) // width)
                bucket = buckets.setdefault(key, {"first": point, "last": point, "min": point, "max": point, "invalid": None})
                bucket["last"] = point
                if value is None:
                    bucket["invalid"] = point
                else:
                    for mode, compare in (("min", lambda a, b: a < b), ("max", lambda a, b: a > b)):
                        old = bucket[mode]["value"]
                        if old is None or compare(value, old):
                            bucket[mode] = point
            result = []
            for bucket in buckets.values():
                candidates = {p["seq"]: p for p in bucket.values() if p is not None}
                result.extend(candidates[key] for key in sorted(candidates))
        return {"points": result, "unit": unit, "invalid_samples": invalid, "total": stream["count"], "field": field, "type": stream["type"], "reduced": len(result) < stream["count"]}

    def events(self, file_id=None, offset=0, limit=100, search=""):
        conditions = ["text LIKE ?"]
        args = ["%" + search + "%"]
        if file_id:
            conditions.append("file_id=?")
            args.append(file_id)
        where = " AND ".join(conditions)
        with self.db() as db:
            total = db.execute("SELECT count(*) FROM events WHERE " + where, args).fetchone()[0]
            rows = db.execute("SELECT e.*,f.name AS file_name FROM events e JOIN files f ON f.id=e.file_id WHERE " + where + " ORDER BY e.id DESC LIMIT ? OFFSET ?", (*args, min(limit, 500), offset)).fetchall()
        return {"total": total, "rows": [dict(row) for row in rows], "offset": offset}

    def note(self, file_id, note):
        if len(note) > 5000:
            raise ValueError("Observação muito longa")
        with self.write_lock, self.db() as db:
            if not db.execute("UPDATE files SET note=? WHERE id=?", (note, file_id)).rowcount:
                raise ValueError("Arquivo não encontrado")

    def export(self, stream_id, format="csv"):
        stream = self.stream(stream_id)
        fields = json.loads(stream["fields"])
        with self.db() as db:
            cursor = db.execute("SELECT seq,ts,segment,data FROM records WHERE stream_id=? ORDER BY seq", (stream_id,))
            if format == "jsonl":
                for row in cursor:
                    yield (dumps({"sequence": row["seq"], "timestamp": row["ts"], "clock_segment": row["segment"], "type": stream["type"], "origin": stream["origin"], "raw": json.loads(row["data"])}) + "\n").encode()
                return
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            writer.writerow(["sequence", "timestamp", "clock_segment", *fields])
            yield ("\ufeff" + buffer.getvalue()).encode("utf-8")
            buffer.seek(0); buffer.truncate()
            for row in cursor:
                values = flatten(json.loads(row["data"]))
                cells = [values.get(field, "") for field in fields]
                # Previne execução de fórmula ao abrir textos de mensagens no Excel.
                cells = ["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in cells]
                writer.writerow([row["seq"], row["ts"], row["segment"], *cells])
                if buffer.tell() > 65536:
                    yield buffer.getvalue().encode("utf-8")
                    buffer.seek(0); buffer.truncate()
            if buffer.tell():
                yield buffer.getvalue().encode("utf-8")

    def overview(self):
        files = self.files()
        stats = {}
        observed = {}
        for file in sorted(files, key=lambda f: f["imported_at"] or 0):
            for name, value in file["summary"].get("statistics", {}).items():
                observation = file["summary"].get("statistics_observed", {}).get(name, {"value": value, "timestamp": None})
                rank = observation.get("timestamp") or 0
                if name not in observed or rank >= (observed[name].get("timestamp") or 0):
                    observed[name] = {**observation, "file": file["id"]}
                    stats[name] = value
        return {"files": len(files), "records": sum(f["summary"].get("records", 0) for f in files), "bytes": sum(f["size"] or 0 for f in files), "statistics": stats, "statistics_observed": observed, "database_bytes": self.path.stat().st_size, "clock_issues": sum(len(f["summary"].get("clock_issues", [])) for f in files), "sessions": files}


class SnapshotStore(Store):
    def __init__(self, store, connection):
        self.directory, self.path, self.catalog = store.directory, store.path, store.catalog
        self.write_lock = store.write_lock
        self._connection = connection
        self._series_cache = OrderedDict()
        self._cache_lock = threading.RLock()

    @contextmanager
    def db(self):
        yield self._connection
