#!/bin/bash

# Configuration
PORT=3001
WORKDIR="/Users/kaali/Desktop/altaris-dev"
CHECK_INTERVAL=5        # How often to check if the server is alive (in seconds)

echo "[WATCHDOG] Initializing Altaris Terminal auto-recovery monitor on port $PORT..."

# Function to clear the port
force_clear_port() {
    # Find any process using our target port and kill it instantly
    PIDS=$(lsof -ti:$PORT)
    if [ ! -z "$PIDS" ]; then
        echo "[WATCHDOG] Force-killing rogue processes on port $PORT: $PIDS"
        kill -9 $PIDS 2>/dev/null
    fi
}

# Function to start the Flask dev server
start_server() {
    cd "$WORKDIR"
    source venv/bin/activate
    export FLASK_ENV=development
    export FLASK_DEBUG=1
    export FLASK_APP=server.py
    flask run --host=0.0.0.0 --port=$PORT --reload &
    SERVER_PID=$!
    echo "[WATCHDOG] Server started with PID $SERVER_PID"
}

# Initial start
force_clear_port
echo "[WATCHDOG] Booting server..."
start_server

# The infinite monitoring loop
while true; do
    sleep $CHECK_INTERVAL

    # Ping the server to see if it's actually responding, not just running
    # --max-time 5 prevents curl itself from hanging on a deadlocked server
    if ! curl --output /dev/null --silent --head --fail --max-time 5 "http://localhost:$PORT"; then
        echo "---------------------------------------------------"
        echo "[WATCHDOG ERROR] Server on port $PORT is dead or hanging!"
        echo "[WATCHDOG] Terminating PID $SERVER_PID and clearing port..."

        # Kill the tracked process and anything else stuck on that port
        kill -9 $SERVER_PID 2>/dev/null
        force_clear_port

        echo "[WATCHDOG] Restarting the Altaris UI Engine..."
        start_server
        echo "---------------------------------------------------"
    fi
done
