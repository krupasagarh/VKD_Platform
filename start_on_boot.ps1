# Start VK live app (:8800) and Cloudflare tunnel after login/reboot.
# Safe to run again: skips anything already listening.

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $Root "data"
$LogFile = Join-Path $LogDir "boot-start.log"
$PythonCmd = Get-Command python -ErrorAction SilentlyContinue
$Python = if ($PythonCmd) { $PythonCmd.Source } else { $null }
$Cloudflared = @(
    "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe",
    "$env:ProgramFiles\cloudflared\cloudflared.exe",
    "C:\Program Files (x86)\cloudflared\cloudflared.exe",
    "C:\Program Files\cloudflared\cloudflared.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-BootLog([string]$Message) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Test-PortOpen([int]$Port) {
    $bound = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return [bool]$bound
}

function Test-CloudflaredRunning {
    return [bool](Get-Process -Name "cloudflared" -ErrorAction SilentlyContinue)
}

Write-BootLog "start_on_boot begin (root=$Root)"

if (-not $Python) {
    Write-BootLog "ERROR: python not found on PATH"
    exit 1
}

if (-not (Test-PortOpen 8800)) {
    Write-BootLog "starting VK Platform on :8800"
    $appLog = Join-Path $LogDir "app-8800.log"
    # cmd /c strips the first and last quote, so the whole line needs an extra pair.
    $appCmd = "`"`"$Python`" run.py --host 0.0.0.0 --port 8800 >> `"$appLog`" 2>&1`""
    Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", $appCmd) `
        -WorkingDirectory $Root -WindowStyle Hidden
} else {
    Write-BootLog "skip app: :8800 already listening"
}

if (-not $Cloudflared) {
    Write-BootLog "ERROR: cloudflared.exe not found"
    exit 1
}

function Get-TunnelEdgeArgs {
    # The router's DNS (192.168.0.1) sometimes refuses Cloudflare's tunnel lookups and
    # cloudflared then exits at once. Resolve the edge via 1.1.1.1 and pass the IPs.
    # Retries cover Wi-Fi not being up yet right after boot.
    for ($try = 1; $try -le 12; $try++) {
        $ips = @()
        foreach ($h in "region1.v2.argotunnel.com", "region2.v2.argotunnel.com") {
            foreach ($server in "1.1.1.1", "8.8.8.8") {
                $found = Resolve-DnsName $h -Type A -Server $server -ErrorAction SilentlyContinue |
                    Where-Object { $_.IPAddress } | Select-Object -ExpandProperty IPAddress -First 2
                if ($found) { $ips += $found; break }
            }
        }
        if ($ips.Count) {
            return ($ips | ForEach-Object { "--edge $($_):7844" }) -join " "
        }
        Start-Sleep -Seconds 10
    }
    return ""
}

if (-not (Test-CloudflaredRunning)) {
    $edgeArgs = Get-TunnelEdgeArgs
    if ($edgeArgs) {
        Write-BootLog "starting Cloudflare tunnel vk-platform ($edgeArgs)"
    } else {
        Write-BootLog "starting Cloudflare tunnel vk-platform (edge lookup failed, using system DNS)"
    }
    $tunLog = Join-Path $LogDir "cloudflared.log"
    $tunCmd = "`"`"$Cloudflared`" tunnel $edgeArgs run vk-platform >> `"$tunLog`" 2>&1`""
    Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", $tunCmd) `
        -WorkingDirectory $Root -WindowStyle Hidden
} else {
    Write-BootLog "skip tunnel: cloudflared already running"
}

Write-BootLog "start_on_boot done"
exit 0
