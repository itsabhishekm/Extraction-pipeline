# Start everything the pipeline needs and activate the Python environment.
# Run it with a dot and a space in front, from the repo folder, so the activation stays in your window:
#
#   . .\start.ps1
#
# It starts Docker Desktop (if needed), Postgres + Qdrant, and Ollama, then activates <data_root>\venv.
# Afterwards:  python run.py status --config config.pilot.yaml

function Get-CcDataRoot {
    $cfg = Join-Path $PSScriptRoot "config.yaml"
    if (-not (Test-Path $cfg)) { return $null }
    $line = Select-String -Path $cfg -Pattern "^data_root:\s*'?([^']+)'?\s*$" | Select-Object -First 1
    if ($line) { return $line.Matches[0].Groups[1].Value.Trim() }
    return $null
}

$ccRoot = Get-CcDataRoot
if (-not $ccRoot) {
    Write-Host "config.yaml not found or has no data_root. Run setup.ps1 first." -ForegroundColor Red
    return
}
Push-Location $PSScriptRoot
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")

# Docker
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    $dd = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dd) {
        Write-Host "Starting Docker Desktop ..."
        Start-Process -FilePath $dd
        for ($i = 0; $i -lt 60; $i++) { Start-Sleep -Seconds 3; & docker info *> $null; if ($LASTEXITCODE -eq 0) { break } }
    }
}
& docker info *> $null
if ($LASTEXITCODE -eq 0) {
    & docker compose up -d *> $null
    Write-Host "Docker: Postgres (localhost:5432) and Qdrant (localhost:6333) running" -ForegroundColor Green
} else {
    Write-Host "Docker is not running: open Docker Desktop and run . .\start.ps1 again" -ForegroundColor Yellow
}

# Ollama
$ollamaExe = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
if (-not (Test-Path $ollamaExe) -and (Get-Command ollama -ErrorAction SilentlyContinue)) { $ollamaExe = (Get-Command ollama).Source }
& $ollamaExe list *> $null
if ($LASTEXITCODE -ne 0) {
    $app = Join-Path (Split-Path $ollamaExe) "ollama app.exe"
    if (Test-Path $app) { Start-Process -FilePath $app } else { Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden }
    for ($i = 0; $i -lt 20; $i++) { Start-Sleep -Seconds 2; & $ollamaExe list *> $null; if ($LASTEXITCODE -eq 0) { break } }
}
if ($LASTEXITCODE -eq 0) { Write-Host "Ollama running" -ForegroundColor Green } else { Write-Host "Ollama did not start; start it from the Start menu" -ForegroundColor Yellow }

# Python environment
$act = Join-Path $ccRoot "venv\Scripts\Activate.ps1"
if (Test-Path $act) {
    . $act
    Write-Host "Python environment active: $ccRoot\venv" -ForegroundColor Green
} else {
    Write-Host "No Python environment at $ccRoot\venv. Run setup.ps1 first." -ForegroundColor Yellow
}
Pop-Location
