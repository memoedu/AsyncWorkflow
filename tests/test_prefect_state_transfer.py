from contextlib import closing
from pathlib import Path
import runpy
import sqlite3

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
transfer = runpy.run_path(str(SCRIPTS / 'prefect-state-transfer.py'))
server_host = runpy.run_path(str(SCRIPTS / 'run-service.py'))['prefect_server_host']


def test_windows_owner_fails_closed(tmp_path):
    prefect_home = tmp_path / 'prefect'
    prefect_home.mkdir()
    owner = tmp_path / 'prefect-server-owner.json'
    frozen = prefect_home / 'cutover-frozen'
    assert server_host(tmp_path) is None
    owner.write_text('{"owner":"mac","ssh_host":"verified-host"}')
    with pytest.raises(RuntimeError):
        server_host(tmp_path)
    frozen.touch()
    assert server_host(tmp_path) == 'verified-host'
    owner.write_text('{"owner":"mac","ssh_host":"-oProxyCommand=unknown"}')
    with pytest.raises(RuntimeError):
        server_host(tmp_path)
    owner.unlink()
    with pytest.raises(RuntimeError):
        server_host(tmp_path)
    owner.write_text('{"owner":"frozen"}')
    with pytest.raises(RuntimeError):
        server_host(tmp_path)


def test_backup_uses_received_wal_and_keeps_identity(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    received = tmp_path / 'received'
    received.mkdir()
    dest = tmp_path / 'final.db'
    with closing(sqlite3.connect(source / 'prefect.db')) as producer:
        producer.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA wal_autocheckpoint=0;
            CREATE TABLE alembic_version(version_num);
            INSERT INTO alembic_version VALUES ('known');
            CREATE TABLE flow(id, name);
            INSERT INTO flow VALUES ('flow','original');
            CREATE TABLE deployment(id, flow_id, name, paused);
            INSERT INTO deployment VALUES ('deployment','flow','local',0);
            CREATE TABLE deployment_schedule(id, deployment_id, active, schedule, parameters);
            INSERT INTO deployment_schedule VALUES ('schedule','deployment',1,'{}','{}');
            CREATE TABLE flow_run(id, deployment_id, flow_id, state_id, state_type, idempotency_key,
              start_time, end_time, expected_start_time, auto_scheduled);
            CREATE TABLE flow_run_state(id, flow_run_id, type, timestamp);
            INSERT INTO flow_run VALUES ('run','deployment','flow','state','SCHEDULED',
              'existing-key',NULL,NULL,'2100-01-01 01:00:00',1);
            INSERT INTO flow_run_state VALUES ('state','run','SCHEDULED','2026-01-01');
        """)
        before = transfer['file_group'](source)
        for name in before:
            (received / name).write_bytes((source / name).read_bytes())
        assert before == transfer['file_group'](received)
    result = transfer['backup'](received, dest)
    assert result['deployments'] == [('deployment', 'flow', 'original', 'local', 0)]
    assert result['schedules'] == [('schedule', 'deployment', 1)]
    assert result['scheduled'] == [('run', 'deployment', '2100-01-01 01:00:00', 1)]
    with closing(sqlite3.connect(dest)) as restored:
        assert restored.execute('SELECT idempotency_key FROM flow_run').fetchone() == ('existing-key',)
    with pytest.raises(FileExistsError):
        transfer['backup'](received, dest)


@pytest.mark.parametrize('state,due', [('RUNNING', 0), ('PAUSED', 0), ('SCHEDULED', 1)])
def test_transfer_guard(state, due):
    with pytest.raises(RuntimeError):
        transfer['assert_idle']({'state_counts': {state: 1}, 'due': due})
