# Resilient WhatsApp bulk probe — stops platform, loops until done or 3 consecutive failures.
Set-Location $PSScriptRoot\..

$log = "scripts/whatsapp_probe_run.log"
$maxConsecutiveFailures = 3
$consecutiveFailures = 0

function Write-LogLine {
    param([string]$Message)
    Write-Host $Message
    try {
        Add-Content -Path $log -Value $Message -Encoding utf8 -ErrorAction Stop
    } catch {
        Write-Warning "Could not append to log: $_"
    }
}

function Stop-Port8800 {
    $conn = Get-NetTCPConnection -LocalPort 8800 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return }
    $serverPid = $conn.OwningProcess
    Write-Host "Stopping VK Platform on port 8800 (PID $serverPid)..."
    Write-LogLine "Stopping port 8800 PID $serverPid $(Get-Date -Format o)"
    Stop-Process -Id $serverPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
}

function Get-UncheckedCount {
    $lines = python scripts/whatsapp_status_report.py 2>&1
    foreach ($line in $lines) {
        if ($line -match '^unchecked\s+(\d+)') {
            return [int]$Matches[1]
        }
    }
    return -1
}

Write-LogLine "=== Resilient WhatsApp probe started $(Get-Date -Format o) ==="

Stop-Port8800

while ($true) {
    $unchecked = Get-UncheckedCount
    if ($unchecked -eq 0) {
        Write-LogLine "All customers checked $(Get-Date -Format o)"
        Write-Host "All customers checked."
        break
    }
    if ($unchecked -lt 0) {
        Write-LogLine "Could not read unchecked count $(Get-Date -Format o)"
        $consecutiveFailures++
        if ($consecutiveFailures -ge $maxConsecutiveFailures) { break }
        Start-Sleep -Seconds 30
        continue
    }

    Write-Host "Unchecked: $unchecked — running probe batch..."
    Write-LogLine "Unchecked: $unchecked — probe run $(Get-Date -Format o)"

    python scripts/probe_whatsapp_customers.py --skip-checked --batch-size 25 2>&1 | ForEach-Object {
        Write-Host $_
        try { Add-Content -Path $log -Value $_ -Encoding utf8 -ErrorAction Stop } catch {}
    }
    $code = $LASTEXITCODE

    if ($code -ne 0) {
        $consecutiveFailures++
        Write-LogLine "Probe failed exit=$code (consecutive failure $consecutiveFailures/$maxConsecutiveFailures) $(Get-Date -Format o)"
        Write-Host "Probe failed (exit $code). Failure $consecutiveFailures/$maxConsecutiveFailures"
        if ($consecutiveFailures -ge $maxConsecutiveFailures) {
            Write-LogLine "Stopping after $maxConsecutiveFailures consecutive failures $(Get-Date -Format o)"
            break
        }
        Start-Sleep -Seconds 30
        Stop-Port8800
        continue
    }

    $consecutiveFailures = 0
    $unchecked = Get-UncheckedCount
    if ($unchecked -eq 0) {
        Write-LogLine "All customers checked after successful run $(Get-Date -Format o)"
        Write-Host "All customers checked."
        break
    }
}

Write-LogLine "=== Final status report $(Get-Date -Format o) ==="
python scripts/whatsapp_status_report.py 2>&1 | ForEach-Object {
    Write-Host $_
    try { Add-Content -Path $log -Value $_ -Encoding utf8 -ErrorAction Stop } catch {}
}

Write-Host "`nStarting VK Platform on port 8800..."
Write-LogLine "Starting VK Platform on port 8800 $(Get-Date -Format o)"
Start-Process -NoNewWindow -FilePath python -ArgumentList "run.py","--host","0.0.0.0","--port","8800" -WorkingDirectory (Get-Location)

Write-LogLine "=== Resilient probe finished $(Get-Date -Format o) ==="
Write-Host "Log: $log"
