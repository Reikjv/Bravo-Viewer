"""Bravo Viewer Desktop: cópia local de vários ROVs, sincronizada pela rede.

O PC baixa os logs originais e os registros novos da coleta ao vivo e faz o
processamento pesado localmente. A Raspberry só entrega arquivos.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from urllib.request import urlopen
import webbrowser

from . import __version__
from .app import create_server
from .blueos import validate_host
from .ingest import append_records, import_file
from .model import dumps
from .service import Service

APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BravoViewerDesktop"
SYNC_SECONDS = 60
ROV_PORT = 8090


class RovClient:
    def __init__(self, host):
        self.base = f"http://{validate_host(host)}:{ROV_PORT}/api/"

    def manifest(self):
        with urlopen(self.base + "sync/manifest", timeout=15) as response:
            return json.load(response)

    def original(self, file_id, destination):
        with urlopen(f"{self.base}original?file={file_id}", timeout=60) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output, 1048576)

    def records(self, file_id, after):
        # Em partes: cada resposta tem no máximo 20.000 registros para não pesar no ROV.
        while True:
            with urlopen(f"{self.base}sync/records?file={file_id}&after={after}&limit=20000", timeout=120) as response:
                lines = [line for line in response.read().splitlines() if line]
            if not lines:
                return
            yield lines
            after = json.loads(lines[-1])["seq"]
            if len(lines) < 20000:
                return


class Fleet:
    """ROVs conhecidos pelo PC; cada um tem um banco local separado."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.registry = self.directory / "equipamentos.json"
        self.devices = json.loads(self.registry.read_text(encoding="utf-8")) if self.registry.exists() else {}
        self.services = {}
        self.lock = threading.RLock()
        self.sync_locks = {}
        self.state = {}
        self.active = next(iter(self.devices), None)
        self.stop = threading.Event()
        self.wake = threading.Event()

    def save(self):
        temporary = self.registry.with_suffix(".tmp")
        temporary.write_text(dumps(self.devices), encoding="utf-8")
        temporary.replace(self.registry)

    def service(self, device_id=None):
        device_id = device_id or self.active or "sem-equipamento"
        with self.lock:
            if device_id not in self.services:
                self.services[device_id] = Service(self.directory / "equipamentos" / device_id, mirror=True)
            return self.services[device_id]

    def connect(self, host):
        """Registra o ROV pelo código interno informado por ele, não pelo endereço."""
        manifest = RovClient(host).manifest()
        device_id = manifest.get("device_id")
        if not device_id:
            raise ValueError("Este Bravo Viewer é anterior à 0.4.0; atualize a extensão no ROV para sincronizar.")
        with self.lock:
            self.devices[device_id] = {**self.devices.get(device_id, {}), "host": host, "equipment": manifest["equipment"]}
            self.active = device_id
            self.save()
        self.wake.set()
        return device_id

    def select(self, device_id):
        if device_id not in self.devices:
            raise ValueError("Equipamento não encontrado")
        self.active = device_id

    def sync(self, device_id):
        lock = self.sync_locks.setdefault(device_id, threading.Lock())
        if not lock.acquire(blocking=False):
            return
        try:
            self._sync(device_id)
        finally:
            lock.release()

    def _sync(self, device_id):
        device = self.devices[device_id]
        state = self.state.setdefault(device_id, {})
        state.update(running=True, message="Conectando ao ROV")
        try:
            client = RovClient(device["host"])
            manifest = client.manifest()
            if manifest.get("device_id") != device_id:
                raise ValueError(f"O endereço {device['host']} agora responde por outro equipamento ({manifest.get('equipment')}).")
            store = self.service(device_id).store
            store.configure({"equipment": manifest["equipment"], "device_id": device_id, "host": device["host"]})
            for op in manifest["operations"]:
                store.mirror_operation(op)
            with store.db() as db:
                local = {row["source_key"]: dict(row) for row in db.execute("SELECT f.id,f.source_key,f.sha256,f.note,(SELECT coalesce(max(seq),-1) FROM records r WHERE r.file_id=f.id) AS last_seq FROM files f")}
            changed = 0
            for index, remote in enumerate(manifest["files"]):
                if self.stop.is_set():
                    return
                state["message"] = f"Sincronizando {index + 1}/{len(manifest['files'])}: {remote['name']}"
                mine = local.get(remote["source_key"])
                if remote["kind"] == "live":
                    if mine and mine["last_seq"] >= remote["last_seq"]:
                        continue
                    after = mine["last_seq"] if mine else -1
                    added = sum(append_records(store, remote, lines) for lines in client.records(remote["id"], after))
                    changed += bool(added)
                    continue
                if mine and mine["sha256"] == remote["sha256"]:
                    if mine["note"] != remote["note"]:
                        store.note(mine["id"], remote["note"])
                    continue
                with tempfile.TemporaryDirectory(dir=self.directory) as temporary:
                    path = Path(temporary) / ("original" + Path(remote["name"]).suffix.lower())
                    client.original(remote["id"], path)
                    result = import_file(store, path, remote["source_key"], display_name=remote["name"], operation_id=remote["operation_id"])
                    if remote["note"]:
                        store.note(result["id"], remote["note"])
                changed += 1
            with self.lock:
                device.update(equipment=manifest["equipment"], last_sync=time.time())
                self.save()
            state.update(online=True, error=None, message=f"Sincronizado · {changed} sessões atualizadas" if changed else "Sincronizado · sem novidades")
        except Exception as error:
            state.update(online=False, error=str(error), message="ROV fora da rede" if isinstance(error, OSError) else str(error))
        finally:
            state["running"] = False

    def loop(self):
        while not self.stop.is_set():
            for device_id in list(self.devices):
                if self.stop.is_set():
                    break
                self.sync(device_id)
            self.wake.wait(SYNC_SECONDS)
            self.wake.clear()

    def status(self):
        devices = [{"id": device_id, **device, **self.state.get(device_id, {})} for device_id, device in self.devices.items()]
        return {"mode": "desktop", "active_device": self.active, "devices": sorted(devices, key=lambda d: d.get("equipment", ""))}


class ActiveService:
    """O servidor HTTP atende sempre o equipamento selecionado no topo da janela."""

    def __init__(self, fleet):
        self._fleet = fleet

    def __getattr__(self, name):
        return getattr(self._fleet.service(), name)


class DesktopApi:
    def __init__(self, fleet):
        self.fleet = fleet

    def status(self):
        return self.fleet.status()

    def get(self, path, query):
        if path == "status":
            return self.fleet.status()
        raise ValueError("Rota não encontrada")

    def post(self, action, body):
        if action == "connect":
            host = str(body.get("host", "")).strip()
            return {"device": self.fleet.connect(host)}
        if action == "select":
            self.fleet.select(str(body.get("device", "")))
            return {"device": self.fleet.active}
        if action == "sync":
            self.fleet.wake.set()
            return {"ok": True}
        raise ValueError("Ação não encontrada")


def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            path = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if path.exists():
                return path
    return None


def running_instance(lock_path):
    """Uma única instância sincroniza; abrir de novo apenas mostra a janela existente."""
    try:
        port = int(lock_path.read_text())
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
            return port
    except (OSError, ValueError):
        return None


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def native_window(url, profile):
    """Janela própria do Windows (WebView2); bloqueia até o usuário fechar."""
    import webview
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.create_window("Bravo Viewer", url, width=1440, height=920, min_size=(1000, 640), background_color="#071219")
    webview.start(storage_path=str(profile), private_mode=False)


def open_window(url, profile):
    """Reserva quando o WebView2 não está disponível: janela de aplicativo do Edge."""
    edge = find_edge()
    if not edge:
        webbrowser.open(url)
        return None
    # Perfil próprio: a janela é independente do navegador do usuário e mantém o processo vivo.
    return subprocess.Popen([str(edge), f"--app={url}", f"--user-data-dir={profile}", "--no-first-run", "--window-size=1440,920"])


def show(url, profile):
    try:
        native_window(url, profile)
    except Exception:
        logging.exception("Janela nativa indisponível; usando o Edge")
        window = open_window(url, profile)
        if window:
            window.wait()
        else:
            while True:
                time.sleep(3600)


def main():
    parser = argparse.ArgumentParser(description="Bravo Viewer Desktop")
    parser.add_argument("--data-dir", default=str(APP_DIR))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-window", action="store_true")
    args = parser.parse_args()
    directory = Path(args.data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=directory / "desktop.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    lock_path = directory / "instancia.txt"
    port = running_instance(lock_path)
    if port and not args.no_window:
        show(f"http://127.0.0.1:{port}/", directory / "janela")
        return
    fleet = Fleet(directory)
    port = args.port or free_port()
    server = create_server(ActiveService(fleet), "127.0.0.1", port, extension=DesktopApi(fleet))
    lock_path.write_text(str(port))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=fleet.loop, daemon=True).start()
    logging.info("Bravo Viewer Desktop %s em http://127.0.0.1:%s", __version__, port)
    try:
        if args.no_window:
            while True:
                time.sleep(3600)
        else:
            show(f"http://127.0.0.1:{port}/", directory / "janela")
    except KeyboardInterrupt:
        pass
    finally:
        fleet.stop.set(); fleet.wake.set()
        server.shutdown()
        for service in fleet.services.values():
            service.close()
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
