"""BlueOS HTTP: consultas e autenticação, sem endpoint de comando MAVLink."""
import json
import re
from urllib.parse import quote
from urllib.request import Request, urlopen


def validate_host(host):
    if not isinstance(host, str) or not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", host):
        raise ValueError("Informe somente o IP ou nome do BlueOS, sem http:// ou porta")
    return host


class BlueOS:
    def __init__(self, host):
        self.host = validate_host(host)
        self.base = "http://" + host
        self.token = None

    def read(self, endpoint):
        with urlopen(self.base + endpoint, timeout=5) as response:
            return json.load(response)

    def folder(self, path):
        if self.token is None:
            request = Request(self.base + "/file-browser/api/login", data=b'{"username":"","password":"","recaptcha":""}', headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                self.token = response.read().decode().strip().strip('"')
        request = Request(self.base + "/file-browser/api/resources" + quote(path, safe="/"), headers={"X-Auth": self.token})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def download(self, path, destination):
        if self.token is None:
            self.folder("/")
        request = Request(self.base + "/file-browser/api/raw" + quote(path, safe="/"), headers={"X-Auth": self.token})
        with urlopen(request, timeout=30) as response, destination.open("wb") as file:
            while chunk := response.read(1024 * 1024):
                file.write(chunk)

    def inventory(self):
        pending = ["/ardupilot_logs/logs/", "/ardupilot_logs/firmware/logs/", "/system_logs/"]
        result = {"files": [], "errors": []}
        visited = set()
        while pending:
            path = pending.pop(0)
            if path in visited:
                continue
            visited.add(path)
            try:
                folder = self.folder(path)
                for item in folder.get("items", []):
                    if item.get("isDir"):
                        pending.append(item["path"].rstrip("/") + "/")
                    else:
                        result["files"].append({k: item.get(k) for k in ("name", "path", "size", "modified", "extension")})
            except Exception as error:
                result["errors"].append({"path": path, "error": str(error)})
        return result
