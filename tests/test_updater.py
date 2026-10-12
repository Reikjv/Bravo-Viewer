import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from rov_history import updater
from rov_history.updater import Updater, newest, version_key


class Response(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def release(tag, setup=True, digest=None, draft=False):
    assets = [{"name": f"Bravo-Viewer-Desktop-v{tag}-Setup.exe", "browser_download_url": f"https://x/{tag}.exe", "size": 4, "digest": digest}] if setup else []
    return {"tag_name": "v" + tag, "draft": draft, "html_url": f"https://github.com/r/{tag}", "body": "notas", "assets": assets}


class UpdaterTests(unittest.TestCase):
    def test_versions_order_betas_before_final(self):
        ordered = ["0.5.0-beta.1", "0.6.0-beta.1", "0.6.1-beta.1", "0.6.1-beta.2", "0.6.1", "0.10.0-beta.1"]
        self.assertEqual(ordered, sorted(ordered, key=version_key))
        self.assertIsNone(version_key("nightly"))

    def test_newest_includes_prereleases_and_needs_installer(self):
        releases = [release("0.6.0-beta.1"), release("0.7.0-beta.1", setup=False), release("0.6.2-beta.1"), release("0.9.0", draft=True)]
        self.assertEqual("v0.6.2-beta.1", newest(releases, "0.6.1-beta.1")["tag_name"])
        self.assertIsNone(newest(releases, "0.6.2-beta.1"))

    def test_download_is_checked_against_sha256(self):
        payload = b"setup"
        good = "sha256:" + hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            update = Updater(directory, fetch=lambda url, accept=None: Response(payload))
            update.release = release("0.7.0-beta.1", digest=good)
            target = update.download()
            self.assertEqual(payload, target.read_bytes())
            update.release = release("0.7.0-beta.1", digest="sha256:" + "0" * 64)
            with self.assertRaises(ValueError):
                update.download()
            self.assertFalse(target.with_suffix(".part").exists())

    def test_checksum_file_is_used_without_api_digest(self):
        payload = b"setup"
        rel = release("0.7.0-beta.1")
        rel["assets"].append({"name": rel["assets"][0]["name"] + ".sha256", "browser_download_url": "https://x/sum"})
        answers = {"https://x/sum": f"{hashlib.sha256(payload).hexdigest()}  setup.exe".encode()}
        with tempfile.TemporaryDirectory() as directory:
            update = Updater(directory, fetch=lambda url, accept=None: Response(answers.get(url, payload)))
            update.release = rel
            self.assertEqual(payload, update.download().read_bytes())

    def test_check_survives_offline_and_portable_copy_does_not_install(self):
        def offline(url, accept=None):
            raise OSError("sem internet")
        with tempfile.TemporaryDirectory() as directory:
            update = Updater(directory, fetch=offline)
            self.assertIsNone(update.check()["error"])
            update.release = release("9.0.0")
            with self.assertRaises(ValueError):
                update.install(lambda: None)

    def test_check_reads_release_list(self):
        listing = json.dumps([release("99.0.0-beta.1"), release("0.1.0")]).encode()
        with tempfile.TemporaryDirectory() as directory:
            state = Updater(directory, fetch=lambda url, accept=None: Response(listing)).check()
        self.assertEqual("99.0.0-beta.1", state["available"])
