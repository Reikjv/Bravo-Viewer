# Build: python -m PyInstaller --noconfirm desktop/BravoViewerDesktop.spec
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent

analysis = Analysis(
    [str(root / "desktop" / "bravo_desktop.py")],
    pathex=[str(root)],
    datas=[(str(root / "web"), "web"), (str(root / "docs" / "catalogos"), "docs/catalogos")],
    # O pymavlink carrega o dialeto por nome em tempo de execução.
    hiddenimports=collect_submodules("pymavlink.dialects"),
    excludes=["tkinter"],
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="BravoViewerDesktop",
          console=False, icon=str(root / "catalog" / "bravo.ico"))
COLLECT(exe, analysis.binaries, analysis.datas, name="BravoViewerDesktop")
