"""Servidor com telemetria sintética apenas para o teste DOM/API do CI."""
import argparse
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pymavlink.dialects.v20 import ardupilotmega as mav
from rov_history.app import create_server
from rov_history.ingest import import_file
from rov_history.service import Service
from tests.test_history import tlog, heartbeat, param


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--port', type=int, default=18090)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        source = directory / 'fixture.tlog'
        now = time.time() - 100
        messages = [(now + i, heartbeat(10 <= i < 20)) for i in range(30)]
        messages += [(now + 30, param('STAT_RUNTIME', 1200)), (now + 31, mav.MAVLink_sys_status_message(0,0,0,0,24500,40,-1,0,0,0,0,0,0))]
        tlog(source, messages)
        service = Service(directory / 'db')
        service.configure({'collect':False, 'equipment':'Fixture · simulação local'})
        import_file(service.store, source)
        server = create_server(service, port=args.port)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally: service.close(); server.server_close()


if __name__ == '__main__': main()
