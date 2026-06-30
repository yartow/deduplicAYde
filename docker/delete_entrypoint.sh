#!/bin/bash
set -e

# Start virtual framebuffer
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
XVFB_PID=$!

# Wait for Xvfb
sleep 1

# Start VNC server (no auth for local use)
x11vnc -display :99 -forever -nopw -rfbport 5900 -quiet &
X11VNC_PID=$!

# Start noVNC WebSocket proxy (port 6080 -> VNC 5900)
websockify --web=/usr/share/novnc/ 6080 localhost:5900 &
NOVNC_PID=$!

echo "=========================================="
echo " Browser visible at: http://localhost:6080/vnc.html"
echo "=========================================="

cleanup() {
    kill $XVFB_PID $X11VNC_PID $NOVNC_PID 2>/dev/null || true
}
trap cleanup EXIT

# Run the actual CLI command
exec python -m deduplicayde.cli "$@"
