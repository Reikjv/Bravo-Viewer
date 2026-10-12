"""Fila de envio contra um Supabase simulado (Auth, Data API e upload retomável TUS)."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse

from rov_history import cloud
from rov_history.cloud import CloudSync, Supabase, protect, unprotect
from rov_history.ingest import import_file
from rov_history.service import Service
from tests.test_history import HistoryFixture


class FakeSupabase:
    def __init__(self):
        self.members = {"user-1": {"role": "operador", "enabled": True, "fleet_access": True}}
        self.devices, self.operations, self.sessions, self.objects, self.uploads = {}, {}, {}, {}, {}
        self.fail_patch_once = False
        self.refreshes = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, body=None, headers=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def body(self):
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))

            def authorized(self):
                return self.headers.get("Authorization") == "Bearer token-1" and self.headers.get("apikey") == "sb_publishable_teste"

            def do_POST(self):
                url = urlparse(self.path)
                raw = self.body()
                if url.path == "/auth/v1/token":
                    data = json.loads(raw)
                    if data.get("password") == "errada":
                        return self.reply(400, {"error_description": "Invalid login credentials"})
                    fake.refreshes += "refresh_token" in data
                    return self.reply(200, {"access_token": "token-1", "refresh_token": f"refresh-{fake.refreshes}", "user": {"id": "user-1"}})
                if not self.authorized():
                    return self.reply(401, {"message": "JWT expired"})
                if url.path == "/rest/v1/rpc/bravo_register_device":
                    data = json.loads(raw)
                    fake.devices[data["target_device"]] = data["equipment_name"]
                    return self.reply(204)
                if url.path.startswith("/rest/v1/"):
                    table = url.path.rsplit("/", 1)[1]
                    for row in json.loads(raw):
                        if table == "bravo_operations":
                            fake.operations[(row["device_id"], row["op_id"])] = row
                        else:
                            assert row["storage_path"] is None or row["storage_path"] in fake.objects, "sessão só depois do arquivo"
                            fake.sessions[(row["device_id"], row["source_key"])] = row
                    return self.reply(201)
                if url.path == "/storage/v1/upload/resumable":
                    meta = dict(item.split(" ") for item in self.headers["Upload-Metadata"].split(","))
                    name = __import__("base64").b64decode(meta["objectName"]).decode()
                    fake.uploads[name] = {"length": int(self.headers["Upload-Length"]), "data": b""}
                    return self.reply(201, headers={"Location": "/storage/v1/upload/resumable/" + name, "Tus-Resumable": "1.0.0"})
                self.reply(404, {"message": "rota"})

            def do_PATCH(self):
                name = urlparse(self.path).path.split("/resumable/", 1)[1]
                upload = fake.uploads[name]
                chunk = self.body()
                assert int(self.headers["Upload-Offset"]) == len(upload["data"])
                upload["data"] += chunk
                if fake.fail_patch_once:
                    fake.fail_patch_once = False
                    return self.reply(500, {"message": "queda simulada"})
                if len(upload["data"]) == upload["length"]:
                    fake.objects[name] = upload["data"]
                self.reply(204, headers={"Upload-Offset": str(len(upload["data"])), "Tus-Resumable": "1.0.0"})

            def do_HEAD(self):
                path = urlparse(self.path).path
                if "/resumable/" in path:
                    upload = fake.uploads[path.split("/resumable/", 1)[1]]
                    return self.reply(200, headers={"Upload-Offset": str(len(upload["data"])), "Tus-Resumable": "1.0.0"})
                name = path.split(f"/object/{cloud.BUCKET}/", 1)[1]
                if name not in fake.objects:
                    return self.reply(400, {"message": "not found"})
                self.reply(200, headers={"Content-Length": str(len(fake.objects[name]))})

            def do_GET(self):
                url = urlparse(self.path)
                if not self.authorized():
                    return self.reply(401, {"message": "JWT expired"})
                query = parse_qs(url.query)
                if url.path == "/rest/v1/bravo_members":
                    member = fake.members.get(query["user_id"][0].split(".", 1)[1])
                    return self.reply(200, [member] if member else [])
                if url.path == "/rest/v1/bravo_sessions":
                    device = query["device_id"][0].split(".", 1)[1]
                    return self.reply(200, [{k: row[k] for k in ("source_key", "source_revision", "sha256", "storage_path")} for (d, _), row in fake.sessions.items() if d == device])
                self.reply(404, {"message": "rota"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


class FakeFleet:
    def __init__(self, service, device):
        self._service = service
        self.devices = {device: {"equipment": "Bravo 12"}}

    def service(self, device_id=None):
        return self._service


class CloudTests(HistoryFixture):
    def setUp(self):
        super().setUp()
        self.fake = FakeSupabase()
        self.addCleanup(self.fake.server.server_close)
        self.addCleanup(self.fake.server.shutdown)
        self.service = Service(self.root / "pc", start=False, mirror=True)
        self.addCleanup(self.service.close)
        self.device = "d" * 32
        self.service.store.mirror_operation({"id": 1, "name": "Casco", "location": "Santos", "client": "", "notes": "", "started_at": self.now, "ended_at": None, "status": "open", "summary": {}})
        self.file = import_file(self.service.store, self.make_file(), "blueos:x:/ardupilot_logs/logs/1.tlog", operation_id=1)
        self.sync = CloudSync(FakeFleet(self.service, self.device), self.root)

    def test_token_protection_roundtrip(self):
        self.assertEqual("segredo", unprotect(protect("segredo")))
        self.assertNotIn("segredo", protect("segredo"))

    def test_rejects_secret_keys_and_wrong_password(self):
        with self.assertRaises(cloud.CloudError):
            Supabase(self.fake.url, "sb_secret_abc")
        with self.assertRaises(cloud.CloudError) as caught:
            self.sync.login(self.fake.url, "sb_publishable_teste", "operador@bravo", "errada")
        self.assertIn("Invalid login", str(caught.exception))

    def test_queue_uploads_original_then_session_and_resumes(self):
        self.sync.login(self.fake.url, "sb_publishable_teste", "operador@bravo", "senha")
        self.assertNotIn("refresh-0", json.dumps(self.sync.config))
        previous, cloud.CHUNK = cloud.CHUNK, 64
        self.addCleanup(setattr, cloud, "CHUNK", previous)
        self.fake.fail_patch_once = True
        self.sync.run_once()
        self.assertIn("Envio pausado", self.sync.state["message"])
        self.assertEqual({}, self.fake.sessions)
        # Nova tentativa continua do ponto em que o upload parou.
        self.sync.run_once()
        local = Path(self.service.store.file(self.file["id"])["path"]).read_bytes()
        self.assertEqual([local], list(self.fake.objects.values()))
        session = next(iter(self.fake.sessions.values()))
        self.assertEqual((1, 6), (session["operation_id"], session["records"]))
        self.assertTrue(session["analysis"]["series"])
        self.assertEqual("Bravo 12", self.fake.devices[self.device])
        self.assertEqual("Casco", self.fake.operations[(self.device, 1)]["name"])
        # Nada novo: nenhum reenvio.
        uploads = len(self.fake.uploads)
        self.sync.run_once()
        self.assertEqual(uploads, len(self.fake.uploads))
        self.assertEqual("Nuvem em dia", self.sync.state["message"])

    def test_account_without_access_stops_queue(self):
        self.sync.login(self.fake.url, "sb_publishable_teste", "operador@bravo", "senha")
        self.fake.members["user-1"]["enabled"] = False
        self.sync.run_once()
        self.assertFalse(self.sync.config["enabled"])
        self.assertIn("acesso", self.sync.state["error"])
