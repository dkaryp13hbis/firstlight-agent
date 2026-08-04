#!/bin/bash
set -e

LOG=/app/logs/briefing.log
mkdir -p /app/logs

# Expose all Docker env vars to cron jobs (cron doesn't inherit them by default)
printenv | grep -v "^PATH=" >> /etc/environment

# Write the daily cron job using the BRIEFING_CRON env var (default: 06:00)
CRON="${BRIEFING_CRON:-0 6 * * *}"
echo "$CRON root cd /app && /usr/local/bin/python main.py >> $LOG 2>&1" \
    > /etc/cron.d/firstlight-briefing
chmod 0644 /etc/cron.d/firstlight-briefing

echo "[firstlight] Cron schedule: $CRON"
echo "[firstlight] Logs: $LOG"

# Start cron daemon in background
service cron start

# Run the cloud-poll daemon in foreground (blocks; handles remote refresh commands)
exec /usr/local/bin/python server.py --daemon
