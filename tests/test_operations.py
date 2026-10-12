import json
import zipfile

from pymavlink.dialects.v20 import ardupilotmega as mav

from rov_history.exporter import build_bundle
from rov_history.ingest import import_file
from rov_history import store as store_module
from rov_history.service import Service, active_files, parse_time
from rov_history.store import Store
from tests.test_history import HistoryFixture, heartbeat, tlog


def position(depth):
    return mav.MAVLink_global_position_int_message(0, 0, 0, 0, int(-depth * 1000), 0, 0, 0, 0)


class OperationTests(HistoryFixture):
    def dive(self, name, depths, external=True, start=None):
        path = self.root / name
        start = self.now if start is None else start
        messages = [(start + i, heartbeat(True)) for i in range(len(depths))]
        messages += [(start + i + .5, position(d)) for i, d in enumerate(depths)]
        if external:
            messages.append((start + .2, mav.MAVLink_scaled_pressure2_message(0, 1100.0, 0.0, 2000)))
        tlog(path, sorted(messages, key=lambda m: m[0]))
        return path

    def test_existing_history_becomes_first_operation(self):
        import_file(self.store, self.make_file(), "blueos:a")
        reopened = Store(self.root / "db")
        operations = reopened.operations()
        self.assertEqual(1, len(operations))
        self.assertEqual("open", operations[0]["status"])
        self.assertEqual(1, len(operations[0]["files"]))

    def test_close_freezes_summary_and_starts_next_operation(self):
        first = import_file(self.store, self.make_file(), "blueos:a")
        ids = self.store.close_operation({"name": "Inspeção píer", "location": "Santos"})
        current = self.store.current_operation()
        self.assertEqual(ids["current"], current["id"])
        closed = next(op for op in self.store.operations() if op["id"] == ids["closed"])
        self.assertEqual("Inspeção píer", closed["name"])
        self.assertEqual(1, closed["summary"]["sessions"])
        # Arquivo de operação encerrada não é substituído por versões posteriores.
        changed = self.dive("changed.tlog", [1, 2])
        self.assertTrue(import_file(self.store, changed, "blueos:a")["frozen"])
        self.assertEqual(first["id"], self.store.files()[0]["id"])
        new = import_file(self.store, self.dive("new.tlog", [1, 2]), "blueos:b")
        self.assertEqual(current["id"], self.store.file(new["id"])["operation_id"])

    def test_depth_requires_external_sensor_and_averages_submerged_time(self):
        import_file(self.store, self.dive("dive.tlog", [0, 1, 3, 5, 3, 0.2]), "blueos:dive")
        depth = self.store.operations()[0]["summary"]["depth"]
        self.assertTrue(depth["available"])
        self.assertAlmostEqual(5, depth["max"])
        # Intervalos de 1 s com profundidade anterior acima de 0,5 m: 1, 3, 5 e 3 m.
        self.assertAlmostEqual(4, depth["submerged_seconds"])
        self.assertAlmostEqual(3, depth["mean"])
        self.store.close_operation()
        import_file(self.store, self.dive("internal.tlog", [2, 4], external=False), "blueos:internal")
        self.assertFalse(self.store.operations()[0]["summary"]["depth"]["available"])

    def test_live_and_tlog_of_same_dive_are_not_summed(self):
        service = Service(self.root / "db", start=False)
        import_file(service.store, self.dive("dive.tlog", [0, 1, 2, 3]), "blueos:dive")
        summary = service.store.operations()[0]["summary"]
        self.assertEqual(3, summary["armed_seconds"])
        self.assertEqual("tlog", summary["time_source"])

    def test_limit_removes_oldest_closed_operations_and_their_data(self):
        service = Service(self.root / "db", start=False)
        store_module.OPERATION_LIMIT = 3
        self.addCleanup(setattr, store_module, "OPERATION_LIMIT", 20)
        originals = []
        for index in range(5):
            result = import_file(service.store, self.dive(f"op{index}.tlog", [1, 2 + index]), f"blueos:{index}")
            originals.append(service.store.file(result["id"])["path"])
            service.jobs["a" * 32] = {"id": "a" * 32}
            service.close_operation("a" * 32, {"values": {"name": f"Mergulho {index}"}})
        closed = [op for op in service.store.operations() if op["status"] == "closed"]
        self.assertEqual(["Mergulho 4", "Mergulho 3", "Mergulho 2"], [op["name"] for op in closed])
        self.assertEqual(3, len(service.store.files()))
        with service.store.db() as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM records WHERE file_id NOT IN (SELECT id FROM files)").fetchone()[0])
        self.assertFalse(any(__import__("pathlib").Path(path).exists() for path in originals[:2]))
        self.assertTrue(all(__import__("pathlib").Path(path).exists() for path in originals[2:]))

    def test_operation_bundle_contains_only_its_sessions_with_progress(self):
        import_file(self.store, self.make_file(), "blueos:old")
        ids = self.store.close_operation({"name": "Antiga"})
        import_file(self.store, self.dive("dive.tlog", [1, 4, 2]), "blueos:dive")
        self.store.close_operation({"name": "Casco"})
        target = self.store.operations()[1]
        steps = []
        build_bundle(self.store, "e" * 32, {"scope": "operation", "operation": target["id"], "include_data": False, "include_originals": True}, lambda text, percent=None: steps.append(percent))
        with zipfile.ZipFile(self.root / "db" / "exports" / ("e" * 32 + ".zip")) as z:
            names = z.namelist()
            operation = json.loads(z.read("Bravo-Viewer/operacao.json"))
            report = z.read("Bravo-Viewer/relatorio.html").decode()
            dives = z.read("Bravo-Viewer/mergulhos.csv").decode("utf-8-sig").splitlines()
        self.assertEqual("Casco", operation["name"])
        self.assertNotEqual(ids["closed"], operation["id"])
        # Relatório visual: indicadores, picos e mergulhos; sem listas de mensagens.
        for text in ("Tempo armado médio", "Profundidade máxima", "4,00 m", "Mergulhos", "Erros e avisos"):
            self.assertIn(text, report)
        self.assertNotIn("Inventário", report)
        self.assertEqual(2, len(dives))
        self.assertEqual(["Bravo-Viewer/logs/dive.tlog"], [name for name in names if name.startswith("Bravo-Viewer/logs/")])
        self.assertFalse(any(name.startswith(("Bravo-Viewer/dados/", "Bravo-Viewer/inventario/")) for name in names))
        self.assertTrue(all(p is None or 0 <= p <= 99 for p in steps))
        self.assertTrue(any(p for p in steps))

    def test_live_collection_follows_current_operation(self):
        service = Service(self.root / "db", start=False)
        from datetime import datetime, timezone
        def cache(counter):
            now = datetime.now(timezone.utc).isoformat()
            return {"vehicles": {"1": {"components": {"1": {"messages": {"HEARTBEAT": {"message": {"type": "HEARTBEAT", "base_mode": {"bits": 0}}, "status": {"time": {"counter": counter, "last_update": now}}}}}}}}}
        service.ingest_live(cache(1))
        ids = service.store.close_operation()
        service.ingest_live(cache(2))
        live = [f for f in service.store.files() if f["kind"] == "live"]
        self.assertEqual({ids["closed"], ids["current"]}, {f["operation_id"] for f in live})

    def test_files_still_being_written_are_not_reimported(self):
        now = parse_time("2026-10-11T20:10:00Z")
        items = [
            {"name": "00029.tlog", "path": "logs/00029.tlog", "extension": ".tlog", "modified": "2026-10-11T19:00:00Z"},
            {"name": "00030.tlog", "path": "logs/00030.tlog", "extension": ".tlog", "modified": "2026-10-11T20:06:41.073590438Z"},
            {"name": "service.log", "path": "system/service.log", "extension": ".log", "modified": "2026-10-11T20:06:00Z"},
            {"name": "old.log", "path": "system/old.log", "extension": ".log", "modified": "2026-10-11T19:00:00Z"},
        ]
        # O .tlog mais recente é a inicialização atual; o log de serviço mudou há menos de 10 minutos.
        self.assertEqual({"logs/00030.tlog", "system/service.log"}, active_files(items, now))

    def test_operation_limit_is_fixed_at_twenty(self):
        service = Service(self.root / "db", start=False)
        service.configure({"operation_limit": 3})
        self.assertEqual(20, service.store.operation_limit())
