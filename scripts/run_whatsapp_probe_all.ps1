Set-Location "c:/Users/A1/Desktop/AI agent/vk_digital_hub/vk_platform"
$log = "scripts/whatsapp_probe_run.log"
"Started $(Get-Date -Format o)" | Out-File -FilePath $log -Encoding utf8
python scripts/probe_whatsapp_customers.py --force --batch-size 25 2>&1 | Tee-Object -FilePath $log -Append
"Finished $(Get-Date -Format o)" | Out-File -FilePath $log -Append -Encoding utf8
Start-Process -NoNewWindow -FilePath python -ArgumentList "run.py","--host","0.0.0.0","--port","8800" -WorkingDirectory (Get-Location)
