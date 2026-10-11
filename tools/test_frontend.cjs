/* Verifica DOM e consultas HTTP; não substitui inspeção visual no navegador. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {JSDOM, VirtualConsole} = require("../.tooling/node_modules/jsdom");

(async () => {
  const url = new URL(process.argv[2] || "http://127.0.0.1:8090/");
  assert.ok(["127.0.0.1", "localhost", "[::1]"].includes(url.hostname), "Use o servidor local de teste");
  const errors = [];
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("jsdomError", error => errors.push(error.message));
  const html = await (await fetch(url)).text();
  const dom = new JSDOM(html, {url: url.href, runScripts: "outside-only", virtualConsole});
  const win = dom.window;
  const requests = [];
  win.fetch = (input, options) => {
    requests.push(String(input));
    assert.equal(options?.method || "GET", "GET", "O teste dos controles não altera a configuração");
    return fetch(input, options);
  };
  win.requestAnimationFrame = callback => callback();
  win.AbortController = AbortController;
  win.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  win.HTMLDialogElement.prototype.close = function () { this.open = false; };
  win.setInterval = () => 0;
  win.eval(fs.readFileSync(path.join(__dirname, "../web/app.js"), "utf8") + '\nwindow.__test = {refresh, loadStatus, loadChartFields, loadChart};');
  const $ = id => win.document.getElementById(id);
  const until = async condition => {
    for (let i = 0; i < 300 && !condition(); i++) await new Promise(resolve => setTimeout(resolve, 50));
    assert.ok(condition(), $("notice").textContent || "Tempo esgotado");
  };
  try {
    for (let attempt = 0; attempt < 300 && !$("updated").textContent.startsWith("Atualizado às"); attempt++) {
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    assert.ok($("updated").textContent.startsWith("Atualizado às"), $("notice").textContent || "Tempo esgotado");
    assert.equal(errors.length, 0, errors.join("\n"));
    assert.equal(win.document.title, "Bravo Viewer");
    assert.equal(win.document.querySelector('.brand img').getAttribute('src'), 'brs.png');
    assert.equal(win.document.querySelectorAll('img[src="brand.png"]').length, 1);
    assert.ok(win.document.querySelector('img[src="brand.png"]').closest('footer'));
    assert.ok(!requests.some(url => url.includes('/api/series') || url.includes('/api/records')), "Visão geral não deve carregar tabelas e gráficos ocultos");
    $("export-viewer").click(); assert.equal($("export-dialog").open, true);
    assert.equal($("export-scope").options.length, 2);
    $("export-close").click(); assert.equal($("export-dialog").open, false);
    const allIds = [...win.document.querySelectorAll("[id]")].map(el => el.id);
    assert.equal(allIds.length, new Set(allIds).size, "IDs do painel devem ser únicos");
    win.document.querySelector('[data-tab="capabilities"]').click();
    await until(() => $("capabilities-table").querySelectorAll("tbody tr").length >= 15);
    assert.equal($("capabilities").hidden, false);
    assert.equal($("overview").hidden, true);
    assert.ok($("capabilities-table").textContent.includes("Painel futuro"));
    assert.ok($("capabilities-table").textContent.includes("depende de sensor"));
    win.document.querySelector('[data-tab="charts"]').click();
    await until(() => $("chart-stream").options.length > 0 && $("chart-unit").textContent.length > 0);
    assert.ok(![...$("chart-stream").options].some(option => option.textContent.includes("AHRS")), "Gráficos desta versão devem ficar no recorte operacional");
    const heartbeat = [...$("chart-stream").options].find(option => option.textContent.startsWith("HEARTBEAT"));
    if (heartbeat) {
      $("chart-stream").value = heartbeat.value;
      await win.__test.loadChartFields();
      assert.equal($("chart-field").value, "_armed");
      assert.ok($("chart-title").textContent.includes("Estado armado"));
      assert.ok($("chart-unit").textContent.includes("desarmado"));
    }
    win.document.querySelector('[data-tab="explorer"]').click();
    await until(() => $("explorer-stream").options.length > 0 && $("export-csv").hasAttribute("href"));
    if ($("explorer-stream").options.length) {
      assert.ok(new URL($("export-csv").href).searchParams.get("stream"));
      assert.equal(new URL($("export-json").href).searchParams.get("format"), "jsonl");
    }
    $("config-equipment").value = "Edição ainda não salva";
    $("config-equipment").dispatchEvent(new win.Event("input", {bubbles: true}));
    await win.__test.loadStatus();
    assert.equal($("config-equipment").value, "Edição ainda não salva", "Atualização não deve apagar a edição");
    assert.equal(errors.length, 0, errors.join("\n"));
    console.log("DOM/API OK: Bravo/BRS, Reikjv no rodapé, carregamento sob demanda, grade, gráficos, exportação e configuração.");
  } finally { win.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
