"""Servidor da extensão e rotas de consulta/exportação."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import mimetypes
import re
from pathlib import Path
import tempfile
import threading
import time
import unicodedata
from urllib.parse import parse_qs, urlparse

from . import __version__
from .model import dumps
from .capabilities import capabilities
from .service import Service

WEB = Path(__file__).resolve().parents[1] / "web"


class LimitedHTTPServer(ThreadingHTTPServer):
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(2)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def slug(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:60] or "Bravo"


def bundle_name(store, identifier):
    """Nome do ZIP com equipamento, operação e data: Bravo-10_Inspecao-Casco_2026-10-11.zip."""
    equipment = slug(store.config()["equipment"])
    zip_path = store.directory / "exports" / (identifier + ".zip")
    day = time.strftime("%Y-%m-%d", time.localtime(zip_path.stat().st_mtime))
    with store.db() as db:
        op = db.execute("SELECT name FROM operations WHERE bundle_id=?", (identifier,)).fetchone()
    return f"{equipment}_{slug(op['name']) if op else 'Historico'}_{day}.zip"


def create_server(service, host="127.0.0.1", port=8090, extension=None):
    """extension: rotas adicionais do Desktop (get/post/status); None no ROV."""
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(30)
            super().setup()
        def log_message(self, *args):
            logging.getLogger("http").debug(*args)

        def response(self, value, status=200):
            body = dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def download(self, chunks, name, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Connection", "close")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.close_connection = True
            for chunk in chunks:
                self.wfile.write(chunk)

        def send_file(self, filepath, name):
            """Download com retomada (Range): uma cópia interrompida continua de onde parou."""
            size = filepath.stat().st_size
            match = re.fullmatch(r"bytes=(\d+)-", self.headers.get("Range", ""))
            start = int(match[1]) if match and int(match[1]) < size else 0
            self.send_response(206 if start else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size - start))
            if start:
                self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
            self.end_headers()
            with filepath.open("rb") as source:
                source.seek(start)
                while chunk := source.read(1048576):
                    self.wfile.write(chunk)

        def routes(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            # Prefixo de proxy BlueOS funciona para HTML, API e downloads.
            if "/api/" in path:
                path = "/api/" + path.split("/api/", 1)[1]
            query = parse_qs(parsed.query)
            def integer(name, default=0):
                value = int(query.get(name, [default])[0])
                if value < 0:
                    raise ValueError("Filtro numérico inválido")
                return value
            if path.endswith("/register_service"):
                return self.response({"name": "Bravo Viewer", "description": "Histórico, energia, operações e exportação do ROV", "icon": "mdi-chart-timeline-variant", "company": "BRS", "version": __version__, "webpage": "", "api": "", "works_in_relative_paths": True, "new_page": False})
            if path.endswith("/health"):
                return self.response({"status": "ok", "version": __version__})
            if path == "/api/status":
                return self.response({**service.status(), "version": __version__, **(extension.status() if extension else {"mode": "rov"})})
            if path == "/api/overview":
                return self.response(service.store.overview())
            if path == "/api/operations":
                return self.response({"operations": service.store.operations(), "limit": service.store.operation_limit()})
            if path == "/api/sync/manifest":
                return self.response(service.store.sync_manifest())
            if path == "/api/sync/meta":
                return self.response(service.store.file_meta(integer("file")))
            if path == "/api/files":
                return self.response(service.store.files())
            if path == "/api/streams":
                return self.response(service.store.streams(integer("file") or None))
            if path == "/api/capabilities":
                return self.response(capabilities(service.store))
            if path == "/api/inventory":
                inventory = service.store.directory / "remote_inventory.json"
                return self.response(json.loads(inventory.read_text(encoding="utf-8")) if inventory.exists() else {"files": [], "errors": [], "captured_at": None, "host": None})
            if path == "/api/records":
                return self.response(service.store.records(integer("stream"), integer("offset"), integer("limit", 100)))
            if path == "/api/metrics":
                return self.response(service.store.metrics(integer("file")))
            if path == "/api/series":
                if not service.heavy_queries.acquire(blocking=False):
                    return self.response({"error": "Consultas em andamento; tente novamente em alguns segundos"}, 503)
                try:
                    return self.response(service.store.points(integer("file"), query.get("metric", [""])[0]))
                finally:
                    service.heavy_queries.release()
            if path == "/api/events":
                return self.response(service.store.events(integer("file") or None, integer("offset"), integer("limit", 100), query.get("search", [""])[0]))
            if path == "/api/export":
                stream_id = integer("stream")
                stream = service.store.stream(stream_id)
                format = query.get("format", ["csv"])[0]
                if format not in ("csv", "jsonl"):
                    raise ValueError("Formato de exportação inválido")
                return self.download(service.store.export(stream_id, format), f"bravo-viewer-tipo-{stream_id}.{format}", "text/csv; charset=utf-8" if format == "csv" else "application/x-ndjson")
            if path == "/api/export-summary":
                return self.download([(dumps(service.store.overview()) + "\n").encode()], "bravo-viewer-resumo.json", "application/json")
            if path == "/api/bundle":
                identifier = query.get("id", [""])[0]
                if not re.fullmatch(r"[a-f0-9]{32}", identifier):
                    raise ValueError("Exportação inválida")
                filepath = service.store.directory / "exports" / (identifier + ".zip")
                if not filepath.is_file():
                    raise ValueError("Exportação indisponível")
                def chunks():
                    with filepath.open("rb") as source:
                        while chunk := source.read(65536):
                            yield chunk
                    service.store.mark_bundle_downloaded(identifier)
                return self.download(chunks(), bundle_name(service.store, identifier), "application/zip")
            if path == "/api/original":
                file = service.store.file(integer("file"))
                filepath = Path(file["path"]).resolve() if file["path"] else None
                if filepath is None or not service.store.readable_original(filepath):
                    raise ValueError("Original indisponível para coleta ao vivo; exporte os tipos")
                return self.send_file(filepath, f"bravo-viewer-original-{file['id']}{filepath.suffix}")
            if extension and path.startswith("/api/desktop/"):
                return self.response(extension.get(path[len("/api/desktop/"):], query))
            if path.startswith("/api/"):
                return self.response({"error": "Rota não encontrada"}, 404)
            name = path.rsplit("/", 1)[-1]
            if name in ("", "index.html") or parsed.path.endswith("/"):
                name = "index.html"
            if name not in ("index.html", "app.js", "style.css", "brand.png", "brs.png", "favicon.png"):
                return self.response({"error": "Arquivo não encontrado"}, 404)
            data = (WEB / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", (mimetypes.guess_type(name)[0] or "application/octet-stream") + ("" if name.endswith(".png") else "; charset=utf-8"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            try:
                self.routes()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (ValueError, KeyError, OSError) as error:
                self.response({"error": str(error)}, 400)
            except Exception:
                logging.exception("Erro na consulta")
                self.response({"error": "Falha na consulta; verifique os logs da extensão"}, 500)

        def do_POST(self):
            if self.headers.get("X-Reikjv-History") != "1":
                return self.response({"error": "Requisição inválida"}, 403)
            upload_path = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                action = urlparse(self.path).path.rsplit("/", 1)[-1]
                if action == "upload":
                    if not 0 < length <= 2 * 1024**3:
                        raise ValueError("Arquivo vazio ou maior que 2 GB")
                    filename = parse_qs(urlparse(self.path).query).get("name", [""])[0]
                    suffix = Path(filename).suffix.lower()
                    if suffix not in (".bin", ".tlog", ".log", ".gz"):
                        raise ValueError("Formato não suportado")
                    inbox = service.store.directory / "inbox"
                    inbox.mkdir(exist_ok=True)
                    with tempfile.NamedTemporaryFile(dir=inbox, suffix=suffix, delete=False) as target:
                        upload_path = Path(target.name)
                        remaining = length
                        while remaining:
                            chunk = self.rfile.read(min(1048576, remaining))
                            if not chunk:
                                raise ValueError("Upload interrompido")
                            target.write(chunk)
                            remaining -= len(chunk)
                    job = service.submit("import", path=target.name, temporary=True, display_name=Path(filename.replace("\\", "/")).name)
                    upload_path = None
                    return self.response(job, 202)
                if not 0 < length <= 16384:
                    raise ValueError("Corpo da requisição inválido")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("JSON inválido")
                if action == "config":
                    return self.response(service.configure(body))
                if action == "sync":
                    return self.response(service.submit("sync", host=service.store.config()["host"]), 202)
                if action == "bundle":
                    from .exporter import validate_context
                    return self.response(service.submit("bundle", context=validate_context(body)), 202)
                if action == "sync-ack":
                    return self.response({"released": service.store.acknowledge_copy(int(body["file"]), str(body["sha256"]))})
                if action == "operation":
                    return self.response(service.store.update_operation(body))
                if action == "operation-close":
                    values = body.get("values") or {}
                    if not isinstance(values, dict) or not isinstance(body.get("include_data", False), bool):
                        raise ValueError("Dados da operação inválidos")
                    return self.response(service.submit("close_operation", values=values, include_data=body.get("include_data", False)), 202)
                if action == "operation-export":
                    from .exporter import validate_context
                    context = validate_context({"scope": "operation", "operation": body.get("id"), "include_data": body.get("include_data", False), "include_originals": True})
                    return self.response(service.submit("bundle", context=context), 202)
                if action == "note":
                    service.store.note(int(body["file"]), str(body["note"]))
                    return self.response({"ok": True})
                if extension and action.startswith("desktop-"):
                    return self.response(extension.post(action[len("desktop-"):], body))
                return self.response({"error": "Ação não encontrada"}, 404)
            except (ValueError, KeyError, TypeError, OSError) as error:
                self.response({"error": str(error)}, 400)
            finally:
                if upload_path is not None:
                    upload_path.unlink(missing_ok=True)

    return LimitedHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Bravo Viewer")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--data-dir", default="data/app")
    parser.add_argument("--blueos-host")
    parser.add_argument("--collect", action="store_true", help="Habilita coleta persistente na primeira instalação")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    service = Service(args.data_dir)
    config_path = service.store.directory / "configured"
    if not config_path.exists():
        service.configure({"host": args.blueos_host or "127.0.0.1", "collect": args.collect})
        config_path.touch()
    server = create_server(service, args.host, args.port)
    logging.info("Bravo Viewer %s: http://%s:%s", __version__, args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        server.server_close()


if __name__ == "__main__":
    main()
