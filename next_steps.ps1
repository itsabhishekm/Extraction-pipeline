# Runs the steps after a code update, in order, and stops at the first one that fails.
#
#   powershell -ExecutionPolicy Bypass -File .\next_steps.ps1                  # pilot (default)
#   powershell -ExecutionPolicy Bypass -File .\next_steps.ps1 -Stage full      # whole quarter
#   powershell -ExecutionPolicy Bypass -File .\next_steps.ps1 -Stage templates # 150 answer-key files
#   powershell -ExecutionPolicy Bypass -File .\next_steps.ps1 -Stage label     # hand-check them
#   powershell -ExecutionPolicy Bypass -File .\next_steps.ps1 -Stage publish   # eval gate + release
#
#   -DataRoot     the pipeline data folder (default: data_root from config.yaml, written by setup.ps1)
#   -ResetDb      recreate Postgres + Qdrant (DELETES pipeline data; needed once after a schema change)
#   -Clean        start from zero: -ResetDb AND delete everything the pipeline wrote under DataRoot
#                 (text, json, reports, traces, logs, retry queue, integrity, evals, releases).
#                 Kept: the PDFs, models, venv, pip-cache, and the code folder (incl. the answer key).
#   -Reviewer     your name for review / labelling
#
# Order for a new quarter:
#   1. pilot -ResetDb   (first time after this update)   -> check the pilot results, review
#   2. full                                              -> python run.py review until nothing is left
#   3. templates -> label (150 cards)                    -> 4. publish
# A log of every run is written to <DataRoot>\logs\next_steps_<time>.log.

param(
    [ValidateSet("pilot", "full", "templates", "label", "publish")]
    [string]$Stage = "pilot",
    [string]$DataRoot = "",
    [switch]$ResetDb,
    [switch]$Clean,
    [string]$Reviewer = $env:USERNAME,
    [int]$Count = 150
)

$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
if (-not $DataRoot) {
    $line = Select-String -Path (Join-Path $here "config.yaml") -Pattern "^data_root:\s*'?([^']+)'?\s*$" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($line) { $DataRoot = $line.Matches[0].Groups[1].Value.Trim() }
    else { Write-Host "config.yaml not found: run setup.ps1 first (or pass -DataRoot)." -ForegroundColor Red; exit 1 }
}
$py = Join-Path $DataRoot "venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
$logDir = Join-Path $DataRoot "logs"

if ($Clean) {
    $ResetDb = $true
    if (-not ((Test-Path (Join-Path $DataRoot "venv")) -or (Test-Path (Join-Path $DataRoot "models")))) {
        Write-Host "$DataRoot does not look like the pipeline data folder (no venv or models inside). Nothing deleted." -ForegroundColor Red
        exit 1
    }
    $outputs = @("text", "json", "reports", "traces", "logs", "integrity", "evals", "releases", "retry_queue.jsonl")
    Write-Host "Deleting pipeline output under ${DataRoot}: $($outputs -join ', ')" -ForegroundColor Yellow
    Write-Host "(kept: models, venv, pip-cache, your PDFs, this code folder)"
    $answer = Read-Host "Type YES to continue"
    if ($answer -ne "YES") { Write-Host "Nothing deleted."; exit 1 }
    foreach ($o in $outputs) {
        $p = Join-Path $DataRoot $o
        if (Test-Path $p) {
            Remove-Item -Recurse -Force $p -ErrorAction SilentlyContinue
            if (Test-Path $p) { Write-Host "Could not delete $p (is a file open?)" -ForegroundColor Red; exit 1 }
            Write-Host "  deleted $p"
        }
    }
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("next_steps_" + (Get-Date -Format "yyyyMMdd_HHmmss") + ".log")
Start-Transcript -Path $log -Append | Out-Null

function Step($t) { Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }
function Fail($t) { Write-Host "STOPPED: $t" -ForegroundColor Red; Write-Host "Log: $log"; Stop-Transcript | Out-Null; exit 1 }
function Run($what, [string[]]$a) {
    Write-Host "> python $($a -join ' ')"
    & $py @a
    if ($LASTEXITCODE -ne 0) { Fail "$what failed (exit code $LASTEXITCODE)" }
}
function WaitPostgres {
    for ($i = 0; $i -lt 60; $i++) {
        docker exec cc-postgres pg_isready -U cc -d cc *> $null
        if ($LASTEXITCODE -eq 0) {
            $n = docker exec cc-postgres psql -U cc -d cc -tAc "select count(*) from cc.field_definitions" 2>$null
            if ("$n".Trim() -eq "31") { Write-Host "Postgres ready (31 fields)"; return }
        }
        Start-Sleep -Seconds 2
    }
    Fail "Postgres did not become ready (docker logs cc-postgres)"
}

$cfgArg = @()
if ($Stage -eq "pilot") { $cfgArg = @("--config", "config.pilot.yaml") }

Step "Docker (Postgres + Qdrant)"
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker is not running. Start Docker Desktop and run this again." }
if ($ResetDb) {
    Write-Host "Recreating the database and Qdrant (pipeline data is deleted; PDFs and .md files are kept)" -ForegroundColor Yellow
    docker compose down -v
}
docker compose up -d
if ($LASTEXITCODE -ne 0) { Fail "docker compose up failed" }
WaitPostgres

Step "Python packages"
& $py -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
Run "setup check" @("check_setup.py")
Run "rule checks" @("-m", "tests.test_gate")

switch ($Stage) {
    { $_ -in "pilot", "full" } {
        Step "Pipeline: scan .. retry, embed, report ($Stage)"
        Run "pipeline" (@("run.py", "all") + $cfgArg)
        Step "Human review"
        $csv = Join-Path $DataRoot "review_$Stage.csv"
        Run "review export" (@("run.py", "review", "--export", $csv) + $cfgArg)
        Write-Host "Open review items are in $csv. Decide them with:"
        Write-Host "   python run.py review $($cfgArg -join ' ')        (one by one)"
        Write-Host "   or fill the decision column in Excel, then: python run.py review --import $csv $($cfgArg -join ' ')"
        Step "Integrity check"
        & $py run.py integrity @cfgArg
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Integrity check did not pass yet (open review items count as a failure). See the lines above." -ForegroundColor Yellow
        }
        Run "status" (@("run.py", "status") + $cfgArg)
    }
    "templates" {
        Step "Answer-key templates ($Count cards)"
        Run "templates" @("run.py", "eval", "--make-template", "--count", "$Count")
        Write-Host "Next: powershell -ExecutionPolicy Bypass -File .\next_steps.ps1 -Stage label"
    }
    "label" {
        Step "Label the answer key (q saves and stops; run again to continue)"
        & $py run.py label --reviewer $Reviewer
    }
    "publish" {
        Step "Eval + release gate + publish"
        & $py run.py publish
        if ($LASTEXITCODE -ne 0) { Fail "not published: see the integrity / eval lines above" }
        Write-Host "Published. See $DataRoot\releases" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "Done. Log: $log" -ForegroundColor Green
Stop-Transcript | Out-Null
