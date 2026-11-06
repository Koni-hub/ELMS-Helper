$ErrorActionPreference = 'Stop'

$projectRoot = $PSScriptRoot
$venvPython = Join-Path $projectRoot '_system\.venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    $pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pythonLauncher) {
        & $pythonLauncher.Source -3 -m venv (Join-Path $projectRoot '_system\.venv')
    } else {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw 'Python 3 was not found. Install Python 3.10 or newer, then run this setup again.'
        }
        & $pythonCommand.Source -m venv (Join-Path $projectRoot '_system\.venv')
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python virtual environment.' }
}

& $venvPython -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required.' }

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Could not update pip.' }

& $venvPython -m pip install -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install the required Python packages.' }

Write-Host ''
Write-Host 'Python dependencies are ready.'
