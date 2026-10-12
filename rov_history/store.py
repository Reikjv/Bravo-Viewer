"""Armazenamento por arquivo/origem; importações se tornam visíveis atomicamente."""
from contextlib import contextmanager
from collections import OrderedDict
import csv
import io
import json
from pathlib import Path
import sqlite3
import hashlib
import itertools
import os
import shutil
import threading
import time
import uuid

from .compact import BUCKET_SECONDS, METRICS
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


# Pastas de logs do BlueOS montadas somente leitura no contêiner (Dockerfile/install.sh).
BLUEOS_LOGS = Path(os.environ.get("BRAVO_BLUEOS_LOGS", "/blueos"))
BLUEOS_FOLDERS = {"ardupilot_logs/": "ardupilot_logs/", "system_logs/": "system_logs/"}


def blueos_path(remote):
    """Caminho do explorador do BlueOS (/ardupilot_logs/logs/x.tlog) na pasta montada, se existir."""
    remote = remote.lstrip("/")
    for prefix, local in BLUEOS_FOLDERS.items():
        if remote.startswith(prefix):
            path = (BLUEOS_LOGS / local / remote[len(prefix):]).resolve()
            return path if path.is_relative_to(BLUEOS_LOGS.resolve()) else None
    return None


class Store:
    def __init__(self, directory, mirror=False):
        """mirror=True: cópia no PC; operações e identidade vêm do ROV, não são criadas aqui."""
        self.directory = Path(directory)
        self.mirror = mirror
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
                CREATE TABLE IF NOT EXISTS series_points(
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    metric TEXT NOT NULL, bucket INTEGER NOT NULL,
                    low REAL NOT NULL, high REAL NOT NULL, total REAL NOT NULL, count INTEGER NOT NULL,
                    PRIMARY KEY(file_id, metric, bucket));
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(files)")}
            for column, kind in (("operation_id", "INTEGER"), ("remote_revision", "REAL"), ("pc_copied_at", "REAL")):
                if column not in columns:
                    db.execute(f"ALTER TABLE files ADD COLUMN {column} {kind}")
            db.execute("CREATE INDEX IF NOT EXISTS files_operation ON files(operation_id)")
            if not mirror and not db.execute("SELECT 1 FROM settings WHERE key='device_id'").fetchone():
                # Identidade fixa do equipamento: renomear o ROV não mistura históricos no PC ou na nuvem.
                db.execute("INSERT INTO settings VALUES ('device_id',?)", (dumps(uuid.uuid4().hex),))
            if not mirror and not db.execute("SELECT 1 FROM operations WHERE status='open'").fetchone():
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
        row = db.execute("SELECT * FROM operations WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

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

    def file_meta(self, file_id):
        """O que o PC precisa de uma sessão, sem as mensagens: inventário, ocorrências e séries."""
        with self.db() as db:
            streams = [dict(row) for row in db.execute("SELECT type,origin,count,fields,first_ts,last_ts FROM streams WHERE file_id=?", (file_id,))]
            events = [list(row) for row in db.execute("SELECT ts,seq,severity,type,text FROM events WHERE file_id=? ORDER BY id", (file_id,))]
            series = [list(row) for row in db.execute("SELECT metric,bucket,low,high,total,count FROM series_points WHERE file_id=?", (file_id,))]
        for stream in streams:
            stream["fields"] = json.loads(stream["fields"])
        return {"streams": streams, "events": events, "series": series}

    def apply_meta(self, remote, meta):
        """Cópia no PC: substitui o resumo de uma sessão pelo do ROV. O log original é copiado à parte."""
        with self.write_lock, self.db() as db:
            row = db.execute("SELECT id,sha256,path FROM files WHERE source_key=?", (remote["source_key"],)).fetchone()
            path = row["path"] if row and row["sha256"] == remote["sha256"] else ""
            values = (remote["name"], remote["kind"], path, remote["sha256"], remote["size"], remote["imported_at"], dumps(remote["summary"]), remote["note"], remote["operation_id"], remote["imported_at"])
            if row:
                file_id = row["id"]
                for table in ("records", "events", "streams", "series_points"):
                    db.execute(f"DELETE FROM {table} WHERE file_id=?", (file_id,))
                db.execute("UPDATE files SET name=?,kind=?,path=?,sha256=?,size=?,imported_at=?,summary=?,note=?,operation_id=?,remote_revision=? WHERE id=?", (*values, file_id))
            else:
                file_id = db.execute("INSERT INTO files(name,kind,path,sha256,size,imported_at,summary,note,operation_id,remote_revision,source_key) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*values, remote["source_key"])).lastrowid
            db.executemany("INSERT INTO streams(file_id,type,origin,count,fields,first_ts,last_ts) VALUES(?,?,?,?,?,?,?)",
                           [(file_id, s["type"], s["origin"], s["count"], dumps(s["fields"]), s["first_ts"], s["last_ts"]) for s in meta["streams"]])
            db.executemany("INSERT INTO events(file_id,ts,seq,severity,type,text) VALUES(?,?,?,?,?,?)", [(file_id, *event) for event in meta["events"]])
            db.executemany("INSERT INTO series_points(file_id,metric,bucket,low,high,total,count) VALUES(?,?,?,?,?,?,?)", [(file_id, *point) for point in meta["series"]])
        self.forget_series()
        return file_id

    def set_original(self, source_key, path):
        with self.write_lock, self.db() as db:
            db.execute("UPDATE files SET path=? WHERE source_key=?", (str(path), source_key))

    def acknowledge_copy(self, file_id, sha):
        """O PC confirmou a cópia íntegra do log original."""
        with self.write_lock, self.db() as db:
            if not db.execute("UPDATE files SET pc_copied_at=? WHERE id=? AND sha256=?", (time.time(), file_id, sha)).rowcount:
                raise ValueError("Sessão ou conteúdo diferente do confirmado")
        return self.release_originals()

    def release_originals(self):
        """Libera cópias próprias de logs de operações encerradas já copiados para o PC.

        Logs lidos direto da pasta do BlueOS não são do Bravo e nunca são apagados aqui.
        """
        originals = (self.directory / "originals").resolve()
        with self.db() as db:
            rows = db.execute("SELECT f.id,f.path FROM files f JOIN operations o ON o.id=f.operation_id WHERE o.status='closed' AND f.pc_copied_at IS NOT NULL AND f.path!=''").fetchall()
        released = 0
        for row in rows:
            path = Path(row["path"]).resolve()
            if not path.is_relative_to(originals):
                continue
            if path.is_file():
                path.unlink()
                released += 1
            with self.write_lock, self.db() as db:
                db.execute("UPDATE files SET path='' WHERE id=?", (row["id"],))
        return released

    def readable_original(self, path):
        """Logs servidos ao PC: cópias próprias ou a pasta do BlueOS montada somente leitura."""
        path = Path(path).resolve()
        roots = [(self.directory / "originals").resolve(), BLUEOS_LOGS.resolve()]
        return path.is_file() and any(path.is_relative_to(root) for root in roots)

    def adopt_blueos_logs(self, progress=lambda text: None):
        """Troca cópias antigas pelo log original na pasta do BlueOS e apaga cópias sem uso."""
        adopted = 0
        if BLUEOS_LOGS.is_dir():
            with self.db() as db:
                rows = db.execute("SELECT id,source_key,path,sha256 FROM files WHERE source_key LIKE 'blueos:%' AND path!=''").fetchall()
            for row in rows:
                local = blueos_path(row["source_key"].split(":", 2)[2])
                if local is None or str(local) == row["path"] or not local.is_file():
                    continue
                progress(f"Usando o log do BlueOS: {local.name}")
                with local.open("rb") as file:
                    if hashlib.file_digest(file, "sha256").hexdigest() != row["sha256"]:
                        continue
                with self.write_lock, self.db() as db:
                    db.execute("UPDATE files SET path=? WHERE id=?", (str(local), row["id"]))
                adopted += 1
        originals = self.directory / "originals"
        removed = 0
        if originals.is_dir():
            with self.db() as db:
                used = {Path(row["path"]).name for row in db.execute("SELECT path FROM files WHERE path!=''")}
            for path in originals.iterdir():
                if path.is_file() and path.name not in used:
                    path.unlink()
                    removed += 1
        return {"adopted": adopted, "removed": removed}

    def compact_legacy(self, progress=lambda text: None):
        """Converte históricos anteriores à 0.5: gera séries e remove as mensagens guardadas uma a uma."""
        from .compact import Series
        from .ingest import read_file
        with self.db() as db:
            legacy = db.execute("SELECT DISTINCT f.id,f.kind,f.path,f.name FROM files f JOIN records r ON r.file_id=f.id").fetchall()
        for index, file in enumerate(legacy):
            progress(f"Compactando histórico anterior: {index + 1}/{len(legacy)} · {file['name']}")
            series = Series()
            path = Path(file["path"]) if file["path"] else None
            if path and path.is_file() and file["kind"] in ("tlog", "bin"):
                series = read_file(self, path, file["kind"])[1]
            else:
                with self.db() as db:
                    for row in db.execute("SELECT r.ts,r.data,s.type,s.origin FROM records r JOIN streams s ON s.id=r.stream_id WHERE r.file_id=? ORDER BY r.seq", (file["id"],)):
                        series.add(row["ts"], row["type"], row["origin"], json.loads(row["data"]), file["kind"], self.catalog)
            with self.write_lock, self.db() as db:
                db.execute("DELETE FROM series_points WHERE file_id=?", (file["id"],))
                db.executemany("INSERT INTO series_points(file_id,metric,bucket,low,high,total,count) VALUES(?,?,?,?,?,?,?)", series.rows(file["id"]))
                db.execute("DELETE FROM records WHERE file_id=?", (file["id"],))
        if legacy:
            # Devolve ao disco o espaço das mensagens removidas (feito uma única vez).
            with self.write_lock:
                connection = sqlite3.connect(self.path, timeout=60)
                try:
                    connection.execute("VACUUM")
                finally:
                    connection.close()
        return len(legacy)

    def sync_manifest(self):
        config = self.config()
        with self.db() as db:
            operations = [dict(row) for row in db.execute("SELECT id,name,location,client,notes,started_at,ended_at,status,summary FROM operations ORDER BY id")]
            files = [dict(row) for row in db.execute("SELECT id,operation_id,source_key,name,kind,sha256,size,imported_at,summary,note,path!='' AS has_original FROM files ORDER BY id")]
        for item in operations + files:
            item["summary"] = json.loads(item["summary"])
        return {"device_id": config.get("device_id"), "equipment": config["equipment"], "operations": operations, "files": files}

    def mirror_operation(self, op):
        """Copia a operação do ROV; o PC mantém operações já removidas no ROV."""
        with self.write_lock, self.db() as db:
            db.execute("INSERT INTO operations(id,name,location,client,notes,started_at,ended_at,status,summary) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,location=excluded.location,client=excluded.client,notes=excluded.notes,started_at=excluded.started_at,ended_at=excluded.ended_at,status=excluded.status,summary=excluded.summary",
                       (op["id"], op["name"], op["location"], op["client"], op["notes"], op["started_at"], op["ended_at"], op["status"], dumps(op["summary"])))

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
                db.execute("DELETE FROM series_points WHERE file_id=?", (file["id"],))
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

    def stream_rows(self, stream):
        """Mensagens de um tipo/origem: do banco (históricos anteriores à 0.5) ou relidas do log original.

        A partir da 0.5 cada mensagem não é guardada; o .tlog é a fonte completa.
        """
        with self.db() as db:
            stored = db.execute("SELECT 1 FROM records WHERE stream_id=? LIMIT 1", (stream["id"],)).fetchone()
            if stored:
                for row in db.execute("SELECT seq,ts,segment,data FROM records WHERE stream_id=? ORDER BY seq", (stream["id"],)):
                    yield row["seq"], row["ts"], row["segment"], json.loads(row["data"])
                return
            file = db.execute("SELECT path,kind FROM files WHERE id=?", (stream["file_id"],)).fetchone()
        path = Path(file["path"]) if file and file["path"] else None
        if path is None or not path.is_file():
            return
        from .ingest import original_records
        for seq, (ts, name, origin, data) in enumerate(original_records(path, file["kind"], {stream["type"].split(":")[0]})):
            if name == stream["type"] and origin == stream["origin"]:
                yield seq, ts, 0, data

    def raw_available(self, stream):
        with self.db() as db:
            if db.execute("SELECT 1 FROM records WHERE stream_id=? LIMIT 1", (stream["id"],)).fetchone():
                return True
            file = db.execute("SELECT path FROM files WHERE id=?", (stream["file_id"],)).fetchone()
        return bool(file and file["path"] and Path(file["path"]).is_file())

    def records(self, stream_id, offset=0, limit=100):
        stream = self.stream(stream_id)
        rows = itertools.islice(self.stream_rows(stream), offset, offset + min(limit, 500))
        return {"stream": {**stream, "fields": json.loads(stream["fields"])}, "total": stream["count"], "offset": offset,
                "available": self.raw_available(stream),
                "rows": [{"seq": seq, "ts": ts, "segment": segment, "data": data} for seq, ts, segment, data in rows]}

    def metrics(self, file_id):
        """Indicadores com série de 10 s nesta sessão; profundidade só com sensor externo registrado."""
        with self.db() as db:
            summary = db.execute("SELECT summary FROM files WHERE id=?", (file_id,)).fetchone()
            rows = db.execute("SELECT metric,count(*) AS buckets FROM series_points WHERE file_id=? GROUP BY metric", (file_id,)).fetchall()
        external = bool(summary and json.loads(summary["summary"]).get("depth", {}).get("external"))
        order = list(METRICS)
        return sorted(({"metric": row["metric"], "label": METRICS[row["metric"]][0], "unit": METRICS[row["metric"]][1], "buckets": row["buckets"]}
                       for row in rows if row["metric"] in METRICS and (row["metric"] != "depth" or external)), key=lambda item: order.index(item["metric"]))

    def points(self, file_id, metric):
        if metric not in METRICS:
            raise ValueError("Indicador desconhecido")
        with self.db() as db:
            rows = db.execute("SELECT bucket,low,high,total,count FROM series_points WHERE file_id=? AND metric=? ORDER BY bucket", (file_id, metric)).fetchall()
        return {"metric": metric, "label": METRICS[metric][0], "unit": METRICS[metric][1], "seconds": BUCKET_SECONDS,
                "points": [{"t": row["bucket"] * BUCKET_SECONDS, "min": row["low"], "max": row["high"], "avg": row["total"] / row["count"]} for row in rows]}

    def forget_series(self):
        with self._cache_lock:
            self._series_cache.clear()

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
        rows = self.stream_rows(stream)
        if format == "jsonl":
            for seq, ts, segment, data in rows:
                yield (dumps({"sequence": seq, "timestamp": ts, "clock_segment": segment, "type": stream["type"], "origin": stream["origin"], "raw": data}) + "\n").encode()
            return
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(["sequence", "timestamp", "clock_segment", *fields])
        yield ("\ufeff" + buffer.getvalue()).encode("utf-8")
        buffer.seek(0); buffer.truncate()
        for seq, ts, segment, data in rows:
            values = flatten(data)
            cells = [values.get(field, "") for field in fields]
            # Previne execução de fórmula ao abrir textos de mensagens no Excel.
            cells = ["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in cells]
            writer.writerow([seq, ts, segment, *cells])
            if buffer.tell() > 65536:
                yield buffer.getvalue().encode("utf-8")
                buffer.seek(0); buffer.truncate()
        if buffer.tell():
            yield buffer.getvalue().encode("utf-8")

    def disk(self):
        usage = shutil.disk_usage(self.directory)
        return {"disk_free": usage.free, "disk_total": usage.total}

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
        return {"files": len(files), "records": sum(f["summary"].get("records", 0) for f in files), "bytes": sum(f["size"] or 0 for f in files), "statistics": stats, "statistics_observed": observed, "database_bytes": self.path.stat().st_size, **self.disk(), "clock_issues": sum(len(f["summary"].get("clock_issues", [])) for f in files), "sessions": files}


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
