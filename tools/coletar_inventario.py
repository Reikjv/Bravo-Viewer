"""Inspeção BlueOS por HTTP e análise offline dos tlogs; sem comandos MAVLink."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def read(url, token=None, data=None):
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if token:
        headers["X-Auth"] = token
    request = Request(url, data=data, headers=headers)
    with urlopen(request, timeout=30) as response:
        return response.read()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def public_report(report):
    """Mantém processos, argumentos e rede apenas na captura local ignorada."""
    result = {k: v for k, v in report.items() if k != "snapshots"}
    snapshots = report["snapshots"]
    result["snapshots"] = {k: v for k, v in snapshots.items() if k not in ("system", "mavlink")}
    system = snapshots.get("system", {})
    result["snapshots"]["system"] = {k: system[k] for k in ("cpu", "temperature", "memory", "info", "unix_time_seconds") if k in system}
    result["snapshots"]["system"]["disk"] = [d for d in system.get("disk", []) if d.get("mount_point") == "/"]
    mavlink = snapshots.get("mavlink", {})
    components = []
    for sid, vehicle in mavlink.get("vehicles", {}).items():
        for cid, component in vehicle.get("components", {}).items():
            components.append({"system_id": sid, "component_id": cid, "message_types": sorted(component.get("messages", {}))})
    result["snapshots"]["mavlink_components"] = components
    result["decoded_fields_note"] = "Campos declarados pelo decodificador; extensões opcionais podem ter valores padrão sem terem sido transmitidas. Disponibilidade requer validar valores, sentinelas e estado dos sensores."
    return result


def analyze(path):
    from pymavlink import mavutil
    conn = mavutil.mavlink_connection(str(path), dialect="ardupilotmega")
    types = {}
    params = {}
    stats = Counter()
    texts = []
    metrics = {}
    ranges = {}
    named_values = {}
    gps_fix_counts = Counter()
    known_fields = {
        "SYS_STATUS": {"voltage_battery", "current_battery", "battery_remaining", "load"},
        "BATTERY_STATUS": {"current_consumed", "energy_consumed", "current_battery", "temperature", "battery_remaining"},
        "SYSTEM_TIME": {"time_boot_ms", "time_unix_usec"},
        "SCALED_PRESSURE": {"press_abs", "temperature"},
        "SCALED_PRESSURE2": {"press_abs", "temperature"},
        "GLOBAL_POSITION_INT": {"relative_alt", "alt"},
    }
    try:
        while True:
            msg = conn.recv_match()
            if msg is None:
                break
            name = msg.get_type()
            ts = getattr(msg, "_timestamp", None)
            if name == "BAD_DATA":
                stats["bad_data_records"] += 1
                continue
            key = f"{msg.get_srcSystem()}:{msg.get_srcComponent()}:{name}"
            info = types.setdefault(key, {"type": name, "system_id": msg.get_srcSystem(), "component_id": msg.get_srcComponent(), "count": 0, "fields": list(msg.get_fieldnames()), "first_timestamp": ts, "last_timestamp": ts})
            info["count"] += 1
            info["last_timestamp"] = ts
            if ts is not None:
                ranges["min"] = min(ts, ranges.get("min", ts))
                ranges["max"] = max(ts, ranges.get("max", ts))
            if name == "HEARTBEAT" and msg.get_srcSystem() == 1 and msg.get_srcComponent() == 1:
                stats["armed_heartbeat_samples" if msg.base_mode & 128 else "disarmed_heartbeat_samples"] += 1
            if name == "PARAM_VALUE":
                p = msg.param_id
                if isinstance(p, bytes):
                    p = p.decode(errors="replace")
                p = p.rstrip("\0")
                if p.startswith(("STAT_", "LOG_", "BATT_")) or p == "BRD_TYPE":
                    params[p] = float(msg.param_value)
            if name == "STATUSTEXT":
                value = {"timestamp": ts, "severity": msg.severity, "text": msg.text}
                if len(texts) < 200:
                    texts.append(value)
            if name == "NAMED_VALUE_FLOAT":
                named_values[msg.name] = msg.value
            if name == "GPS_RAW_INT":
                gps_fix_counts[msg.fix_type] += 1
            if msg.get_srcSystem() == 1 and msg.get_srcComponent() == 1:
                for field in known_fields.get(name, set()):
                    value = getattr(msg, field, None)
                    if not isinstance(value, (int, float)):
                        continue
                    m = metrics.setdefault(name + "." + field, {"min_raw": value, "max_raw": value, "last_raw": value})
                    m["min_raw"] = min(value, m["min_raw"])
                    m["max_raw"] = max(value, m["max_raw"])
                    m["last_raw"] = value
    finally:
        conn.close()
    return {"types": sorted(types.values(), key=lambda i: (i["system_id"], i["component_id"], i["type"])), "selected_parameters_last_observed": params, "sample_counts": dict(stats), "metrics_raw_protocol_units": metrics, "timestamps": ranges, "status_text_first_200": texts, "named_values_last": named_values, "gps_fix_type_sample_counts": dict(gps_fix_counts)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.2.2")
    args = parser.parse_args()
    base = "http://" + args.host
    now = datetime.now(timezone.utc)
    capture = ROOT / "data" / ("inspecao_" + now.strftime("%Y%m%dT%H%M%SZ"))
    capture.mkdir(parents=True, exist_ok=True)
    report = {"host": args.host, "captured_at_utc": now.isoformat(), "pymavlink_version": version("pymavlink"), "mode": "HTTP GET; POST somente para autenticar o File Browser; nenhum comando MAVLink", "snapshots": {}, "folders": {}, "logs": [], "errors": []}
    endpoints = {
        "blueos_version": ":8081/v1.0/version/current",
        "autopilot_board": ":8000/v1.0/board",
        "autopilot_firmware": ":8000/v1.0/firmware_info",
        "router": ":8000/v1.0/preferred_router",
        "system": ":6030/system",
        "mavlink": ":6040/v1/mavlink",
    }
    for label, endpoint in endpoints.items():
        try:
            obj = json.loads(read(base + endpoint))
            report["snapshots"][label] = obj
        except Exception as error:
            report["errors"].append({"endpoint": endpoint, "error": str(error)})
    login = json.dumps({"username": "", "password": "", "recaptcha": ""}).encode()
    token = read(base + "/file-browser/api/login", data=login).decode().strip().strip('"')
    pending = ["/ardupilot_logs/", "/ardupilot_logs/firmware/logs/", "/ardupilot_logs/logs/", "/system_logs/"]
    visited = set()
    while pending:
        folder = pending.pop(0)
        if folder in visited:
            continue
        visited.add(folder)
        try:
            obj = json.loads(read(base + "/file-browser/api/resources" + quote(folder, safe="/"), token))
            items = [{k: item.get(k) for k in ("name", "path", "size", "modified", "isDir", "extension")} for item in obj.get("items", [])]
            report["folders"][folder] = items
            for item in items:
                if item["isDir"] and folder.startswith("/system_logs/"):
                    pending.append(item["path"].rstrip("/") + "/")
                if not item["isDir"] and item["extension"].lower() in (".tlog", ".bin"):
                    if any(log["path"] == item["path"] for log in report["logs"]):
                        continue
                    info = dict(item)
                    report["logs"].append(info)
                    if not item["size"]:
                        info["analysis_status"] = "empty"
                        continue
                    destination = capture / "logs" / item["name"]
                    destination.parent.mkdir(exist_ok=True)
                    print("Copiando " + item["name"], flush=True)
                    raw = read(base + "/file-browser/api/raw" + quote(item["path"], safe="/"), token)
                    destination.write_bytes(raw)
                    info["snapshot_size"] = len(raw)
                    info["sha256"] = hashlib.sha256(raw).hexdigest()
                    info["local_snapshot"] = str(destination.relative_to(ROOT))
                    try:
                        info["analysis"] = analyze(destination)
                        info["analysis_status"] = "parsed"
                        print(f"  {len(info['analysis']['types'])} tipos/origens", flush=True)
                    except Exception as error:
                        info["analysis_status"] = "error"
                        info["error"] = str(error)
        except Exception as error:
            report["errors"].append({"folder": folder, "error": str(error)})
    write(capture / "inventario.json", report)
    write(ROOT / "docs" / "inventario_rov.json", public_report(report))
    print(f"Inventário: {len(report['logs'])} arquivos; {len(report['folders'])} pastas; {len(report['errors'])} erros. {capture}")


if __name__ == "__main__":
    main()
