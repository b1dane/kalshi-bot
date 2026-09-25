#!/usr/bin/env bash
# run_bot.sh — start the Kalshi BTC 15-min paper trading bot
# Usage: bash run_bot.sh
set -e

# Export the TYPESAFE_API_KEY so the bot can call Jev
export TYPESAFE_API_KEY="apikey_2796c6cb32855fb4aaf9deb42ae1109f841_5b69313b903fff059a75a9c621fb1a8bd039e0b3a7990a4df3df7291cfa17f00"

echo "=== Kalshi BTC 15-min Paper Bot ==="
echo "TYPESAFE_API_KEY: ${TYPESAFE_API_KEY:0:20}..."
echo "Running runner.py ..."
echo ""

cd ~/workspace/kalshi-bot
exec python3.11 runner.py