import csv
import io
import json
import threading
import time
import zipfile
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from rov_history.app import create_server
from rov_history.exporter import build_bundle, validate_context
from rov_history.ingest import import_file
from rov_history.service import Service
from tests.test_history import HistoryFixture, tlog, heartbeat


class ExportTests(HistoryFixture):
    def test_bundle_contains_all_records_and_report(self):
        result = import_file(self.store, self.make_file())
        self.store.note(result["id"], '<script>alert(1)</script>')
        bundle = build_bundle(self.store, "a" * 32, {"scope": "all", "include_originals": True})
        with zipfile.ZipFile(self.store.directory / "exports" / (bundle["id"] + ".zip")) as z:
            manifest = json.loads(z.read("Bravo-Viewer/manifest.json"))
            count = sum(len(z.read("Bravo-Viewer/" + item["jsonl"]).splitlines()) for item in manifest["entries"])
            self.assertEqual(6, count)
            self.assertEqual(6, sum(item["records"] for item in manifest["entries"]))
            report = z.read("Bravo-Viewer/relatorio.html").decode()
            self.assertIn("&lt;script&gt;", report)
            self.assertNotIn("<script>", report)
            self.assertIn('assets/brs.png', report)
            self.assertIn('<footer><img src="assets/brand.png"', report)
            self.assertEqual(1, len(manifest["originals"]))
            item = next(item for item in manifest["entries"] if item["type"] == "STATUSTEXT")
            raw = json.loads(z.read("Bravo-Viewer/" + item["jsonl"]))
            self.assertEqual("=unsafe-text", raw["raw"]["text"])
            self.assertIn("'=unsafe-text", z.read("Bravo-Viewer/" + item["csv"]).decode('utf-8-sig'))

    def test_view_filters_stream_fields_and_events(self):
        import_file(self.store, self.make_file())
        stream = next(s for s in self.store.streams() if s["type"] == "STATUSTEXT")
        build_bundle(self.store, "b" * 32, {"scope": "view", "tab": "explorer", "stream": stream["id"], "field": "text"})
        with zipfile.ZipFile(self.store.directory / "exports" / ("b" * 32 + ".zip")) as z:
            manifest = json.loads(z.read("Bravo-Viewer/manifest.json"))
            self.assertEqual(1, len(manifest["entries"]))
            entry = manifest["entries"][0]
            self.assertEqual(["text"], entry["csv_fields"])
            self.assertEqual(1, entry["records"])
        build_bundle(self.store, "c" * 32, {"scope": "view", "tab": "events", "search": "unsafe"})
        with zipfile.ZipFile(self.store.directory / "exports" / ("c" * 32 + ".zip")) as z:
            rows = list(csv.DictReader(io.StringIO(z.read("Bravo-Viewer/ocorrencias/ocorrencias.csv").decode("utf-8-sig"))))
            self.assertEqual(1, len(rows))
            self.assertIn("unsafe", rows[0]["Mensagem"])

    def test_snapshot_remains_consistent_during_replacement(self):
        source = self.make_file()
        import_file(self.store, source, "same-source")
        with self.store.snapshot() as snapshot:
            before = snapshot.overview()["records"]
            tlog(source, [(self.now, heartbeat(False))])
            import_file(self.store, source, "same-source")
            self.assertEqual(6, before)
            self.assertEqual(6, snapshot.overview()["records"])
        self.assertEqual(1, self.store.overview()["records"])

    def test_series_are_replaced_when_source_is_updated(self):
        source = self.make_file()
        first = import_file(self.store, source, "same-source")["id"]
        self.assertEqual(1, max(p["max"] for p in self.store.points(first, "armed")["points"]))
        tlog(source, [(self.now, heartbeat(False))])
        self.assertEqual(first, import_file(self.store, source, "same-source")["id"])
        points = self.store.points(first, "armed")["points"]
        self.assertEqual([0], [point["max"] for point in points])

    def test_cancelled_export_cleans_partial_file(self):
        import_file(self.store, self.make_file())
        stop = threading.Event(); stop.set()
        with self.assertRaises(ValueError): build_bundle(self.store, "d" * 32, {"scope": "all"}, stop=stop)
        self.assertFalse(list((self.store.directory / "exports").glob("*.partial")))
        self.assertFalse(list((self.store.directory / "exports").glob("*.zip")))

    def test_queue_deduplicates_sync_and_limits_exports(self):
        service = Service(self.root / "queue", start=False)
        first = service.submit("sync", host="192.168.2.2")
        second = service.submit("sync", host="192.168.2.2")
        self.assertEqual(first["id"], second["id"])
        service.submit("bundle", context={"scope": "all"})
        with self.assertRaises(ValueError): service.submit("bundle", context={"scope": "view"})

    def test_invalid_context_rejected(self):
        with self.assertRaises(ValueError): validate_context({"scope": "invalid"})
        with self.assertRaises(ValueError): validate_context({"include_originals": "false"})
        with self.assertRaises(ValueError): build_bundle(self.store, "../invalid", {})

    def test_http_bundle_job_download_and_busy_series(self):
        service = Service(self.root / "http-bundle")
        service.configure({"collect": False})
        import_file(service.store, self.make_file())
        server = create_server(service, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:" + str(server.server_port)
        try:
            request = Request(base + "/history/api/bundle", data=b'{"scope":"all"}',
                              headers={"X-Reikjv-History": "1", "Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                self.assertEqual(202, response.status)
                job = json.load(response)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with urlopen(base + "/api/status", timeout=5) as response:
                    current = next(item for item in json.load(response)["jobs"] if item["id"] == job["id"])
                if current["status"] in ("done", "error"):
                    break
                time.sleep(.02)
            self.assertEqual("done", current["status"], current)
            with urlopen(base + "/history/" + current["result"]["download"], timeout=5) as response:
                self.assertEqual("application/zip", response.headers["Content-Type"])
                with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                    self.assertIsNone(archive.testzip())
                    manifest = json.loads(archive.read("Bravo-Viewer/manifest.json"))
                    self.assertEqual(6, sum(item["records"] for item in manifest["entries"]))
            file_id = service.store.files()[0]["id"]
            service.heavy_queries.acquire(); service.heavy_queries.acquire()
            try:
                with self.assertRaises(HTTPError) as caught:
                    urlopen(base + f"/api/series?file={file_id}&metric=armed")
                self.assertEqual(503, caught.exception.code)
                caught.exception.close()
                with urlopen(base + "/health") as response:
                    self.assertEqual("ok", json.load(response)["status"])
            finally:
                service.heavy_queries.release(); service.heavy_queries.release()
            with urlopen(base + f"/api/series?file={file_id}&metric=armed") as response:
                points = json.load(response)["points"]
                self.assertEqual((0, 1), (min(p["min"] for p in points), max(p["max"] for p in points)))
        finally:
            service.close(); server.shutdown(); server.server_close()

