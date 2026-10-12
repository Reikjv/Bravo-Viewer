"""Relatório visual da operação: o que importa ver, sem listas de mensagens.

Indicadores, mergulhos (períodos armados), picos com horário, gráficos e
ocorrências relevantes. Os dados completos ficam nos logs originais do ZIP.
"""
from datetime import datetime, timezone
import html
import io
import json
from pathlib import Path
import time
import zipfile

from . import __version__
from .compact import BUCKET_SECONDS, METRICS
from .model import dumps

# Severidade MAVLink: 0 emergência … 4 aviso; 5+ são informativas.
RELEVANT = {"0", "1", "2", "3", "4", "EMERGENCY", "ALERT", "CRITICAL", "ERROR", "WARNING", "WARN"}
SEVERITY = {"0": "Emergência", "1": "Alerta", "2": "Crítico", "3": "Erro", "4": "Aviso"}
CHARTS = ("depth", "voltage", "current")


def when(ts, zone):
    return datetime.fromtimestamp(ts, zone).strftime("%d/%m/%Y %H:%M:%S") if ts else "—"


def hours(seconds):
    seconds = int(seconds or 0)
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


def number(value, unit="", digits=2):
    if value is None:
        return "—"
    return f"{value:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".") + (f" {unit}" if unit else "")


def operation_data(store, operation):
    """Junta as sessões da operação usando a fonte de maior cobertura (log de voo antes da coleta ao vivo)."""
    files = [f for f in store.files() if f["operation_id"] == operation["id"]]
    summary = operation["summary"]
    source = [f for f in files if f["kind"] == summary.get("time_source")] or files
    dives = []
    for file in source:
        periods = list(file["summary"].get("armed_periods", []))
        if file["summary"].get("active_armed_period"):
            periods.append(file["summary"]["active_armed_period"])
        dives += [p for p in periods if p.get("start") and p.get("observed_seconds", 0) > 0]
    dives.sort(key=lambda p: p["start"])
    series = {}
    # Só o período da operação: um log com relógio errado (ex.: 2022) não estica os gráficos.
    end = operation["ended_at"] or time.time()
    window = (operation["started_at"] - 6 * 3600, end + 6 * 3600)
    for metric in METRICS:
        points = []
        for file in source:
            points += [p for p in store.points(file["id"], metric)["points"] if window[0] <= p["t"] <= window[1]]
        if points:
            series[metric] = {"metric": metric, "label": METRICS[metric][0], "unit": METRICS[metric][1], "seconds": BUCKET_SECONDS, "points": sorted(points, key=lambda p: p["t"])}
    ids = {f["id"] for f in files}
    with store.db() as db:
        events = [dict(row) for row in db.execute("SELECT ts,severity,type,text,file_id FROM events ORDER BY ts") if row["file_id"] in ids]
    relevant = [e for e in events if str(e["severity"]).upper() in RELEVANT]
    return {"files": files, "source": source, "dives": dives, "series": series, "events": relevant, "all_events": len(events)}


GAP_SECONDS = 300


def timeline_chart(series, zone):
    """Gráfico só dos trechos com dados: pausas maiores que 5 min viram uma linha tracejada.

    Uma operação de dias não fica achatada pelos períodos com o ROV desligado.
    """
    points = series["points"]
    if not points:
        return '<p class="muted">Sem medições.</p>'
    runs = [[points[0]]]
    for previous, point in zip(points, points[1:]):
        if point["t"] - previous["t"] > GAP_SECONDS:
            runs.append([])
        runs[-1].append(point)
    left, right, top, bottom, gap = 62, 760, 16, 206, 10
    spans = [max(run[-1]["t"] - run[0]["t"], series["seconds"]) for run in runs]
    usable = right - left - gap * (len(runs) - 1)
    lo, hi = min(p["min"] for p in points), max(p["max"] for p in points)
    pad = max((hi - lo) * .1, abs(hi) * .002, .001)
    lower, upper = lo - pad, hi + pad
    y = lambda v: top + (upper - v) / (upper - lower) * (bottom - top)
    parts = [f'<line x1="{left}" x2="{right}" y1="{top + i * (bottom - top) / 4:.1f}" y2="{top + i * (bottom - top) / 4:.1f}" stroke="#dbe7ea" stroke-dasharray="3 5"/>' for i in range(5)]
    x0 = left
    step = series["metric"] == "armed"
    for index, (run, span) in enumerate(zip(runs, spans)):
        width = usable * span / sum(spans)
        x = lambda t, x0=x0, run=run, span=span, width=width: x0 + (t - run[0]["t"]) / span * width
        top_line = " L".join(f"{x(p['t']):.1f},{y(p['max']):.1f}" for p in run)
        low_line = " L".join(f"{x(p['t']):.1f},{y(p['min']):.1f}" for p in reversed(run))
        parts.append(f'<path d="M{top_line} L{low_line} Z" fill="url(#bv-area)"/>')
        coords = []
        for i, p in enumerate(run):
            if step and i:
                coords.append(f"{x(p['t']):.1f},{y(run[i - 1]['avg']):.1f}")
            coords.append(f"{x(p['t']):.1f},{y(p['avg']):.1f}")
        if len(coords) == 1:
            coords.append(f"{x0 + 2:.1f},{y(run[0]['avg']):.1f}")
        parts.append(f'<path d="M{" L".join(coords)}" fill="none" stroke="#217987" stroke-width="2" stroke-linejoin="round"/>')
        if index:
            parts.append(f'<line x1="{x0 - gap / 2:.1f}" x2="{x0 - gap / 2:.1f}" y1="{top}" y2="{bottom}" stroke="#9fb3ba" stroke-dasharray="4 4"/>')
            if width > 70:
                parts.append(f'<text x="{x0 + 3:.1f}" y="{top + 12}">{when(run[0]["t"], zone)[:16]}</text>')
        x0 += width + gap
    e = html.escape
    first, last = when(points[0]["t"], zone)[:16], when(points[-1]["t"], zone)[:16]
    pauses = f" · {len(runs) - 1} {'pausa' if len(runs) == 2 else 'pausas'} sem dados (tracejado)" if len(runs) > 1 else ""
    return (f'<svg viewBox="0 0 780 250" role="img" aria-label="{e(series["label"])}"><rect width="780" height="250" rx="14" fill="#f4f9fa"/>'
            f'{"".join(parts)}<text x="8" y="22">{e(number(hi, "", 2))}</text><text x="8" y="210">{e(number(lo, "", 2))}</text>'
            f'<text x="{left}" y="236">{e(first)} → {e(last)}{e(pauses)}</text></svg>')


def grouped(events):
    """Mensagens iguais repetidas (ex.: heartbeat lost a cada 30 s) viram uma linha com contagem."""
    groups = {}
    for event in events:
        key = (str(event["severity"]), event["text"])
        group = groups.setdefault(key, {**event, "count": 0, "first": event["ts"], "last": event["ts"]})
        group["count"] += 1
        group["last"] = event["ts"] or group["last"]
    return sorted(groups.values(), key=lambda g: (g["first"] or 0))


def peak(series, metric, mode):
    points = series.get(metric, {}).get("points")
    if not points:
        return None
    point = max(points, key=lambda p: p["max"]) if mode == "max" else min(points, key=lambda p: p["min"])
    return {"value": point[mode], "t": point["t"], "unit": series[metric]["unit"]}


def render(store, operation, data, zone=timezone.utc, chart_svg=None):
    e = html.escape
    s = operation["summary"]
    depth = s.get("depth", {})
    dives = data["dives"]
    durations = [d["observed_seconds"] for d in dives]
    longest = max(dives, key=lambda d: d["observed_seconds"]) if dives else None
    peaks = [("Corrente máxima", peak(data["series"], "current", "max")),
             ("Tensão mínima", peak(data["series"], "voltage", "min")),
             ("Tensão máxima", peak(data["series"], "voltage", "max")),
             ("Profundidade máxima", peak(data["series"], "depth", "max") if depth.get("available") else None)]
    elapsed = (operation["ended_at"] or max((f["last_ts"] or 0 for f in data["files"]), default=0) or operation["started_at"]) - operation["started_at"]
    cards = [
        ("Duração da operação", hours(max(0, elapsed))),
        ("Tempo armado total", hours(s.get("armed_seconds"))),
        ("Mergulhos", str(len(dives))),
        ("Tempo armado médio", hours(sum(durations) / len(durations)) if durations else "—"),
        ("Maior mergulho", hours(longest["observed_seconds"]) if longest else "—"),
        ("Profundidade máxima", number(depth.get("max"), "m") if depth.get("available") else "Sem sensor"),
        ("Profundidade média", number(depth.get("mean"), "m") if depth.get("available") else "Sem sensor"),
        ("Consumo registrado", number(s["consumption"]["value"], s["consumption"]["unit"], 3) if s.get("consumption") else "—"),
    ]
    kpis = "".join(f'<div class="kpi"><small>{e(label)}</small><strong>{e(value)}</strong></div>' for label, value in cards)
    peak_rows = "".join(f'<div class="peak"><small>{e(label)}</small><strong>{e(number(p["value"], p["unit"], 3))}</strong><span>{e(when(p["t"], zone))}</span></div>'
                        for label, p in peaks if p)
    dive_rows = "".join(f"<tr><td>{index}</td><td>{e(when(d['start'], zone))}</td><td>{e(when(d['end'], zone))}</td><td>{e(hours(d['observed_seconds']))}</td></tr>"
                        for index, d in enumerate(dives, 1)) or '<tr><td colspan="4" class="muted">Nenhum período armado registrado nesta operação.</td></tr>'
    charts = "".join(f'<section class="card"><h3>{e(data["series"][m]["label"])} <span>{e(data["series"][m]["unit"])} · faixa = mínimo e máximo a cada 10 s; linha = média</span></h3>{timeline_chart(data["series"][m], zone)}</section>'
                     for m in CHARTS if m in data["series"] and (m != "depth" or depth.get("available")))
    groups = grouped(data["events"])
    def moment(g):
        return e(when(g["first"], zone)) + (f'<small>até {e(when(g["last"], zone))}</small>' if g["count"] > 1 else "")
    event_rows = "".join(f'<tr><td>{moment(g)}</td><td><span class="sev s{e(str(g["severity"]))}">{e(SEVERITY.get(str(g["severity"]), str(g["severity"])))}</span></td><td>{e(g["text"])}</td><td class="count">{g["count"]}×</td></tr>'
                         for g in groups[:150]) or '<tr><td colspan="4" class="muted">Nenhum erro ou aviso registrado.</td></tr>'
    more = f'<p class="muted">Mostrando 150 de {len(groups)} tipos de mensagem; todas as ocorrências estão em ocorrencias.csv.</p>' if len(groups) > 150 else ""
    place = " · ".join(filter(None, [operation["location"], operation["client"]]))
    period = f'{when(operation["started_at"], zone)} → {when(operation["ended_at"], zone) if operation["ended_at"] else "em andamento"}'
    zone_name = "UTC" if zone == timezone.utc else datetime.now(zone).strftime("%Z") or "horário local"
    return f'''<!doctype html><html lang="pt-BR"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(operation["name"])} · Bravo Viewer</title>
<style>
:root{{--ink:#173a47;--muted:#5c7781;--line:#dbe7ea;--accent:#217987;--soft:#f3f8f9}}
*{{box-sizing:border-box}}body{{margin:0;background:#eef4f5;color:var(--ink);font:14px/1.5 "Segoe UI",system-ui,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:32px 22px 48px}}
header{{display:flex;align-items:center;gap:20px;background:linear-gradient(120deg,#0f3442,#1d5566);color:#fff;border-radius:22px;padding:26px 30px;margin-bottom:22px}}
header img{{width:96px;background:#fff;border-radius:14px;padding:8px}}header h1{{margin:0;font-size:28px}}header p{{margin:4px 0 0;color:#cfe6ea}}
.eyebrow{{font-size:11px;letter-spacing:2px;color:#9fd3db;font-weight:700}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}}
.kpi,.peak,.card{{background:#fff;border:1px solid var(--line);border-radius:16px;box-shadow:0 8px 22px -18px #173a4766}}
.kpi{{padding:16px 18px}}.kpi small,.peak small{{display:block;color:var(--muted);font-size:12px}}.kpi strong{{font-size:22px}}
h2{{font-size:18px;margin:28px 0 12px}}h3{{margin:0 0 10px;font-size:15px}}h3 span{{color:var(--muted);font-weight:400;font-size:12px}}
.peaks{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.peak{{padding:14px 16px}}.peak strong{{display:block;font-size:20px;color:var(--accent)}}.peak span{{color:var(--muted);font-size:12px}}
.card{{padding:18px;margin-bottom:14px}}svg{{width:100%;height:auto;display:block}}svg text{{font:12px "Segoe UI",system-ui;fill:#5c7781}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:16px;overflow:hidden;border:1px solid var(--line)}}th,td{{padding:10px 14px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}}
th{{background:var(--soft);font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted)}}tr:last-child td{{border-bottom:0}}
.sev{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;background:#fff1d6;color:#8a5a00}}.s0,.s1,.s2,.s3{{background:#fde2e2;color:#9b1c1c}}
.muted{{color:var(--muted)}}td small{{display:block;color:var(--muted);font-size:12px}}.count{{font-variant-numeric:tabular-nums;color:var(--muted);white-space:nowrap}}footer{{margin-top:34px;color:var(--muted);font-size:12px;display:flex;align-items:center;gap:10px}}footer img{{width:30px}}
.notes{{background:#fff;border:1px solid var(--line);border-radius:16px;padding:14px 18px;white-space:pre-wrap}}
@media(max-width:800px){{.kpis,.peaks{{grid-template-columns:repeat(2,1fr)}}header{{flex-direction:column;align-items:flex-start}}}}
@media print{{body{{background:#fff}}header{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}}}
</style>
<svg width="0" height="0" style="position:absolute"><defs><linearGradient id="bv-area" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#217987" stop-opacity=".3"/><stop offset="1" stop-color="#217987" stop-opacity=".08"/></linearGradient></defs></svg>
<main>
<header><img src="assets/brs.png" alt="BRS"><div><div class="eyebrow">RELATÓRIO DA OPERAÇÃO · {e(store.config()["equipment"])}</div><h1>{e(operation["name"])}</h1><p>{e(place or "Local não informado")} · {e(period)}</p></div></header>
<div class="kpis">{kpis}</div>
<h2>Picos registrados</h2><div class="peaks">{peak_rows or '<p class="muted">Sem medições de alimentação nesta operação.</p>'}</div>
<h2>Mergulhos</h2><table><thead><tr><th>#</th><th>Início</th><th>Fim</th><th>Tempo armado</th></tr></thead><tbody>{dive_rows}</tbody></table>
<h2>Gráficos</h2>{charts or '<p class="muted">Sem séries de alimentação ou profundidade nesta operação.</p>'}
<h2>Erros e avisos <span class="muted" style="font-size:13px;font-weight:400">{len(data["events"])} no total, mensagens iguais agrupadas</span></h2><table><thead><tr><th>Horário</th><th>Nível</th><th>Mensagem</th><th>Vezes</th></tr></thead><tbody>{event_rows}</tbody></table>{more}
{f'<h2>Observações</h2><div class="notes">{e(operation["notes"])}</div>' if operation["notes"] else ""}
<footer><img src="assets/brand.png" alt="Reikjv">Bravo Viewer {e(__version__)} · desenvolvido por Reikjv · horários em {e(zone_name)} · tempo armado inclui bancada; lacunas sem telemetria não são contadas.</footer>
</main></html>'''


def build(store, operation, archive, root, zone, chart_svg, write_csv, utc, check, include_originals, advance):
    """Escreve no ZIP o relatório visual e os arquivos úteis da operação."""
    data = operation_data(store, operation)
    advance(0, "Montando o relatório da operação")
    archive.writestr(root + "relatorio.html", render(store, operation, data, zone, chart_svg))
    web = Path(__file__).resolve().parents[1] / "web"
    for name in ("brs.png", "brand.png"):
        archive.write(web / name, root + "assets/" + name)
    archive.writestr(root + "operacao.json", dumps({k: operation[k] for k in ("id", "name", "location", "client", "notes", "started_at", "ended_at", "summary")}) + "\n")
    write_csv(archive, root + "mergulhos.csv", ["Mergulho", "Início UTC", "Fim UTC", "Tempo armado s"],
              ([i, utc(d["start"]), utc(d["end"]), round(d["observed_seconds"])] for i, d in enumerate(data["dives"], 1)), check)
    write_csv(archive, root + "ocorrencias.csv", ["Data UTC", "Nível", "Tipo", "Mensagem"],
              ([utc(ev["ts"]), SEVERITY.get(str(ev["severity"]), ev["severity"]), ev["type"], ev["text"]] for ev in data["events"]), check)
    for metric, series in data["series"].items():
        write_csv(archive, root + f"graficos/{metric}.csv", ["Início UTC", "Mínimo", "Máximo", "Média", "Unidade"],
                  ([utc(p["t"]), p["min"], p["max"], p["avg"], series["unit"]] for p in series["points"]), check)
    copied = 0
    if include_originals:
        for file in data["files"]:
            if file["kind"] == "live":
                continue
            path = Path(store.file(file["id"])["path"] or "")
            if path.is_file() and store.readable_original(path):
                check(); advance(0, f"Copiando log: {file['name']}")
                with path.open("rb") as source, archive.open(root + "logs/" + file["name"], "w", force_zip64=True) as output:
                    while chunk := source.read(1048576):
                        check(); output.write(chunk); advance(len(chunk) // 200)
                copied += 1
    archive.writestr(root + "LEIA-ME.txt", "Bravo Viewer · operação\n\nAbra relatorio.html no navegador.\n"
                     "mergulhos.csv: períodos armados.\nocorrencias.csv: erros e avisos.\ngraficos/: séries de 10 s (mínimo, máximo e média).\n"
                     "logs/: logs de voo originais (.tlog/.bin), para análise completa em outros programas.\nDatas dos CSV em UTC.\n")
    return {"dives": len(data["dives"]), "events": len(data["events"]), "logs": copied}
