<#
.SYNOPSIS
Build the Windows package: dist\Scuffle\ with Scuffle.exe and its version's folder, and
dist\Scuffle-<version>-windows-x64.zip of it for a release.

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
$package = Join-Path $root 'dist\Scuffle'
$exe = Join-Path $package 'Scuffle.exe'
$help = & $exe --help | Out-String
if ($LASTEXITCODE -or $help -notmatch 'usage: scuffle') { throw "Scuffle.exe --help failed:`n$help" }
# The browser engine loads in the package, with settings of its own rather than the user's.
$saved = $env:APPDATA, $env:LOCALAPPDATA
$env:APPDATA = $env:LOCALAPPDATA = Join-Path $work 'empty-profile'
try {
    $started = Get-Date
    $report = & $exe diagnostics | Out-String
    $seconds = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
} finally {
    $env:APPDATA, $env:LOCALAPPDATA = $saved
}
if ($LASTEXITCODE -or $report -notmatch "Sign-in: Scuffle's window") { throw "Scuffle.exe has no sign-in window:`n$report" }
$version = (& $python -c "import tomllib; print(tomllib.load(open(r'$root\pyproject.toml', 'rb'))['project']['version'])").Trim()
$zip = Join-Path $root "dist\Scuffle-$version-windows-x64.zip"
Remove-Item -Force -ErrorAction SilentlyContinue $zip
Invoke-Native $python (Join-Path $PSScriptRoot 'zip_package.py') $package $zip
$size = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Output "Built $zip ($size MB, a report in $seconds s)"
