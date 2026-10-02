"""A ready marker must identify the actual independently launched provider."""
import json
import os
import secrets
import subprocess
import sys
import time
from urllib.request import Request, urlopen


def test_payment_marker_binds_to_started_process_and_live_server(tmp_path):
    marker = tmp_path / 'payment-ready.json'
    token = secrets.token_hex(32)
    with (tmp_path / 'server.log').open('w') as log:
        process = subprocess.Popen([
            sys.executable, '-m', 'demo.route.payments.http_server',
            '--directory', str(tmp_path / 'database'), '--ready-file', str(marker)],
            env={**os.environ, 'ROUTE_PAYMENT_TOKEN': token},
            stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            data = None
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    data = json.loads(marker.read_text())
                    break
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(0.02)
            assert data is not None, 'actual server never published a ready marker'
            assert data.get('pid') == process.pid, 'ready marker has no matching process identity'
            with urlopen(Request(data['url'] + '/health', headers={'Authorization': 'Bearer ' + token}), timeout=3) as response:
                assert json.load(response)['storage_ready'] is True
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
