#!/bin/bash
# ============================================
# Altaris Terminal — DEV Server
# Runs Flask in debug mode on port 3001
# Access at: http://localhost:3001
# ============================================

cd "/Users/kaali/Desktop/altaris-dev"
source venv/bin/activate

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🛠  ALTARIS DEV SERVER"
echo "  📍 http://localhost:3001"
echo "  🔄 Auto-reload ON"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

export FLASK_ENV=development
export FLASK_DEBUG=1
export FLASK_APP=server.py
exec flask run --host=0.0.0.0 --port=3001 --reload
