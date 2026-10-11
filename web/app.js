"use strict";
const $ = id => document.getElementById(id);
const base = new URL(location.href);
if (!base.pathname.endsWith("/")) base.pathname = base.pathname.replace(/[^/]*$/, "");
base.search = ""; base.hash = "";
const endpoint = path => new URL(path, base).href;
const titles = {
  overview: ["Visão geral", "Horas, operações, energia e ocorrências do equipamento."],
  operations: ["Operações", "Operação atual, encerramento com ZIP e histórico das operações anteriores."],
  sessions: ["Sessões", "Histórico por arquivo ou coleta, períodos armados e observações."],
  explorer: ["Inventário de dados", "Todos os tipos e campos disponíveis nos registros preservados."],
  charts: ["Energia e gráficos", "Tensão, corrente, consumo, energia e estado armado por origem."],
  events: ["Ocorrências", "Alertas, eventos e mensagens de diagnóstico em contexto."],
  files: ["Arquivos e exportação", "Importação, originais preservados e inventário dos arquivos BlueOS."],
  capabilities: ["Dados e possibilidades", "Recursos desta versão, dados identificados e possibilidades futuras."],
  settings: ["Configuração", "Identificação do equipamento e coleta automática."],
};
let files = [], streams = [], active = "overview", recordOffset = 0, eventOffset = 0;
let recordData = null, focusedField = null, chartData = null, plotted = [];
let completedJobs = new Set(), refreshing = false, chartRequest = 0;
let configDirty = false;
let initialized = false;
let exportJob = null, closeJob = null, operationsData = null, opDirty = false;
const opExportJobs = new Set();
let chartAbort = null;
const loadedTabs = new Set(), loadingTabs = new Set();
const chartIndicators = {
  SYS_STATUS: {voltage_battery: "Tensão da alimentação", current_battery: "Corrente total"},
  BATTERY_STATUS: {current_consumed: "Consumo acumulado", energy_consumed: "Energia acumulada"},
  BAT: {Volt: "Tensão da alimentação", Curr: "Corrente total", CurrTot: "Consumo acumulado", EnrgTot: "Energia acumulada"},
  HEARTBEAT: {_armed: "Estado armado (0 / 1)"},
};
const fmt = (value, digits = 2) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("pt-BR", {maximumFractionDigits: digits}) : "—";
const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const bytes = value => value >= 1073741824 ? fmt(value / 1073741824) + " GB" : value >= 1048576 ? fmt(value / 1048576) + " MB" : fmt(value / 1024) + " KB";
const duration = value => typeof value === "number" ? `${Math.floor(value / 3600)} h ${Math.floor(value % 3600 / 60)} min` : "—";
const date = ts => ts ? new Date(ts * 1000).toLocaleString("pt-BR") : "Sem data válida";
const plural = (count, one, many) => `${fmt(count, 0)} ${count === 1 ? one : many}`;
let busyCount = 0, noticeTimer = 0, revealChart = false, resizeFrame = 0;
function busy(delta) {
  busyCount = Math.max(0, busyCount + delta);
  document.body.classList.toggle("busy", busyCount > 0);
}
function setValue(id, text) {
  const element = $(id);
  if (element.textContent === text) return;
  element.textContent = text;
  element.classList.remove("updated"); void element.offsetWidth; element.classList.add("updated");
}
function flatten(value, prefix = "", out = {}) {
  if (Array.isArray(value)) value.forEach((v, i) => flatten(v, `${prefix}[${i}]`, out));
  else if (value !== null && typeof value === "object") Object.entries(value).forEach(([k, v]) => flatten(v, prefix ? `${prefix}.${k}` : k, out));
  else if (prefix) out[prefix] = value;
  return out;
}
async function api(path, options) {
  const tracked = path !== "status";
  if (tracked) busy(1);
  try {
    const response = await fetch(endpoint("api/" + path), options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "A consulta não foi concluída.");
    return data;
  } finally { if (tracked) busy(-1); }
}
function post(path, data) {
  return api(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Reikjv-History": "1"}, body: JSON.stringify(data)});
}
function notice(text, error = false) {
  const element = $("notice");
  element.textContent = text; element.classList.toggle("error", error);
  element.hidden = true; void element.offsetWidth; element.hidden = false;
  clearTimeout(noticeTimer);
  if (!error) noticeTimer = setTimeout(() => { element.hidden = true; }, 7000);
}
function task(fn) { return async (...args) => { try { await fn(...args); } catch (error) { if (error.name !== "AbortError") notice(error.message, true); } }; }
function table(headers, rows, empty = "Nenhum registro disponível.") {
  return `<table><thead><tr>${headers.map(h => `<th>${escape(h)}</th>`).join("")}</tr></thead><tbody>${rows.length ? rows.map(row => `<tr>${row.map(cell => `<td>${cell}</td>`).join("")}</tr>`).join("") : `<tr><td colspan="${headers.length}" class="muted">${escape(empty)}</td></tr>`}</tbody></table>`;
}
function options(id, items, all = false) {
  const select = $(id), previous = select.value;
  select.replaceChildren();
  if (all) select.add(new Option("Todos os arquivos", ""));
  items.forEach(item => select.add(new Option(item.label, item.value)));
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
}
function go(tab) {
  if (!titles[tab]) return;
  const changed = active !== tab;
  active = tab;
  document.querySelectorAll(".page").forEach(page => page.hidden = page.id !== tab);
  document.querySelectorAll("nav button").forEach(button => button.classList.toggle("active", button.dataset.tab === tab));
  $("page-title").textContent = titles[tab][0]; $("page-description").textContent = titles[tab][1];
  if (location.hash.slice(1) !== tab) history.pushState(null, "", "#" + tab);
  document.documentElement.scrollTop = 0;
  if (tab === "charts") requestAnimationFrame(drawChart);
  if (initialized && (changed || !loadedTabs.has(tab))) task(loadActivePage)();
}
async function loadActivePage() {
  const tab = active;
  if (loadingTabs.has(tab)) return;
  loadingTabs.add(tab);
  try {
    if (tab === "operations") await loadOperations();
    else if (tab === "explorer") await loadExplorer();
    else if (tab === "charts") await loadChartStreams();
    else if (tab === "events") await loadEvents();
    else if (tab === "files") await loadInventory();
    else if (tab === "capabilities") await loadCapabilities();
    loadedTabs.add(tab);
  } finally { loadingTabs.delete(tab); }
}
const meters = value => typeof value === "number" ? `${fmt(value, 2)} m` : "—";
function operationName(id) {
  return operationsData?.operations.find(op => op.id === id)?.name || "Fora de operação";
}
function operationStats(op) {
  const s = op.summary, depth = s.depth || {};
  const elapsed = (op.ended_at || Date.now() / 1000) - op.started_at;
  return [
    ["Duração", duration(elapsed)], ["Tempo armado", duration(s.armed_seconds)],
    ["Profundidade máxima", depth.available ? meters(depth.max) : "Sem sensor"],
    ["Profundidade média", depth.available ? meters(depth.mean) : "Sem sensor"],
    ["Sessões", fmt(s.sessions || 0, 0)],
  ].map(([label, value]) => `<div><small>${label}</small><strong>${escape(value)}</strong></div>`).join("");
}
function renderOperations() {
  if (!operationsData) return;
  const ops = operationsData.operations, current = ops.find(op => op.status === "open");
  const closed = ops.filter(op => op.status === "closed"), limit = operationsData.limit;
  if (current) {
    $("current-op-name").textContent = current.name;
    $("current-op-meta").textContent = [current.location, current.client, `desde ${date(current.started_at)}`].filter(Boolean).join(" · ");
    $("current-op-stats").innerHTML = operationStats(current);
    $("op-stats").innerHTML = operationStats(current);
    $("op-started").textContent = "INICIADA EM " + date(current.started_at).toUpperCase();
    if (!opDirty && document.activeElement.closest?.("#op-form") == null) {
      $("op-name").value = current.name; $("op-location").value = current.location;
      $("op-client").value = current.client; $("op-notes").value = current.notes;
    }
  }
  const oldest = closed.at(-1);
  $("op-retention").textContent = `${closed.length} de ${limit} operações encerradas guardadas no ROV` + (closed.length >= limit && oldest ? ` · ao encerrar a atual, “${oldest.name}” será removida` : "");
  $("operations-table").innerHTML = table(["Operação", "Período", "Tempo armado", "Prof. máxima", "Prof. média", "Sessões", "ZIP"], closed.map(op => {
    const depth = op.summary.depth || {};
    const zip = op.bundle_ready
      ? `<a class="button secondary" href="${escape(endpoint("api/bundle?id=" + op.bundle_id))}">↓ Baixar</a><small class="${op.downloaded_at ? "badge-ok" : "badge-warn"}">${op.downloaded_at ? "Baixado" : "Ainda não baixado"}</small>`
      : `<button class="secondary" data-op-export="${op.id}">Gerar ZIP</button>`;
    return [
      `<strong>${escape(op.name)}</strong><small>${escape([op.location, op.client].filter(Boolean).join(" · ") || "Sem local informado")}</small>`,
      `${escape(date(op.started_at))}<small>até ${escape(date(op.ended_at))}</small>`,
      duration(op.summary.armed_seconds),
      depth.available ? meters(depth.max) : '<span class="muted">Sem sensor</span>',
      depth.available ? meters(depth.mean) : '<span class="muted">—</span>',
      fmt(op.summary.sessions || 0, 0), zip,
    ];
  }), "Nenhuma operação encerrada ainda. Encerre a operação atual ao voltar para a base.");
}
async function loadOperations() {
  operationsData = await api("operations");
  renderOperations();
}
function openCloseDialog() {
  const current = operationsData?.operations.find(op => op.status === "open");
  if (!current) return;
  const closed = operationsData.operations.filter(op => op.status === "closed");
  $("close-summary").textContent = `“${$("op-name").value || current.name}” será encerrada agora. A coleta continua na próxima operação, iniciada automaticamente.`;
  $("close-stats").innerHTML = operationStats(current);
  $("close-retention").textContent = closed.length >= operationsData.limit ? `Limite de ${operationsData.limit} operações atingido: “${closed.at(-1).name}” e seus registros serão removidos do ROV.` : `O ROV guarda as últimas ${operationsData.limit} operações encerradas.`;
  $("close-confirm").disabled = false; $("close-download").hidden = true; $("close-bar").hidden = true; $("close-progress").textContent = "";
  $("close-dialog").showModal();
}
function progressBar(id, percent) {
  $(id).hidden = typeof percent !== "number";
  $(id).firstElementChild.style.setProperty("--p", (percent || 0) / 100);
}
function sessionRows(list) {
  return list.map(file => [
    `<strong>${escape(file.name)}</strong><small>${escape(file.kind.toUpperCase())} · ${escape(operationName(file.operation_id))}</small>`,
    fmt(file.summary.records || 0, 0),
    duration(file.summary.observed_seconds),
    duration(file.summary.armed_seconds),
    file.summary.clock_issues?.length ? `<span class="severity warning">${plural(file.summary.clock_issues.length, "descontinuidade", "descontinuidades")}</span>` : '<span class="severity ok">Sem saltos de relógio</span>',
    `<button class="secondary" data-session="${file.id}">Detalhes →</button>`,
  ]);
}
function renderOverview(data) {
  const stat = data.statistics;
  setValue("runtime", duration(stat.STAT_RUNTIME));
  setValue("armed-total", duration(stat.STAT_FLTTIME));
  setValue("bootcount", fmt(stat.STAT_BOOTCNT, 0));
  setValue("record-count", fmt(data.records, 0));
  $("record-detail").textContent = `${plural(data.files, "fonte", "fontes")} · ${bytes(data.bytes)} de originais`;
  $("clock-quality").textContent = data.clock_issues ? plural(data.clock_issues, "descontinuidade", "descontinuidades") : "Sem saltos identificados";
  $("database-size").textContent = `Base de dados: ${bytes(data.database_bytes)} · Originais: ${bytes(data.bytes)}. Crescimento depende da taxa e dos tipos registrados.`;
  const latest = files.find(file => file.summary.metrics && Object.keys(file.summary.metrics).some(key => key.includes("voltage_battery") || key === "BAT.Volt"));
  const metrics = latest?.summary.metrics || {};
  const voltage = metrics["SYS_STATUS.voltage_battery"] || metrics["BAT.Volt"];
  const current = metrics["SYS_STATUS.current_battery"] || metrics["BAT.Curr"];
  const consumed = metrics["BATTERY_STATUS.current_consumed"] || metrics["BAT.CurrTot"];
  const energy = metrics["BATTERY_STATUS.energy_consumed"] || metrics["BAT.EnrgTot"];
  setValue("voltage", voltage ? `${fmt(voltage.min, 3)} ${voltage.unit}` : "—");
  setValue("current", current ? `${fmt(current.max)} ${current.unit}` : "—");
  setValue("consumption", consumed ? `${fmt(consumed.last, 3)} ${consumed.unit}` : "—");
  setValue("energy", energy ? `${fmt(energy.last, 3)} ${energy.unit}` : "—");
  $("energy-source").textContent = latest ? `Fonte: ${latest.name}` : "Importe um log para consultar a alimentação.";
  $("recent-sessions").innerHTML = table(["Sessão / fonte", "Registros", "Tempo observado", "Tempo armado", "Continuidade", ""], sessionRows(files.slice(0, 5)));
  $("sessions-table").innerHTML = table(["Sessão / fonte", "Registros", "Tempo observado", "Tempo armado", "Continuidade", ""], sessionRows(files));
  $("empty-state").hidden = files.length > 0;
  $("files-size").textContent = `${plural(files.length, "fonte", "fontes")} · ${bytes(data.bytes)} de originais preservados`;
}
function renderFiles() {
  const search = $("files-search").value.toLowerCase();
  const rows = files.filter(file => `${file.name} ${file.source_key}`.toLowerCase().includes(search)).map(file => [
    `<div class="file-name"><strong>${escape(file.name)}</strong><small>${escape(file.source_key.startsWith("blueos:") ? file.source_key.slice(file.source_key.indexOf(":", 7) + 1) : file.kind === "live" ? "Coleta amostrada" : "Importação local")}</small></div>`,
    escape(file.kind.toUpperCase()), bytes(file.size || 0), fmt(file.summary.records || 0, 0), fmt(file.stream_count, 0), date(file.imported_at),
    file.kind === "live" ? '<span class="muted">Exportar pelo inventário</span>' : `<a class="button secondary" href="${escape(endpoint(`api/original?file=${file.id}`))}">↓ Original</a>`,
  ]);
  $("files-table").innerHTML = table(["Arquivo", "Formato", "Tamanho", "Registros", "Tipos / origens", "Importado", "Download"], rows);
}
function renderSession() {
  const file = files.find(file => String(file.id) === $("session-select").value);
  if (!file) { $("session-detail").textContent = "Nenhuma sessão importada."; return; }
  const s = file.summary;
  $("session-note").value = file.note;
  const periods = [...(s.armed_periods || []), ...(s.active_armed_period ? [{...s.active_armed_period, reason: "Em andamento"}] : [])];
  const periodRows = periods.map(period => [date(period.start), date(period.end), duration(period.observed_seconds), escape(period.reason)]);
  $("session-detail").innerHTML = `<div class="measurements"><div><small>Tempo observado por heartbeats</small><strong>${duration(s.observed_seconds)}</strong></div><div><small>Tempo armado observado</small><strong>${duration(s.armed_seconds)}</strong></div><div><small>Lacunas</small><strong>${fmt(s.gaps || 0, 0)}</strong></div></div><p class="hint">Tempos observados incluem apenas intervalos contínuos de até 5 segundos entre heartbeats. A duração total de uma operação não é presumida a partir do início e fim do arquivo.</p><h2>Períodos armados</h2>${table(["Início", "Fim", "Tempo observado", "Encerramento"], periodRows, "Nenhum período armado identificado nesta sessão.")}<details><summary>Resumo completo de métricas e qualidade</summary><pre>${escape(JSON.stringify(s, null, 2))}</pre></details>`;
}
function streamsFor(file) { return streams.filter(stream => !file || String(stream.file_id) === file); }
function streamOptions(id, file, group = "") {
  const items = streamsFor(file).filter(stream => !group || stream.category === group);
  options(id, items.map(stream => ({value: stream.id, label: `${stream.type} · ${stream.origin}${file ? "" : " · " + stream.file_name}`})));
  return items;
}
async function loadExplorer(reset = true) {
  if (reset) { recordOffset = 0; focusedField = null; }
  streamOptions("explorer-stream", $("explorer-file").value, $("explorer-group").value);
  await loadRecords();
}
async function loadRecords() {
  const stream = $("explorer-stream").value;
  if (!stream) { $("records-table").innerHTML = table(["Registros"], []); $("field-list").replaceChildren(); return; }
  recordData = await api(`records?stream=${stream}&offset=${recordOffset}&limit=100`);
  $("stream-info").textContent = `${recordData.stream.type} · origem ${recordData.stream.origin} · ${plural(recordData.total, "registro", "registros")} · ${plural(recordData.stream.fields.length, "campo", "campos")}`;
  $("export-csv").href = endpoint(`api/export?stream=${stream}&format=csv`);
  $("export-json").href = endpoint(`api/export?stream=${stream}&format=jsonl`);
  $("records-prev").disabled = recordOffset === 0;
  $("records-next").disabled = recordOffset + recordData.rows.length >= recordData.total;
  $("records-page").textContent = `${fmt(recordOffset + (recordData.rows.length ? 1 : 0), 0)}–${fmt(recordOffset + recordData.rows.length, 0)} de ${fmt(recordData.total, 0)}`;
  renderRecords();
}
function renderRecords() {
  if (!recordData) return;
  const search = $("field-search").value.toLowerCase();
  const matched = recordData.stream.fields.filter(field => field.toLowerCase().includes(search));
  const fields = focusedField ? [focusedField] : matched.slice(0, 12);
  $("field-list").replaceChildren();
  matched.forEach(field => {
    const button = document.createElement("button"); button.textContent = field;
    button.title = focusedField === field ? "Mostrar todas as colunas" : "Mostrar somente esta coluna";
    button.classList.toggle("selected", focusedField === field);
    button.onclick = () => { focusedField = focusedField === field ? null : field; renderRecords(); };
    $("field-list").append(button);
  });
  const rows = recordData.rows.map(row => {
    const values = flatten(row.data);
    return [fmt(row.seq, 0), date(row.ts), String(row.segment), ...fields.map(field => escape(typeof values[field] === "number" ? fmt(values[field], 6) : values[field] === null ? "Indisponível" : values[field] ?? "—"))];
  });
  $("records-table").innerHTML = table(["Registro", "Data", "Trecho", ...fields], rows);
}
async function loadChartStreams() {
  const items = streamsFor($("chart-file").value).filter(stream => chartIndicators[stream.type] && (stream.origin === "1:1" || stream.kind === "bin"));
  const order = {SYS_STATUS: 0, BAT: 1, BATTERY_STATUS: 2, HEARTBEAT: 3};
  items.sort((a, b) => order[a.type] - order[b.type]);
  options("chart-stream", items.map(stream => ({value: stream.id, label: `${stream.type} · ${stream.origin} · ${stream.file_name}`})));
  await loadChartFields();
}
async function loadChartFields() {
  const stream = streams.find(stream => String(stream.id) === $("chart-stream").value);
  const indicators = chartIndicators[stream?.type] || {};
  options("chart-field", Object.entries(indicators).filter(([field]) => field === "_armed" || stream.fields.includes(field)).map(([field, label]) => ({value: field, label})));
  await loadChart();
}
async function loadChart() {
  chartAbort?.abort(); chartAbort = new AbortController();
  const request = ++chartRequest;
  const stream = $("chart-stream").value, field = $("chart-field").value;
  if (!stream || !field) { chartData = null; $("chart-title").textContent = "Nenhum indicador disponível nesta fonte"; $("chart-stats").replaceChildren(); $("chart-unit").textContent = ""; $("chart-info").textContent = "Importe logs com alimentação ou estado do autopiloto."; drawChart(); return; }
  const data = await api(`series?stream=${stream}&field=${encodeURIComponent(field)}&normalized=${$("chart-normalized").value}`, {signal: chartAbort.signal});
  if (request !== chartRequest) return;
  chartData = data;
  $("chart-title").textContent = `${chartIndicators[data.type]?.[field] || field} · ${data.type}`;
  $("chart-unit").textContent = data.unit || "SEM UNIDADE DEFINIDA";
  $("chart-info").textContent = `${plural(data.total, "registro", "registros")} · ${plural(data.points.length, "ponto", "pontos")} na visualização · ${plural(data.invalid_samples, "valor não numérico ou desconhecido", "valores não numéricos ou desconhecidos")}. Passe o cursor sobre o gráfico para consultar cada ponto.`;
  const vals = data.points.filter(point => typeof point.value === "number").map(point => point.value);
  $("chart-stats").innerHTML = vals.length ? `<div><small>Mínimo</small><strong>${fmt(Math.min(...vals), 4)} ${escape(data.unit)}</strong></div><div><small>Máximo</small><strong>${fmt(Math.max(...vals), 4)} ${escape(data.unit)}</strong></div><div><small>Último ponto exibido</small><strong>${fmt(vals.at(-1), 4)} ${escape(data.unit)}</strong></div>` : "";
  revealChart = true;
  drawChart();
}
function drawChart() {
  const canvas = $("plot"), rect = canvas.getBoundingClientRect();
  if (!rect.width) return;
  const ratio = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = Math.round(rect.width * ratio); canvas.height = Math.round(rect.height * ratio);
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
  const w = rect.width, h = rect.height, points = chartData?.points || [];
  const valid = points.filter(point => typeof point.value === "number" && Number.isFinite(point.value));
  $("chart-empty").hidden = valid.length > 0;
  $("chart-box").classList.remove("hover");
  plotted = [];
  if (!valid.length) return;
  let lo = Infinity, hi = -Infinity;
  valid.forEach(point => { lo = Math.min(lo, point.value); hi = Math.max(hi, point.value); });
  const pad = Math.max((hi - lo) * .12, Math.abs(hi) * .002, .001); lo -= pad; hi += pad;
  const left = 70, right = w - 22, top = 20, bottom = h - 46;
  const minSeq = points[0].seq, maxSeq = points.at(-1).seq;
  const x = seq => left + (seq - minSeq) / (maxSeq - minSeq || 1) * (right - left);
  const y = value => top + (hi - value) / (hi - lo) * (bottom - top);
  ctx.font = '10.5px Inter,"Segoe UI",sans-serif';
  ctx.setLineDash([3, 5]); ctx.lineWidth = 1;
  for (let index = 0; index <= 4; index++) {
    const lineY = Math.round(top + index * (bottom - top) / 4) + .5;
    ctx.strokeStyle = "#1d3945"; ctx.beginPath(); ctx.moveTo(left, lineY); ctx.lineTo(right, lineY); ctx.stroke();
    ctx.fillStyle = "#7f99a6"; ctx.textAlign = "right"; ctx.fillText(fmt(hi - index * (hi - lo) / 4, 3), left - 12, lineY + 4);
  }
  ctx.setLineDash([]);
  ctx.fillStyle = "#7f99a6"; ctx.textAlign = "left"; ctx.fillText(`#${fmt(minSeq, 0)} · ${date(points[0].timestamp)}`, left, h - 17);
  ctx.textAlign = "right"; ctx.fillText(`#${fmt(maxSeq, 0)} · ${date(points.at(-1).timestamp)}`, right, h - 17);
  const step = chartData.field === "_armed";
  const segments = [];
  let current = null, previous = null;
  points.forEach(point => {
    if (typeof point.value !== "number" || !Number.isFinite(point.value)) { previous = null; return; }
    if (!previous || previous.segment !== point.segment) { current = []; segments.push(current); }
    const px = x(point.seq), py = y(point.value);
    if (step && current.length) current.push([px, current.at(-1)[1]]);
    current.push([px, py]);
    plotted.push({...point, x: px, y: py}); previous = point;
  });
  const fill = ctx.createLinearGradient(0, top, 0, bottom);
  fill.addColorStop(0, "rgba(95,180,192,.28)"); fill.addColorStop(1, "rgba(95,180,192,0)");
  const stroke = ctx.createLinearGradient(left, 0, right, 0);
  stroke.addColorStop(0, "#4fa3b1"); stroke.addColorStop(1, "#a6e1e9");
  ctx.fillStyle = fill;
  segments.forEach(segment => {
    ctx.beginPath(); ctx.moveTo(segment[0][0], bottom);
    segment.forEach(([px, py]) => ctx.lineTo(px, py));
    ctx.lineTo(segment.at(-1)[0], bottom); ctx.closePath(); ctx.fill();
  });
  ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.lineCap = "round"; ctx.strokeStyle = stroke;
  segments.forEach(segment => {
    ctx.beginPath(); segment.forEach(([px, py], index) => index ? ctx.lineTo(px, py) : ctx.moveTo(px, py));
    ctx.stroke();
    if (segment.length === 1) { ctx.beginPath(); ctx.arc(segment[0][0], segment[0][1], 3.5, 0, Math.PI * 2); ctx.fillStyle = "#a6e1e9"; ctx.fill(); }
  });
  if (revealChart) {
    revealChart = false;
    canvas.classList.remove("reveal"); void canvas.offsetWidth; canvas.classList.add("reveal");
  }
}
function chartHover(event) {
  const box = $("chart-box");
  if (!plotted.length) { box.classList.remove("hover"); return; }
  const px = event.clientX - $("plot").getBoundingClientRect().left;
  let low = 0, high = plotted.length - 1;
  while (low < high) { const mid = (low + high) >> 1; if (plotted[mid].x < px) low = mid + 1; else high = mid; }
  const point = low > 0 && Math.abs(plotted[low - 1].x - px) < Math.abs(plotted[low].x - px) ? plotted[low - 1] : plotted[low];
  const tip = $("chart-tip");
  $("chart-cross").style.transform = `translateX(${point.x}px)`;
  $("chart-dot").style.transform = `translate(${point.x}px,${point.y}px)`;
  tip.innerHTML = `<strong>${fmt(point.value, 6)} ${escape(chartData.unit)}</strong><span>${escape(date(point.timestamp))} · registro ${fmt(point.seq, 0)}</span>`;
  const flip = point.x > box.clientWidth - tip.offsetWidth - 30;
  const tipY = Math.max(8, Math.min(point.y - tip.offsetHeight - 14, box.clientHeight - tip.offsetHeight - 8));
  tip.style.transform = `translate(${flip ? point.x - tip.offsetWidth - 14 : point.x + 14}px,${tipY}px)`;
  box.classList.add("hover");
}
async function loadEvents(reset = true) {
  if (reset) eventOffset = 0;
  const data = await api(`events?file=${$("event-file").value || 0}&search=${encodeURIComponent($("event-search").value)}&offset=${eventOffset}&limit=100`);
  const rows = data.rows.map(row => [date(row.ts), `<span class="severity ${["0","1","2","3","4","ERROR","WARN","WARNING","CRITICAL"].includes(row.severity) ? "warning" : ""}">${escape(row.severity)}</span>`, escape(row.type), `<div class="event-text">${escape(row.text)}</div>`, escape(row.file_name)]);
  $("events-table").innerHTML = table(["Data", "Nível", "Tipo", "Mensagem", "Fonte"], rows);
  $("events-page").textContent = `${fmt(eventOffset + (data.rows.length ? 1 : 0), 0)}–${fmt(eventOffset + data.rows.length, 0)} de ${fmt(data.total, 0)}`;
  $("events-prev").disabled = eventOffset === 0; $("events-next").disabled = eventOffset + data.rows.length >= data.total;
}
async function loadStatus() {
  const data = await api("status");
  $("version").textContent = data.version;
  $("equipment-name").textContent = data.config.equipment;
  $("equipment-description").textContent = `BlueOS ${data.config.host} · Coleta a cada ${data.config.poll_seconds} s`;
  $("connection").classList.toggle("connected", data.collection.connected);
  $("connection").textContent = data.collection.connected ? "Coleta ativa" : data.config.collect ? "Aguardando BlueOS" : "Coleta pausada";
  $("connection").title = data.collection.error || "Estado da coleta de telemetria";
  if (!configDirty && document.activeElement.closest?.("#config-form") == null) {
    $("config-equipment").value = data.config.equipment;
    $("config-host").value = data.config.host;
    $("config-collect").checked = data.config.collect;
    $("config-poll").value = String(data.config.poll_seconds);
  }
  const running = data.jobs.filter(job => ["queued", "running"].includes(job.status));
  const bundle = data.jobs.find(job => job.id === exportJob);
  if (bundle) {
    $("export-progress").textContent = bundle.status === "done" ? "Pronto" : `${bundle.progress}${typeof bundle.percent === "number" ? ` · ${bundle.percent}%` : ""}`;
    progressBar("export-bar", bundle.status === "done" ? 100 : bundle.percent);
    $("export-start").disabled = ["queued", "running"].includes(bundle.status);
    if (bundle.status === "done") {
      $("export-download").href = endpoint(bundle.result.download);
      $("export-download").hidden = false;
      $("export-download").title = `${bytes(bundle.result.size)} · relatório e dados organizados`;
    }
  }
  $("job").hidden = !running.length;
  if (running.length) {
    const job = running.at(-1);
    $("job-text").textContent = `${job.progress}${typeof job.percent === "number" ? ` · ${job.percent}%` : ""}`;
    progressBar("job-bar", job.percent);
  }
  const closing = data.jobs.find(job => job.id === closeJob);
  if (closing) {
    $("close-progress").textContent = closing.status === "done" ? "Operação encerrada" : closing.status === "error" ? closing.progress : `${closing.progress} · ${closing.percent || 0}%`;
    progressBar("close-bar", closing.status === "error" ? undefined : closing.status === "done" ? 100 : closing.percent || 0);
    if (closing.status === "done") { $("close-download").href = endpoint(closing.result.download); $("close-download").hidden = false; }
  }
  let changed = false;
  data.jobs.filter(job => ["done", "error"].includes(job.status)).forEach(job => {
    if (!completedJobs.has(job.id)) {
      completedJobs.add(job.id); changed = job.action !== "bundle" || changed;
      if (job.status === "error") notice(job.progress, true);
      else if (job.action === "sync") notice(`Busca concluída: ${plural(job.result.imported, "arquivo importado", "arquivos importados")}, ${fmt(job.result.skipped, 0)} já conhecidos ou vazios e ${fmt(job.result.errors.length, 0)} pastas/arquivos indisponíveis.`);
      else if (job.action === "bundle" && opExportJobs.has(job.id)) { opExportJobs.delete(job.id); notice("ZIP da operação pronto para download."); task(loadOperations)(); }
      else if (job.action === "bundle") notice("Exportação pronta. Use Baixar ZIP para salvar o relatório e os dados.");
      else if (job.action === "close_operation") notice(`Operação encerrada. ZIP pronto${job.result.removed.length ? `; ${job.result.removed.map(op => op.name).join(", ")} removida do ROV pelo limite` : ""}. A nova operação já está registrando.${job.result.sync_error ? " Atenção: o BlueOS não respondeu à busca final; logs ainda não importados não entraram nesta operação." : ""}`, Boolean(job.result.sync_error));
      else notice(job.result.duplicate ? "Arquivo já conhecido; o histórico foi preservado sem duplicação." : `Importação concluída: ${plural(job.result.records, "registro", "registros")}.`);
    }
  });
  if (changed && !refreshing) await refresh();
  return data;
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    [files, streams, operationsData] = await Promise.all([api("files"), api("streams"), api("operations")]);
    renderOperations();
    const overview = await api("overview");
    renderOverview(overview); renderFiles();
    const fileOptions = files.map(file => ({value: file.id, label: file.name}));
    ["explorer-file", "event-file"].forEach(id => options(id, fileOptions, true));
    options("chart-file", fileOptions);
    options("session-select", fileOptions);
    const groups = [...new Set(streams.map(stream => stream.category))].sort();
    const groupSelect = $("explorer-group"), prev = groupSelect.value;
    groupSelect.replaceChildren(new Option("Todos os grupos", ""));
    groups.forEach(group => groupSelect.add(new Option(group, group)));
    if (groups.includes(prev)) groupSelect.value = prev;
    renderSession(); initialized = true; loadedTabs.clear(); await loadActivePage();
    $("updated").textContent = "Atualizado às " + new Date().toLocaleTimeString("pt-BR");
  } finally { refreshing = false; }
}
async function loadCapabilities() {
  const data = await api("capabilities");
  $("capability-note").textContent = data.note;
  $("capabilities-table").innerHTML = table(["Informação", "Nesta versão", "Dados no histórico", "Fonte / condição"], data.rows.map(row => [
    `<strong>${escape(row.name)}</strong>`,
    `<span class="severity ${row.scope.startsWith("Disponível") ? "" : "warning"}">${escape(row.scope)}</span>`,
    `${escape(row.data)}${row.sources.length ? `<small class="source-types">${row.sources.map(escape).join(", ")}</small>` : ""}`,
    escape(row.condition),
  ]));
}
async function loadInventory() {
  const inventory = await api("inventory");
  $("inventory-info").textContent = inventory.captured_at ? `${inventory.host} · consulta em ${date(inventory.captured_at)} · ${plural(inventory.files.length, "arquivo encontrado", "arquivos encontrados")} · ${plural(inventory.errors.length, "caminho indisponível", "caminhos indisponíveis")}` : "Use Buscar no BlueOS para consultar os arquivos, inclusive os vazios.";
  $("remote-files-table").innerHTML = table(["Caminho", "Formato", "Tamanho", "Situação"], inventory.files.map(item => [
    escape(item.path), escape(item.extension), bytes(item.size || 0),
    item.size === 0 ? '<span class="severity warning">Vazio · sem registros</span>' : files.some(file => file.source_key === `blueos:${inventory.host}:${item.path}`) ? "Importado · cópia do momento" : "Ainda não importado",
  ]));
}
document.querySelectorAll("[data-tab]").forEach(button => button.onclick = () => go(button.dataset.tab));
document.querySelectorAll("[data-goto]").forEach(button => button.onclick = event => { event.preventDefault(); go(button.dataset.goto); });
document.addEventListener("click", task(async event => {
  const exporter = event.target.closest("[data-op-export]");
  if (!exporter) return;
  exporter.disabled = true; exporter.textContent = "Gerando…";
  const job = await post("operation-export", {id: Number(exporter.dataset.opExport)});
  opExportJobs.add(job.id); await loadStatus();
}));
document.addEventListener("click", event => {
  const button = event.target.closest("[data-session]");
  if (button) { $("session-select").value = button.dataset.session; renderSession(); go("sessions"); }
});
$("refresh").onclick = task(refresh);
$("notice").onclick = () => { $("notice").hidden = true; };
$("plot").onpointermove = chartHover;
$("plot").onpointerleave = () => $("chart-box").classList.remove("hover");
$("files-search").oninput = renderFiles;
$("field-search").oninput = () => { focusedField = null; renderRecords(); };
$("session-select").onchange = renderSession;
$("save-note").onclick = task(async () => { await post("note", {file: Number($("session-select").value), note: $("session-note").value}); notice("Observação salva."); files.find(file => String(file.id) === $("session-select").value).note = $("session-note").value; });
$("explorer-file").onchange = $("explorer-group").onchange = task(() => loadExplorer());
$("explorer-stream").onchange = task(async () => { recordOffset = 0; focusedField = null; await loadRecords(); });
$("records-prev").onclick = task(async () => { recordOffset = Math.max(0, recordOffset - 100); await loadRecords(); });
$("records-next").onclick = task(async () => { recordOffset += 100; await loadRecords(); });
$("chart-file").onchange = task(loadChartStreams);
$("chart-stream").onchange = task(loadChartFields);
$("chart-field").onchange = $("chart-normalized").onchange = task(loadChart);
$("event-file").onchange = $("search-events").onclick = task(() => loadEvents());
$("events-prev").onclick = task(async () => { eventOffset = Math.max(0, eventOffset - 100); await loadEvents(false); });
$("events-next").onclick = task(async () => { eventOffset += 100; await loadEvents(false); });
$("sync").onclick = task(async () => { await post("sync", {}); await loadStatus(); });
$("upload").onchange = task(async () => {
  for (const file of $("upload").files) {
    await api("upload?name=" + encodeURIComponent(file.name), {method: "POST", headers: {"X-Reikjv-History": "1", "Content-Type": "application/octet-stream"}, body: file});
  }
  $("upload").value = ""; await loadStatus();
});
$("config-form").onsubmit = task(async event => {
  event.preventDefault();
  await post("config", {equipment: $("config-equipment").value, host: $("config-host").value, collect: $("config-collect").checked, poll_seconds: Number($("config-poll").value)});
  configDirty = false;
  notice("Configuração salva. A coleta segue o estado escolhido."); await loadStatus();
});
$("config-form").oninput = () => { configDirty = true; };
$("export-summary").href = endpoint("api/export-summary");
const opValues = () => ({name: $("op-name").value, location: $("op-location").value, client: $("op-client").value, notes: $("op-notes").value});
$("op-form").oninput = () => { opDirty = true; };
$("op-form").onsubmit = task(async event => {
  event.preventDefault();
  await post("operation", opValues()); opDirty = false;
  notice("Dados da operação salvos."); await loadOperations();
});
$("close-op").onclick = $("overview-close-op").onclick = openCloseDialog;
$("close-cancel").onclick = () => $("close-dialog").close();
$("close-form").onsubmit = task(async event => {
  event.preventDefault();
  const values = opDirty || active === "operations" ? opValues() : {};
  const job = await post("operation-close", {values, include_data: $("close-data").checked});
  closeJob = job.id; opDirty = false; $("close-confirm").disabled = true;
  $("close-progress").textContent = "Encerrando…"; progressBar("close-bar", 0);
  await loadStatus();
});
$("export-viewer").onclick = () => $("export-dialog").showModal();
$("export-close").onclick = () => $("export-dialog").close();
$("export-form").onsubmit = task(async event => {
  event.preventDefault();
  const context = {scope: $("export-scope").value, tab: active,
    include_data: $("export-data").checked, include_originals: $("export-originals").checked,
    normalized: $("chart-normalized").value === "1", timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    file: 0, stream: 0, field: "", search: "", group: ""};
  if (active === "charts") Object.assign(context, {file: $("chart-file").value, stream: $("chart-stream").value, field: $("chart-field").value});
  if (active === "explorer") Object.assign(context, {file: $("explorer-file").value, stream: $("explorer-stream").value, field: focusedField || "", search: $("field-search").value, group: $("explorer-group").value});
  if (active === "sessions") context.file = $("session-select").value;
  if (active === "operations" && context.scope === "view") Object.assign(context, {scope: "operation", operation: operationsData?.operations.find(op => op.status === "open")?.id || 0});
  if (active === "events") Object.assign(context, {file: $("event-file").value, search: $("event-search").value});
  if (active === "files") context.search = $("files-search").value;
  const job = await post("bundle", context);
  exportJob = job.id; $("export-start").disabled = true; $("export-download").hidden = true;
  $("export-progress").textContent = "Preparando exportação…";
  await loadStatus();
});
window.addEventListener("resize", () => { cancelAnimationFrame(resizeFrame); resizeFrame = requestAnimationFrame(drawChart); });
window.addEventListener("hashchange", () => go(location.hash.slice(1)));
go(titles[location.hash.slice(1)] ? location.hash.slice(1) : "overview");
task(async () => { await loadStatus(); await refresh(); })();
setInterval(task(loadStatus), 3000);
