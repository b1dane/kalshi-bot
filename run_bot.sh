#!/usr/bin/env bash
# run_bot.sh — start the Kalshi BTC 15-min paper trading bot.
# Secrets are loaded by config.py (env vars, then ./.env). Never hardcode keys here.
set -euo pipefail
cd "$(dirname "$0")"
echo "=== Kalshi BTC 15-min Paper Bot ==="
exec python3.11 runner.py
