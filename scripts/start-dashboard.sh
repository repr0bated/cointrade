#!/usr/bin/env bash
set -euo pipefail
cd /srv/git/cointrade
mkdir -p data
exec 9>data/gui.lock
flock -n 9 || { echo 'Cointrade scanner is already running.' >&2; exit 1; }
printf '%s\n' "$$" >data/gui.pid
export COINTRADE_RPC_PROVIDER=goldsky
exec python -u -m cointrade --db data/gui-live.sqlite gui --scan --port 8765 --astra-live --momentum-experiment
