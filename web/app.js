"use strict";
const $ = id => document.getElementById(id);
const base = new URL(location.href);
if (!base.pathname.endsWith("/")) base.pathname = base.pathname.replace(/[^/]*$/, "");
base.search = ""; base.hash = "";
const endpoint = path => new URL(path, base).href;
const titles = {
  overview: ["Visão geral", "Horas, operações, energia e ocorrências do equipamento."],
  sessions: ["Sessões e operações", "Histórico por fonte, períodos armados e observações do serviço."],
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
let exportJob = null;
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
function flatten(value, prefix = "", out = {}) {
  if (Array.isArray(value)) value.forEach((v, i) => flatten(v, `${prefix}[${i}]`, out));
  else if (value !== null && typeof value === "object") Object.entries(value).forEach(([k, v]) => flatten(v, prefix ? `${prefix}.${k}` : k, out));
  else if (prefix) out[prefix] = value;
  return out;
}
async function api(path, options) {
  const response = await fetch(endpoint("api/" + path), options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "A consulta não foi concluída");
  return data;
}
function post(path, data) {
  return api(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Reikjv-History": "1"}, body: JSON.stringify(data)});
}
function notice(text, error = false) {
  $("notice").textContent = text; $("notice").hidden = false;
  $("notice").style.borderColor = error ? "#845559" : "#59604b";
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
  location.hash = tab;
  if (tab === "charts") requestAnimationFrame(drawChart);
  if (initialized && (changed || !loadedTabs.has(tab))) task(loadActivePage)();
}
async function loadActivePage() {
  const tab = active;
  if (loadingTabs.has(tab)) return;
  loadingTabs.add(tab);
  try {
    if (tab === "explorer") await loadExplorer();
    else if (tab === "charts") await loadChartStreams();
    else if (tab === "events") await loadEvents();
    else if (tab === "files") await loadInventory();
    else if (tab === "capabilities") await loadCapabilities();
    loadedTabs.add(tab);
  } finally { loadingTabs.delete(tab); }
}
function sessionRows(list) {
  return list.map(file => [
    `<strong>${escape(file.name)}</strong><small>${escape(file.kind.toUpperCase())} · ${escape(file.source_key.split(":")[0])}</small>`,
    fmt(file.summary.records || 0, 0),
    duration(file.summary.observed_seconds),
    duration(file.summary.armed_seconds),
    file.summary.clock_issues?.length ? `<span class="severity warning">${file.summary.clock_issues.length} descontinuidades</span>` : '<span class="severity">Consultar qualidade</span>',
    `<button class="secondary" data-session="${file.id}">Detalhes →</button>`,
  ]);
}
function renderOverview(data) {
  const stat = data.statistics;
  $("runtime").textContent = duration(stat.STAT_RUNTIME);
  $("armed-total").textContent = duration(stat.STAT_FLTTIME);
  $("bootcount").textContent = fmt(stat.STAT_BOOTCNT, 0);
  $("record-count").textContent = fmt(data.records, 0);
  $("record-detail").textContent = `${data.files} fontes · ${bytes(data.bytes)} de originais`;
  $("clock-quality").textContent = data.clock_issues ? `${data.clock_issues} descontinuidades` : "Sem saltos identificados";
  $("database-size").textContent = `Base de dados: ${bytes(data.database_bytes)} · Originais: ${bytes(data.bytes)}. Crescimento depende da taxa e dos tipos registrados.`;
  const latest = files.find(file => file.summary.metrics && Object.keys(file.summary.metrics).some(key => key.includes("voltage_battery") || key === "BAT.Volt"));
  const metrics = latest?.summary.metrics || {};
  const voltage = metrics["SYS_STATUS.voltage_battery"] || metrics["BAT.Volt"];
  const current = metrics["SYS_STATUS.current_battery"] || metrics["BAT.Curr"];
  const consumed = metrics["BATTERY_STATUS.current_consumed"] || metrics["BAT.CurrTot"];
  const energy = metrics["BATTERY_STATUS.energy_consumed"] || metrics["BAT.EnrgTot"];
  $("voltage").textContent = voltage ? `${fmt(voltage.min, 3)} ${voltage.unit}` : "—";
  $("current").textContent = current ? `${fmt(current.max)} ${current.unit}` : "—";
  $("consumption").textContent = consumed ? `${fmt(consumed.last, 3)} ${consumed.unit}` : "—";
  $("energy").textContent = energy ? `${fmt(energy.last, 3)} ${energy.unit}` : "—";
  $("energy-source").textContent = latest ? `Fonte: ${latest.name}` : "Importe um log para consultar alimentação.";
  $("recent-sessions").innerHTML = table(["Sessão / fonte", "Registros", "Tempo observado", "Tempo armado", "Continuidade", ""], sessionRows(files.slice(0, 5)));
  $("sessions-table").innerHTML = table(["Sessão / fonte", "Registros", "Tempo observado", "Tempo armado", "Continuidade", ""], sessionRows(files));
  $("empty-state").hidden = files.length > 0;
  $("files-size").textContent = `${files.length} fontes · ${bytes(data.bytes)} de originais preservados`;
}
function renderFiles() {
  const search = $("files-search").value.toLowerCase();
  const rows = files.filter(file => `${file.name} ${file.source_key}`.toLowerCase().includes(search)).map(file => [
    `<div class="file-name"><strong>${escape(file.name)}</strong><small>${escape(file.source_key.startsWith("blueos:") ? file.source_key.slice(file.source_key.indexOf(":", 7) + 1) : file.kind === "live" ? "Coleta amostrada" : "Importação local")}</small></div>`,
    escape(file.kind.toUpperCase()), bytes(file.size || 0), fmt(file.summary.records || 0, 0), fmt(file.stream_count, 0), date(file.imported_at),
    file.kind === "live" ? '<span class="muted">Exportar tipos</span>' : `<a class="button secondary" href="${escape(endpoint(`api/original?file=${file.id}`))}">↓ Original</a>`,
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
  $("stream-info").textContent = `${recordData.stream.type} · origem ${recordData.stream.origin} · ${fmt(recordData.total, 0)} registros · ${recordData.stream.fields.length} campos`;
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
    button.title = "Mostrar somente esta coluna";
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
  $("chart-info").textContent = `${fmt(data.total, 0)} registros · ${fmt(data.points.length, 0)} pontos na visualização · ${fmt(data.invalid_samples, 0)} valores não numéricos ou desconhecidos. Passe o mouse para consultar um ponto.`;
  const vals = data.points.filter(point => typeof point.value === "number").map(point => point.value);
  $("chart-stats").innerHTML = vals.length ? `<div><small>Mínimo</small><strong>${fmt(Math.min(...vals), 4)} ${escape(data.unit)}</strong></div><div><small>Máximo</small><strong>${fmt(Math.max(...vals), 4)} ${escape(data.unit)}</strong></div><div><small>Último ponto exibido</small><strong>${fmt(vals.at(-1), 4)} ${escape(data.unit)}</strong></div>` : "";
  drawChart();
}
function drawChart() {
  const canvas = $("plot"), rect = canvas.getBoundingClientRect();
  if (!rect.width) return;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = rect.width * ratio; canvas.height = rect.height * ratio;
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
  const w = rect.width, h = rect.height, points = chartData?.points || [];
  const valid = points.filter(point => typeof point.value === "number" && Number.isFinite(point.value));
  $("chart-empty").hidden = valid.length > 0;
  plotted = [];
  if (!valid.length) return;
  let lo = Math.min(...valid.map(point => point.value)), hi = Math.max(...valid.map(point => point.value));
  const pad = Math.max((hi - lo) * .12, Math.abs(hi) * .002, .001); lo -= pad; hi += pad;
  const minSeq = points[0].seq, maxSeq = points.at(-1).seq;
  const x = seq => 66 + (seq - minSeq) / (maxSeq - minSeq || 1) * (w - 87);
  const y = value => 20 + (hi - value) / (hi - lo) * (h - 65);
  ctx.font = '10px "Segoe UI",sans-serif';
  for (let index = 0; index <= 4; index++) {
    const lineY = 20 + index * (h - 65) / 4;
    ctx.strokeStyle = "#25404b"; ctx.beginPath(); ctx.moveTo(66, lineY); ctx.lineTo(w - 21, lineY); ctx.stroke();
    ctx.fillStyle = "#8fa8b4"; ctx.textAlign = "right"; ctx.fillText(fmt(hi - index * (hi - lo) / 4, 3), 55, lineY + 4);
  }
  ctx.fillStyle = "#8fa8b4"; ctx.textAlign = "left"; ctx.fillText(`#${fmt(minSeq, 0)} · ${date(points[0].timestamp)}`, 66, h - 15);
  ctx.textAlign = "right"; ctx.fillText(`#${fmt(maxSeq, 0)}`, w - 21, h - 15);
  ctx.strokeStyle = "#77bfca"; ctx.lineWidth = 1.6; ctx.beginPath();
  let previous = null;
  points.forEach(point => {
    if (point.value === null) { previous = null; return; }
    const px = x(point.seq), py = y(point.value);
    if (!previous || previous.segment !== point.segment) ctx.moveTo(px, py);
    else {
      if (chartData.field === "_armed") ctx.lineTo(px, y(previous.value));
      ctx.lineTo(px, py);
    }
    plotted.push({...point, x: px, y: py}); previous = point;
  });
  ctx.stroke();
  canvas.onpointermove = event => {
    const px = event.clientX - canvas.getBoundingClientRect().left;
    const closest = plotted.reduce((a, b) => Math.abs(b.x - px) < Math.abs(a.x - px) ? b : a, plotted[0]);
    if (closest) canvas.title = `${fmt(closest.value, 6)} ${chartData.unit} · ${date(closest.timestamp)} · registro ${closest.seq}`;
  };
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
    $("export-progress").textContent = bundle.progress;
    $("export-start").disabled = ["queued", "running"].includes(bundle.status);
    if (bundle.status === "done") {
      $("export-download").href = endpoint(bundle.result.download);
      $("export-download").hidden = false;
      $("export-download").title = `${bytes(bundle.result.size)} · relatório e dados organizados`;
    }
  }
  $("job").hidden = !running.length;
  if (running.length) $("job-text").textContent = running.at(-1).progress;
  let changed = false;
  data.jobs.filter(job => ["done", "error"].includes(job.status)).forEach(job => {
    if (!completedJobs.has(job.id)) {
      completedJobs.add(job.id); changed = job.action !== "bundle" || changed;
      if (job.status === "error") notice(job.progress, true);
      else if (job.action === "sync") notice(`Busca concluída: ${job.result.imported} arquivos importados, ${job.result.skipped} já conhecidos ou vazios. ${job.result.errors.length} pastas/arquivos indisponíveis.`);
      else if (job.action === "bundle") notice("Exportação pronta. Use Baixar ZIP para salvar o relatório e os dados.");
      else notice(job.result.duplicate ? "Arquivo já conhecido; histórico preservado sem duplicação." : `Importação concluída: ${fmt(job.result.records, 0)} registros.`);
    }
  });
  if (changed && !refreshing) await refresh();
  return data;
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    [files, streams] = await Promise.all([api("files"), api("streams")]);
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
  $("inventory-info").textContent = inventory.captured_at ? `${inventory.host} · Consulta em ${date(inventory.captured_at)} · ${inventory.files.length} arquivos encontrados · ${inventory.errors.length} caminhos indisponíveis` : "Use Buscar no BlueOS para consultar os arquivos, inclusive os vazios.";
  $("remote-files-table").innerHTML = table(["Caminho", "Formato", "Tamanho", "Situação"], inventory.files.map(item => [
    escape(item.path), escape(item.extension), bytes(item.size || 0),
    item.size === 0 ? '<span class="severity warning">Vazio · sem registros</span>' : files.some(file => file.source_key === `blueos:${inventory.host}:${item.path}`) ? "Importado · cópia do momento" : "Ainda não importado",
  ]));
}
document.querySelectorAll("[data-tab]").forEach(button => button.onclick = () => go(button.dataset.tab));
document.querySelectorAll("[data-goto]").forEach(button => button.onclick = event => { event.preventDefault(); go(button.dataset.goto); });
document.addEventListener("click", event => {
  const button = event.target.closest("[data-session]");
  if (button) { $("session-select").value = button.dataset.session; renderSession(); go("sessions"); }
});
$("refresh").onclick = task(refresh);
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
  if (active === "events") Object.assign(context, {file: $("event-file").value, search: $("event-search").value});
  if (active === "files") context.search = $("files-search").value;
  const job = await post("bundle", context);
  exportJob = job.id; $("export-start").disabled = true; $("export-download").hidden = true;
  $("export-progress").textContent = "Preparando exportação…";
  await loadStatus();
});
window.addEventListener("resize", drawChart);
window.addEventListener("hashchange", () => go(location.hash.slice(1)));
go(titles[location.hash.slice(1)] ? location.hash.slice(1) : "overview");
task(async () => { await loadStatus(); await refresh(); })();
setInterval(task(loadStatus), 3000);
