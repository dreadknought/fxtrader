# C:\Users\dread\dev\fxtrader\scripts\run_trade_stream.ps1

$ErrorActionPreference = "Stop"

$RepoDir   = "C:\Users\dread\dev\fxtrader"
$EnvFile   = Join-Path $RepoDir ".env.trade"
$LogDir    = Join-Path $RepoDir "out\logs"
$LockFile  = Join-Path $env:TEMP "fxtrader_trade_stream.lock"
$AggLog    = Join-Path $LogDir "trade_stream_aggregated.log"
$DateLog   = Join-Path $LogDir ("trade_stream_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $RepoDir

if (-not (Test-Path $EnvFile)) {
    $msg = "{0} ERROR: env file missing: {1}" -f (Get-Date -Format "s"), $EnvFile
    Write-Error $msg
    exit 2
}

# Load KEY=VALUE pairs from .env.trade into process environment
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()

    if (-not $line) { return }
    if ($line.StartsWith("#")) { return }

    $parts = $line -split "=", 2
    if ($parts.Count -ne 2) { return }

    $name = $parts[0].Trim()
    $value = $parts[1].Trim()

    # Strip matching surrounding quotes if present
    if (
        ($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))
    ) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
}

# Prevent overlap via exclusive lock file handle
$lockStream = $null
try {
    $lockStream = [System.IO.File]::Open($LockFile, 'OpenOrCreate', 'ReadWrite', 'None')
} catch {
    $msg = "{0} another fxtrader instance is running; exiting." -f (Get-Date -Format "s")
    Write-Output $msg
    Add-Content -Path $AggLog -Value $msg
    Add-Content -Path $DateLog -Value $msg
    exit 0
}

function Write-Log {
    param([string]$Message)

    Write-Output $Message
    Add-Content -Path $AggLog -Value $Message
    Add-Content -Path $DateLog -Value $Message
}

try {
    Write-Log ""
    Write-Log ("===== {0} fxtrader run start =====" -f (Get-Date -Format "s"))

    # Run inside the uv-managed project environment
    $output = & uv run python -m src.trade_stream 2>&1
    $exitCode = $LASTEXITCODE

    if ($output) {
        $output | ForEach-Object { Write-Log $_ }
    }

    if ($exitCode -ne 0) {
        Write-Log ("===== {0} fxtrader run failed (exit {1}) =====" -f (Get-Date -Format "s"), $exitCode)
        exit $exitCode
    }

    Write-Log ("===== {0} fxtrader run end =====" -f (Get-Date -Format "s"))
}
finally {
    if ($lockStream) {
        $lockStream.Close()
        $lockStream.Dispose()
    }
}
