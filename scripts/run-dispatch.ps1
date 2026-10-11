# Runs dispatch.py once. Task Scheduler calls this every few minutes.
# (ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less files in the ANSI code page.)
param(
    [Parameter(Mandatory = $true)][string]$User,
    [string]$Config = ""
)

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# The standard library is enough. A .venv is used only if one is left over from the older version.
$venv = Join-Path $repo ".venv\Scripts\python.exe"
$py = if (Test-Path $venv) { $venv } else { "python" }

$argsList = @("-X", "utf8", "pipeline\dispatch.py", "--user", $User)
if ($Config) { $argsList += @("--config", $Config) }

$line = (& $py @argsList 2>&1 | Out-String).Trim()
$code = $LASTEXITCODE

$logDir = Join-Path $env:USERPROFILE ".discord-mention-triage"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "dispatch.log"

# Do not log idle runs. Keep only runs where something happened, and errors.
$idle = '^OK (POLL=ok FOUND=0 EMITTED=0 CLAIMED=0 RAN=0|POLL=resident_watcher CLAIMED=0 RAN=0|SKIPPED=locked)$'
if ($code -ne 0 -or $line -notmatch $idle) {
    $stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$stamp exit=$code $line" | Add-Content -Path $log -Encoding ascii
}
# Rotate at 1 MB.
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 1MB)) { Move-Item -Force $log "$log.1" }
exit $code
