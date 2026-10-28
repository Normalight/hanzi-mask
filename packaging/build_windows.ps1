[CmdletBinding()]
param(
    [string]$Python,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable failed with exit code $LASTEXITCODE"
    }
}

if ($env:OS -ne 'Windows_NT') { throw 'Windows x64 is required.' }
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
if (!(Test-Path -LiteralPath $vswhere)) { throw 'Install MSVC C++ Build Tools first.' }
$vsPath = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (!$vsPath) { throw 'Install the MSVC x64 C++ tools and Windows SDK first.' }
$devShell = Join-Path $vsPath 'Common7/Tools/Microsoft.VisualStudio.DevShell.dll'
Import-Module $devShell
Enter-VsDevShell -VsInstallPath $vsPath -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64'

Push-Location $projectRoot
$savedEnvironment = @{}
foreach ($name in @('PYTHONUTF8', 'QT_QPA_PLATFORM', 'HANZI_PROTECT', 'PATH')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    $env:PYTHONUTF8 = '1'
    $env:QT_QPA_PLATFORM = 'offscreen'
    $env:HANZI_PROTECT = '1'
    $venvPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
    if (!(Test-Path -LiteralPath $venvPython)) {
        if ($Python) {
            Invoke-Checked $Python @('-m', 'venv', '.venv')
        } else {
            Invoke-Checked 'py' @('-3.11', '-m', 'venv', '.venv')
        }
    }
    Invoke-Checked $venvPython @('-c', 'import pathlib, sys; root = pathlib.Path.cwd().resolve(); base = pathlib.Path(sys.base_prefix).resolve(); assert not base.is_relative_to(root), ''Install the base Python interpreter outside the project directory.''')
    $basePython = & $venvPython -c 'import sys; print(sys.base_prefix)'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate the base Python runtime.' }
    $runtimeBin = Join-Path $basePython 'Library/bin'
    if (Test-Path -LiteralPath $runtimeBin) {
        $env:PATH = "$runtimeBin;$basePython;$env:PATH"
    }
    Invoke-Checked $venvPython @('packaging/build_desktop.py', '--check', '--target', 'windows-x64')
    Invoke-Checked $venvPython @('-m', 'pip', 'install', '--no-cache-dir', '--timeout', '120', '--retries', '5', '--only-binary=:all:', '-r', 'packaging/requirements-windows.txt')
    Invoke-Checked $venvPython @('-m', 'pip', 'check')
    if (!$SkipTests) {
        New-Item -ItemType Directory -Path 'build' -Force | Out-Null
        Invoke-Checked $venvPython @('-m', 'pytest', '-q', '--junitxml=build/test-results.xml')
    }
    Invoke-Checked $venvPython @('packaging/build_desktop.py', '--target', 'windows-x64')
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
    Pop-Location
}
