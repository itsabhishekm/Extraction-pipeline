# One-shot setup for the CFPB card extraction pipeline on a Windows PC with an NVIDIA GPU.
#
# Run in PowerShell from the repo folder:
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -InputRoot "D:\2026_Q1"
#
#   -InputRoot   folder with one subfolder per issuer (the CFPB 2026_Q1 download)      [asked if missing]
#   -PilotRoot   folder with the 19 pilot PDFs  (default: <InputRoot>\_pipeline_sample\pilot)
#   -DataRoot    where models, the Python environment and all pipeline output go
#                (default: D:\cc_pipeline if a D: drive exists, else C:\cc_pipeline)
#   -SkipModels  do not download the AI models (Ollama models and BGE-M3)
#   -Reconfigure rewrite config.yaml / config.pilot.yaml from the templates
#
# What it does (every step checks first and skips what is already done, so re-run it any time):
#    1. installs what is missing with winget: Git, Python 3.12, Ollama, Docker Desktop (+ WSL 2)
#    2. checks the NVIDIA driver (it cannot install drivers)
#    3. creates the data folders and points Ollama / Hugging Face / pip downloads at them
#    4. writes config.yaml and config.pilot.yaml for this computer
#    5. creates the Python environment (<DataRoot>\venv) with PyTorch for CUDA and requirements.txt
#    6. downloads BGE-M3 (embeddings) to <DataRoot>\models\bge-m3
#    7. starts Ollama and downloads GLM-OCR and Qwen3.5-4B to <DataRoot>\models\ollama
#    8. starts Docker Desktop, creates Postgres (schema + seeds) and Qdrant
#    9. runs check_setup.py --models (every line should say OK)
# Installing Docker Desktop / WSL asks for administrator approval and may need ONE restart:
# the script says so; restart and run it again.
# Full log: <DataRoot>\logs\setup_<time>.log

param(
    [string]$InputRoot = "",
    [string]$PilotRoot = "",
    [string]$DataRoot = "",
    [switch]$SkipModels,
    [switch]$Reconfigure
)

$ErrorActionPreference = "Continue"   # PowerShell 5.1 treats stderr from native tools as errors; we check exit codes
$here = $PSScriptRoot
Set-Location $here

function Step($m) { Write-Host ""; Write-Host "=== $m ===" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  OK    $m" -ForegroundColor Green }
function Note($m) { Write-Host "  NOTE  $m" -ForegroundColor Yellow }
function Fail($m) {
    Write-Host "  FAIL  $m" -ForegroundColor Red
    Write-Host "Fix the problem above and run setup.ps1 again; finished steps are skipped." -ForegroundColor Yellow
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}
function RefreshPath {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}
function Have($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function WingetInstall($id, $name, [string[]]$extra = @()) {
    if (-not (Have "winget")) { Fail "winget is not available, so $name cannot be installed automatically. Install $name by hand and re-run." }
    Write-Host "  installing $name with winget (approve the administrator prompt if one appears) ..."
    & winget install --id $id -e --silent --accept-source-agreements --accept-package-agreements @extra
    RefreshPath
}

# ------------------------------------------------------------------ paths
if (-not $DataRoot) { $DataRoot = if (Test-Path "D:\") { "D:\cc_pipeline" } else { "C:\cc_pipeline" } }
if (-not $InputRoot) {
    $InputRoot = Read-Host "Folder with the issuer subfolders (the 2026_Q1 PDFs), e.g. D:\2026_Q1"
}
$InputRoot = $InputRoot.Trim('"').TrimEnd('\')
if (-not $PilotRoot) { $PilotRoot = Join-Path $InputRoot "_pipeline_sample\pilot" }

$drive     = Split-Path -Qualifier $DataRoot
$ollamaDir = Join-Path $DataRoot "models\ollama"
$bgeDir    = Join-Path $DataRoot "models\bge-m3"
$hfDir     = Join-Path $DataRoot "models\hf"
$pipCache  = Join-Path $DataRoot "pip-cache"
$venv      = Join-Path $DataRoot "venv"
$vpy       = Join-Path $venv "Scripts\python.exe"
$logDir    = Join-Path $DataRoot "logs"

if (-not (Test-Path "$drive\")) { Write-Host "Drive $drive not found. Pass -DataRoot on an existing drive." -ForegroundColor Red; exit 1 }
foreach ($d in @($DataRoot, $ollamaDir, $bgeDir, $hfDir, $pipCache, $logDir)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
Start-Transcript -Path (Join-Path $logDir ("setup_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")) | Out-Null
Write-Host "Repo:       $here"
Write-Host "PDFs:       $InputRoot"
Write-Host "Pilot PDFs: $PilotRoot"
Write-Host "Data root:  $DataRoot"
$freeGB = [math]::Round((Get-PSDrive ($drive.TrimEnd(':'))).Free / 1GB, 1)
if ($freeGB -lt 40) { Fail "only $freeGB GB free on $drive; about 40 GB is needed (models, Python, Docker data, pipeline output)" }
Ok "$freeGB GB free on $drive"
if (-not (Test-Path $InputRoot)) { Note "PDF folder $InputRoot not found yet. Copy the PDFs there before running the pipeline." }
if (-not (Test-Path $PilotRoot)) { Note "pilot folder $PilotRoot not found. Copy _pipeline_sample\pilot there (or pass -PilotRoot) before running the pilot." }

# ------------------------------------------------------------------ 1. software
Step "1. Software: Git, Python 3.12, Ollama, Docker Desktop"
RefreshPath
if (Have "git") { Ok "Git" } else { WingetInstall "Git.Git" "Git"; if (Have "git") { Ok "Git installed" } else { Note "Git not found after install (only needed for git pull)" } }

$pyCmd = $null    # e.g. "py -3.12" or a full path to python.exe
foreach ($cand in @("py -3.12", "py -3.13", "py -3.11", "python")) {
    $exe, $arg = $cand.Split(" ", 2)
    if (Have $exe) {
        if ($arg) { $v = & $exe $arg -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null }
        else      { $v = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null }
        if ($LASTEXITCODE -eq 0 -and "$v" -match '^3\.1[0-3]$') { $pyCmd = $cand; break }
    }
}
if (-not $pyCmd) {
    WingetInstall "Python.Python.3.12" "Python 3.12" @("--scope", "user")
    $userPy = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path $userPy) { $pyCmd = $userPy } elseif (Have "py") { $pyCmd = "py -3.12" }
    if (-not $pyCmd) { Fail "Python 3.12 not found after install. Open a new PowerShell window and re-run setup.ps1." }
}
Ok "Python: $pyCmd"

$ollamaExe = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
if (-not (Test-Path $ollamaExe) -and (Have "ollama")) { $ollamaExe = (Get-Command ollama).Source }
if (-not (Test-Path $ollamaExe)) {
    WingetInstall "Ollama.Ollama" "Ollama"
    $ollamaExe = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (-not (Test-Path $ollamaExe) -and (Have "ollama")) { $ollamaExe = (Get-Command ollama).Source }
    if (-not (Test-Path $ollamaExe)) { Fail "Ollama not found after install. Install it from https://ollama.com/download/windows and re-run." }
}
Ok "Ollama: $ollamaExe"

# Docker Desktop (it runs on WSL 2, which is installed with it)
$needRestart = $false
$dockerDesktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
if (-not (Test-Path $dockerDesktop)) {
    Write-Host "  installing WSL 2 (administrator prompt) ..."
    Start-Process -FilePath "wsl.exe" -ArgumentList "--install", "--no-distribution" -Verb RunAs -Wait
    WingetInstall "Docker.DockerDesktop" "Docker Desktop"
    if (-not (Test-Path $dockerDesktop)) { Fail "Docker Desktop not found after install. Install it from https://www.docker.com/products/docker-desktop/ and re-run." }
    $needRestart = $true
    Ok "WSL 2 and Docker Desktop installed"
} else { Ok "Docker Desktop" }
if ($needRestart) {
    Write-Host ""
    Write-Host "RESTART NEEDED: WSL / Docker Desktop were just installed. Restart Windows, then run setup.ps1 again" -ForegroundColor Yellow
    Write-Host "(same command). It continues from here." -ForegroundColor Yellow
    Stop-Transcript | Out-Null
    exit 3
}

# ------------------------------------------------------------------ 2. GPU
Step "2. NVIDIA GPU and driver"
if (-not (Have "nvidia-smi")) { Fail "nvidia-smi not found: install the NVIDIA driver (https://www.nvidia.com/en-us/drivers/) and re-run." }
$smi = (& nvidia-smi) | Out-String
$m = [regex]::Match($smi, "CUDA Version:\s*([\d.]+)")
if (-not $m.Success) { Fail "could not read the driver's CUDA version from nvidia-smi; update the NVIDIA driver." }
$driverCuda = [double]$m.Groups[1].Value
$gpu = ((& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits) | Select-Object -First 1).Split(",")
$gpuName = $gpu[0].Trim(); $gpuMB = [int]$gpu[1].Trim()
if (($gpuName -match "Blackwell|RTX 50|RTX PRO") -and $driverCuda -lt 12.8) {
    Fail "$gpuName needs a driver that supports CUDA 12.8 or newer (this one: $driverCuda). Update the NVIDIA driver and re-run."
}
Ok "$gpuName, $([math]::Round($gpuMB / 1024)) GB, driver supports CUDA $driverCuda"

# ------------------------------------------------------------------ 3. download folders + Ollama settings
Step "3. Model folders and Ollama settings"
$big = $gpuMB -ge 20000      # 20+ GB of GPU memory: keep all models loaded, serve requests in parallel
$vars = [ordered]@{
    "OLLAMA_MODELS" = $ollamaDir; "HF_HOME" = $hfDir; "PIP_CACHE_DIR" = $pipCache;
    "OLLAMA_FLASH_ATTENTION" = "1";
    "OLLAMA_NUM_PARALLEL" = $(if ($big) { "8" } else { "1" });
    "OLLAMA_MAX_LOADED_MODELS" = $(if ($big) { "3" } else { "1" })
}
foreach ($k in $vars.Keys) {
    [Environment]::SetEnvironmentVariable($k, $vars[$k], "User")
    Set-Item -Path "Env:$k" -Value $vars[$k]
    Ok "$k = $($vars[$k])"
}

# ------------------------------------------------------------------ 4. config files
Step "4. config.yaml and config.pilot.yaml for this computer"
foreach ($pair in @(@("config.example.yaml", "config.yaml", $InputRoot), @("config.pilot.example.yaml", "config.pilot.yaml", $PilotRoot))) {
    $src = Join-Path $here $pair[0]; $dst = Join-Path $here $pair[1]
    if ((Test-Path $dst) -and -not $Reconfigure) { Ok "$($pair[1]) exists (use -Reconfigure to rewrite it)"; continue }
    $t = [IO.File]::ReadAllText($src)
    $t = $t.Replace("__INPUT_ROOT__", $pair[2]).Replace("__PILOT_ROOT__", $pair[2]).Replace("__DATA_ROOT__", $DataRoot)
    [IO.File]::WriteAllText($dst, $t)
    Ok "$($pair[1]) -> input $($pair[2]), data $DataRoot"
}

# ------------------------------------------------------------------ 5. Python environment
Step "5. Python environment at $venv"
if (-not (Test-Path $vpy)) {
    if (Test-Path $pyCmd) { & $pyCmd -m venv $venv } else {
        $exe, $arg = $pyCmd.Split(" ", 2)
        if ($arg) { & $exe $arg -m venv $venv } else { & $exe -m venv $venv }
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $vpy)) { Fail "could not create $venv" }
    Ok "created"
} else { Ok "exists" }
& $vpy -m pip install --upgrade pip --quiet
& $vpy -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>$null
if ($LASTEXITCODE -eq 0) { Ok "PyTorch with CUDA already installed" } else {
    $done = $false
    foreach ($b in @("cu130", "cu129", "cu128", "cu126", "cu124", "cu121")) {
        $n = $b.Substring(2); $cv = [double]("{0}.{1}" -f $n.Substring(0, $n.Length - 1), $n.Substring($n.Length - 1))
        if ($cv -gt $driverCuda) { continue }
        Write-Host "  trying PyTorch for CUDA $cv ..."
        & $vpy -m pip uninstall -y torch --quiet 2>$null | Out-Null
        & $vpy -m pip install torch --index-url "https://download.pytorch.org/whl/$b"
        if ($LASTEXITCODE -ne 0) { continue }
        & $vpy -c "import torch, sys; x = torch.ones(2, device='cuda'); sys.exit(0 if float(x.sum()) == 2 else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $done = $true; Ok "PyTorch $b works on the GPU"; break }
    }
    if (-not $done) { Fail "no CUDA build of PyTorch worked with this driver. Install it with the command from https://pytorch.org/get-started/locally and re-run." }
}
& $vpy -m pip install -r (Join-Path $here "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install -r requirements.txt failed (see above)" }
& $vpy -c "import torch, sys; sys.exit(0 if torch.cuda.is_available() else 1)"
if ($LASTEXITCODE -ne 0) { Fail "a package replaced PyTorch with the CPU-only build; re-run setup.ps1 to reinstall the CUDA build" }
Ok "pipeline packages installed"

# ------------------------------------------------------------------ 6. BGE-M3
Step "6. BGE-M3 embedding model -> $bgeDir"
if ($SkipModels) { Note "skipped (-SkipModels)" } else {
    $base = "https://huggingface.co/BAAI/bge-m3/resolve/main"
    $files = [ordered]@{ "config.json" = 100; "sparse_linear.pt" = 1000; "colbert_linear.pt" = 1000000;
        "sentencepiece.bpe.model" = 1000000; "tokenizer.json" = 1000000; "tokenizer_config.json" = 100;
        "special_tokens_map.json" = 100; "pytorch_model.bin" = 2000000000 }
    foreach ($f in $files.Keys) {
        $dest = Join-Path $bgeDir $f
        if ((Test-Path $dest) -and ((Get-Item $dest).Length -ge $files[$f])) { Ok "$f"; continue }
        Write-Host "  downloading $f ..."
        & curl.exe -L --fail --retry 5 --retry-delay 5 -C - -o $dest "$base/$f"
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $dest) -or (Get-Item $dest).Length -lt $files[$f]) { Fail "download of $f failed; re-run to resume" }
        Ok "$f"
    }
}

# ------------------------------------------------------------------ 7. Ollama models
Step "7. Ollama: GLM-OCR and Qwen3.5-4B -> $ollamaDir"
# restart Ollama so it picks up the settings from step 3
Get-Process -Name "ollama app", "ollama" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
$ollamaApp = Join-Path (Split-Path $ollamaExe) "ollama app.exe"
if (Test-Path $ollamaApp) { Start-Process -FilePath $ollamaApp } else { Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden }
$up = $false
for ($i = 0; $i -lt 30; $i++) { Start-Sleep -Seconds 2; & $ollamaExe list *> $null; if ($LASTEXITCODE -eq 0) { $up = $true; break } }
if (-not $up) { Fail "Ollama did not start; start it from the Start menu and re-run." }
Ok "Ollama running"
if ($SkipModels) { Note "skipped (-SkipModels)" } else {
    foreach ($model in @("glm-ocr", "qwen3.5:4b")) {
        Write-Host "  pulling $model ..."
        & $ollamaExe pull $model
        if ($LASTEXITCODE -ne 0) { Fail "ollama pull $model failed" }
        Ok $model
    }
}

# ------------------------------------------------------------------ 8. Docker: Postgres + Qdrant
Step "8. Docker: Postgres and Qdrant"
RefreshPath
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  starting Docker Desktop (the first start can take a few minutes; accept its terms if it asks) ..."
    Start-Process -FilePath $dockerDesktop
    for ($i = 0; $i -lt 90; $i++) { Start-Sleep -Seconds 4; & docker info *> $null; if ($LASTEXITCODE -eq 0) { break } }
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    $err = (& docker info 2>&1) | Out-String
    if ($err -match "denied|docker-users") {
        Write-Host "  adding $env:USERNAME to the docker-users group (administrator prompt) ..."
        Start-Process -FilePath "net.exe" -ArgumentList "localgroup", "docker-users", $env:USERNAME, "/add" -Verb RunAs -Wait
        Fail "added you to docker-users. Sign out of Windows and back in, then re-run setup.ps1."
    }
    Fail "Docker is not running. Open Docker Desktop, wait until it says 'Engine running', and re-run setup.ps1."
}
Ok "Docker running"
Note "to keep Docker data off C:, set Docker Desktop > Settings > Resources > Advanced > Disk image location to a folder on $drive"
& docker compose up -d
if ($LASTEXITCODE -ne 0) { Fail "docker compose up -d failed (see above)" }
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    $n = & docker exec cc-postgres psql -U cc -d cc -tAc "select count(*) from cc.field_definitions" 2>$null
    if ("$n".Trim() -eq "31") { $ready = $true; break }
}
if (-not $ready) { Fail "Postgres did not finish creating the schema; check: docker logs cc-postgres" }
Ok "Postgres on localhost:5432 (schema cc, 31 fields), Qdrant on http://localhost:6333"

# ------------------------------------------------------------------ 9. check
Step "9. Check everything"
& $vpy check_setup.py --models
$rc = $LASTEXITCODE
& $vpy -m tests.test_gate
if ($LASTEXITCODE -ne 0) { $rc = 1 }

Write-Host ""
if ($rc -eq 0) {
    Write-Host "Setup complete." -ForegroundColor Green
    Write-Host "Every time you open a new PowerShell window in this folder:   . .\start.ps1"
    Write-Host "  (starts Docker + Ollama and activates the Python environment)"
    Write-Host "Then run the pilot:   powershell -ExecutionPolicy Bypass -File .\next_steps.ps1"
} else {
    Write-Host "Setup finished, but some checks failed (see above). Fix them and re-run setup.ps1." -ForegroundColor Yellow
}
Stop-Transcript | Out-Null
exit $rc
