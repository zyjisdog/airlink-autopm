# Create (or refresh) the Windows scheduled task that keeps the linkage running.
# Usage:  powershell -ExecutionPolicy Bypass -File install_task.ps1
$ErrorActionPreference = 'Stop'
$name = 'airlink-autopm'
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) { throw 'pythonw.exe not found in PATH' }

$act = New-ScheduledTaskAction -Execute $pyw -Argument (Join-Path $dir 'autopm.py') -WorkingDirectory $dir
# logon trigger repeated every 5 minutes: with IgnoreNew this acts as a watchdog,
# so a crash or a manual kill is recovered within 5 minutes
$trig = New-ScheduledTaskTrigger -AtLogOn
$trig.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 9999)).Repetition
# ExecutionTimeLimit 0 matters: the default 72h cap silently kills long-running tasks
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $name -Action $act -Trigger $trig -Settings $set `
    -User "$env:USERDOMAIN\$env:USERNAME" -RunLevel Highest -Force | Out-Null
Write-Host "registered task $name"
Start-ScheduledTask -TaskName $name
Write-Host "started; log: $(Join-Path $dir 'autopm.log')"
