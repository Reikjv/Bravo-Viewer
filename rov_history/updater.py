"""Atualização do Desktop pelas releases do GitHub, sempre com confirmação do usuário.

A versão nova é baixada, conferida (SHA-256) e instalada em modo silencioso por cima
da atual; dados, ROVs e login da nuvem ficam em %LOCALAPPDATA% e são preservados.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

from . import __version__

RELEASES = "https://api.github.com/repos/Reikjv/Bravo-Viewer/releases?per_page=20"
CHECK_SECONDS = 6 * 3600


def version_key(text):
    """0.6.1-beta.1 < 0.6.1 < 0.6.2-beta.1; uma versão final vence suas betas."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-([a-z]+)\.?(\d+))?", text or "")
    if not match:
        return None
    major, minor, patch, stage, number = match.groups()
    return (int(major), int(minor), int(patch), 0 if stage else 1, int(number or 0))


def setup_asset(release):
    return next((a for a in release.get("assets", []) if a["name"].endswith("-Setup.exe")), None)


def newest(releases, current=__version__):
    """Maior release com instalador acima da versão atual (betas incluídas)."""
    best = None
    for release in releases:
        key = version_key(release.get("tag_name"))
        if key is None or release.get("draft") or not setup_asset(release):
            continue
        if key > version_key(current) and (best is None or key > version_key(best["tag_name"])):
            best = release
    return best


def installed_app():
    """Executável instalado pelo instalador (há desinstalador ao lado); portátil e código-fonte não se atualizam sozinhos."""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable)
    return exe if any(exe.parent.glob("unins*.exe")) else None


class Updater:
    def __init__(self, directory, fetch=None):
        self.directory = Path(directory) / "atualizacoes"
        self.fetch = fetch or self._fetch
        self.state = {"current": __version__, "available": None, "url": None, "notes": None,
                      "downloading": False, "progress": 0, "error": None, "installable": installed_app() is not None,
                      "checked": None}
        self.release = None
        self.lock = threading.Lock()

    def _fetch(self, url, accept="application/vnd.github+json"):
        request = Request(url, headers={"Accept": accept, "User-Agent": "Bravo-Viewer-Desktop/" + __version__})
        return urlopen(request, timeout=30)

    def check(self):
        try:
            with self.fetch(RELEASES) as response:
                release = newest(json.load(response))
            self.release = release
            self.state.update(available=release["tag_name"].lstrip("v") if release else None,
                              url=release["html_url"] if release else None,
                              notes=(release.get("body") or "")[:600] if release else None,
                              error=None, checked=time.time())
        except Exception as error:
            # Sem internet: o programa continua normalmente e tenta mais tarde.
            self.state.update(error=None if isinstance(error, OSError) else str(error), checked=time.time())
        return self.state

    def expected_sha(self, release, asset):
        digest = asset.get("digest") or ""
        if digest.startswith("sha256:"):
            return digest.split(":", 1)[1]
        sums = next((a for a in release["assets"] if a["name"] == asset["name"] + ".sha256"), None)
        if sums:
            with self.fetch(sums["browser_download_url"], "application/octet-stream") as response:
                return response.read().decode().split()[0]
        raise ValueError("A release não informa o SHA-256 do instalador; atualização cancelada.")

    def download(self):
        release = self.release
        asset = setup_asset(release)
        expected = self.expected_sha(release, asset).lower()
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / asset["name"]
        partial = target.with_suffix(".part")
        digest = hashlib.sha256()
        with self.fetch(asset["browser_download_url"], "application/octet-stream") as response, partial.open("wb") as output:
            total = int(response.headers.get("Content-Length") or asset.get("size") or 0)
            done = 0
            while chunk := response.read(1048576):
                output.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                self.state["progress"] = done * 100 // max(total, 1)
        if digest.hexdigest() != expected:
            partial.unlink(missing_ok=True)
            raise ValueError("O instalador baixado não confere com o SHA-256 da release; atualização cancelada.")
        partial.replace(target)
        return target

    def install(self, on_exit):
        """Baixa, confere e instala; o programa fecha para o instalador substituir os arquivos e reabre."""
        app = installed_app()
        if app is None:
            raise ValueError("Esta cópia não foi instalada pelo instalador; baixe a versão nova na página da release.")
        if not self.release:
            raise ValueError("Nenhuma versão nova disponível.")
        with self.lock:
            self.state.update(downloading=True, progress=0, error=None)
            try:
                setup = self.download()
            except Exception as error:
                self.state.update(downloading=False, error=str(error))
                raise
        # Espera este processo sair, instala em silêncio e reabre o programa já atualizado.
        script = (f'ping -n 3 127.0.0.1 >nul & "{setup}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS '
                  f'& start "" "{app}"')
        subprocess.Popen(["cmd", "/c", script], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                         close_fds=True, cwd=os.environ.get("TEMP"))
        on_exit()

    def loop(self, stop):
        while not stop.is_set():
            self.check()
            stop.wait(CHECK_SECONDS)
