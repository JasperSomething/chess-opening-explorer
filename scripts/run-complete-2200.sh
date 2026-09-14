#!/bin/bash
# Supervisor for the complete 2200+ position index.
#
# Resumes complete_2200.py from its last committed checkpoint, restarting it if
# the process dies before the index reaches phase 'complete'. One writer at a
# time; the importer's own flock prevents accidental concurrency.
#
# Usage: tmux new-session -d -s atlas /path/to/scripts/run-complete-2200.sh
REPO=/home/jasper/Documents/Codex/2026-09-13/build-a-local-chess-opening-explorer/outputs/chess-explorer
PGN="$REPO/data/LumbrasGigaBase_OTB_Complete.pgn"
DB="$REPO/data/lumbra-2200-complete.sqlite"
IMPORT_LOG="$REPO/data/complete-2200-import.log"
SUP_LOG="$REPO/data/complete-2200-supervisor.log"

cd "$REPO" || exit 1
exec >>"$SUP_LOG" 2>&1

phase() {
  python3 -c "
import json, sqlite3, sys
try:
    with sqlite3.connect('file:$DB?mode=ro', uri=True, timeout=15) as db:
        print(json.loads(db.execute('SELECT value FROM state WHERE id=1').fetchone()[0]).get('phase'))
except Exception as e:
    print('unreadable', file=sys.stderr)
    print('unreadable')
"
}

echo "[$(date -Is)] supervisor start (pid $$)"
attempt=0
while :; do
  attempt=$((attempt + 1))
  echo "[$(date -Is)] attempt $attempt: starting complete_2200.py"
  python3 complete_2200.py >>"$IMPORT_LOG" 2>&1
  code=$?
  now_phase=$(phase)
  echo "[$(date -Is)] attempt $attempt exited with code $code, phase=$now_phase"
  if [ "$now_phase" = "complete" ]; then
    echo "[$(date -Is)] index complete; supervisor exiting"
    exit 0
  fi
  if [ "$attempt" -ge 20 ]; then
    echo "[$(date -Is)] 20 attempts without completion; supervisor giving up (state is checkpointed, rerun to resume)"
    exit 1
  fi
  sleep 60
done
