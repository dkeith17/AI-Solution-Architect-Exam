# Create .venv with Python 3.14 and install the project with test dependencies.
# Usage (from anywhere):  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    py -V:3.14 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the venv with Python 3.14. Install it with: py install 3.14" }

    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed" }

    .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }

    Write-Host "Setup complete. Run the tests with: scripts\run-tests.ps1"
}
finally {
    Pop-Location
}
