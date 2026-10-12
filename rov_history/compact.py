"""Leitura compacta: resumo, ocorrências e séries de 10 s sem guardar cada mensagem.

Um .tlog grava cerca de 470 mil mensagens por hora, a maioria atitude e
orientação em alta frequência. Guardar cada uma ocupava cerca de 5 vezes o
tamanho do log. Aqui só os tipos usados nos indicadores são decodificados; os
demais entram apenas na contagem do inventário. O .tlog original continua
sendo a fonte completa.
"""
import mmap
from pathlib import Path
import struct
import time

from pymavlink.dialects.v20 import ardupilotmega as mav

from .model import clean

BUCKET_SECONDS = 10
DECODE = {"HEARTBEAT", "SYS_STATUS", "BATTERY_STATUS", "GLOBAL_POSITION_INT", "SCALED_PRESSURE",
          "SCALED_PRESSURE2", "STATUSTEXT", "PARAM_VALUE", "NAMED_VALUE_FLOAT"}
NAMES = {msgid: cls.msgname for msgid, cls in mav.mavlink_map.items()}
DECODE_IDS = {msgid for msgid, name in NAMES.items() if name in DECODE}

METRICS = {
    "depth": ("Profundidade", "m"),
    "voltage": ("Tensão da alimentação", "V"),
    "current": ("Corrente total", "A"),
    "consumed": ("Consumo acumulado", "Ah"),
    "energy": ("Energia acumulada", "Wh"),
    "armed": ("Estado armado", "0 desarmado / 1 armado"),
}
SOURCES = {
    ("SYS_STATUS", "voltage_battery"): "voltage", ("SYS_STATUS", "current_battery"): "current",
    ("BATTERY_STATUS", "current_consumed"): "consumed", ("BATTERY_STATUS", "energy_consumed"): "energy",
    ("BAT", "Volt"): "voltage", ("BAT", "Curr"): "current", ("BAT", "CurrTot"): "consumed", ("BAT", "EnrgTot"): "energy",
}


def valid_time(value):
    return value if 946684800 <= value <= time.time() + 86400 else None


def scan_tlog(path, decode_all=False, only=None):
    """Percorre um .tlog: (ts, tipo, origem, dados).

    dados é None para mensagens que não alimentam indicadores, exceto a
    primeira de cada tipo/origem, decodificada para listar os campos.
    """
    path = Path(path)
    if path.stat().st_size == 0:
        return
    # mmap: um log de um dia inteiro (~200 MB) não é carregado na memória do contêiner (256 MB).
    with path.open("rb") as file, mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as data:
        yield from _frames(data, decode_all, only)


def _frames(data, decode_all=False, only=None):
    # only: decodifica somente esses tipos (leitura sob demanda de um tipo no inventário).
    wanted = {msgid for msgid, name in NAMES.items() if name in only} if only else None
    link = mav.MAVLink(None)
    link.robust_parsing = True
    seen = set()
    size = len(data)
    i = 0
    skipped = 0
    while i + 9 < size:
        magic = data[i + 8]
        if magic == 0xFD and i + 18 <= size:
            length = data[i + 9]
            msgid = data[i + 15] | data[i + 16] << 8 | data[i + 17] << 16
            origin = f"{data[i + 13]}:{data[i + 14]}"
            frame = 10 + length + 2 + (13 if data[i + 10] & 1 else 0)
        elif magic == 0xFE and i + 14 <= size:
            length = data[i + 9]
            msgid = data[i + 13]
            origin = f"{data[i + 11]}:{data[i + 12]}"
            frame = 6 + length + 2
        else:
            skipped += 1
            i += 1
            continue
        if i + 8 + frame > size:
            break
        if skipped:
            yield None, "BAD_DATA", "decoder", {"reason": f"{skipped} bytes sem quadro MAVLink válido"}
            skipped = 0
        ts = valid_time(struct.unpack_from(">Q", data, i)[0] / 1e6)
        name = NAMES.get(msgid, f"UNKNOWN_{msgid}")
        message = None
        if (msgid in wanted) if wanted is not None else (decode_all or msgid in DECODE_IDS or (msgid, origin) not in seen):
            try:
                decoded = link.decode(bytearray(data[i + 8:i + 8 + frame]))
                message = clean(decoded.to_dict())
                message.pop("mavpackettype", None)
                seen.add((msgid, origin))
            except Exception as error:
                yield ts, "BAD_DATA", "decoder", {"reason": str(error)[:200]}
                i += 8 + frame
                continue
        if name == "NAMED_VALUE_FLOAT" and message is not None:
            name += ":" + str(message.get("name", ""))
        yield ts, name, origin, message
        i += 8 + frame
    if skipped:
        yield None, "BAD_DATA", "decoder", {"reason": f"{skipped} bytes sem quadro MAVLink válido"}


class Series:
    """Mínimo, máximo e média por intervalo de 10 s; mantém picos sem guardar cada amostra."""

    def __init__(self):
        self.buckets = {}

    def put(self, metric, ts, value):
        key = (metric, int(ts // BUCKET_SECONDS))
        bucket = self.buckets.get(key)
        if bucket is None:
            self.buckets[key] = [value, value, value, 1]
        else:
            bucket[0] = min(bucket[0], value)
            bucket[1] = max(bucket[1], value)
            bucket[2] += value
            bucket[3] += 1

    def add(self, ts, name, origin, data, kind, catalog):
        if ts is None or not data or (kind != "bin" and origin != "1:1"):
            return
        if name == "HEARTBEAT":
            mode = data.get("base_mode", 0)
            if isinstance(mode, dict):
                mode = mode.get("bits", 0)
            if isinstance(mode, int):
                self.put("armed", ts, int(bool(mode & 128)))
            return
        if name == "GLOBAL_POSITION_INT":
            if isinstance(data.get("relative_alt"), (int, float)):
                self.put("depth", ts, -data["relative_alt"] / 1000)
            return
        for (msgtype, field), metric in SOURCES.items():
            if msgtype == name and field in data:
                value = data[field]
                if kind != "bin":
                    measure = catalog.measure(kind, name, field, value, data)
                    value = measure["value"] if measure["valid"] else None
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    self.put(metric, ts, value)

    def rows(self, file_id):
        return [(file_id, metric, bucket, low, high, total, count) for (metric, bucket), (low, high, total, count) in self.buckets.items()]

    def clear(self):
        self.buckets.clear()
