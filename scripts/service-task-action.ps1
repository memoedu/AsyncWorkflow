function New-AsyncWorkflowServiceAction {
    param([Parameter(Mandatory)][string]$ProjectRoot,
          [Parameter(Mandatory)][ValidateSet('server','runner','watcher','xiaomi','remnote','dashboard','mirror','dictation')][string]$Role)
    $venvHome=@(Get-Content -LiteralPath (Join-Path $ProjectRoot '.venv\pyvenv.cfg') |
        Where-Object {$_ -match '^home = (.+)$'} | ForEach-Object {$Matches[1]})
    if($venvHome.Count -ne 1){throw 'Cannot resolve the base Python interpreter'}
    $pythonw=Join-Path $venvHome[0] 'pythonw.exe'
    $servicePython=Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $launcher='D:\Projects\DeveloperGovernance\run-hidden.pyw'
    $hostScript=Join-Path $ProjectRoot 'scripts\run-service.py'
    foreach($required in @($pythonw,$servicePython,$launcher,$hostScript)) {
        if(-not (Test-Path -LiteralPath $required -PathType Leaf)){throw "Missing background launcher dependency: $required"}
    }
    $arguments='"'+$launcher+'" --no-timeout -- "'+$servicePython+'" -X utf8 "'+$hostScript+'" --role '+$Role
    New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $ProjectRoot
}
