# Registers the ONE Task Scheduler task of this system: it runs dispatch every few minutes
# (no admin rights needed). dispatch reads Discord (search, GET only) and starts Claude only when
# there is a new mention. This is an OS feature, not a Claude scheduled task, so the desktop app
# does not have to be open. It runs only while the PC is awake and you are logged on.
#
# Run it through powershell.exe with -ExecutionPolicy Bypass, because a new PC refuses .ps1 files:
#   powershell -ExecutionPolicy Bypass -File .\scripts\install-task.ps1 -User yourname              register
#   powershell -ExecutionPolicy Bypass -File .\scripts\install-task.ps1 -User yourname -Uninstall   remove
# (ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less files in the ANSI code page.)
param(
    [string]$User = "",
    [switch]$Uninstall,
    [int]$IntervalMinutes = 2,
    [string]$Config = ""
)

$taskName = "mention-triage"

# The resident-watcher era had two tasks. The watcher they start no longer exists, so always remove them.
foreach ($old in @("mention-triage-watcher", "mention-triage-dispatch")) {
    if (Get-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $old -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $old -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "removed old task: $old"
    }
}
# A resident watcher that is still running (started by the old task, or by hand) would keep writing to the
# same inbox, so stop it as well. The current "python -m watcher" is a one-shot that ends within seconds;
# only a process that has been alive for 3 minutes or more is the old resident one.
$now = Get-Date
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and ($_.ProcessId -ne $PID) -and (($now - $_.CreationDate).TotalSeconds -ge 180) -and (
            (($_.Name -like "python*") -and ($_.CommandLine -like "*-m watcher*") -and
             ($_.CommandLine -notlike "*--check*") -and ($_.CommandLine -notlike "*--dry-run*")) -or
            ((($_.Name -like "powershell*") -or ($_.Name -like "conhost*")) -and ($_.CommandLine -like "*-File*run-watcher.ps1*")))
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "stopped old watcher process: $($_.ProcessId)"
    }

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "removed: $taskName"
    exit 0
}

if (-not $User) {
    Write-Error "-User is required (the name in config.json: watchers[].name)"
    exit 2
}
if ($IntervalMinutes -lt 1) { $IntervalMinutes = 1 }

$script = Join-Path $PSScriptRoot "run-dispatch.ps1"
$arg = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -User $User"
if ($Config) { $arg += " -Config `"$Config`"" }

# conhost --headless starts PowerShell without creating a console window. With -WindowStyle Hidden alone,
# a black window still flashes on screen every time Task Scheduler launches it.
$conhost = Join-Path $env:SystemRoot "System32\conhost.exe"
if (Test-Path $conhost) {
    $action = New-ScheduledTaskAction -Execute $conhost -Argument ("--headless powershell.exe " + $arg)
} else {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg
}
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
# One run at a time: while Claude is working (minutes), the next start is skipped (IgnoreNew).
# The limit must be longer than claude_timeout_sec (30 minutes by default).
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 60)

# Say so when the registration fails (it used to print "registered" even then), and exit non-zero.
try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "discord-mention-triage: reads Discord mentions (read-only) and starts Claude only when there is a new one" `
        -Force -ErrorAction Stop | Out-Null
} catch {
    Write-Host "FAILED to register: $taskName"
    Write-Host $_.Exception.Message
    exit 1
}
if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    Write-Host "FAILED to register: $taskName (the task is not there after registering)"
    exit 1
}
Write-Host "registered: $taskName (every $IntervalMinutes minutes). Check with: Get-ScheduledTaskInfo -TaskName $taskName"
