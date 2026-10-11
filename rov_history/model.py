"""Unidades de apresentação e preservação de campos brutos."""
import json
import math
import array
from pathlib import Path

CATALOGS = Path(__file__).resolve().parents[1] / "docs" / "catalogos"


def clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").rstrip("\0")
    if isinstance(value, array.array):
        return [clean(v) for v in value]
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    return value


def dumps(value):
    return json.dumps(clean(value), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def flatten(value, prefix=""):
    result = {}
    if isinstance(value, dict):
        for key, child in value.items():
            result.update(flatten(child, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.update(flatten(child, f"{prefix}[{index}]"))
    elif prefix:
        result[prefix] = value
    return result


def get_field(data, field):
    return flatten(data).get(field)


class Catalog:
    def __init__(self):
        self.schemas = {}
        for kind, filename in (("tlog", "mavlink_ardupilotmega.json"), ("bin", "dataflash_sub.json")):
            path = CATALOGS / filename
            if path.exists():
                for message in json.loads(path.read_text(encoding="utf-8"))["messages"]:
                    self.schemas[kind, message["name"]] = {f["name"]: f for f in message["fields"]}

    def unit(self, kind, msgtype, field):
        if msgtype == "SYSTEM_TEMPERATURE" and field.endswith((".temperature", ".maximum_temperature", ".critical_temperature")):
            return "°C"
        if msgtype == "SYSTEM_CPU" and field.endswith(".usage"):
            return "%"
        if msgtype == "SYSTEM_CPU" and field.endswith(".frequency"):
            return "MHz"
        if msgtype.startswith("SYSTEM_") and field.endswith("_kB"):
            return "kB"
        if msgtype.startswith("SYSTEM_") and field.endswith("_B"):
            return "B"
        if kind == "live":
            kind = "tlog"
        base = msgtype.split(":", 1)[0]
        top = field.split("[", 1)[0].split(".", 1)[0]
        return self.schemas.get((kind, base), {}).get(top, {}).get("units", "")

    def measure(self, kind, msgtype, field, value, data):
        unit = self.unit(kind, msgtype, field)
        if value is None or isinstance(value, bool) or not isinstance(value, (float, int)):
            return {"value": value, "unit": unit, "valid": value is not None, "reason": "Não é um campo numérico"}
        base = msgtype.split(":", 1)[0]
        invalid = False
        reason = ""
        if kind in ("tlog", "live"):
            if base in ("BATTERY_STATUS", "SYS_STATUS"):
                invalid = (field in ("current_battery", "battery_remaining", "current_consumed", "energy_consumed") and value < 0)
                invalid |= field == "temperature" and value == 32767
                invalid |= (field.startswith("voltages[") or field == "voltage_battery") and value in (0, 65535)
                invalid |= field == "time_remaining" and value == 0
            if base == "GPS_RAW_INT" and field in ("lat", "lon", "alt", "vel", "cog"):
                invalid |= data.get("fix_type", 0) < 2
            if base == "GPS_RAW_INT" and field in ("eph", "epv"):
                invalid |= value == 65535
            if base == "SYSTEM_TIME" and field == "time_unix_usec":
                invalid |= value == 0
        if invalid:
            return {"value": None, "unit": unit, "valid": False, "reason": "Desconhecido ou sem condição válida no protocolo"}
        scale, shown = {"mV": (.001, "V"), "cA": (.01, "A"), "cdegC": (.01, "°C"), "degC": (1, "°C"), "mAh": (.001, "Ah"), "hJ": (1 / 36, "Wh"), "mm": (.001, "m"), "cm/s": (.01, "m/s"), "cdeg": (.01, "°"), "degE7": (1e-7, "°"), "rad": (180 / math.pi, "°")}.get(unit, (1, unit))
        return {"value": value * scale, "unit": shown, "valid": True, "reason": reason}


def category(msgtype):
    name = msgtype.split(":", 1)[0]
    if name.startswith("SYSTEM_") or name.startswith("LOG_"):
        return "Computador e serviços"
    if any(s in name for s in ("BAT", "POWER", "POWR", "ESC")):
        return "Alimentação e ESC"
    if any(s in name for s in ("GPS", "POSITION", "ODOMETRY", "VISION", "VISP", "VISO", "XKF", "NKF", "EKF", "TERRAIN", "RANGE", "DISTANCE", "NAV_")):
        return "Navegação e sensores externos"
    if any(s in name for s in ("IMU", "ATT", "AHRS", "VIB", "MAG", "GYR", "ACC", "PRESSURE", "BARO")):
        return "Atitude e sensores"
    if any(s in name for s in ("RC", "SERVO", "MANUAL", "NAMED", "GIMBAL", "PID", "RCOU")):
        return "Controle e acessórios"
    return "Estado, eventos e configuração"
