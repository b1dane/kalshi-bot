#!/usr/bin/env bash
# run_bot.sh — start the Kalshi BTC 15-min paper trading bot
# Usage: bash run_bot.sh
# Secrets live in .env (gitignored) — never put keys in this file.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "Missing .env — copy .env.example to .env and fill in your keys."
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

if [ -z "${TYPESAFE_API_KEY:-}" ]; then
  echo "TYPESAFE_API_KEY is not set in .env"
  exit 1
fi

echo "=== Kalshi BTC 15-min Paper Bot ==="
echo "Running runner.py ..."
echo ""

exec python3.11 runner.py
