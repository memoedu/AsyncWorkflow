param([switch]$Start,[ValidateSet('server','runner','watcher','xiaomi','remnote','dashboard','mirror','dictation')][string[]]$Roles=@('server','runner','watcher','xiaomi','remnote','dashboard'))
$ErrorActionPreference='Stop'
$projectRoot='D:\Projects\AsyncWorkflow'
. (Join-Path $PSScriptRoot 'service-task-action.ps1')
$identity=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal=New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $identity
$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Hidden
foreach($role in $Roles) {
  # Prefect services need normal CPU, I/O and memory priority under desktop load.
  $settings.Priority=if($role -in @('server','runner','watcher')){4}else{7}
  $action=New-AsyncWorkflowServiceAction -ProjectRoot $projectRoot -Role $role
  $roleTriggers=@($trigger)
  if($role -eq 'dictation') {
    # Same task + IgnoreNew: recover after a clean exit or exhausted failure
    # retries without a second watchdog service or duplicate listener.
    $roleTriggers+=New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
  }
  Register-ScheduledTask -TaskName "AsyncWorkflow-$role" -Action $action -Trigger $roleTriggers -Principal $principal -Settings $settings -Description 'Personal asynchronous recording workflow; current-user local services.' -Force | Out-Null
  if($Start){Start-ScheduledTask -TaskName "AsyncWorkflow-$role"}
}
Get-ScheduledTask -TaskName 'AsyncWorkflow-*' | Select-Object TaskName,State
