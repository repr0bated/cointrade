#!/usr/bin/env bash
set -euo pipefail
cd /srv/git/cointrade
mkdir -p data
exec 9>data/terminal.lock
flock -n 9 || { echo 'Cointrade terminal is already running.' >&2; exit 1; }
printf '%s\n' "$$" >data/terminal.pid
cd imports/robinhood-terminal
export NODE_ENV=production
exec bun run start
