$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot

function Assert-WorkspacePath([string]$Path) {
    $absolute = [IO.Path]::GetFullPath($Path)
    $prefix = $projectRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $absolute.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Pfad liegt ausserhalb des Projekts: $absolute"
    }
    # Resolve-Path alone does not follow Windows junction targets. Refuse
    # archive operations through any reparse point below the project root.
    $ancestor = $absolute
    while ($ancestor -ne $projectRoot -and $ancestor.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        if (Test-Path -LiteralPath $ancestor) {
            $item = Get-Item -LiteralPath $ancestor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Verknuepfung kann nicht sicher archiviert werden: $ancestor"
            }
        }
        $ancestor = Split-Path -Parent $ancestor
    }
    return $absolute
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw ('uv fehlt / uv is missing. Einmalig installieren / install once: ' +
           'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"')
}

# A project-local runtime: no PATH or registry changes, same CPython ABI as aubio.
# Taken from uv's own built-in list. This used to point uv at a download list
# on uv's main branch; uv moved that file, the address answered 404, and a
# fresh setup failed for everyone. A current uv already knows 3.12.14.
uv python install 3.12.14 --install-dir .python-runtime --no-bin --no-registry --no-config
if ($LASTEXITCODE -ne 0) {
    throw ('Python 3.12.14 konnte nicht eingerichtet werden / could not be set up. ' +
           'uv aktualisieren / update uv: uv self update')
}
$runtimePython = Join-Path $projectRoot '.python-runtime\cpython-3.12.14-windows-x86_64-none\python.exe'
$venvPath = Join-Path $projectRoot '.venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'
$sameAbi = $false
if ((Test-Path -LiteralPath (Join-Path $venvPath 'pyvenv.cfg')) -and (Test-Path -LiteralPath $venvPython)) {
    try {
        $existingAbi = & $venvPython -c "import sys, struct; print('%s.%s|%s' % (*sys.version_info[:2], struct.calcsize('P') * 8))"
        $sameAbi = $LASTEXITCODE -eq 0 -and "$existingAbi".Trim() -eq '3.12|64'
    } catch { $sameAbi = $false }
}
$backupTag = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
if ($sameAbi) {
    # Only interpreter launchers/configuration change; retain site-packages.
    $backup = Assert-WorkspacePath (Join-Path $projectRoot ('scratchpad\runtime-backup-' + $backupTag))
    $venvPath = Assert-WorkspacePath $venvPath
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $venvPath 'pyvenv.cfg') -Destination $backup
    foreach ($launcher in @('python.exe', 'pythonw.exe')) {
        $source = Join-Path $venvPath ('Scripts\' + $launcher)
        if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $backup }
    }
    & $runtimePython -m venv --upgrade $venvPath
} else {
    if (Test-Path -LiteralPath $venvPath) {
        # Extensions built for another Python minor version or architecture
        # cannot survive --upgrade. Preserve that environment intact instead.
        $venvPath = Assert-WorkspacePath $venvPath
        $archive = Assert-WorkspacePath (Join-Path $projectRoot ('scratchpad\venv-backup-' + $backupTag))
        New-Item -ItemType Directory -Path (Split-Path -Parent $archive) -Force | Out-Null
        Move-Item -LiteralPath $venvPath -Destination $archive
        Write-Host "Bisherige Umgebung gesichert: $archive"
    }
    & $runtimePython -m venv $venvPath
}
if ($LASTEXITCODE -ne 0) { throw 'Virtuelle Umgebung konnte nicht eingerichtet werden. / Could not set up the virtual environment.' }

$wheel = Join-Path $projectRoot 'wheels\aubio-0.4.9-cp312-cp312-win_amd64.whl'
if ((Get-FileHash -LiteralPath $wheel -Algorithm SHA256).Hash -ne '624B508CFB51C34ABF682EA3EB64E8A12CDBB03FF214B688683FF82328052DD5') {
    throw 'Die aubio-Datei stimmt nicht mit der geprueften Pruefsumme ueberein. / The aubio file does not match its checked checksum.'
}
uv pip install --python $venvPython --find-links wheels -c constraints.txt -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Abhaengigkeiten konnten nicht installiert werden. / Could not install the dependencies.' }
& $venvPython -m pickhero --self-test scratchpad/setup-selftest.json
if ($LASTEXITCODE -ne 0) { throw 'Selbsttest fehlgeschlagen, siehe / Self-test failed, see scratchpad/setup-selftest.json' }
Write-Host 'PickHero ist eingerichtet, Start mit START.bat. / PickHero is set up, start it with START.bat.'
