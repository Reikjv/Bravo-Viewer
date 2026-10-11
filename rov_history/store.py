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
            """)

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
