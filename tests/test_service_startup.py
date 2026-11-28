"""Exercise task registration without registering or starting production tasks."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


INSTALLER = Path(__file__).parents[1] / 'scripts/install-startup.ps1'
pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='Windows task installer')


def registration(tmp_path, role, missing=''):
    fixture = tmp_path / 'register.ps1'
    fixture.write_text(r'''
$ErrorActionPreference='Stop'
function Test-Path {
  param($LiteralPath,$PathType)
  if($LiteralPath -eq 'D:\SyntheticPython\pythonw.exe'){return MISSING -ne 'base'}
  if($LiteralPath -like '*\run-hidden.pyw'){return MISSING -ne 'launcher'}
  if($LiteralPath -like '*\.venv\Scripts\python.exe'){return MISSING -ne 'runtime'}
  if($LiteralPath -like '*\run-service.py'){return MISSING -ne 'service'}
  return $false
}
function Get-Content {param($LiteralPath); return 'home = D:\SyntheticPython'}
function New-ScheduledTaskPrincipal {param($UserId,$LogonType,$RunLevel); return @{}}
function New-ScheduledTaskTrigger {
  param([switch]$AtLogOn,$User,[switch]$Once,$At,$RepetitionInterval)
  return @{logon=[bool]$AtLogOn; interval=$RepetitionInterval.TotalSeconds}
}
function New-ScheduledTaskSettingsSet {
  param([switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries,
    [switch]$StartWhenAvailable,$RestartCount,$RestartInterval,$ExecutionTimeLimit,
    $MultipleInstances,[switch]$Hidden)
  return @{Priority=0;MultipleInstances=$MultipleInstances;RestartCount=$RestartCount}
}
function New-ScheduledTaskAction {
  param($Execute,$Argument,$WorkingDirectory)
  return @{execute=$Execute;arguments=$Argument}
}
function Register-ScheduledTask {
  param($TaskName,$Action,$Trigger,$Principal,$Settings,$Description,[switch]$Force)
  $global:result=@{action=$Action;triggers=$Trigger;settings=$Settings}
}
function Get-ScheduledTask {param($TaskName)}
& INSTALLER -Roles ROLE
$global:result | ConvertTo-Json -Depth 6 -Compress
'''.replace('MISSING', "'" + missing + "'").replace('-Roles ROLE', "-Roles '" + role + "'")
       .replace('INSTALLER', "'" + str(INSTALLER) + "'"), encoding='utf-8')
    return subprocess.run([shutil.which('pwsh'), '-NoLogo', '-NoProfile', '-File', str(fixture)],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('role,priority', [
    ('dictation', 7), ('runner', 4), ('dashboard', 7),
])
def test_tasks_use_native_job_host_and_preserve_trigger_contract(tmp_path, role, priority):
    process = registration(tmp_path, role)
    assert process.returncode == 0, process.stderr
    task = json.loads(process.stdout)
    assert task['action']['execute'] == r'D:\SyntheticPython\pythonw.exe'
    assert 'run-hidden.pyw" --no-timeout -- "' in task['action']['arguments']
    assert r'.venv\Scripts\python.exe' in task['action']['arguments']
    assert f'run-service.py" --role {role}' in task['action']['arguments']
    assert 'pwsh.exe' not in task['action']['arguments']
    assert task['settings']['MultipleInstances'] == 'IgnoreNew'
    assert task['settings']['Priority'] == priority
    assert task['settings']['RestartCount'] == 3
    assert any(t['logon'] for t in task['triggers'])
    assert any(t['interval'] == 60 for t in task['triggers']) is (role == 'dictation')


@pytest.mark.parametrize('missing', ['base', 'launcher', 'runtime', 'service'])
def test_missing_dependency_fails_before_registering_tasks(tmp_path, missing):
    process = registration(tmp_path, 'dictation', missing=missing)
    assert process.returncode != 0
    assert 'Missing background launcher dependency' in process.stderr
