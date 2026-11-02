#!/bin/sh
set -e

mkdir -p /data/whatsapp_web /data/screenshots /data/railtel_invoices

# Chrome refuses a profile whose lock names another host, and the container id changes
# on every recreate — so the WhatsApp login would look "in use" forever.
rm -f /data/whatsapp_web/SingletonLock /data/whatsapp_web/SingletonCookie /data/whatsapp_web/SingletonSocket

# Provider portals and WhatsApp Web run a real (headed) Chromium on a virtual screen.
if [ -z "$DISPLAY" ]; then
  rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
  Xvfb :99 -screen 0 1366x900x24 -nolisten tcp >/dev/null 2>&1 &
  export DISPLAY=:99
fi

exec python run.py --host "${VK_PLATFORM_HOST:-0.0.0.0}" --port "${VK_PLATFORM_PORT:-8800}"
