#!/bin/bash
# Run smoke test (2 videos from 2 channels, 3 languages)
cd /home/ubuntu/youtube_scrapper
source venv/bin/activate

echo "Running smoke test..."
python3 run.py --smoke-test 2>&1 | tee logs/smoke_test_$(date +%Y%m%d_%H%M%S).log
