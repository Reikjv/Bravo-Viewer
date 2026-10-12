import gzip
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

from pymavlink.dialects.v20 import ardupilotmega as mav

from rov_history.app import create_server
from rov_history.capabilities import capabilities
from rov_history.ingest import import_file
from rov_history.model import Catalog
from rov_history.service import Service
from rov_history.store import Store


def tlog(path, messages):
    link = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    with path.open("wb") as file:
        for ts, msg in messages:
            file.write(struct.pack(">Q", int(ts * 1e6)))
            file.write(msg.pack(link))


def heartbeat(armed):
    return mav.MAVLink_heartbeat_message(12, 3, 128 if armed else 0, 0, 4, 3)


def param(name, value):
    return mav.MAVLink_param_value_message(name.encode(), value, 9, 1, 0)


class HistoryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "db")
        self.now = time.time() - 100

    def tearDown(self):
        self.temp.cleanup()

    def make_file(self):
        path = self.root / "sample.tlog"
        ts = self.now
        tlog(path, [(ts, heartbeat(False)), (ts + 1, heartbeat(True)), (ts + 2, heartbeat(True)), (ts + 3, heartbeat(False)), (ts + 4, param("STAT_RUNTIME", 300)), (ts + 5, mav.MAVLink_statustext_message(4, b'=unsafe-text'))])
        return path


class HistoryTests(HistoryFixture):
    def test_roundtrip_duplicates_and_replacement(self):
        path = self.make_file()
        first = import_file(self.store, path, "blueos:test:file")
        self.assertEqual(6, first["records"])
        self.assertTrue(import_file(self.store, path, "blueos:test:file")["duplicate"])
        self.assertEqual(1, len(self.store.files()))
        tlog(path, [(self.now, heartbeat(False)), (self.now + 1, param("STAT_RUNTIME", 400))])
        changed = import_file(self.store, path, "blueos:test:file")
        self.assertEqual(first["id"], changed["id"])
        self.assertEqual(2, self.store.overview()["records"])
        self.assertEqual(400, self.store.overview()["statistics"]["STAT_RUNTIME"])

    def test_armed_intervals_do_not_count_missing_time(self):
        path = self.root / "gaps.tlog"
        tlog(path, [(self.now, heartbeat(True)), (self.now+1, heartbeat(True)), (self.now+50, heartbeat(True)), (self.now+51, heartbeat(True)), (self.now+52, heartbeat(False))])
        import_file(self.store, path)
        summary = self.store.files()[0]["summary"]
        self.assertEqual(2, summary["armed_seconds"])
        self.assertEqual(3, summary["observed_seconds"])
        self.assertEqual(1, summary["gaps"])
        self.assertEqual(2, len(summary["armed_periods"]))

    def test_clock_jump_breaks_series(self):
        path = self.root / "clock.tlog"
        tlog(path, [(self.now, heartbeat(True)), (self.now+1, heartbeat(True)), (self.now+10000, heartbeat(True))])
        file_id = import_file(self.store, path)["id"]
        summary = self.store.files()[0]["summary"]
        self.assertEqual(1, len(summary["clock_issues"]))
        self.assertEqual(1, summary["armed_seconds"])
        # O salto cai em intervalos de 10 s distantes; o gráfico interrompe a linha nessa lacuna.
        times = [point["t"] for point in self.store.points(file_id, "armed")["points"]]
        self.assertIn(len(times), (2, 3))
        self.assertGreater(times[-1] - times[0], 9000)

    def test_armed_chart_uses_bit_not_entire_mode(self):
        file_id = import_file(self.store, self.make_file())["id"]
        points = self.store.points(file_id, "armed")["points"]
        # base_mode 128 marca armado; outros bits do modo não contam.
        self.assertEqual((0, 1), (min(p["min"] for p in points), max(p["max"] for p in points)))
        self.assertIn("armed", [m["metric"] for m in self.store.metrics(file_id)])
        with self.assertRaises(ValueError): self.store.points(file_id, "base_mode")

    def test_capability_grid_separates_scope_and_observed_data(self):
        path = self.root / "attitude.tlog"
        tlog(path, [(self.now, mav.MAVLink_attitude_message(0, 0, 0, 0, 0, 0, 0))])
        import_file(self.store, path)
        rows = capabilities(self.store)["rows"]
        attitude = next(row for row in rows if row["name"].startswith("Atitude"))
        self.assertEqual("Painel futuro", attitude["scope"])
        self.assertEqual("Registros identificados", attitude["data"])
        depth = next(row for row in rows if row["name"].startswith("Profundidade"))
        self.assertIn("depende de sensor", depth["scope"])
        self.assertFalse(depth["sources"])
        hours = rows[0]
        self.assertEqual("Disponível nesta versão", hours["scope"])
        self.assertEqual("Ainda sem registros identificados", hours["data"])

    def test_units_and_unknown_battery_values(self):
        c = Catalog()
        self.assertAlmostEqual(24.5, c.measure("tlog", "SYS_STATUS", "voltage_battery", 24500, {})["value"])
        self.assertAlmostEqual(.13, c.measure("tlog", "SYS_STATUS", "current_battery", 13, {})["value"])
        self.assertAlmostEqual(1, c.measure("tlog", "BATTERY_STATUS", "energy_consumed", 36, {})["value"])
        self.assertFalse(c.measure("tlog", "BATTERY_STATUS", "temperature", 32767, {})["valid"])
        self.assertFalse(c.measure("tlog", "GPS_RAW_INT", "lat", 0, {"fix_type": 0})["valid"])

    def test_csv_and_jsonl_keep_all_rows_and_escape_text(self):
        import_file(self.store, self.make_file())
        stream = next(s for s in self.store.streams() if s["type"] == "STATUSTEXT")
        csv = b"".join(self.store.export(stream["id"])).decode("utf-8-sig")
        self.assertIn("'=unsafe-text", csv)
        data = [json.loads(line) for line in b"".join(self.store.export(stream["id"], "jsonl")).decode().splitlines()]
        self.assertEqual("=unsafe-text", data[0]["raw"]["text"])
        heartbeats = next(s for s in self.store.streams() if s["type"] == "HEARTBEAT")
        self.assertEqual(4, len(b"".join(self.store.export(heartbeats["id"], "jsonl")).splitlines()))

    def test_gz_system_logs_and_original_preserved(self):
        path = self.root / "linux.log.gz"
        line = '2026-10-10T21:00:00Z INFO recorder: system-memory: {"ram":{"used_kB":123}}\n2026-10-10T21:00:01Z ERROR worker: Failure\n'
        with gzip.open(path, "wt") as file: file.write(line)
        result = import_file(self.store, path)
        self.assertEqual(2, result["records"])
        self.assertEqual(1, self.store.events()["total"])
        stream = next(s for s in self.store.streams() if s["type"] == "SYSTEM_MEMORY")
        self.assertIn("value.ram.used_kB", stream["fields"])
        self.assertEqual(path.read_bytes(), Path(self.store.file(result["id"])["path"]).read_bytes())

    def test_import_failure_rolls_back_previous_version(self):
        path = self.root / "test.log.gz"
        with gzip.open(path, "wt") as file: file.write("2026-10-10T21:00:00Z INFO working\n")
        result = import_file(self.store, path, "source")
        path.write_bytes(b"not a gzip")
        with self.assertRaises(OSError): import_file(self.store, path, "source")
        self.assertEqual(result["id"], self.store.files()[0]["id"])
        self.assertEqual(1, self.store.overview()["records"])

    def test_live_cache_is_not_repeated_and_persists(self):
        service = Service(self.root / "live", start=False)
        stamp = __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
        cache = {"vehicles":{"1":{"components":{"1":{"messages":{"HEARTBEAT":{"message":{"type":"HEARTBEAT","base_mode":{"bits":0}},"status":{"time":{"counter":1,"last_update":stamp}}}}}}}}}
        self.assertEqual(1, service.ingest_live(cache))
        self.assertEqual(0, service.ingest_live(cache))
        second = Service(self.root / "live", start=False)
        self.assertEqual(1, second.store.overview()["records"])

    def test_live_api_character_arrays_and_message_order(self):
        service = Service(self.root / "live-fields", start=False)
        from datetime import datetime, timezone
        def entry(message, ts):
            return {"message": message, "status": {"time": {"counter": 1, "last_update": datetime.fromtimestamp(ts, timezone.utc).isoformat()}}}
        now = time.time()
        messages = {
            "PARAM_VALUE": entry({"param_id": list("STAT_RUNTIME\0"), "param_value": 8000}, now),
            "HEARTBEAT": entry({"base_mode": {"bits": 81}}, now - 4),
        }
        cache = {"vehicles": {"1": {"components": {"1": {"messages": messages}}}}}
        service.ingest_live(cache)
        self.assertEqual(8000, service.store.overview()["statistics"]["STAT_RUNTIME"])
        self.assertEqual(0, service.store.overview()["clock_issues"])

    def test_old_import_does_not_override_newer_statistics(self):
        recent = self.root / "recent.tlog"
        old = self.root / "old.tlog"
        tlog(recent, [(self.now, param("STAT_RUNTIME", 500))])
        tlog(old, [(self.now - 100, param("STAT_RUNTIME", 100))])
        import_file(self.store, recent)
        import_file(self.store, old)
        self.assertEqual(500, self.store.overview()["statistics"]["STAT_RUNTIME"])

    def test_dataflash_uses_file_formats_including_unknown_type(self):
        path = self.root / "sample.bin"
        fmt = b'\xa3\x95\x80' + struct.pack('<BB4s16s64s', 200, 15, b'USER', b'Qf', b'TimeUS,Custom')
        row = b'\xa3\x95\xc8' + struct.pack('<Qf', 1000000, 24.5)
        path.write_bytes(fmt + row)
        import_file(self.store, path)
        stream = next(s for s in self.store.streams() if s["type"] == "USER")
        self.assertIn("Custom", stream["fields"])
        self.assertEqual(24.5,self.store.records(stream["id"])["rows"][0]["data"]["Custom"])

    def test_http_queries_proxy_and_exports(self):
        service = Service(self.root / "http", start=False)
        import_file(service.store, self.make_file())
        server = create_server(service, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        try:
            with urlopen(base+"/history/api/overview") as response:
                self.assertEqual(6,json.load(response)["records"])
            with urlopen(base+"/history/") as response:
                self.assertIn(b"Bravo Viewer", response.read())
            with urlopen(base+"/history/api/capabilities") as response:
                self.assertGreater(len(json.load(response)["rows"]), 10)
            inventory = {"host": "192.168.2.2", "captured_at": self.now, "files": [{"path": "/ardupilot_logs/logs/empty.bin", "size": 0}], "errors": []}
            (service.store.directory / "remote_inventory.json").write_text(json.dumps(inventory), encoding="utf-8")
            with urlopen(base+"/history/api/inventory") as response:
                self.assertEqual(0, json.load(response)["files"][0]["size"])
            request = Request(base+"/api/config",data=b'{"host":"192.168.2.2","collect":false}',headers={"X-Reikjv-History":"1","Content-Type":"application/json"})
            with urlopen(request) as response:self.assertEqual("192.168.2.2", json.load(response)["host"])
        finally:
            server.shutdown(); server.server_close(); service.close()


if __name__ == "__main__": unittest.main()
