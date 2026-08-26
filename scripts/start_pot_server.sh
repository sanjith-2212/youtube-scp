#!/bin/bash
# Start the PO Token server for yt-dlp YouTube downloads
cd /home/ubuntu/youtube_scrapper/pot_server/server
exec node build/main.js --port 4416
