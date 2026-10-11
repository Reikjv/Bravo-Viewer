"""Baixa esquemas de referência; não conecta ao ROV nem lê logs reais."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "docs" / "catalogos"


def download(url):
    request = Request(url, headers={"User-Agent": "Reikjv-ROV-log-survey"})
    with urlopen(request, timeout=45) as response:
        return response.read(), response.headers.get("Last-Modified")


def save(name, data):
    DEST.mkdir(parents=True, exist_ok=True)
    (DEST / name).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main():
    fetched_at = datetime.now(timezone.utc).isoformat()
    url = "https://autotest.ardupilot.org/LogMessages/Sub/LogMessages.json"
    raw, modified = download(url)
    formats = json.loads(raw)["logformats"]
    messages = [
        {
            "name": item["name"],
            "fields": [
                {key: field[key] for key in ("name", "type", "units") if key in field}
                for field in item["fields"]
            ],
        }
        for item in formats
    ]
    save("dataflash_sub.json", {
        "source": url,
        "fetched_at_utc": fetched_at,
        "source_last_modified": modified,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "scope": "Catálogo gerado do ArduSub em desenvolvimento; não representa uma versão instalada nem um log real.",
        "observed_on_vehicle": False,
        "message_count": len(messages),
        "messages": messages,
    })
    commit_raw, _ = download("https://api.github.com/repos/mavlink/mavlink/commits/master")
    commit = json.loads(commit_raw)["sha"]
    sources = []
    all_messages = []
    # A herança é percorrida para incluir também minimal/standard e os dialetos
    # de fabricantes importados por ardupilotmega, sem depender de pymavlink.
    pending = ["ardupilotmega.xml"]
    visited = set()
    while pending:
        filename = pending.pop(0)
        if filename in visited:
            continue
        visited.add(filename)
        url = f"https://raw.githubusercontent.com/mavlink/mavlink/{commit}/message_definitions/v1.0/{filename}"
        raw, _ = download(url)
        root = ET.fromstring(raw)
        pending.extend(node.text.strip() for node in root.findall("include"))
        sources.append({"url": url, "sha256": hashlib.sha256(raw).hexdigest()})
        for message in root.findall("./messages/message"):
            fields = []
            extension = False
            for field in message:
                if field.tag == "extensions":
                    extension = True
                if field.tag != "field":
                    continue
                fields.append({
                    **{key: field.attrib[key] for key in ("name", "type", "units", "enum", "invalid", "multiplier") if key in field.attrib},
                    "extension": extension,
                })
            all_messages.append({
                "id": int(message.attrib["id"]),
                "name": message.attrib["name"],
                "defined_in": filename,
                "fields": fields,
            })
    all_messages.sort(key=lambda item: (item["id"], item["name"]))
    ids = [item["id"] for item in all_messages]
    if len(ids) != len(set(ids)):
        raise ValueError("IDs MAVLink duplicados no catálogo herdado")
    save("mavlink_ardupilotmega.json", {
        "sources": sources,
        "source_commit": commit,
        "fetched_at_utc": fetched_at,
        "scope": "Definições MAVLink ardupilotmega e sua herança; não garante emissão pelo ArduSub nem registro em arquivo.",
        "observed_on_vehicle": False,
        "message_count": len(all_messages),
        "messages": all_messages,
    })
    print(f"DataFlash: {len(messages)} tipos; MAVLink: {len(all_messages)} tipos; destino: {DEST}")


if __name__ == "__main__":
    main()
