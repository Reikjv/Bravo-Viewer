"""Testes locais sem GitHub: abre o Bravo Viewer com dados de bancada.

    python tools/dev.py            extensão do ROV no navegador (http://127.0.0.1:8090)
    python tools/dev.py desktop    programa do PC, em janela própria, direto do código
    python tools/dev.py instalador gera o instalador do Windows em artifacts/desktop (não publica)

As alterações em web/ aparecem ao recarregar a página (F5); mudanças em Python
pedem reiniciar o comando. Nada é enviado ao GitHub.
"""
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "data" / "preview"
WORK = Path(tempfile.gettempdir()) / "bravo-dev"


def bench_copy():
    """Cópia descartável dos dados de bancada; o original em data/ nunca é alterado."""
    target = WORK / "rov"
    if (target / "history.sqlite3").exists():
        return target
    if not (BENCH / "history.sqlite3").exists():
        print("Sem dados de bancada em data/preview; usando o histórico de exemplo dos testes.")
        return None
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    shutil.copy2(BENCH / "history.sqlite3", target)
    if (BENCH / "originals").is_dir():
        shutil.copytree(BENCH / "originals", target / "originals")
    db = sqlite3.connect(target / "history.sqlite3")
    for file_id, path in db.execute("SELECT id,path FROM files WHERE path!=''").fetchall():
        db.execute("UPDATE files SET path=? WHERE id=?", (str(target / "originals" / Path(path).name), file_id))
    db.commit()
    db.close()
    return target


def rov():
    data = bench_copy()
    if data is None:
        command = [sys.executable, str(ROOT / "tools" / "preview_fixture.py"), "--port", "8090"]
    else:
        command = [sys.executable, "-m", "rov_history.app", "--host", "127.0.0.1", "--port", "8090", "--data-dir", str(data)]
    print("Extensão do ROV em http://127.0.0.1:8090/  (Ctrl+C para parar)")
    process = subprocess.Popen(command, cwd=ROOT)
    time.sleep(2)
    webbrowser.open("http://127.0.0.1:8090/")
    try:
        process.wait()
    except KeyboardInterrupt:
        process.terminate()


def desktop():
    # Pasta própria: não mistura com o Bravo Viewer instalado neste PC.
    print("Programa do PC direto do código, com dados em", WORK / "desktop")
    subprocess.call([sys.executable, "-m", "rov_history.desktop", "--data-dir", str(WORK / "desktop")], cwd=ROOT)


def installer():
    version = subprocess.check_output([sys.executable, "-c", "from rov_history import __version__; print(__version__)"], cwd=ROOT, text=True).strip()
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", "dist", "--workpath", "build",
                           "desktop/BravoViewerDesktop.spec"], cwd=ROOT)
    iscc = Path(os.environ.get("ProgramFiles(x86)", "")) / "Inno Setup 6" / "ISCC.exe"
    if not iscc.exists():
        print(f"Executável pronto em {ROOT / 'dist' / 'BravoViewerDesktop'}.")
        print("Para gerar também o instalador, instale o Inno Setup uma vez: winget install JRSoftware.InnoSetup")
        return
    numeric = version.split("-")[0] + ".0"
    subprocess.check_call([str(iscc), f"/DAppVersion={version}-local", f"/DAppNumeric={numeric}", "desktop/installer.iss"], cwd=ROOT)
    print("Instalador de teste em", ROOT / "artifacts" / "desktop")


if __name__ == "__main__":
    {"desktop": desktop, "instalador": installer}.get(sys.argv[1] if len(sys.argv) > 1 else "", rov)()
