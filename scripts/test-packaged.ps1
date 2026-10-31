param(
    [string]$Executable = 'dist\PickHero.exe',
    [string]$ReportPath = 'build\packaged-selftest.json'
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot
$executablePath = (Resolve-Path -LiteralPath $Executable).Path
$report = if ([IO.Path]::IsPathRooted($ReportPath)) {
    [IO.Path]::GetFullPath($ReportPath)
} else {
    [IO.Path]::GetFullPath((Join-Path $projectRoot $ReportPath))
}
if ([IO.Path]::GetExtension($report) -ne '.json') { throw 'Der Testbericht muss eine JSON-Datei sein.' }
if (Test-Path -LiteralPath $report -PathType Container) { throw 'Der Berichtspfad ist ein Verzeichnis.' }
New-Item -ItemType Directory -Path (Split-Path -Parent $report) -Force | Out-Null
# A stale successful report must never satisfy a failed new invocation.
if (Test-Path -LiteralPath $report) { Remove-Item -LiteralPath $report }
$arguments = '--self-test "' + $report + '"'
$process = Start-Process -FilePath $executablePath -ArgumentList $arguments -PassThru -WindowStyle Hidden
if (-not $process.WaitForExit(60000)) {
    $process.Kill()
    throw 'Der Selbsttest der EXE hat das Zeitlimit ueberschritten.'
}
if ($process.ExitCode -ne 0) { throw "Der Selbsttest der EXE ist fehlgeschlagen: $report" }
$result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
if ($result.ok -ne $true -or $result.render_cases -ne 72 -or $result.interface_render_cases -ne 252 -or $result.expansion_render_cases -ne 1368 -or $result.completion_render_cases -ne 468 -or $result.audio_profiles -ne 3) {
    throw 'Der Selbsttest der EXE ist unvollstaendig.'
}
if ([version]$result.python -lt [version]'3.12.14' -or $result.python -notmatch '^3\.12\.' -or
        [version]($result.expat -replace '^expat_', '') -lt [version]'2.8.3') {
    throw 'Die EXE enthaelt eine unpassende oder ungepatchte Laufzeit.'
}
if ($result.license_texts_verified -lt 1) {
    throw 'Die Lizenznachweise der EXE wurden nicht geprueft.'
}
if ($result.music_render_cases -ne 1152) {
    throw 'Die Musikstuecke und ihre Ergebnisansichten wurden nicht vollstaendig geprueft.'
}
if ($result.library_render_cases -ne 216) {
    throw 'Die drei Bibliotheksbereiche mit ihren Einstellungen wurden nicht vollstaendig geprueft.'
}
Write-Host "EXE erfolgreich geprueft: 72 Hauptansichten, 252 Unterseiten/Overlays, 1368 Lern-/Importansichten, 468 Einrichtungs-/Passagenansichten, 3 Audio-Profile. Bericht: $report"
