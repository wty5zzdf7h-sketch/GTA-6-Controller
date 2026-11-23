# Richtet den Restock-Wächter auf einem Windows-Notebook ein (einmalig, als Administrator ausführen).
#   - Aufgabe „GTA6-Restock-Waechter“ alle 10 Minuten, läuft als SYSTEM
#     (= unabhängig von der Benutzeranmeldung, kein Passwort nötig), auch nach Neustart.
#   - NTFY_TOPIC als systemweite Umgebungsvariable.
#   - Energieoptionen am Netzteil: kein Standby/Ruhezustand, Zuklappen = nichts tun.
#
# Aufruf (PowerShell als Administrator, im Repo-Ordner):
#   powershell -ExecutionPolicy Bypass -File .\install-windows-task.ps1 -Topic <dein-topic>
param(
    [Parameter(Mandatory = $true)][string]$Topic,
    [string]$Python = "",
    [string]$TaskName = "GTA6-Restock-Waechter"
)
$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Bitte PowerShell als Administrator starten."
}

$repo = $PSScriptRoot
if (-not $Python) {
    $Python = (Get-Command python.exe -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike "*WindowsApps*" } | Select-Object -First 1).Source
    if (-not $Python) { $Python = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe" }
}
if (-not (Test-Path $Python)) { throw "python.exe nicht gefunden: $Python" }

# 1) Topic systemweit (die Aufgabe läuft als SYSTEM und sieht keine Benutzer-Variablen)
[Environment]::SetEnvironmentVariable("NTFY_TOPIC", $Topic, "Machine")

# 2) Aufgabe
New-Item -ItemType Directory -Force (Join-Path $repo "state") | Out-Null
$log = Join-Path $repo "state\monitor-local.log"   # schreibt monitor.py selbst (settings.log_file)
$action = New-ScheduledTaskAction -Execute $Python -Argument "monitor.py --config config-local.yaml" -WorkingDirectory $repo

$every10 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 10)
$boot = New-ScheduledTaskTrigger -AtStartup
$boot.Delay = "PT2M"   # nach dem Start kurz aufs Netzwerk warten
$boot.Repetition = $every10.Repetition

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 8) -MultipleInstances IgnoreNew -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)
$system = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($every10, $boot) -Settings $settings `
    -Principal $system -Description "Restock-Wächter GTA VI DualSense (config-local.yaml)" -Force | Out-Null

# 3) Energieoptionen (nur am Netzteil)
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT

Start-ScheduledTask -TaskName $TaskName
Write-Host "Fertig. Aufgabe '$TaskName' läuft alle 10 Minuten als SYSTEM. Log: $log"
