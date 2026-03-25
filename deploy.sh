#!/bin/bash
# ============================================
# Altaris Terminal — Deploy DEV → PROD
# Pushes tested changes to the live kaaliweb.uk server
# ============================================

set -e

DEV_DIR="/Users/kaali/Desktop/altaris-dev"
PROD_DIR="/Users/kaali/.gemini/antigravity/scratch/trading-app/war mechine terminal part 2"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🚀 DEPLOYING DEV → PROD"
echo "  FROM: $DEV_DIR"
echo "  TO:   $PROD_DIR"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Confirm deployment
read -p "  Deploy to kaaliweb.uk? (y/n): " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
    echo "  ❌ Deployment cancelled."
    exit 0
fi

echo ""
echo "  📦 Syncing files..."
rsync -av \
    --exclude='venv' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='.DS_Store' \
    --exclude='*.pyc' \
    --exclude='run_dev.sh' \
    --exclude='deploy.sh' \
    --exclude='.env.dev' \
    "$DEV_DIR/" "$PROD_DIR/"

echo ""
echo "  🔄 Restarting prod server..."

# Check if PM2 is managing it
if pm2 describe altaris-prod &>/dev/null; then
    pm2 restart altaris-prod
    echo "  ✅ PM2 restarted altaris-prod"
else
    # Fallback: kill and restart gunicorn directly
    echo "  ⚠️  PM2 not managing prod, restarting Gunicorn manually..."
    pkill -f "gunicorn server:app.*3000" 2>/dev/null || true
    sleep 1
    cd "$PROD_DIR"
    source venv/bin/activate
    nohup gunicorn server:app --bind 0.0.0.0:3000 --workers 2 --threads 4 --timeout 120 &>/dev/null &
    echo "  ✅ Gunicorn restarted on port 3000"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ DEPLOYED! Live at https://kaaliweb.uk"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
