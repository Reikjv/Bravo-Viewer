"""Pacotes fonte/offline, arquitetura e hashes, no formato usado pelo SNav."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rov_history import __version__


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", choices=("arm64", "armv7", "source"), default="source")
    parser.add_argument("--image", type=Path)
    parser.add_argument("--output", type=Path, default=Path("artifacts"))
    parser.add_argument("--public", action="store_true", help="Pacote offline de distribuição sem documentos/capturas da inspeção")
    args = parser.parse_args()
    if (args.arch != "source") != bool(args.image):
        parser.error("Pacotes ARM exigem --image; source não usa imagem")
    root = Path(__file__).resolve().parents[1]
    files = [name for name in subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0") if name]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root).decode().strip()
    if args.public:
        if args.arch == "source":
            parser.error("Use --public somente para pacotes offline ARM")
        files = ["install.sh"]
    name = f"Bravo-Viewer-v{__version__}-{args.arch}"
    args.output.mkdir(parents=True, exist_ok=True)
    archive = args.output / (name + ".zip")
    hashes = []
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as package:
        for filename in files:
            content = (root / filename).read_bytes()
            hashes.append(f"{hashlib.sha256(content).hexdigest()}  {filename}")
            info = zipfile.ZipInfo(name + "/" + filename)
            info.external_attr = (0o100755 if filename.endswith(".sh") else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            package.writestr(info, content)
        package.writestr(name + "/architecture.txt", args.arch + "\n")
        if args.public:
            package.writestr(name + "/LEIA-ME.txt", "Bravo Viewer\nPacote offline: extrair e executar bash install.sh no BlueOS.\nPara instalação pelo gerenciador de extensões, use o catálogo em:\nhttps://raw.githubusercontent.com/Reikjv/Bravo-Viewer/main/manifest.json\n")
        package.writestr(name + "/manifest.json", json.dumps({"version": __version__, "architecture": args.arch, "commit": commit}, indent=2) + "\n")
        if args.image:
            with args.image.open("rb") as file:
                hashes.append(f"{hashlib.file_digest(file, 'sha256').hexdigest()}  image.tar.gz")
            package.write(args.image, name + "/image.tar.gz", compress_type=zipfile.ZIP_STORED)
        package.writestr(name + "/SHA256SUMS", "\n".join(hashes) + "\n")
    with archive.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(archive)


if __name__ == "__main__": main()
