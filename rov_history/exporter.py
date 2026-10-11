"""ZIP com relatório e dados completos, em uma leitura consistente do histórico."""
import csv
from datetime import datetime, timezone
import html
import io
import json
from pathlib import Path
import re
import threading
import time
import zipfile

from . import __version__
from .capabilities import capabilities
from .model import dumps, flatten

INDICATORS = {
    "SYS_STATUS": {"voltage_battery": "Tensão", "current_battery": "Corrente"},
    "BATTERY_STATUS": {"current_consumed": "Consumo acumulado", "energy_consumed": "Energia acumulada"},
    "BAT": {"Volt": "Tensão", "Curr": "Corrente", "CurrTot": "Consumo acumulado", "EnrgTot": "Energia acumulada"},
    "HEARTBEAT": {"_armed": "Estado armado"},
}
TABS = {"overview", "sessions", "charts", "events", "explorer", "files", "capabilities", "settings"}


def validate_context(value):
    if not isinstance(value, dict):
        raise ValueError("Configuração de exportação inválida")
    scope, tab = value.get("scope", "all"), value.get("tab", "overview")
    if scope not in ("all", "view") or tab not in TABS:
        raise ValueError("Escolha o conteúdo da exportação")
    result = {"scope": scope, "tab": tab}
    for name in ("include_data", "include_originals", "normalized"):
        v = value.get(name, name != "include_originals")
        if not isinstance(v, bool):
            raise ValueError("Opção de exportação inválida")
        result[name] = v
    for name in ("file", "stream"):
        try:
            result[name] = int(value.get(name) or 0)
        except (TypeError, ValueError):
            raise ValueError("Origem inválida") from None
        if result[name] < 0:
            raise ValueError("Origem inválida")
    for name in ("field", "search", "group", "timezone"):
        v = value.get(name, "")
        if not isinstance(v, str) or len(v) > 300:
            raise ValueError("Filtro inválido")
        result[name] = v
    return result


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts is not None else ""


def csv_cells(cells):
    return ["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in cells]


def write_csv(archive, name, headers, rows, check):
    with archive.open(name, "w", force_zip64=True) as raw:
        wrapper = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        writer = csv.writer(wrapper)
        writer.writerow(headers)
        for index, row in enumerate(rows):
            if index % 1000 == 0:
                check()
            writer.writerow(csv_cells(row))
        wrapper.flush()
        wrapper.detach()


def report_table(headers, rows):
    e = lambda x: html.escape(str(x if x is not None else "—"))
    return '<div class="table"><table><thead><tr>' + "".join(f"<th>{e(h)}</th>" for h in headers) + '</tr></thead><tbody>' + "".join('<tr>' + "".join(f"<td>{e(c)}</td>" for c in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def chart_svg(series, label):
    points = series["points"]
    valid = [p for p in points if p["value"] is not None]
    if not valid:
        return '<p>Sem medições numéricas válidas.</p>'
    lo, hi = min(p["value"] for p in valid), max(p["value"] for p in valid)
    pad = max((hi - lo) * .1, abs(hi) * .002, .001)
    lower, upper = lo - pad, hi + pad
    first, last = points[0]["seq"], points[-1]["seq"]
    segments, segment, previous = [], [], None
    for p in points:
        if p["value"] is None or previous and p["segment"] != previous["segment"]:
            if segment:
                segments.append(segment); segment = []
            previous = None
        if p["value"] is None:
            continue
        x, y = 65 + (p["seq"] - first) / (last - first or 1) * 680, 18 + (upper - p["value"]) / (upper - lower) * 195
        if previous and series["field"] == "_armed":
            segment.append((x, segment[-1][1]))
        segment.append((x, y))
        previous = p
    if segment:
        segments.append(segment)
    coords = lambda seg: " L".join(f"{x:.2f},{y:.2f}" for x, y in seg)
    # Área sob cada trecho contínuo; o gradiente é declarado uma vez no relatório.
    areas = ''.join(f'<path d="M{seg[0][0]:.2f},213 L{coords(seg)} L{seg[-1][0]:.2f},213 Z" fill="url(#bv-area)"/>' for seg in segments)
    lines = ''.join(f'<path d="M{coords(seg)}" fill="none" stroke="#217987" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>' for seg in segments)
    grid = ''.join(f'<line x1="65" x2="745" y1="{18 + i * 195 / 4:.1f}" y2="{18 + i * 195 / 4:.1f}" stroke="#d6e5e8" stroke-dasharray="3 5"/>' for i in range(5))
    e = html.escape
    return f'<svg viewBox="0 0 780 250" role="img" aria-label="{e(label)}"><rect width="780" height="250" rx="14" fill="#f4f9fa"/>{grid}<text x="8" y="24">{hi:.4g}</text><text x="8" y="210">{lo:.4g}</text>{areas}{lines}<text x="65" y="238">Registro {first} → {last} · {e(series["unit"])}</text></svg>'


def build_bundle(store, identifier, context, progress=lambda text: None, stop=None):
    context = validate_context(context)
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("Identificador de exportação inválido")
    directory = store.directory / "exports"
    directory.mkdir(exist_ok=True)
    target = directory / (identifier + ".zip")
    temporary = directory / (identifier + ".partial")
    stop = stop or threading.Event()
    def check():
        if stop.is_set():
            raise ValueError("Exportação interrompida")
    manifest = {"application": "Bravo Viewer", "version": __version__, "generated_at_utc": utc(time.time()), "selection": context, "entries": []}
    root = "Bravo-Viewer/"
    charts_html = []
    try:
        with store.snapshot() as snap, zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
            files, streams = snap.files(), snap.streams()
            overview, grid, config = snap.overview(), capabilities(snap), snap.config()
            all_scope, tab = context["scope"] == "all", context["tab"]
            if not all_scope:
                if tab in ("sessions", "charts", "explorer", "events") and context["file"]:
                    files = [f for f in files if f["id"] == context["file"]]
                if tab == "files" and context["search"]:
                    files = [f for f in files if context["search"].lower() in (f["name"] + f["source_key"]).lower()]
                if tab in ("settings", "capabilities"):
                    files = []
                streams = [s for s in streams if s["file_id"] in {f["id"] for f in files}]
                if tab in ("charts", "explorer"):
                    streams = [s for s in streams if s["id"] == context["stream"]]
                    if not streams:
                        raise ValueError("Selecione uma origem com dados antes de exportar esta visão")
                    files = [f for f in files if f["id"] == streams[0]["file_id"]]
            selected_ids = {f["id"] for f in files}
            manifest["equipment"] = config["equipment"]
            manifest["files"] = [{k: f[k] for k in ("id", "name", "source_key", "kind", "sha256", "size", "note")} for f in files]
            def add_json(name, value):
                archive.writestr(root + name, dumps(value) + "\n")
            add_json("resumo/visao_geral.json", overview if all_scope or tab == "overview" else {"equipment": config["equipment"], "sessions": files})
            add_json("resumo/sessoes.json", files)
            if all_scope or tab == "settings":
                add_json("resumo/configuracao.json", config)
            if all_scope or tab == "capabilities":
                add_json("inventario/dados_e_possibilidades.json", grid)
                write_csv(archive, root + "inventario/dados_e_possibilidades.csv", ["Informação", "Nesta versão", "Dados no histórico", "Fontes", "Condição"], ([r["name"], r["scope"], r["data"], ", ".join(r["sources"]), r["condition"]] for r in grid["rows"]), check)
            write_csv(archive, root + "resumo/sessoes.csv", ["ID", "Arquivo/coleta", "Origem", "Formato", "Registros", "Tempo observado s", "Tempo armado observado s", "Lacunas", "Observações"], ([f["id"], f["name"], f["source_key"], f["kind"], f["summary"].get("records", 0), f["summary"].get("observed_seconds", 0), f["summary"].get("armed_seconds", 0), f["summary"].get("gaps", 0), f["note"]] for f in files), check)
            periods = ([f["id"], utc(p.get("start")), utc(p.get("end")), p.get("observed_seconds", 0), p.get("open", True), p.get("reason", "Em andamento")] for f in files for p in [*f["summary"].get("armed_periods", []), *([f["summary"]["active_armed_period"]] if f["summary"].get("active_armed_period") else [])])
            write_csv(archive, root + "resumo/periodos_armados.csv", ["Sessão", "Início UTC", "Fim observado UTC", "Tempo observado s", "Aberto/incompleto", "Encerramento"], periods, check)
            add_json("inventario/tipos_e_campos.json", streams)
            if all_scope or tab == "files":
                inventory_path = store.directory / "remote_inventory.json"
                remote = json.loads(inventory_path.read_text(encoding="utf-8")) if inventory_path.exists() else {"files": [], "errors": []}
                if not all_scope and context["search"]:
                    remote["files"] = [f for f in remote["files"] if context["search"].lower() in f["path"].lower()]
                add_json("inventario/arquivos_blueos.json", remote)
                write_csv(archive, root + "inventario/arquivos_blueos.csv", ["Caminho", "Formato", "Bytes", "Modificado"], ([f["path"], f["extension"], f["size"], f["modified"]] for f in remote["files"]), check)
            events = []
            if all_scope or tab in ("events", "sessions", "overview"):
                # A leitura é iterada; a prévia HTML fica limitada a 200 ocorrências.
                def event_rows():
                    with snap.db() as db:
                        query = "SELECT e.*,f.name AS file_name FROM events e JOIN files f ON f.id=e.file_id ORDER BY e.id DESC"
                        for row in db.execute(query):
                            if row["file_id"] not in selected_ids:
                                continue
                            if not all_scope and tab == "events" and context["search"].lower() not in row["text"].lower():
                                continue
                            values = [utc(row["ts"]), row["file_name"], row["severity"], row["type"], row["text"], row["seq"]]
                            if len(events) < 200:
                                events.append(values)
                            yield values
                write_csv(archive, root + "ocorrencias/ocorrencias.csv", ["Data UTC", "Fonte", "Nível", "Tipo", "Mensagem", "Sequência"], event_rows(), check)
            raw_streams = streams if all_scope else streams if tab == "explorer" else []
            if context["include_data"]:
                for index, stream in enumerate(raw_streams):
                    check(); progress(f"Dados completos: {index + 1}/{len(raw_streams)} tipos/origens")
                    readable = re.sub(r'[^a-zA-Z0-9_.-]', '_', stream['type'] + '_' + stream['origin'])[:100]
                    folder = f"dados/sessao_{stream['file_id']:06d}/tipo_{stream['id']:06d}_{readable}"
                    fields = stream["fields"]
                    if not all_scope and context["field"]:
                        if context["field"] not in fields:
                            raise ValueError("Campo selecionado não encontrado")
                        fields = [context["field"]]
                    elif not all_scope and context["search"]:
                        fields = [f for f in fields if context["search"].lower() in f.lower()]
                    def raw_rows():
                        with snap.db() as db:
                            for row in db.execute("SELECT seq,ts,segment,data FROM records WHERE stream_id=? ORDER BY seq", (stream["id"],)):
                                values = flatten(json.loads(row["data"]))
                                yield [row["seq"], utc(row["ts"]), row["segment"], *[values.get(f, "") for f in fields]]
                    write_csv(archive, root + folder + ".csv", ["Sequência", "Data UTC", "Trecho", *fields], raw_rows(), check)
                    with archive.open(root + folder + ".jsonl", "w", force_zip64=True) as output:
                        for index, chunk in enumerate(snap.export(stream["id"], "jsonl")):
                            if index % 1000 == 0: check()
                            output.write(chunk)
                    manifest["entries"].append({"type": stream["type"], "origin": stream["origin"], "session": stream["file_id"], "records": stream["count"], "csv": folder + ".csv", "jsonl": folder + ".jsonl", "csv_fields": fields})
            chart_streams = streams if all_scope or tab in ("charts", "overview", "sessions") else []
            for stream in chart_streams:
                if stream["type"] not in INDICATORS or not (stream["origin"] == "1:1" or stream["kind"] == "bin"):
                    continue
                fields = INDICATORS[stream["type"]]
                if not all_scope and tab == "charts":
                    if context["field"] not in fields:
                        raise ValueError("Indicador não disponível nesta visão")
                    fields = {context["field"]: fields.get(context["field"], context["field"])}
                for field, label in fields.items():
                    if field != "_armed" and field not in stream["fields"]:
                        continue
                    check(); progress(f"Gráficos e séries: {stream['file_name']} · {label}")
                    normalized = context["normalized"] if tab == "charts" and not all_scope else True
                    series = snap.series(stream["id"], field, 350, normalized)
                    filename = f"energia/sessao_{stream['file_id']:06d}_tipo_{stream['id']:06d}_{field}.csv"
                    def measured_rows():
                        with snap.db() as db:
                            for row in db.execute("SELECT seq,ts,segment,data FROM records WHERE stream_id=? ORDER BY seq", (stream["id"],)):
                                data = json.loads(row["data"])
                                raw = flatten(data).get(field)
                                measure = snap.catalog.measure(stream["kind"], stream["type"], field, raw, data)
                                if field == "_armed":
                                    mode = data.get("base_mode", 0)
                                    if isinstance(mode, dict): mode = mode.get("bits", 0)
                                    raw = int(bool(mode & 128))
                                    measure = {"value": raw, "valid": True}
                                if stream["kind"] == "bin":
                                    measure = {"value": raw, "valid": raw is not None}
                                value = (measure["value"] if normalized else raw) if measure["valid"] else None
                                yield [row["seq"], utc(row["ts"]), row["segment"], value, raw, series["unit"]]
                    write_csv(archive, root + filename, ["Sequência", "Data UTC", "Trecho", label, "Valor registrado", "Unidade exibida"], measured_rows(), check)
                    charts_html.append(f'<article><h3>{html.escape(label)} · {html.escape(stream["file_name"])}</h3>{chart_svg(series, label)}<a href="{filename}">Série completa CSV</a></article>')
            if context["include_originals"]:
                for file in files:
                    check()
                    actual = snap.file(file["id"])
                    path = Path(actual["path"]).resolve()
                    if not path.is_relative_to((store.directory / "originals").resolve()) or not path.is_file():
                        continue
                    progress(f"Copiando original: {file['name']}")
                    name = f"originais/sessao_{file['id']:06d}{path.suffix}"
                    with path.open("rb") as source, archive.open(root + name, "w", force_zip64=True) as output:
                        while chunk := source.read(65536):
                            check(); output.write(chunk)
                    manifest.setdefault("originals", []).append({"name": file["name"], "archive": name, "sha256": file["sha256"]})
            sections = ['<h2>Sessões e operações</h2>' + report_table(["Fonte", "Registros", "Tempo observado s", "Tempo armado s", "Observações"], [[f["name"], f["summary"].get("records", 0), f["summary"].get("observed_seconds", 0), f["summary"].get("armed_seconds", 0), f["note"]] for f in files])]
            if all_scope or tab == "overview":
                sections.insert(0, '<h2>Horas e contadores registrados</h2>' + report_table(["Contador", "Último valor"], overview["statistics"].items()))
            if charts_html: sections.append('<h2>Energia e gráficos</h2>' + ''.join(charts_html))
            if events: sections.append('<h2>Ocorrências · prévia de até 200 mensagens</h2>' + report_table(["Data UTC", "Fonte", "Nível", "Tipo", "Mensagem", "Sequência"], events) + '<a href="ocorrencias/ocorrencias.csv">Todas as ocorrências selecionadas</a>')
            if all_scope or tab == "capabilities":
                sections.append('<h2>Dados e possibilidades</h2>' + report_table(["Informação", "Nesta versão", "Dados", "Condição"], [[r["name"], r["scope"], r["data"], r["condition"]] for r in grid["rows"]]))
            if manifest["entries"]:
                sections.append('<h2>Inventário e dados completos</h2>' + ''.join(f'<p>{html.escape(entry["type"])} · {html.escape(entry["origin"])} · {entry["records"]} registros · <a href="{entry["csv"]}">CSV</a> · <a href="{entry["jsonl"]}">JSONL</a></p>' for entry in manifest["entries"]))
            if all_scope or tab == "files":
                sections.append('<h2>Arquivos preservados</h2>' + report_table(["Arquivo", "Formato", "Bytes", "Tipos/origens", "Hash SHA256"], [[f["name"], f["kind"], f["size"], f["stream_count"], f["sha256"]] for f in files]))
            if all_scope or tab == "settings":
                sections.append('<h2>Equipamento e coleta</h2>' + report_table(["Configuração", "Valor"], config.items()))
            report = f'<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Bravo Viewer · relatório</title><style>body{{font:14px system-ui;color:#183c49;background:#f7fbfb;margin:35px auto;padding:20px;max-width:1150px;line-height:1.55}}h1{{font-size:30px;margin:12px 0 4px}}h2{{margin-top:38px;font-size:19px}}h3{{margin:0 0 12px;font-size:15px}}th,td{{padding:10px 12px;border-bottom:1px solid #e2ecee;text-align:left;vertical-align:top;overflow-wrap:anywhere}}th{{background:#eaf3f4;font-size:11px;text-transform:uppercase;letter-spacing:.5px}}tr:last-child td{{border-bottom:0}}table{{border-collapse:separate;border-spacing:0;width:100%;font-size:12px}}.table{{overflow:auto;background:#fff;border:1px solid #d8e4e7;border-radius:14px}}article{{background:#fff;border:1px solid #d8e4e7;border-radius:16px;padding:20px;margin:18px 0;box-shadow:0 6px 20px -14px #183c4966}}svg{{width:100%;height:auto;max-height:320px;display:block;margin-bottom:10px}}svg text{{font:12px system-ui;fill:#56737d}}a{{color:#217987;font-weight:600}}footer{{margin-top:40px;border-top:1px solid #d8e4e7;padding-top:15px;color:#56737d}}</style><svg width="0" height="0" style="position:absolute"><defs><linearGradient id="bv-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#217987" stop-opacity=".25"/><stop offset="1" stop-color="#217987" stop-opacity="0"/></linearGradient></defs></svg><h1>Bravo Viewer</h1><p>{html.escape(config["equipment"])} · {manifest["generated_at_utc"]}</p><p>Exportação: {"todo o histórico" if all_scope else "visão atual · " + html.escape(tab)}. Dados completos em CSV/JSONL; gráficos são uma redução que mantém extremos e segmentos. Datas em UTC. Contadores e acumuladores não são somados entre fontes.</p>' + ''.join(sections) + '<footer>Bravo Viewer · desenvolvido por Reikjv</footer></html>'
            web = Path(__file__).resolve().parents[1] / "web"
            for filename in ("brs.png", "brand.png"):
                archive.write(web / filename, root + "assets/" + filename)
            report = report.replace('<h1>Bravo Viewer</h1>', '<img src="assets/brs.png" alt="BRS" style="width:110px"><h1>Bravo Viewer</h1>')
            report = report.replace('<footer>Bravo Viewer', '<footer><img src="assets/brand.png" alt="Reikjv" style="width:40px;vertical-align:middle;margin-right:10px">Bravo Viewer')
            archive.writestr(root + "relatorio.html", report)
            archive.writestr(root + "LEIA-ME.txt", "Bravo Viewer\nExtraia o ZIP e abra relatorio.html no navegador.\nresumo: sessões, contadores e observações salvas.\nenergia: séries completas nas unidades de leitura.\nocorrencias: todas as mensagens correspondentes aos filtros.\ninventario: tipos/campos, arquivos BlueOS e possibilidades.\ndados: CSV por tipo/origem e JSONL preservando os campos brutos completos.\noriginais: arquivos opcionais; coletas de API não possuem original binário.\nCSV protege textos que poderiam ser executados como fórmula; JSONL preserva esses textos.\nA exportação usa um instantâneo consistente da base local. O inventário remoto representa a última consulta.\nDatas ausentes permanecem vazias. Tempos armados incluem bancada, e lacunas não contam como operação contínua.\n")
            add_json("manifest.json", manifest)
        temporary.replace(target)
        # Mantém apenas cinco pacotes concluídos; logs e originais não são removidos.
        for old in sorted(directory.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)[5:]:
            if old.resolve().is_relative_to(directory.resolve()):
                try: old.unlink()
                except OSError: pass
        return {"id": identifier, "size": target.stat().st_size, "streams": len(manifest["entries"]), "download": "api/bundle?id=" + identifier}
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
