#!/bin/bash
# Run full scraping pipeline in background, survives SSH disconnect
# Usage: ./scripts/run_full.sh [--languages en hi es ...]

cd /home/ubuntu/youtube_scrapper
source venv/bin/activate

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="logs/run_${TIMESTAMP}.log"

echo "Starting full pipeline run at $(date)"
echo "Log file: $LOG_FILE"
echo "PID will be written to logs/run.pid"

nohup python3 run.py "$@" > "$LOG_FILE" 2>&1 &
PID=$!
echo $PID > logs/run.pid

echo "Pipeline started with PID: $PID"
echo "Monitor with: tail -f $LOG_FILE"
echo "Stop with: kill $PID"
