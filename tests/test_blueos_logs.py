from pathlib import Path
import shutil

from rov_history import store as store_module
from rov_history.ingest import import_file
from rov_history.store import blueos_path
from tests.test_history import HistoryFixture


class BlueOSLogsTests(HistoryFixture):
    def setUp(self):
        super().setUp()
        self.mount = self.root / "blueos"
        (self.mount / "ardupilot_logs" / "logs").mkdir(parents=True)
        (self.mount / "system_logs").mkdir()
        previous, store_module.BLUEOS_LOGS = store_module.BLUEOS_LOGS, self.mount
        self.addCleanup(setattr, store_module, "BLUEOS_LOGS", previous)

    def test_file_browser_paths_map_to_read_only_mount(self):
        self.assertEqual((self.mount / "ardupilot_logs/logs/00040.tlog").resolve(), blueos_path("/ardupilot_logs/logs/00040.tlog"))
        self.assertEqual((self.mount / "system_logs/ping/a.log").resolve(), blueos_path("system_logs/ping/a.log"))
        self.assertIsNone(blueos_path("/userdata/outra.tlog"))
        # Caminhos que tentam sair da pasta montada são recusados.
        self.assertIsNone(blueos_path("/ardupilot_logs/../../etc/passwd"))

    def test_mounted_log_is_indexed_in_place_without_copy(self):
        log = self.mount / "ardupilot_logs/logs/00040.tlog"
        shutil.copyfile(self.make_file(), log)
        result = import_file(self.store, log, "blueos:host:/ardupilot_logs/logs/00040.tlog", reference=True)
        self.assertEqual(log.resolve(), Path(self.store.file(result["id"])["path"]).resolve())
        self.assertFalse((self.store.directory / "originals").exists())
        self.assertTrue(self.store.readable_original(log))
        stream = next(s for s in self.store.streams() if s["type"] == "STATUSTEXT")
        self.assertEqual("=unsafe-text", self.store.records(stream["id"])["rows"][0]["data"]["text"])

    def test_old_copies_are_replaced_by_blueos_log_and_orphans_removed(self):
        source = self.make_file()
        copy = import_file(self.store, source, "blueos:host:/ardupilot_logs/logs/00040.tlog")
        originals = self.store.directory / "originals"
        orphan = originals / ("f" * 64 + ".tlog")
        orphan.write_bytes(b"versao antiga de um log em gravacao")
        shutil.copyfile(source, self.mount / "ardupilot_logs/logs/00040.tlog")
        self.assertEqual({"adopted": 1, "removed": 2}, self.store.adopt_blueos_logs())
        self.assertEqual(str(blueos_path("/ardupilot_logs/logs/00040.tlog")), self.store.file(copy["id"])["path"])
        self.assertEqual([], list(originals.iterdir()))

    def test_released_logs_never_touch_blueos_folder(self):
        log = self.mount / "ardupilot_logs/logs/00040.tlog"
        shutil.copyfile(self.make_file(), log)
        result = import_file(self.store, log, "blueos:host:/ardupilot_logs/logs/00040.tlog", reference=True)
        self.store.acknowledge_copy(result["id"], self.store.file(result["id"])["sha256"])
        self.store.close_operation()
        self.assertEqual(0, self.store.release_originals())
        self.assertTrue(log.is_file())
        self.assertEqual(log.resolve(), Path(self.store.file(result["id"])["path"]).resolve())
