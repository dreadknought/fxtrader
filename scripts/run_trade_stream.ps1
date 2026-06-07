# C:\Users\dread\dev\fxtrader\scripts\run_trade_stream.ps1

$ErrorActionPreference = "Stop"

$RepoDir   = "C:\Users\dread\dev\fxtrader"
$EnvFile   = Join-Path $RepoDir ".env.trade"
$LogDir    = Join-Path $RepoDir "out\logs"
$LockFile  = Join-Path $env:TEMP "fxtrader_trade_stream.lock"
$AggLog    = Join-Path $LogDir "aggregated.log"
$DateLog   = Join-Path $LogDir ("trade_stream_{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Location $RepoDir

if (-not (Test-Path $EnvFile)) {
    $msg = "{0} ERROR: env file missing: {1}" -f (Get-Date -Format "s"), $EnvFile
    Write-Error $msg
    exit 2
}

Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()

    if (-not $line) { return }
    if ($line.StartsWith("#")) { return }

    $parts = $line -split "=", 2
    if ($parts.Count -ne 2) { return }

    $name = $parts[0].Trim()
    $value = $parts[1].Trim()

    if (
        ($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))
    ) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
}

$lockStream = $null
try {
    $lockStream = [System.IO.File]::Open($LockFile, 'OpenOrCreate', 'ReadWrite', 'None')
}
catch {
    $msg = "{0} another fxtrader instance is running; exiting." -f (Get-Date -Format "s")
    $msg | Tee-Object -FilePath $AggLog -Append | Tee-Object -FilePath $DateLog -Append
    exit 0
}

function Write-Log {
    param([string]$Message)
    $Message | Tee-Object -FilePath $AggLog -Append | Tee-Object -FilePath $DateLog -Append
}

try {
    Write-Log ""
    Write-Log ("===== {0} fxtrader run start =====" -f (Get-Date -Format "s"))

    $stdoutFile = Join-Path $env:TEMP ("fxtrader_trade_stream_stdout_{0}_{1}.log" -f $PID, ([guid]::NewGuid().ToString("N")))
    $stderrFile = Join-Path $env:TEMP ("fxtrader_trade_stream_stderr_{0}_{1}.log" -f $PID, ([guid]::NewGuid().ToString("N")))

    try {
        $proc = Start-Process `
            -FilePath "uv" `
            -ArgumentList @("run", "python", "-m", "src.trade_stream") `
            -WorkingDirectory $RepoDir `
            -NoNewWindow `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $stdoutFile `
            -RedirectStandardError $stderrFile

        if (Test-Path $stdoutFile) {
            Get-Content $stdoutFile | ForEach-Object {
                Write-Log "$_"
            }
        }

        if (Test-Path $stderrFile) {
            Get-Content $stderrFile | ForEach-Object {
                Write-Log "$_"
            }
        }

        $exitCode = $proc.ExitCode

        if ($exitCode -ne 0) {
            Write-Log ("===== {0} fxtrader run failed (exit {1}) =====" -f (Get-Date -Format "s"), $exitCode)
            exit $exitCode
        }

        Write-Log ("===== {0} fxtrader run end =====" -f (Get-Date -Format "s"))
    }
    finally {
        if (Test-Path $stdoutFile) {
            Remove-Item $stdoutFile -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path $stderrFile) {
            Remove-Item $stderrFile -Force -ErrorAction SilentlyContinue
        }
    }
}
catch {
    Write-Log ("===== {0} fxtrader wrapper exception =====" -f (Get-Date -Format "s"))
    Write-Log $_.Exception.ToString()
    exit 1
}
finally {
    if ($lockStream) {
        $lockStream.Close()
        $lockStream.Dispose()
    }
}
