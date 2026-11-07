# Run unit tests, then (only if they pass) integration tests that start real servers.
# Usage (from anywhere):  powershell -ExecutionPolicy Bypass -File scripts\run-tests.ps1
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    $py = ".\.venv\Scripts\python.exe"
    if (-not (Test-Path $py)) {
        Write-Error "No virtual environment found. Run scripts\setup.ps1 first."
        exit 1
    }

    Write-Host "== Unit tests ==" -ForegroundColor Cyan
    & $py -m pytest -m "not integration"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Write-Host "== Integration tests (start local servers) ==" -ForegroundColor Cyan
    & $py -m pytest -m integration
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
