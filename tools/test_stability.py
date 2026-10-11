"""Carga local com cópia da base real; não conecta nem escreve no ROV."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import sqlite3
import statistics
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rov_history.app import create_server
from rov_history.service import Service
from rov_history.store import Store


def rss():
    if os.name == "nt":
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in ("peak", "working", "paged_peak", "paged", "nonpaged_peak", "nonpaged", "pagefile", "pagefile_peak")]
        value = Counters(); value.cb = ctypes.sizeof(value)
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        memory = ctypes.windll.psapi.GetProcessMemoryInfo
        memory.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        if memory(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb): return value.working
    else:
        try: return int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
        except OSError: pass
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='data/preview')
    parser.add_argument('--seconds', type=int, default=90)
    parser.add_argument('--clients', type=int, default=8)
    args = parser.parse_args()
    run = Path('data/stability') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    run.mkdir(parents=True, exist_ok=False)
    source = Store(args.source)
    with source.db() as db, sqlite3.connect(run / 'history.sqlite3') as target: db.backup(target)
    Store(run).configure({'collect': False, 'host': '127.0.0.1', 'equipment': 'Fixture de estabilidade · simulação local'})
    service = Service(run)
    server = create_server(service, port=0)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True); server_thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    streams = service.store.streams()
    numeric = [s for s in streams if s['type'] == 'SYS_STATUS' and s['origin'] == '1:1']
    endpoints = ['/health', '/api/status', '/api/overview', '/api/files', '/api/streams', '/api/events?limit=100']
    if streams: endpoints.append('/api/records?stream=' + str(streams[0]['id']))
    for stream in numeric:
        for field in ('voltage_battery', 'current_battery'):
            endpoints.append('/api/series?stream=' + str(stream['id']) + '&field=' + field)
    results, health_times, memory, errors = Counter(), [], [], []
    lock = threading.Lock(); end = time.monotonic() + args.seconds
    def client(index):
        rng = random.Random(index)
        while time.monotonic() < end:
            endpoint = rng.choice(endpoints)
            start = time.monotonic()
            try:
                with urlopen(base + endpoint, timeout=15) as response:
                    response.read(); status = response.status
            except HTTPError as error:
                status = error.code
            except Exception as error:
                status = 'failed'
                with lock: errors.append(str(error))
            with lock:
                results[str(status)] += 1
                if endpoint == '/health' and status == 200: health_times.append(time.monotonic() - start)
            time.sleep(.03)
    request = Request(base + '/api/bundle', data=b'{"scope":"all","include_data":true}', headers={'Content-Type':'application/json', 'X-Reikjv-History':'1'})
    with urlopen(request) as response: job = json.load(response)
    started = time.monotonic(); last_live = 0; count = 0
    print('Carga iniciada:', args.clients, 'clientes;', service.store.overview()['records'], 'registros na cópia local.', flush=True)
    try:
        with ThreadPoolExecutor(max_workers=args.clients) as pool:
            futures = [pool.submit(client, i) for i in range(args.clients)]
            while time.monotonic() < end:
                memory.append({'elapsed_s': round(time.monotonic() - started, 2), 'rss_bytes': rss()})
                if time.monotonic() - last_live > 2:
                    count += 1; stamp = datetime.now(timezone.utc).isoformat()
                    cache = {'vehicles':{'1':{'components':{'1':{'messages':{'HEARTBEAT':{'message':{'base_mode':{'bits':0}},'status':{'time':{'counter':count,'last_update':stamp}}}}}}}}}
                    service.ingest_live(cache, host='estabilidade.local')
                    last_live = time.monotonic()
                time.sleep(1)
            for future in futures: future.result()
        # Aguarda apenas a tarefa iniciada pelo teste; o health continua sendo consultado.
        deadline = time.monotonic() + 180
        while service.jobs[job['id']]['status'] in ('queued', 'running') and time.monotonic() < deadline:
            with urlopen(base + '/health', timeout=5) as response: assert json.load(response)['status'] == 'ok'
            time.sleep(1)
        export = service.jobs[job['id']]
        assert export['status'] == 'done', export
        archive = run / 'exports' / (job['id'] + '.zip')
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            manifest = json.loads(z.read('Bravo-Viewer/manifest.json'))
            exported = 0
            for entry in manifest['entries']:
                with z.open('Bravo-Viewer/' + entry['jsonl']) as file:
                    rows = sum(1 for _ in file)
                assert rows == entry['records']
                exported += rows
        ordered = sorted(health_times)
        report = {'scope':'Teste local, incluindo o gerador de carga no mesmo processo; sem comunicação com o ROV',
                  'duration_s':round(time.monotonic() - started, 2), 'clients':args.clients,
                  'requests':dict(results), 'errors':errors, 'health_requests':len(health_times),
                  'health_p95_ms':round(ordered[min(len(ordered)-1, int(len(ordered)*.95))]*1000, 2) if ordered else None,
                  'health_max_ms':round(max(health_times)*1000, 2) if health_times else None,
                  'rss_peak_mb':round(max(p['rss_bytes'] or 0 for p in memory)/1048576, 2),
                  'rss_last_mb':round((memory[-1]['rss_bytes'] or 0)/1048576, 2),
                  'exported_records':exported, 'bundle_bytes':archive.stat().st_size,
                  'collection_records':count, 'database_integrity':None, 'memory_samples':memory}
        with service.store.db() as db: report['database_integrity'] = db.execute('PRAGMA integrity_check').fetchone()[0]
        (run / 'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k != 'memory_samples'},ensure_ascii=False,indent=2), flush=True)
        assert not errors and '500' not in results and results['200'] > 0
        assert report['database_integrity'] == 'ok'
        assert report['health_p95_ms'] < 2000, 'Health com latência elevada'
        print('RELATÓRIO:', run / 'report.json', flush=True)
    finally:
        service.close(); server.shutdown(); server.server_close()


if __name__ == '__main__': main()
