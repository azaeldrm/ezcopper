#!/bin/bash
set -e

echo "Starting Discord-Amazon Bot..."
echo "MODE: ${MODE:-bootstrap}"

# Create data directories if they don't exist
mkdir -p /data/profile /data/artifacts

# Clean up any stale browser lock files from previous runs
rm -f /data/profile/Singleton* 2>/dev/null || true

# Start Xvfb (virtual framebuffer)
echo "Starting Xvfb..."
Xvfb :99 -screen 0 1920x1080x24 -ac &
sleep 2

# Start fluxbox window manager
echo "Starting Fluxbox..."
DISPLAY=:99 fluxbox &
sleep 1

# Set VNC password if provided
VNC_OPTS="-forever -shared -rfbport 5900"
if [ -n "$VNC_PASSWORD" ]; then
    mkdir -p ~/.vnc
    x11vnc -storepasswd "$VNC_PASSWORD" ~/.vnc/passwd
    VNC_OPTS="$VNC_OPTS -usepw"
else
    VNC_OPTS="$VNC_OPTS -nopw"
fi

# Start x11vnc
echo "Starting x11vnc..."
DISPLAY=:99 x11vnc $VNC_OPTS -display :99 &
sleep 1

# Start noVNC (websockify)
echo "Starting noVNC on port 6080..."
/opt/noVNC/utils/novnc_proxy --vnc localhost:5900 --listen 6080 &
sleep 1

echo "noVNC available at http://localhost:6080"
echo "Starting Python application..."

# Run the Python application
exec python -m app.main
