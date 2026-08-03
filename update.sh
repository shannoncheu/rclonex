#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo 'Updating rclonex...'
git pull --ff-only
./deploy.sh
