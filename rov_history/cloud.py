"""Envio do Desktop ao Supabase, conforme docs/supabase/INTEGRACAO.md.

O PC faz login como usuário (chave publishable + sessão do usuário). Nenhuma senha
ou chave administrativa fica no programa; o refresh token é protegido pelo
Windows (DPAPI). Uma fila só é concluída depois de metadados e original enviados.
"""
import base64
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .model import dumps

BUCKET = "bravo-logs"
CHUNK = 6 * 1024 * 1024  # O upload retomável do Supabase exige partes de 6 MB.


class CloudError(ValueError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def protect(text):
    """Protege o token com a conta do Windows; em outros sistemas (testes), apenas codifica."""
    data = text.encode()
    try:
        import ctypes
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]
        source = Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
        result = Blob()
        if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)):
            raise OSError("DPAPI")
        protected = ctypes.string_at(result.data, result.size)
        ctypes.windll.kernel32.LocalFree(result.data)
        return "dpapi:" + base64.b64encode(protected).decode()
    except (AttributeError, OSError):
        return "plain:" + base64.b64encode(data).decode()


def unprotect(value):
    kind, _, payload = (value or "").partition(":")
    data = base64.b64decode(payload)
    if kind == "plain":
        return data.decode()
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]
    source = Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char)))
    result = Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)):
        raise CloudError("Sessão salva não pode ser lida nesta conta do Windows; entre novamente.")
    text = ctypes.string_at(result.data, result.size).decode()
    ctypes.windll.kernel32.LocalFree(result.data)
    return text


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def epoch(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() if text else None


class Supabase:
    def __init__(self, url, key, timeout=30):
        if not re.fullmatch(r"https://[a-z0-9-]+\.supabase\.co|http://127\.0\.0\.1:\d+", (url or "").rstrip("/")):
            raise CloudError("Informe a URL do projeto (https://<projeto>.supabase.co).")
        if not key or key.startswith("sb_secret") or "service_role" in key:
            raise CloudError("Use a chave publishable (anon); chaves secretas não podem ficar no PC.")
        self.url = url.rstrip("/")
        self.key = key
        self.timeout = timeout
        self.session = None

    def call(self, method, path, body=None, headers=None, data=None, auth=True):
        headers = {"apikey": self.key, **(headers or {})}
        if auth and self.session:
            headers["Authorization"] = "Bearer " + self.session["access_token"]
        if body is not None:
            data = dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        request = Request(self.url + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                return response.status, response.headers, json.loads(raw) if raw and "json" in response.headers.get("Content-Type", "") else raw
        except HTTPError as error:
            with error:
                raw = error.read()
            try:
                detail = json.loads(raw)
                message = detail.get("msg") or detail.get("message") or detail.get("error_description") or detail.get("error") or str(detail)
            except (ValueError, AttributeError):
                message = raw.decode("utf-8", "replace")[:200] or error.reason
            raise CloudError(f"{error.code}: {message}", error.code) from None

    def login(self, email, password):
        _, _, session = self.call("POST", "/auth/v1/token?grant_type=password", {"email": email, "password": password}, auth=False)
        self.session = session
        return session

    def refresh(self, refresh_token):
        _, _, session = self.call("POST", "/auth/v1/token?grant_type=refresh_token", {"refresh_token": refresh_token}, auth=False)
        self.session = session
        return session

    def member(self):
        uid = self.session["user"]["id"]
        _, _, rows = self.call("GET", f"/rest/v1/bravo_members?user_id=eq.{uid}&select=role,enabled,fleet_access")
        if not rows or not rows[0]["enabled"]:
            raise CloudError("Conta sem acesso ao Bravo Viewer; peça liberação à gestão.", 403)
        return rows[0]

    def register_device(self, device, equipment):
        self.call("POST", "/rest/v1/rpc/bravo_register_device", {"target_device": device, "equipment_name": equipment})

    def upsert(self, table, rows, conflict):
        if rows:
            self.call("POST", f"/rest/v1/{table}?on_conflict={conflict}", rows,
                      {"Prefer": "resolution=merge-duplicates,return=minimal"})

    def sessions(self, device):
        _, _, rows = self.call("GET", f"/rest/v1/bravo_sessions?device_id=eq.{device}&select=source_key,source_revision,sha256,storage_path")
        return {row["source_key"]: row for row in rows}

    def object_size(self, name):
        try:
            _, headers, _ = self.call("HEAD", f"/storage/v1/object/{BUCKET}/{quote(name)}")
            return int(headers.get("Content-Length", -1))
        except CloudError as error:
            if error.status in (400, 404):
                return None
            raise

    def upload(self, path, name, progress=lambda done, total: None, resume=None):
        """Upload retomável (TUS). resume: endereço de um upload interrompido."""
        size = Path(path).stat().st_size
        tus = {"Tus-Resumable": "1.0.0"}
        location, offset = resume, 0
        if location:
            try:
                _, headers, _ = self.call("HEAD", location, headers=tus)
                offset = int(headers.get("Upload-Offset", 0))
            except CloudError:
                location = None
        if not location:
            meta = ",".join(f"{k} {base64.b64encode(v.encode()).decode()}" for k, v in
                             (("bucketName", BUCKET), ("objectName", name), ("contentType", "application/octet-stream")))
            _, headers, _ = self.call("POST", "/storage/v1/upload/resumable",
                                      headers={**tus, "Upload-Length": str(size), "Upload-Metadata": meta, "x-upsert": "false"}, data=b"")
            location = headers["Location"]
            if location.startswith(self.url):
                location = location[len(self.url):]
        with open(path, "rb") as source:
            source.seek(offset)
            while offset < size:
                chunk = source.read(CHUNK)
                _, headers, _ = self.call("PATCH", location, data=chunk,
                                          headers={**tus, "Upload-Offset": str(offset), "Content-Type": "application/offset+octet-stream"})
                offset = int(headers.get("Upload-Offset", offset + len(chunk)))
                progress(offset, size)
        return location


class CloudSync:
    """Fila de envio do PC para o Supabase: operações, sessões compactas e logs originais."""

    def __init__(self, fleet, directory):
        self.fleet = fleet
        self.path = Path(directory) / "nuvem.json"
        self.config = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.state = {"connected": False, "message": "Nuvem não configurada", "error": None, "pending": 0, "running": False}
        self.wake = threading.Event()
        self.lock = threading.Lock()

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(dumps(self.config), encoding="utf-8")
        temporary.replace(self.path)

    def public(self):
        return {**self.state, "url": self.config.get("url", ""), "key": self.config.get("key", ""),
                "email": self.config.get("email", ""), "enabled": self.config.get("enabled", False),
                "logged_in": bool(self.config.get("refresh"))}

    def login(self, url, key, email, password, enabled=True):
        client = Supabase(url, key)
        session = client.login(email, password)
        member = client.member()
        self.config = {"url": client.url, "key": key, "email": email, "enabled": enabled,
                       "refresh": protect(session["refresh_token"]), "role": member["role"]}
        self.save()
        self.state.update(connected=True, error=None, role=member["role"], message=f"Conectado como {email} ({member['role']})")
        self.wake.set()
        return self.public()

    def logout(self):
        self.config.pop("refresh", None)
        self.config["enabled"] = False
        self.save()
        self.state.update(connected=False, message="Desconectado da nuvem", error=None)

    def client(self):
        client = Supabase(self.config["url"], self.config["key"])
        session = client.refresh(unprotect(self.config["refresh"]))
        # O refresh token é rotacionado a cada uso.
        self.config["refresh"] = protect(session["refresh_token"])
        self.save()
        member = client.member()
        self.state.update(connected=True, role=member["role"])
        return client

    def run_once(self):
        if not (self.config.get("enabled") and self.config.get("refresh")):
            return
        with self.lock:
            self.state.update(running=True, error=None, message="Conectando à nuvem")
            try:
                client = self.client()
                sent = 0
                for device_id, device in list(self.fleet.devices.items()):
                    if device.get("cloud") and not device.get("host"):
                        continue  # ROV visto só pela nuvem: nada local para enviar.
                    sent += self.sync_device(client, device_id, device)
                self.state["message"] = "Recebendo dados de outros PCs"
                received = self.pull(client)
                parts = [f"{sent} {'sessão enviada' if sent == 1 else 'sessões enviadas'}" if sent else "", f"{received} {'recebida' if received == 1 else 'recebidas'}" if received else ""]
                self.state.update(message="Nuvem em dia" + (" · " + ", ".join(p for p in parts if p) if any(parts) else ""), pending=0, last=time.time())
            except CloudError as error:
                auth = error.status in (401, 403)
                self.state.update(connected=not auth, error=str(error),
                                  message="Entre novamente na nuvem ou peça acesso à gestão" if auth else f"Envio pausado: {error}")
                if auth:
                    self.config["enabled"] = False
                    self.save()
            except OSError as error:
                self.state.update(error=str(error), message="Sem internet · envio pendente")
            finally:
                self.state["running"] = False

    def sync_device(self, client, device_id, device):
        store = self.fleet.service(device_id).store
        equipment = device.get("equipment") or "ROV"
        registered = self.config.setdefault("registered", {})
        if registered.get(device_id) != equipment:
            client.register_device(device_id, equipment)
            registered[device_id] = equipment
        operations = store.operations()
        # Só sobe operação que mudou desde o último envio: dados recebidos da nuvem não voltam para ela.
        sent_ops = self.config.setdefault("sent_ops", {}).setdefault(device_id, {})
        rows = []
        for op in operations:
            row = {"device_id": device_id, "op_id": op["id"], "name": op["name"], "location": op["location"], "client": op["client"],
                   "notes": op["notes"], "started_at": iso(op["started_at"]), "ended_at": iso(op["ended_at"]), "status": op["status"],
                   "summary": op["summary"]}
            fingerprint = hashlib.sha256(dumps(row).encode()).hexdigest()
            if sent_ops.get(str(op["id"])) != fingerprint:
                rows.append((row, fingerprint))
        client.upsert("bravo_operations", [row for row, _ in rows], "device_id,op_id")
        for row, fingerprint in rows:
            sent_ops[str(row["op_id"])] = fingerprint
        remote = client.sessions(device_id)
        with store.db() as db:
            files = [dict(row) for row in db.execute("SELECT id,source_key,name,kind,sha256,size,summary,note,operation_id,path,remote_revision,imported_at FROM files")]
        known = {op["id"] for op in operations}
        pending = []
        for file in files:
            revision = file["remote_revision"] or file["imported_at"]
            mine = remote.get(file["source_key"])
            has_log = file["kind"] != "live" and file["path"] and Path(file["path"]).is_file()
            if mine and mine["source_revision"] is not None and mine["source_revision"] >= revision and mine["sha256"] == file["sha256"] and (mine["storage_path"] or not has_log):
                continue
            pending.append((file, revision, has_log))
        self.state["pending"] = len(pending)
        uploads = self.config.setdefault("uploads", {})
        for index, (file, revision, has_log) in enumerate(pending):
            storage_path = None
            if has_log:
                storage_path = f"{device_id}/{file['sha256']}{Path(file['path']).suffix.lower()}"
                size = client.object_size(storage_path)
                if size is None:
                    def progress(done, total, name=file["name"], number=index + 1):
                        self.state["message"] = f"Enviando {device.get('equipment', '')} · {number}/{len(pending)} · {done * 100 // max(total, 1)}% · {name}"
                    location = client.upload(file["path"], storage_path, progress, uploads.get(storage_path))
                    uploads[storage_path] = location
                    self.save()
                    size = client.object_size(storage_path)
                if size != Path(file["path"]).stat().st_size:
                    raise CloudError(f"Arquivo na nuvem diferente do log local: {file['name']}")
                uploads.pop(storage_path, None)
            summary = json.loads(file["summary"])
            client.upsert("bravo_sessions", [{
                "device_id": device_id, "source_key": file["source_key"],
                "operation_id": file["operation_id"] if file["operation_id"] in known else None,
                "name": file["name"], "kind": file["kind"], "sha256": file["sha256"] if has_log else None,
                "size": file["size"], "records": summary.get("records", 0), "source_revision": revision,
                "summary": summary, "analysis": store.file_meta(file["id"]), "note": file["note"],
                "storage_path": storage_path}], "device_id,source_key")
            self.state["pending"] = len(pending) - index - 1
        self.save()
        return len(pending)

    def pull(self, client):
        """Recebe da nuvem o que outros PCs enviaram: ROVs, operações, resumos, gráficos e ocorrências.

        Só busca o que mudou desde a última leitura. Os logs originais ficam na nuvem.
        """
        pulled = self.config.setdefault("pulled", {})
        _, _, devices = client.call("GET", "/rest/v1/bravo_devices?select=id,equipment")
        received = 0
        for device in devices:
            device_id, equipment = device["id"], device["equipment"]
            with self.fleet.lock:
                known = self.fleet.devices.setdefault(device_id, {})
                if not known.get("host"):
                    known.update(equipment=equipment, cloud=True)
                if self.fleet.active is None:
                    self.fleet.active = device_id
                self.fleet.save()
            store = self.fleet.service(device_id).store
            if not known.get("host"):
                store.configure({"equipment": equipment, "device_id": device_id})
            since = pulled.get(device_id, {})
            # Operações antes das sessões: a sessão aponta para a operação.
            ops_since = f"&updated_at=gt.{quote(since['operations'])}" if since.get("operations") else ""
            _, _, operations = client.call("GET", f"/rest/v1/bravo_operations?device_id=eq.{device_id}{ops_since}&order=updated_at&select=op_id,name,location,client,notes,started_at,ended_at,status,summary,updated_at")
            for op in operations:
                store.mirror_operation({"id": op["op_id"], "name": op["name"], "location": op["location"], "client": op["client"], "notes": op["notes"],
                                        "started_at": epoch(op["started_at"]), "ended_at": epoch(op["ended_at"]), "status": op["status"], "summary": op["summary"]})
            if operations:
                since["operations"] = operations[-1]["updated_at"]
            with store.db() as db:
                local = {row["source_key"]: dict(row) for row in db.execute("SELECT source_key,sha256,path,remote_revision FROM files")}
            while True:
                sessions_since = f"&updated_at=gt.{quote(since['sessions'])}" if since.get("sessions") else ""
                _, _, sessions = client.call("GET", f"/rest/v1/bravo_sessions?device_id=eq.{device_id}{sessions_since}&order=updated_at&limit=200"
                                                    "&select=source_key,operation_id,name,kind,sha256,size,source_revision,summary,analysis,note,storage_path,updated_at")
                for row in sessions:
                    mine = local.get(row["source_key"])
                    # Já temos igual ou mais novo (ex.: processado aqui a partir do log): nada a fazer.
                    if mine and mine["sha256"] == row["sha256"] and (mine["remote_revision"] or 0) >= row["source_revision"]:
                        continue
                    if mine and mine["path"] and mine["sha256"] == row["sha256"]:
                        continue
                    store.apply_meta({"source_key": row["source_key"], "name": row["name"], "kind": row["kind"], "sha256": row["sha256"],
                                      "size": row["size"], "imported_at": row["source_revision"], "summary": row["summary"],
                                      "note": row["note"], "operation_id": row["operation_id"]}, row["analysis"] or {"streams": [], "events": [], "series": []})
                    received += 1
                if sessions:
                    since["sessions"] = sessions[-1]["updated_at"]
                if len(sessions) < 200:
                    break
            pulled[device_id] = since
        self.save()
        return received

    def loop(self, stop, seconds=60):
        while not stop.is_set():
            self.run_once()
            self.wake.wait(seconds)
            self.wake.clear()
