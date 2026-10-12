from datetime import datetime, timezone
import json
from pathlib import Path
import threading

from rov_history import desktop
from rov_history.app import bundle_name, create_server
from rov_history.ingest import import_file
from rov_history.service import Service
from rov_history.store import Store
from tests.test_history import HistoryFixture


def live_cache(counter, name="HEARTBEAT"):
    now = datetime.now(timezone.utc).isoformat()
    return {"vehicles": {"1": {"components": {"1": {"messages": {name: {"message": {"type": name, "base_mode": {"bits": 0}}, "status": {"time": {"counter": counter, "last_update": now}}}}}}}}}


class DesktopSyncTests(HistoryFixture):
    def setUp(self):
        super().setUp()
        self.rov = Service(self.root / "rov", start=False)
        self.rov.configure({"equipment": "Bravo 10"})
        self.server = create_server(self.rov, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        port, desktop.ROV_PORT = desktop.ROV_PORT, self.server.server_address[1]
        self.addCleanup(setattr, desktop, "ROV_PORT", port)
        self.fleet = desktop.Fleet(self.root / "pc")

    def local(self, device):
        return self.fleet.service(device).store

    def test_device_identity_is_created_once(self):
        first = self.rov.store.config()["device_id"]
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertEqual(first, Store(self.root / "rov").config()["device_id"])
        self.assertNotIn("device_id", Store(self.root / "mirror", mirror=True).config())

    def test_pc_gets_summaries_series_and_original_logs(self):
        tlog_id = import_file(self.rov.store, self.make_file(), "blueos:127.0.0.1:logs/00001.tlog")["id"]
        self.rov.ingest_live(live_cache(1))
        self.rov.store.update_operation({"name": "Casco", "location": "Santos"})
        device = self.fleet.connect("127.0.0.1")
        self.fleet.sync(device)
        local = self.local(device)
        self.assertEqual("Bravo 10", local.config()["equipment"])
        self.assertEqual(["Casco"], [op["name"] for op in local.operations()])
        remote = {f["source_key"]: f["summary"]["records"] for f in self.rov.store.files()}
        self.assertEqual(remote, {f["source_key"]: f["summary"]["records"] for f in local.files()})
        mine = next(f for f in local.files() if f["kind"] == "tlog")
        self.assertEqual(self.rov.store.points(tlog_id, "armed"), local.points(mine["id"], "armed"))
        # O log original foi copiado, conferido e confirmado ao ROV; o inventário relê o original no PC.
        stream = next(s for s in local.streams() if s["type"] == "STATUSTEXT")
        self.assertEqual("=unsafe-text", local.records(stream["id"])["rows"][0]["data"]["text"])
        self.assertIsNotNone(self.rov.store.file(tlog_id)["pc_copied_at"])
        # Coleta ao vivo: o resumo acompanha as amostras novas sem copiar mensagem a mensagem.
        self.rov.ingest_live(live_cache(2))
        self.fleet.sync(device)
        live = next(f for f in local.files() if f["kind"] == "live")
        self.assertEqual(2, live["summary"]["records"])
        self.assertIn("Sincronizado", self.fleet.state[device]["message"])

    def test_interrupted_copy_resumes_and_rov_releases_closed_logs(self):
        tlog_id = import_file(self.rov.store, self.make_file(), "blueos:a")["id"]
        original = self.rov.store.file(tlog_id)
        device = self.fleet.connect("127.0.0.1")
        partial = self.root / "pc" / "downloads"
        partial.mkdir(parents=True)
        data = Path(original["path"]).read_bytes()
        (partial / (original["sha256"] + ".part")).write_bytes(data[:10])
        self.fleet.sync(device)
        local = next(f for f in self.local(device).files() if f["source_key"] == "blueos:a")
        self.assertEqual(data, Path(self.local(device).file(local["id"])["path"]).read_bytes())
        # Operação ainda aberta: o ROV mantém o log mesmo com a cópia confirmada.
        self.assertTrue(Path(original["path"]).is_file())
        self.rov.store.close_operation()
        self.assertEqual(1, self.rov.store.release_originals())
        self.assertEqual("", self.rov.store.file(tlog_id)["path"])
        # Resumo e gráficos continuam no ROV depois de liberar o log.
        self.assertTrue(self.rov.store.points(tlog_id, "armed")["points"])

    def test_pc_keeps_operations_removed_from_rov(self):
        import_file(self.rov.store, self.make_file(), "blueos:a")
        closed = self.rov.store.close_operation({"name": "Antiga"})["closed"]
        device = self.fleet.connect("127.0.0.1")
        self.fleet.sync(device)
        self.rov.store.delete_operation(closed)
        self.fleet.sync(device)
        names = [op["name"] for op in self.local(device).operations()]
        self.assertIn("Antiga", names)
        self.assertEqual(1, len(self.local(device).files()))

    def test_other_equipment_on_same_address_is_rejected(self):
        device = self.fleet.connect("127.0.0.1")
        with self.rov.store.db() as db:
            db.execute("UPDATE settings SET value=? WHERE key='device_id'", (json.dumps("f" * 32),))
        self.fleet.sync(device)
        self.assertIn("outro equipamento", self.fleet.state[device]["error"])

    def test_bundle_name_has_equipment_operation_and_date(self):
        self.rov.store.update_operation({"name": "Inspeção de Casco"})
        op = self.rov.store.current_operation()
        (self.rov.store.directory / "exports").mkdir(exist_ok=True)
        (self.rov.store.directory / "exports" / ("a" * 32 + ".zip")).write_bytes(b"zip")
        self.rov.store.set_operation_bundle(op["id"], "a" * 32)
        self.assertRegex(bundle_name(self.rov.store, "a" * 32), r"^Bravo-10_Inspecao-de-Casco_\d{4}-\d\d-\d\d\.zip$")
