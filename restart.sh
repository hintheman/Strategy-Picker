#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source /home/ubuntu/venv/bin/activate

echo "Stopping any existing strategy-picker instances..."
pkill -f strategy-picker 2>/dev/null
sleep 1

echo "Starting strategy-picker..."
python3 strategy-picker "$@" > nohup.log 2>&1 &
BOT_PID=$!
echo "strategy-picker started — PID=$BOT_PID (args: $@)"

