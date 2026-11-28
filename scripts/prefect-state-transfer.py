"""Move a fenced Prefect SQLite file group without a second source-side database."""

import argparse
from contextlib import closing
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory


DB = 'prefect.db'
FILES = (DB, DB + '-wal', DB + '-shm')
GUARDED_STATES = ('PENDING', 'RUNNING', 'CANCELLING', 'PAUSED')


def connect(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def file_group(root):
    result = {}
    for name in FILES:
        path = root / name
        if path.is_file():
            with path.open('rb') as stream:
                result[name] = {'bytes': path.stat().st_size, 'sha256': file_digest(stream, 'sha256').hexdigest()}
    if DB not in result:
        raise FileNotFoundError(root / DB)
    return result


def rows_digest(db, query):
    digest = sha256()
    for row in db.execute(query):
        digest.update(json.dumps(row, separators=(',', ':')).encode())
        digest.update(b'\n')
    return digest.hexdigest()


def inventory(db, due_after=0, check=False):
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    if check and db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise RuntimeError('SQLite integrity check failed')
    if check and db.execute('PRAGMA foreign_key_check').fetchone():
        raise RuntimeError('SQLite foreign key check failed')
    tables = db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    counts = {}
    for (name,) in tables:
        quoted = '"' + name.replace('"', '""') + '"'
        counts[name] = db.execute(f'SELECT COUNT(*) FROM {quoted}').fetchone()[0]
    return {
        'revision': db.execute('SELECT version_num FROM alembic_version').fetchone()[0],
        'counts': counts,
        'deployments': db.execute(
            'SELECT deployment.id, flow.id, flow.name, deployment.name, deployment.paused '
            'FROM deployment JOIN flow ON flow.id=deployment.flow_id ORDER BY deployment.id'
        ).fetchall(),
        'schedules': db.execute(
            'SELECT id, deployment_id, active FROM deployment_schedule ORDER BY id'
        ).fetchall(),
        'schedules_digest': rows_digest(db,
            'SELECT id, deployment_id, active, schedule, parameters FROM deployment_schedule ORDER BY id'),
        'run_history_digest': rows_digest(db,
            'SELECT id, deployment_id, flow_id, state_id, state_type, idempotency_key, start_time, end_time '
            'FROM flow_run ORDER BY id'),
        'state_history_digest': rows_digest(db,
            'SELECT id, flow_run_id, type, timestamp FROM flow_run_state ORDER BY id'),
        'scheduled': db.execute(
            "SELECT id, deployment_id, expected_start_time, auto_scheduled FROM flow_run "
            "WHERE state_type='SCHEDULED' ORDER BY expected_start_time, id"
        ).fetchall(),
        'state_counts': dict(db.execute('SELECT state_type, COUNT(*) FROM flow_run GROUP BY state_type')),
        'due': db.execute(
            "SELECT COUNT(*) FROM flow_run WHERE state_type='SCHEDULED' "
            "AND (expected_start_time IS NULL OR julianday(expected_start_time) <= julianday('now', ?))",
            (f'+{due_after} seconds',),
        ).fetchone()[0],
    }


def assert_idle(index):
    if index['due'] or any(index['state_counts'].get(state, 0) for state in GUARDED_STATES):
        raise RuntimeError('Prefect has active or due work')


def send(root, remote, destination):
    # Run only against a fenced source. The remote receiver checks the group
    # again before using it. Never write an archive on the source drive.
    with closing(connect(root / DB)) as source:
        assert_idle(inventory(source, due_after=120))
    metadata = file_group(root)
    process = subprocess.Popen(remote + [destination], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        with process.stdin, tarfile.open(fileobj=process.stdin, mode='w|gz', compresslevel=1) as archive:
            for name, spec in metadata.items():
                entry = tarfile.TarInfo(name)
                entry.size = spec['bytes']
                with (root / name).open('rb') as stream:
                    archive.addfile(entry, stream)
    finally:
        with process.stdout:
            received = process.stdout.read()
        status = process.wait()
    if status:
        raise RuntimeError(f'Remote transfer exited {status}')
    if file_group(root) != metadata:
        raise RuntimeError('Frozen source file group changed during transport')
    if json.loads(received) != metadata:
        raise RuntimeError('Received file group differs from the frozen source')
    return metadata


def receive(root):
    # Never extract arbitrary names, overwrite a completed backup, or enable
    # the server from staging.
    root.mkdir(mode=0o700)
    seen = set()
    with tarfile.open(fileobj=sys.stdin.buffer, mode='r|gz') as archive:
        for entry in archive:
            if entry.name not in FILES or entry.name in seen or not entry.isfile():
                raise RuntimeError('Invalid SQLite file group archive')
            with archive.extractfile(entry) as source, (root / entry.name).open('xb') as target:
                shutil.copyfileobj(source, target)
            seen.add(entry.name)
    return file_group(root)


def backup(staged, dest, guard_idle=True):
    if dest.exists():
        raise FileExistsError(dest)
    # Reader shared-memory locks are local to the receiving host. Rebuild
    # that file from the received WAL instead of reusing its lock state.
    shm = staged / (DB + '-shm')
    if shm.exists():
        shm.rename(staged / (DB + '-shm.received'))
    with TemporaryDirectory(dir=dest.parent) as temporary:
        copy = Path(temporary) / DB
        with closing(connect(staged / DB)) as source, closing(sqlite3.connect(copy)) as target:
            source.backup(target, pages=512)
        with closing(connect(copy)) as target:
            index = inventory(target, check=True)
            if guard_idle:
                assert_idle(index)
        if dest.exists():
            raise FileExistsError(dest)
        copy.replace(dest)
    return index


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    for action in ('inventory', 'group', 'receive', 'backup', 'send'):
        command = actions.add_parser(action)
        command.add_argument('root', type=Path)
        if action == 'inventory':
            command.add_argument('--due-after', type=int, default=0)
            command.add_argument('--require-idle', action='store_true')
            command.add_argument('--check', action='store_true')
        elif action == 'backup':
            command.add_argument('destination', type=Path)
            command.add_argument('--allow-due', action='store_true')
        elif action == 'send':
            command.add_argument('destination')
            command.add_argument('remote', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == 'inventory':
        with closing(connect(args.root / DB)) as db:
            result = inventory(db, args.due_after, args.check)
            if args.require_idle:
                assert_idle(result)
    elif args.action == 'group':
        result = file_group(args.root)
    elif args.action == 'receive':
        result = receive(args.root)
    elif args.action == 'backup':
        result = backup(args.root, args.destination, not args.allow_due)
    else:
        result = send(args.root, args.remote, args.destination)
    print(json.dumps(result, indent=2), flush=True)


if __name__ == '__main__':
    main()
