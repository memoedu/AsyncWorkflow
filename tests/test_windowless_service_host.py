from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import runpy
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='native Windows service host')
PROJECT = Path(__file__).resolve().parents[1]
ENTRY = PROJECT/'scripts'/'run-service.py'
LAUNCHER = Path('D:/Projects/DeveloperGovernance/run-hidden.pyw')


def test_lease_conflicts_with_old_windows_file_share(tmp_path):
    lease = runpy.run_path(str(ENTRY))['service_lease']
    path = tmp_path/'service-dashboard.lock'
    shell = Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    code = ("try {$l=[IO.File]::Open('" + str(path).replace("'", "''") +
            "','OpenOrCreate','ReadWrite','None'); $l.Dispose(); exit 0} catch {exit 7}")
    call = [str(shell), '-NoProfile', '-NonInteractive', '-Command', code]
    with lease(path) as acquired:
        assert acquired
        assert subprocess.run(call, timeout=15).returncode == 7
        with lease(path) as nested_lease:
            assert not nested_lease
    assert subprocess.run(call, timeout=15).returncode == 0


def test_health_wait_retries_original_loopback_route():
    wait_for_prefect = runpy.run_path(str(ENTRY))['wait_for_prefect']
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            self.send_response(503 if len(calls) == 1 else 200)
            self.end_headers()

    with HTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert wait_for_prefect(f'http://127.0.0.1:{server.server_port}/api', attempts=2, delay=0)
        finally:
            server.shutdown()
            thread.join()
    assert calls == ['/api/health', '/api/health']


def managed_command(code):
    if not LAUNCHER.is_file():
        pytest.skip('shared DeveloperGovernance launcher not installed')
    pythonw = Path(sys._base_executable).with_name('pythonw.exe')
    return [str(pythonw), str(LAUNCHER), '--no-timeout', '--',
            sys.executable, '-X', 'utf8', '-c', code]


def test_windowless_lifecycle_writes_native_output_and_preserves_exit_code(tmp_path):
    code = f"""from pathlib import Path
import ctypes, os, runpy, subprocess, sys
host = runpy.run_path({str(ENTRY)!r})['serve']
def fixture(*_):
    assert not ctypes.WinDLL('kernel32').GetConsoleWindow()
    assert os.environ['PREFECT_API_URL'] == 'http://127.0.0.1:4200/api'
    print('python service output')
    subprocess.run([sys.executable, '-u', '-c', 'import sys;print("native child stderr",file=sys.stderr)'], check=True)
    return 27
sys.exit(host('dashboard', Path({str(tmp_path)!r}), Path({str(tmp_path/'cache')!r}), fixture))
"""
    assert subprocess.run(managed_command(code), timeout=20).returncode == 27
    output = (tmp_path/'logs'/'dashboard.output.log').read_text(encoding='utf-8')
    assert 'python service output' in output and 'native child stderr' in output
    assert 'exited 27' in (tmp_path/'logs'/'dashboard.task.log').read_text(encoding='utf-8')


def test_managed_child_tree_and_lease_disappear_on_launcher_stop(tmp_path):
    code = f"""from pathlib import Path
import runpy, subprocess, sys, time
host = runpy.run_path({str(ENTRY)!r})['serve']
def fixture(*_):
    child = subprocess.Popen([str(Path(sys._base_executable)), '-c', 'import time;time.sleep(90)'])
    Path({str(tmp_path/'child.pid')!r}).write_text(str(child.pid))
    time.sleep(90)
sys.exit(host('dashboard', Path({str(tmp_path)!r}), Path({str(tmp_path/'cache')!r}), fixture))
"""
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    with subprocess.Popen(managed_command(code)) as parent:
        try:
            until = time.monotonic() + 15
            marker = tmp_path/'child.pid'
            while time.monotonic() < until and (not marker.is_file() or not marker.stat().st_size):
                time.sleep(.1)
            child = api.OpenProcess(0x100000, False, int(marker.read_text()))
            assert child
            try:
                parent.terminate()
                parent.wait(timeout=10)
                assert api.WaitForSingleObject(child, 5000) == 0
            finally:
                api.CloseHandle(child)
        finally:
            if parent.poll() is None:
                parent.kill()
    with runpy.run_path(str(ENTRY))['service_lease'](tmp_path/'locks'/'service-dashboard.lock') as acquired:
        assert acquired
