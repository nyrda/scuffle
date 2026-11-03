<#
.SYNOPSIS
Build dist\Scuffle.exe, the one-file Windows package.

.DESCRIPTION
Runs on Windows from a checkout. Build tools are installed from the hashed
requirements.txt into a virtual environment, which is reused while the
requirements stay the same.
#>
param(
    [string] $Python = '3.14',
    [string] $Venv = ''
)
$ErrorActionPreference = 'Stop'

function Invoke-Native {
    $command, $arguments = $args
    & $command @arguments
    if ($LASTEXITCODE) { throw "$command exited with $LASTEXITCODE" }
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$work = Join-Path $root 'build\windows'
$requirements = Join-Path $PSScriptRoot 'requirements.txt'
if (-not $Venv) { $Venv = Join-Path $work 'venv' }
New-Item -ItemType Directory -Force $work | Out-Null

$stamp = Join-Path $Venv 'requirements.sha256'
$wanted = "$Python $((Get-FileHash $requirements -Algorithm SHA256).Hash)"
if (-not (Test-Path $stamp) -or (Get-Content $stamp) -ne $wanted) {
    if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
    Invoke-Native py "-$Python" -m venv $Venv
    Invoke-Native "$Venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check `
        --require-hashes --only-binary ':all:' -r $requirements
    Set-Content $stamp $wanted
}
$python = Join-Path $Venv 'Scripts\python.exe'

$icon = Join-Path $work 'Scuffle.ico'
Invoke-Native $python (Join-Path $root 'packaging\make_icon.py') (Join-Path $root 'src\scuffle\ui\icon.svg') $icon (Join-Path $root 'src\scuffle\ui\icon-small.svg')

$env:SCUFFLE_ICON = $icon
Invoke-Native $python -m PyInstaller --noconfirm --clean --log-level WARN `
    --distpath (Join-Path $root 'dist') --workpath (Join-Path $work 'pyinstaller') `
    (Join-Path $PSScriptRoot 'scuffle.spec')

# The windowed program writes to a redirected stream, and PowerShell waits for it.
$exe = Join-Path $root 'dist\Scuffle.exe'
$help = & $exe --help | Out-String
if ($LASTEXITCODE -or $help -notmatch 'usage: scuffle') { throw "Scuffle.exe --help failed:`n$help" }
$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Output "Built $exe ($size MB)"
