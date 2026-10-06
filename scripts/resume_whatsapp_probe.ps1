# Resume WhatsApp probe for unchecked customers only.
# Stop VK Platform first (Ctrl+C on run.py) — browser profile cannot be shared.
Set-Location $PSScriptRoot\..

$log = "scripts/whatsapp_probe_run.log"
"Resume started $(Get-Date -Format o)" | Out-File -FilePath $log -Append -Encoding utf8

$serverPid = (Get-NetTCPConnection -LocalPort 8800 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
if ($serverPid) {
    Write-Host "Stopping VK Platform on port 8800 (PID $serverPid)..."
    Stop-Process -Id $serverPid -Force
    Start-Sleep -Seconds 3
}

python scripts/probe_whatsapp_customers.py --skip-checked --batch-size 25 2>&1 | Tee-Object -FilePath $log -Append
$code = $LASTEXITCODE
"Resume finished $(Get-Date -Format o) exit=$code" | Out-File -FilePath $log -Append -Encoding utf8

Write-Host "Done. Start the platform again with: python run.py --host 0.0.0.0 --port 8800"
