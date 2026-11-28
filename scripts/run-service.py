"""Windowless entry for the existing Windows task roles."""
import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


MODULES = {
    'runner': ('async_workflow.cli', 'serve'),
    'watcher': ('async_workflow.cli', 'watch'),
    'dashboard': ('async_workflow.dashboard',),
    'remnote': ('async_workflow.remnote_bridge',),
    'mirror': ('async_workflow.mirror_sync',),
    'dictation': ('async_workflow.dictation_gateway',),
}
ROLES = (*MODULES, 'server', 'xiaomi')


@contextmanager
def service_lease(path):
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    # Same exclusive file handle as FileShare.None in the legacy entry.
    handle = api.CreateFileW(str(path), 0xc0000000, 0, None, 4, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        if error not in (32, 33):
            raise ctypes.WinError(error)
        yield False
        return
    try:
        yield True
    finally:
        api.CloseHandle(handle)


def wait_for_prefect(url, attempts=45, delay=2):
    opener = build_opener(ProxyHandler({}))
    for _ in range(attempts):
        try:
            with opener.open(url + '/health', timeout=3) as response:
                if response.status == 200:
                    return True
        except (URLError, OSError):
            pass
        time.sleep(delay)
    return False


def prepare_environment(runtime, temporary):
    values = {
        'PREFECT_HOME': runtime/'prefect', 'PREFECT_API_URL': 'http://127.0.0.1:4200/api',
        'PREFECT_LOCAL_STORAGE_PATH': runtime/'prefect-results',
        'PREFECT_SERVER_ANALYTICS_ENABLED': 'false', 'ASYNC_WORKFLOW_CONFIG': runtime/'config.json',
        'PYTHONUTF8': '1', 'TEMP': temporary, 'TMP': temporary,
    }
    os.environ.update({key: str(value) for key, value in values.items()})
    for path in (temporary, runtime/'logs', runtime/'locks', runtime/'xiaomi'):
        path.mkdir(parents=True, exist_ok=True)


def attach_stdio(output):
    import msvcrt

    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    api.SetStdHandle.restype = wintypes.BOOL
    for fd in (1, 2):
        os.dup2(output.fileno(), fd)
        if not api.SetStdHandle(-10-fd, msvcrt.get_osfhandle(fd)):
            raise ctypes.WinError(ctypes.get_last_error())
    sys.stdout = sys.stderr = output


def prefect_server_host(runtime):
    config = runtime / 'prefect-server-owner.json'
    frozen = runtime / 'prefect' / 'cutover-frozen'
    owner = json.loads(config.read_text(encoding='utf-8')) if config.exists() else {'owner': 'windows'}
    if owner == {'owner': 'windows'} and not frozen.exists():
        return None
    if owner == {'owner': 'frozen'}:
        raise RuntimeError('Prefect ownership transfer is in progress')
    if isinstance(owner, dict) and set(owner) == {'owner', 'ssh_host'} and owner['owner'] == 'mac':
        host = owner['ssh_host']
        if frozen.is_file() and isinstance(host, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', host):
            return host
    raise RuntimeError('Invalid Prefect ownership configuration')


def run_prefect_tunnel(host, output, port=4200):
    # The child stays in the existing scheduler Job Object.
    command = [str(Path(os.environ['SystemRoot']) / 'System32' / 'OpenSSH' / 'ssh.exe'), '-N', '-T']
    for option in ('BatchMode=yes', 'StrictHostKeyChecking=yes', 'ExitOnForwardFailure=yes',
                   'ConnectTimeout=10', 'ServerAliveInterval=10', 'ServerAliveCountMax=2',
                   'ControlMaster=no', 'ControlPath=none'):
        command.extend(('-o', option))
    command.extend(('-L', f'127.0.0.1:{port}:127.0.0.1:{port}', host))
    while True:
        child = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT)
        print(f'Prefect SSH forwarding exited {child.returncode}. Reconnecting.', file=output, flush=True)
        time.sleep(2)


def run_service(role, runtime, temporary, output):
    if role == 'xiaomi':
        os.environ.update(SERVER_PORT='43140', APP_DB_PATH=str(runtime/'xiaomi'/'xiaomi.db'))
        return subprocess.run([
            r'D:\Applications\XiaomiAlbumSyncer\xiaomi-album-syncer-windows-x64-v0.19.1.exe',
            '-server.host=127.0.0.1', '-Dorg.sqlite.tmpdir=' + str(temporary),
        ], cwd=runtime/'xiaomi', stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT).returncode
    if role == 'server':
        host = prefect_server_host(runtime)
        if host:
            return run_prefect_tunnel(host, output)
        from prefect.cli import app
        sys.argv = ['prefect', 'server', 'start', '--host', '127.0.0.1', '--port', '4200']
        app()
    else:
        module, *arguments = MODULES[role]
        sys.argv = [module, *arguments]
        runpy.run_module(module, run_name='__main__', alter_sys=True)
    return 0


def lifecycle_event(path, text):
    with path.open('a', encoding='utf-8') as log:
        log.write(f'{datetime.now().astimezone().isoformat()} {text}\n')


def serve(role, runtime, temporary, service=run_service):
    prepare_environment(runtime, temporary)
    task_log = runtime/'logs'/f'{role}.task.log'
    output_log = task_log if role in ('xiaomi', 'remnote') else runtime/'logs'/f'{role}.output.log'
    with service_lease(runtime/'locks'/f'service-{role}.lock') as acquired:
        if not acquired:
            return 0
        lifecycle_log = output_log if role == 'dictation' else task_log
        lifecycle_event(lifecycle_log, f'Started {role} pid={os.getpid()}')
        if role in ('runner', 'watcher') and not wait_for_prefect(os.environ['PREFECT_API_URL']):
            lifecycle_event(lifecycle_log, f'{role} exited 1: Prefect health wait exhausted')
            return 1
        # Keep the stream open through Python's shutdown, including atexit handlers.
        output = output_log.open('a', encoding='utf-8', errors='backslashreplace', buffering=1)
        attach_stdio(output)
        try:
            result = service(role, runtime, temporary, output)
        except SystemExit as exc:
            lifecycle_event(lifecycle_log, f'{role} exited {exc.code if isinstance(exc.code, int) else int(exc.code is not None)}')
            raise
        except BaseException:
            lifecycle_event(lifecycle_log, f'{role} exited after an exception')
            raise
        lifecycle_event(lifecycle_log, f'{role} exited {result}')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', required=True, choices=ROLES)
    role = parser.parse_args().role
    os.chdir(Path(__file__).resolve().parents[1])
    return serve(role, Path('D:/AppData/AsyncWorkflow'), Path('D:/Caches/AsyncWorkflow/tmp'))


if __name__ == '__main__':
    sys.exit(main())
