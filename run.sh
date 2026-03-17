#!/bin/bash
# War Machine Terminal launcher for PM2
cd "/Users/kaali/.gemini/antigravity/scratch/trading-app/war mechine terminal part 2"
source venv/bin/activate
exec gunicorn server:app --bind 0.0.0.0:3000 --workers 2 --threads 4 --timeout 120
