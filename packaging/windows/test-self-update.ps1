<#
.SYNOPSIS
Update a real Windows package to another real one on this Windows machine.

.DESCRIPTION
Builds two packages from copies of this checkout, one that asks a local server instead of GitHub
and a newer one, then runs the update: a wrong checksum must change nothing, and a good release
must unpack beside the running program, replace it, start the new version and clear away the old
program and its folder.
#>
param([Parameter(Mandatory)][string] $Venv)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Venv = (Resolve-Path $Venv).Path
$python = Join-Path $Venv 'Scripts\python.exe'
$work = Join-Path ([IO.Path]::GetTempPath()) "scuffle-update-$PID"
$port = 18443
$newVersion = '99.0.0'
$current = ((Get-Content (Join-Path $root 'pyproject.toml') -Raw) -split "`n" | Where-Object { $_ -match '^version' }) -replace '.*"(.*)".*', '$1'
$current = $current.Trim()

function Build-Copy([string] $name, [string] $version) {
    $copy = Join-Path $work $name
    New-Item -ItemType Directory -Force $copy | Out-Null
    foreach ($item in 'src', 'packaging', 'pyproject.toml') { Copy-Item -Recurse (Join-Path $root $item) $copy }
    $module = Join-Path $copy 'src\scuffle\selfupdate.py'
    $text = (Get-Content $module -Raw).Replace(
        'https://api.github.com/repos/nyrda/scuffle/releases/latest', "http://127.0.0.1:$port/latest").Replace(
        'https://github.com/nyrda/scuffle/releases/download/', "http://127.0.0.1:$port/download/")
    [IO.File]::WriteAllText($module, $text)
    $project = Join-Path $copy 'pyproject.toml'
    [IO.File]::WriteAllText($project, (Get-Content $project -Raw).Replace("version = `"$current`"", "version = `"$version`""))
    & (Join-Path $copy 'packaging\windows\build.ps1') -Venv $Venv | Out-Host     # only the path is this function's answer
    return Join-Path $copy 'dist'
}

function Invoke-Scuffle([string[]] $arguments) {
    $output = Join-Path $work 'output.txt'
    $errors = Join-Path $work 'errors.txt'
    # Not -Wait, which also waits for what the program starts, as the updated Scuffle's window.
    $process = Start-Process $exe -ArgumentList $arguments -PassThru -NoNewWindow `
        -RedirectStandardOutput $output -RedirectStandardError $errors
    $null = $process.Handle
    $process.WaitForExit()
    return [pscustomobject]@{ Code = $process.ExitCode; Output = (Get-Content $output -Raw); Errors = (Get-Content $errors -Raw) }
}

function Start-Server([string[]] $extra) {
    $server = Start-Process $python -PassThru -NoNewWindow -ArgumentList (@((Join-Path $root 'tools\self_update_server.py'), $port, $newVersion, $released) + $extra)
    Start-Sleep -Seconds 3
    return $server
}

function Assert-That([bool] $condition, [string] $what) {
    if (-not $condition) { throw "FAIL: $what" }
    Write-Host "ok: $what"
}

New-Item -ItemType Directory -Force $work | Out-Null
$env:APPDATA = Join-Path $work 'appdata'
$env:LOCALAPPDATA = Join-Path $work 'localappdata'
$env:QT_QPA_PLATFORM = 'offscreen'
New-Item -ItemType Directory -Force $env:APPDATA, $env:LOCALAPPDATA | Out-Null

$older = Build-Copy 'older' $current
$newer = Build-Copy 'newer' $newVersion
$publish = Join-Path $work 'publish'
New-Item -ItemType Directory -Force $publish | Out-Null
$released = Join-Path $publish "Scuffle-$newVersion-windows-x64.zip"
Copy-Item (Join-Path $newer "Scuffle-$newVersion-windows-x64.zip") $released
$install = Join-Path $work 'install'
Copy-Item -Recurse (Join-Path $older 'Scuffle') $install
$exe = Join-Path $install 'Scuffle.exe'
$before = (Get-FileHash $exe).Hash
$oldFolder = Join-Path $install "scuffle-$current"
$newFolder = Join-Path $install "scuffle-$newVersion"
Assert-That (Test-Path $oldFolder) 'the package keeps its version in a folder of its own'

Assert-That ((Invoke-Scuffle @('--version')).Output.Trim() -eq "scuffle $current") 'the old package reports its version'

$server = Start-Server @('--wrong-sum')
$refused = Invoke-Scuffle @('self-update')
Stop-Process -Id $server.Id -Force
Assert-That ($refused.Code -ne 0) 'a wrong checksum fails the update'
Assert-That ((Get-FileHash $exe).Hash -eq $before) 'a wrong checksum leaves the program as it was'
Assert-That (-not (Test-Path (Join-Path $install 'Scuffle.new.zip')) -and -not (Test-Path (Join-Path $install 'Scuffle.new.exe')) `
    -and -not (Test-Path $newFolder)) 'a wrong checksum leaves no download behind'

$server = Start-Server @()
$result = Invoke-Scuffle @('self-update', '--start')
Write-Host $result.Output $result.Errors
Assert-That ($result.Code -eq 0 -and $result.Output -match '"updated": true') 'the update reports success'
Assert-That ((Get-FileHash $exe).Hash -eq (Get-FileHash (Join-Path $newer 'Scuffle\Scuffle.exe')).Hash) 'the program file is the release'
Assert-That (Test-Path (Join-Path $newFolder 'base_library.zip')) 'the new version folder is beside the program'
Assert-That (-not (Test-Path (Join-Path $install 'Scuffle.new.zip'))) 'the download is gone'

# The old program and its folder go only once the new version has opened its window.
$old = Join-Path $install 'Scuffle.old.exe'
$deadline = (Get-Date).AddSeconds(120)
while (((Test-Path $old) -or (Test-Path $oldFolder)) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
Assert-That (-not (Test-Path $old)) 'the restarted Scuffle cleared away the old program'
Assert-That (-not (Test-Path $oldFolder)) 'the restarted Scuffle cleared away the old version folder'
$running = @(Get-Process Scuffle -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe })
Assert-That ($running.Count -gt 0) 'the updated Scuffle is running'
$running | Stop-Process -Force
Stop-Process -Id $server.Id -Force
Start-Sleep -Seconds 3
Assert-That ((Invoke-Scuffle @('--version')).Output.Trim() -eq "scuffle $newVersion") 'the updated program reports the new version'
