#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo 'Starting rclonex...'
test -f .env || { echo 'Run: cp .env.example .env, then edit .env'; exit 1; }
mkdir -p rclone
docker compose up -d --build
docker compose ps
