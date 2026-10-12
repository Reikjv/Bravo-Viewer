"""Bravo Viewer Desktop: cópia local de vários ROVs, sincronizada pela rede.

O PC baixa os logs originais e os registros novos da coleta ao vivo e faz o
processamento pesado localmente. A Raspberry só entrega arquivos.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
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
from urllib.request import Request, urlopen
import webbrowser

from . import __version__
from .app import create_server
from .blueos import validate_host
from .cloud import CloudError, CloudSync
from .ingest import import_file
from .model import dumps
from .service import Service

APP_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "BravoViewerDesktop"
SYNC_SECONDS = 60
SCAN_SECONDS = 120
ROV_PORT = 8090


class RovClient:
    def __init__(self, host):
        self.base = f"http://{validate_host(host)}:{ROV_PORT}/api/"

    def get(self, path, timeout=15):
        with urlopen(self.base + path, timeout=timeout) as response:
            return json.load(response)

    def manifest(self):
        return self.get("sync/manifest")

    def meta(self, file_id):
        return self.get(f"sync/meta?file={file_id}", timeout=60)

    def original(self, file_id, partial, progress=lambda done: None):
        """Baixa o log original continuando de um download parcial anterior."""
        start = partial.stat().st_size if partial.exists() else 0
        request = Request(f"{self.base}original?file={file_id}", headers={"Range": f"bytes={start}-"} if start else {})
        with urlopen(request, timeout=60) as response:
            mode = "ab" if response.status == 206 else "wb"
            done = start if response.status == 206 else 0
            with partial.open(mode) as output:
                while chunk := response.read(1048576):
                    output.write(chunk)
                    done += len(chunk)
                    progress(done)

    def acknowledge(self, file_id, sha):
        request = Request(self.base + "sync-ack", dumps({"file": file_id, "sha256": sha}).encode(),
                          {"Content-Type": "application/json", "X-Reikjv-History": "1"})
        with urlopen(request, timeout=15) as response:
            return json.load(response)


def local_networks():
    """Redes /24 do PC (cabo do ROV, Wi-Fi, rede da base)."""
    addresses = set()
    try:
        addresses.update(info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    for target in ("192.168.2.2", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect((target, 9))
                addresses.add(probe.getsockname()[0])
        except OSError:
            pass
    return {ip.rsplit(".", 1)[0] for ip in addresses if not ip.startswith(("127.", "169.254.", "0."))}


def discover(timeout=.4):
    """Procura Bravo Viewer na rede: porta aberta e código interno do equipamento."""
    candidates = {"192.168.2.2"} | {f"{net}.{host}" for net in local_networks() for host in range(1, 255)}

    def probe(host):
        try:
            with socket.create_connection((host, ROV_PORT), timeout=timeout):
                return host
        except OSError:
            return None

    with ThreadPoolExecutor(128) as pool:
        reachable = [host for host in pool.map(probe, sorted(candidates)) if host]
    found = []
    for host in reachable:
        try:
            manifest = RovClient(host).get("sync/manifest", timeout=5)
        except Exception:
            continue
        if manifest.get("device_id"):
            found.append({"host": host, "device": manifest["device_id"], "equipment": manifest["equipment"]})
    return found


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
        self.scan_wake = threading.Event()
        self.cloud = CloudSync(self, self.directory)
        self.scan = {"running": False, "last": None, "found": 0}

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
        """Sincroniza um ROV: o PC baixa os .tlog inteiros e calcula tudo localmente.

        Do ROV vem pronto apenas o que não tem log: coleta ao vivo e sessões cujo log
        já não existe mais no ROV. A Raspberry só entrega arquivos.
        """
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
                local = {row["source_key"]: dict(row) for row in db.execute("SELECT id,source_key,sha256,path,remote_revision,note,operation_id FROM files")}
            processed = lambda remote, mine: mine and mine["sha256"] == remote["sha256"] and mine["path"] and Path(mine["path"]).is_file()
            # 1) Sem log para baixar: resumo pronto do ROV (rápido).
            summaries = 0
            for remote in manifest["files"]:
                mine = local.get(remote["source_key"])
                if remote["kind"] != "live" and remote["has_original"] or processed(remote, mine):
                    continue
                if mine and mine["remote_revision"] == remote["imported_at"] and mine["sha256"] == remote["sha256"]:
                    continue
                state["message"] = f"Atualizando resumos: {remote['name']}"
                store.apply_meta(remote, client.meta(remote["id"]))
                summaries += 1
            # Observações e operação podem mudar no ROV sem mudar o log.
            for remote in manifest["files"]:
                mine = local.get(remote["source_key"])
                if mine and processed(remote, mine) and (mine["note"] != remote["note"] or mine["operation_id"] != remote["operation_id"]):
                    with store.write_lock, store.db() as db:
                        db.execute("UPDATE files SET note=?,operation_id=? WHERE id=?", (remote["note"], remote["operation_id"], mine["id"]))
            with self.lock:
                device.update(equipment=manifest["equipment"], last_sync=time.time())
                self.save()
            state.update(online=True, error=None)
            # 2) Logs completos: baixa (retomando cópias interrompidas) e processa no PC.
            pending = [f for f in manifest["files"] if f["kind"] != "live" and f["has_original"] and f["sha256"]
                       and not processed(f, local.get(f["source_key"]))]
            total = sum(f["size"] or 0 for f in pending)
            done = 0
            downloads = self.directory / "downloads"
            downloads.mkdir(exist_ok=True)
            originals = store.directory / "originals"
            originals.mkdir(exist_ok=True)
            for index, remote in enumerate(pending):
                if self.stop.is_set():
                    return
                partial = downloads / f"{remote['sha256']}.part"
                base = done
                def report(position, name=remote["name"], number=index + 1):
                    state["message"] = f"Baixando logs {number}/{len(pending)} · {(base + position) * 100 // max(total, 1)}% · {name}"
                client.original(remote["id"], partial, report)
                with partial.open("rb") as file:
                    if hashlib.file_digest(file, "sha256").hexdigest() != remote["sha256"]:
                        partial.unlink()
                        raise ValueError(f"Cópia de {remote['name']} corrompida; será baixada de novo.")
                target = originals / (remote["sha256"] + Path(remote["name"]).suffix.lower())
                partial.replace(target)
                state["message"] = f"Processando no PC {index + 1}/{len(pending)} · {remote['name']}"
                result = import_file(store, target, remote["source_key"], display_name=remote["name"], operation_id=remote["operation_id"], reference=True)
                with store.write_lock, store.db() as db:
                    db.execute("UPDATE files SET path=?,note=?,remote_revision=? WHERE id=?", (str(target.resolve()), remote["note"], remote["imported_at"], result["id"]))
                client.acknowledge(remote["id"], remote["sha256"])
                done += remote["size"] or 0
            parts = []
            if pending:
                parts.append(f"{len(pending)} {'log processado' if len(pending) == 1 else 'logs processados'} no PC")
            if summaries:
                parts.append(f"{summaries} {'resumo de coleta atualizado' if summaries == 1 else 'resumos de coleta atualizados'}")
            state.update(message="Sincronizado · " + (", ".join(parts) or "sem novidades"))
            if pending or summaries:
                self.cloud.wake.set()
        except Exception as error:
            state.update(online=False, error=str(error), message="ROV fora da rede" if isinstance(error, OSError) else str(error))
        finally:
            state["running"] = False

    def discover(self):
        """Cadastra ou atualiza o endereço dos ROVs encontrados; o código interno identifica cada um."""
        self.scan["running"] = True
        try:
            found = discover()
            # O mesmo ROV pode responder pelo cabo e pela rede da base: um equipamento, um endereço.
            hosts = {}
            for item in found:
                hosts.setdefault(item["device"], []).append(item)
            with self.lock:
                for device, items in hosts.items():
                    known = self.devices.setdefault(device, {})
                    current = next((i for i in items if i["host"] == known.get("host")), None)
                    item = current or min(items, key=lambda i: (not i["host"].startswith("192.168.2."), i["host"]))
                    known.update(host=item["host"], equipment=item["equipment"], addresses=sorted(i["host"] for i in items))
                    if self.active is None:
                        self.active = device
                if found:
                    self.save()
            self.scan.update(last=time.time(), found=len(hosts))
            if found:
                self.wake.set()
            return found
        finally:
            self.scan["running"] = False

    def scan_loop(self):
        while not self.stop.is_set():
            try:
                self.discover()
            except Exception:
                logging.exception("Falha ao procurar ROVs")
            self.scan_wake.wait(SCAN_SECONDS)
            self.scan_wake.clear()

    def loop(self):
        while not self.stop.is_set():
            for device_id in list(self.devices):
                if self.stop.is_set():
                    break
                self.sync(device_id)
            self.wake.wait(SYNC_SECONDS)
            self.wake.clear()

    def overview(self):
        """Cartões da frota: status de rede e operação atual de cada ROV, sem trocar o equipamento ativo."""
        cards = []
        for device_id, device in list(self.devices.items()):
            store = self.service(device_id).store
            operations = store.operations()
            current = next((op for op in operations if op["status"] == "open"), None)
            closed = [op for op in operations if op["status"] == "closed"]
            with store.db() as db:
                sessions, pending = db.execute("SELECT count(*), sum(kind!='live' AND path='') FROM files").fetchone()
            cards.append({"id": device_id, **device, **self.state.get(device_id, {}),
                          "current": current and {k: current[k] for k in ("name", "location", "client", "started_at", "summary")},
                          "closed": len(closed), "sessions": sessions, "pending_logs": pending or 0,
                          "last_closed": closed[0]["name"] if closed else None})
        return {"devices": sorted(cards, key=lambda d: d.get("equipment", "")), "scan": self.scan, "active": self.active}

    def status(self):
        devices = [{"id": device_id, **device, **self.state.get(device_id, {})} for device_id, device in self.devices.items()]
        return {"mode": "desktop", "active_device": self.active, "scan": self.scan, "cloud": self.cloud.public(), "devices": sorted(devices, key=lambda d: d.get("equipment", ""))}


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
        if path == "fleet":
            return self.fleet.overview()
        if path == "cloud":
            return self.fleet.cloud.public()
        raise ValueError("Rota não encontrada")

    def post(self, action, body):
        if action == "connect":
            host = str(body.get("host", "")).strip()
            return {"device": self.fleet.connect(host)}
        if action == "select":
            self.fleet.select(str(body.get("device", "")))
            return {"device": self.fleet.active}
        if action == "sync":
            device = str(body.get("device", ""))
            if device in self.fleet.devices:
                # Sincroniza um ROV específico sem trocar o equipamento aberto na tela.
                threading.Thread(target=self.fleet.sync, args=(device,), daemon=True).start()
            else:
                self.fleet.wake.set()
            return {"ok": True}
        if action == "scan":
            self.fleet.scan_wake.set()
            return {"ok": True}
        cloud = self.fleet.cloud
        if action == "cloud-login":
            return cloud.login(str(body.get("url", "")).strip(), str(body.get("key", "")).strip(), str(body.get("email", "")).strip(), str(body.get("password", "")), bool(body.get("enabled", True)))
        if action == "cloud-logout":
            cloud.logout()
            return cloud.public()
        if action == "cloud-sync":
            cloud.config["enabled"] = True
            cloud.save()
            cloud.wake.set()
            return cloud.public()
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
    threading.Thread(target=fleet.scan_loop, daemon=True).start()
    threading.Thread(target=fleet.cloud.loop, args=(fleet.stop,), daemon=True).start()
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
        fleet.stop.set(); fleet.wake.set(); fleet.cloud.wake.set(); fleet.scan_wake.set()
        server.shutdown()
        for service in fleet.services.values():
            service.close()
        lock_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
