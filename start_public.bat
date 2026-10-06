@echo off
setlocal
cd /d "%~dp0"

echo Starting VK Platform (live :8800, demo :8801, Cloudflare tunnel)...
echo Keep all three windows open. Closing cloudflared stops public URLs.

start "VK Live :8800" cmd /k "python run.py --host 0.0.0.0 --port 8800"
timeout /t 2 /nobreak >nul
start "VK Demo :8801" cmd /k "python run_demo.py"
timeout /t 2 /nobreak >nul
start "Cloudflare Tunnel" cmd /k "\"C:\Program Files (x86)\cloudflared\cloudflared.exe\" tunnel run vk-platform"

echo.
echo Live : https://vkdigital.tipturbroadband.in
echo Demo : https://demo.tipturbroadband.in
echo.
echo Wait for "Registered tunnel connection" in the tunnel window before testing.
pause
