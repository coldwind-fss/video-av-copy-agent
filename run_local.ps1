param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) {
    python -m venv .venv
    $Python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
}
& $Python -m pip install -r requirements.txt
$env:PORT = [string]$Port
& $Python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
