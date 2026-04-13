#!/bin/bash
# ============================================
# Altaris Terminal — DEV Server
# Runs on port 3001 via socketio.run()
# Access at: http://localhost:3001
#
# NOTE: Do NOT use `flask run --reload` — it deadlocks
# with Flask-SocketIO's event loop. Use python server.py.
# ============================================

cd "/Users/kaali/Desktop/altaris-dev"
source venv/bin/activate

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🛠  ALTARIS DEV SERVER"
echo "  📍 http://localhost:3001"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

export PORT=3001
exec python server.py
