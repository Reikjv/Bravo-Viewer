"""Importação em fila e coleta amostrada independente do navegador."""
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import threading
import time
import uuid

from .blueos import BlueOS, validate_host
from .ingest import Summary, import_file, timestamp
from .model import clean, dumps, flatten
from .store import Store


class Service:
    def __init__(self, directory, start=True):
        self.store = Store(directory)
        self.jobs = {}
        self.job_lock = threading.RLock()
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.collection = {"connected": False, "last_success": None, "error": None, "samples": 0}
        self.live_seen = {}
        self.summaries = {}
        self.heavy_queries = threading.BoundedSemaphore(2)
        self.busy_sync = False
        self.threads = []
        if start:
            for target in (self.worker, self.collector):
                thread = threading.Thread(target=target, daemon=True)
                thread.start()
                self.threads.append(thread)

    def configure(self, updates):
        allowed = {}
        if "host" in updates:
            allowed["host"] = validate_host(updates["host"])
        if "collect" in updates:
            if not isinstance(updates["collect"], bool):
                raise ValueError("Estado de coleta inválido")
            allowed["collect"] = updates["collect"]
        if "equipment" in updates:
            if not isinstance(updates["equipment"], str) or not 1 <= len(updates["equipment"].strip()) <= 100:
                raise ValueError("Nome de equipamento inválido")
            allowed["equipment"] = updates["equipment"].strip()
        if "poll_seconds" in updates:
            allowed["poll_seconds"] = min(60, max(1, int(updates["poll_seconds"])))
        if "operation_limit" in updates:
            allowed["operation_limit"] = min(30, max(3, int(updates["operation_limit"])))
        old = self.store.config()
        result = self.store.configure(allowed)
        if old["host"] != result["host"]:
            self.live_seen.clear()
        return result

    def submit(self, action, **args):
        job_id = uuid.uuid4().hex
        with self.job_lock:
            active = [job for job in self.jobs.values() if job["status"] in ("queued", "running")]
            if action == "sync":
                for job in active:
                    if job["action"] == "sync" and job.get("host") == args.get("host"):
                        return job.copy()
            if action in ("bundle", "close_operation") and any(job["action"] in ("bundle", "close_operation") for job in active):
                raise ValueError("Uma exportação já está sendo preparada. Aguarde sua conclusão.")
            if len(active) >= 16:
                raise ValueError("A fila de processamento está cheia. Aguarde os arquivos atuais.")
            for old_id, job in list(self.jobs.items()):
                if len(self.jobs) <= 100:
                    break
                if job["status"] not in ("queued", "running"):
                    del self.jobs[old_id]
            self.jobs[job_id] = {"id": job_id, "action": action, "status": "queued", "progress": "Na fila", "started": time.time()}
            if action == "sync":
                self.jobs[job_id]["host"] = args.get("host")
        self.queue.put((job_id, action, args))
        return self.jobs[job_id].copy()

    def status(self):
        with self.job_lock:
            jobs = [job.copy() for job in list(self.jobs.values())[-20:]]
        return {"config": self.store.config(), "collection": self.collection.copy(), "jobs": jobs}

    def worker(self):
        while not self.stop.is_set():
            try:
                job_id, action, args = self.queue.get(timeout=.5)
            except queue.Empty:
                continue
            self.jobs[job_id].update(status="running", progress="Iniciando", percent=0)
            def progress(name, count):
                self.jobs[job_id]["progress"] = f"{name}: {count:,} registros"
            try:
                if action == "import":
                    result = import_file(self.store, args["path"], args.get("source_key"), progress, args.get("display_name"))
                elif action == "sync":
                    result = self.sync(args.get("host", self.store.config()["host"]), progress)
                elif action == "bundle":
                    from .exporter import build_bundle
                    result = build_bundle(self.store, job_id, args["context"], self.reporter(job_id), self.stop)
                    if args["context"]["scope"] == "operation":
                        self.store.set_operation_bundle(args["context"]["operation"], job_id)
                elif action == "close_operation":
                    result = self.close_operation(job_id, args)
                else:
                    raise ValueError("Ação desconhecida")
                self.jobs[job_id].update(status="done", progress="Concluído", result=result)
            except Exception as error:
                self.jobs[job_id].update(status="error", progress=str(error))
            finally:
                self.queue.task_done()
                if action == "import" and args.get("temporary"):
                    Path(args["path"]).unlink(missing_ok=True)

    def reporter(self, job_id):
        def report(text, percent=None):
            self.jobs[job_id]["progress"] = text
            if percent is not None:
                self.jobs[job_id]["percent"] = max(0, min(100, round(percent)))
        return report

    def close_operation(self, job_id, args):
        """Encerra, exporta a operação e aplica o limite de operações guardadas."""
        from .exporter import build_bundle
        report = self.reporter(job_id)
        report("Encerrando a operação", 0)
        ids = self.store.close_operation(args.get("values"))
        context = {"scope": "operation", "operation": ids["closed"], "tab": "overview", "include_data": args.get("include_data", False), "include_originals": True}
        result = build_bundle(self.store, job_id, context, lambda text, percent=None: report(text, None if percent is None else percent * .9), self.stop)
        self.store.set_operation_bundle(ids["closed"], job_id)
        removed = []
        for old in self.store.expired_operations():
            removed.append(self.store.delete_operation(old, lambda text: report(text, 95)))
        report("Concluído", 100)
        return {**result, **ids, "removed": removed}

    def sync(self, host, progress):
        client = BlueOS(host)
        inventory = client.inventory()
        inventory.update(captured_at=time.time(), host=host)
        index_path = self.store.directory / "remote_index.json"
        index = json.loads(index_path.read_text()) if index_path.exists() else {}
        imported = []
        skipped = 0
        errors = inventory["errors"]
        inbox = self.store.directory / "inbox"
        inbox.mkdir(exist_ok=True)
        for item in inventory["files"]:
            key = f"blueos:{host}:{item['path']}"
            marker = [item["size"], item["modified"]]
            if not item["size"] or item.get("extension", "").lower() not in (".bin", ".tlog", ".log", ".gz") or index.get(key) == marker:
                skipped += 1
                continue
            progress(item["name"], 0)
            local = inbox / item["name"]
            try:
                client.download(item["path"], local)
                result = import_file(self.store, local, key, progress)
                imported.append(result)
                index[key] = marker
            except Exception as error:
                errors.append({"path": item["path"], "error": str(error)})
            finally:
                local.unlink(missing_ok=True)
        tmp = index_path.with_suffix(".tmp")
        tmp.write_text(dumps(index), encoding="utf-8")
        tmp.replace(index_path)
        inv = self.store.directory / "remote_inventory.json"
        inv_tmp = inv.with_suffix(".tmp")
        inv_tmp.write_text(dumps(inventory), encoding="utf-8")
        inv_tmp.replace(inv)
        return {"imported": len(imported), "skipped": skipped, "errors": errors, "found": len(inventory["files"])}

    def ingest_live(self, cache, system=None, host=None):
        config = self.store.config()
        host = host or config["host"]
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        operation = self.store.current_operation()
        key = f"live:{host}:{date}:op{operation['id']}"
        entries = []
        markers = {}
        for sid, vehicle in cache.get("vehicles", {}).items():
            for cid, component in vehicle.get("components", {}).items():
                for name, obj in component.get("messages", {}).items():
                    meta = obj.get("status", {}).get("time", {})
                    marker = (meta.get("counter"), meta.get("last_update"))
                    seen_key = (host, sid, cid, name)
                    if marker == self.live_seen.get(seen_key):
                        continue
                    update = meta.get("last_update")
                    if not update:
                        continue
                    try:
                        ts = datetime.fromisoformat(update.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        continue
                    # Dados antigos no cache não são uma leitura atual.
                    if abs(time.time() - ts) > max(10, config["poll_seconds"] * 3):
                        continue
                    data = clean(obj.get("message", {}))
                    data.pop("type", None)
                    if name == "PARAM_VALUE" and isinstance(data.get("param_id"), list):
                        data["param_id"] = "".join(data["param_id"]).rstrip("\0")
                    msgtype = name
                    if name == "NAMED_VALUE_FLOAT":
                        named = data.get("name", "")
                        if isinstance(named, list):
                            named = "".join(named).rstrip("\0")
                        msgtype += ":" + str(named)
                    if name == "STATUSTEXT" and isinstance(data.get("text"), list):
                        data["text"] = "".join(data["text"]).rstrip("\0")
                    entries.append((ts, msgtype, f"{sid}:{cid}", data))
                    markers[seen_key] = marker
        if system:
            now = time.time()
            for name, value in system.items():
                entries.append((now, "SYSTEM_" + name.upper(), "linux2rest", {"value": value}))
        if not entries:
            return 0
        entries.sort(key=lambda entry: entry[0])
        summary = self.summaries.setdefault(key, Summary())
        with self.store.write_lock, self.store.db() as db:
            row = db.execute("SELECT id,summary FROM files WHERE source_key=?", (key,)).fetchone()
            if row:
                file_id = row["id"]
                if summary.value["records"] == 0:
                    summary.value.update(json.loads(row["summary"]))
                    summary.heartbeat = None
                    summary.value["gaps"] += 1
                    summary.segment = summary.value.get("last_segment", 0) + 1
                    active = summary.value.pop("active_armed_period", None)
                    if active:
                        summary.value["armed_periods"].append({**active, "open": True, "reason": "Coleta interrompida antes do reinício"})
            else:
                file_id = db.execute("INSERT INTO files(source_key,name,kind,path,size,imported_at,operation_id) VALUES(?,?,'live','',0,?,?)", (key, f"Coleta {host} · {date} UTC · {operation['name']}", time.time(), operation["id"])).lastrowid
            seq = db.execute("SELECT coalesce(max(seq),-1) FROM records WHERE file_id=?", (file_id,)).fetchone()[0] + 1
            for ts, name, origin, data in entries:
                stream = db.execute("SELECT id,fields FROM streams WHERE file_id=? AND type=? AND origin=?", (file_id, name, origin)).fetchone()
                fields = set(flatten(data))
                if stream:
                    stream_id = stream["id"]
                    fields.update(json.loads(stream["fields"]))
                    db.execute("UPDATE streams SET count=count+1,last_ts=?,fields=? WHERE id=?", (ts, dumps(sorted(fields)), stream_id))
                else:
                    stream_id = db.execute("INSERT INTO streams(file_id,type,origin,count,fields,first_ts,last_ts) VALUES(?,?,?,1,?,?,?)", (file_id, name, origin, dumps(sorted(fields)), ts, ts)).lastrowid
                segment = summary.add(ts, name, origin, data, seq, "live", self.store.catalog)
                db.execute("INSERT INTO records(file_id,stream_id,seq,ts,segment,data) VALUES(?,?,?,?,?,?)", (file_id, stream_id, seq, ts, segment, dumps(data)))
                if name == "STATUSTEXT":
                    db.execute("INSERT INTO events(file_id,ts,seq,severity,type,text) VALUES(?,?,?,?,?,?)", (file_id, ts, seq, str(data.get("severity", "INFO")), name, str(data.get("text", ""))))
                seq += 1
            db.execute("UPDATE files SET summary=?,imported_at=? WHERE id=?", (dumps(summary.value), time.time(), file_id))
        self.live_seen.update(markers)
        self.collection["samples"] += len(entries)
        return len(entries)

    def collector(self):
        last_system = 0
        last_sync = 0
        while not self.stop.is_set():
            config = self.store.config()
            if config["collect"]:
                try:
                    client = BlueOS(config["host"])
                    cache = client.read(":6040/v1/mavlink")
                    system = None
                    if time.time() - last_system > 10:
                        try:
                            system = client.read(":6030/system")
                        except Exception:
                            pass
                        last_system = time.time()
                    self.ingest_live(cache, system)
                    self.collection.update(connected=True, last_success=time.time(), error=None)
                    if time.time() - last_sync > 300 and not any(j["action"] == "sync" and j["status"] in ("running", "queued") for j in self.jobs.values()):
                        self.submit("sync", host=config["host"])
                        last_sync = time.time()
                except Exception as error:
                    self.collection.update(connected=False, error=str(error))
            else:
                self.collection["connected"] = False
            self.stop.wait(config["poll_seconds"])

    def close(self):
        self.stop.set()
