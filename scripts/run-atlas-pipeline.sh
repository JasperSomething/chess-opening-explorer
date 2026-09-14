#!/bin/bash
# Opening Atlas unattended pipeline.
#
#   stage 1: complete 2200+ index (every position, no frequency cutoff)
#   stage 2: verify the finished complete index
#   stage 3: all-games reference (lumbra.py, threshold 100, resumable)
#   stage 4: verify the finished all-games graph
#   stage 5: Lichess enrichment of the completed all-games retained graph
#
# Every stage is resumable from its own checkpoint and is restarted if the
# process dies. Failures are recorded in data/atlas-PIPELINE-STATUS.txt.
#
# Usage: tmux new-session -d -s atlas /path/to/scripts/run-atlas-pipeline.sh
set -u

REPO=/home/jasper/Documents/Codex/2026-09-13/build-a-local-chess-opening-explorer/outputs/chess-explorer
TOKEN_FILE=/home/jasper/.config/chess-project/LICHESS_TOKEN
STATUS="$REPO/data/atlas-PIPELINE-STATUS.txt"

cd "$REPO" || exit 1
exec >>"$REPO/data/atlas-pipeline.log" 2>&1

log() { echo "[$(date -Is)] $*"; }

phase_of() {
  python3 -c '
import json, sqlite3, sys
try:
    with sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True, timeout=20) as db:
        print(json.loads(db.execute("SELECT value FROM state WHERE id=1").fetchone()[0]).get("phase"))
except Exception:
    print("unreadable")
' "$1"
}

# run_stage <name> <db> <stage log> <command...>
run_stage() {
  local name="$1" db="$2" stage_log="$3"; shift 3
  local attempt=0 ph=unknown
  while :; do
    attempt=$((attempt + 1))
    log "$name: attempt $attempt starting: $*"
    "$@" >>"$stage_log" 2>&1
    local code=$?
    ph=$(phase_of "$db")
    log "$name: attempt $attempt exited code=$code phase=$ph"
    [ "$ph" = complete ] && return 0
    if [ "$attempt" -ge 20 ]; then
      log "$name: 20 attempts without completion; giving up (state is checkpointed, rerun to resume)"
      return 1
    fi
    sleep 60
  done
}

write_status() { printf '%s\n' "$*" >"$STATUS"; }

write_status "RUNNING stage1 complete-2200+ since $(date -Is)"
log "pipeline start (python3: $(python3 --version 2>&1))"
log "disk at start: $(df -h "$REPO" | tail -1)"

# ---------------------------------------------------------------- stage 1
if ! run_stage complete-2200+ data/lumbra-2200-complete.sqlite data/complete-2200-import.log \
      python3 complete_2200.py; then
  write_status "FAILED stage1 complete-2200+ did not reach phase=complete; all-games not started"
  log "stage1 failed; stopping"
  exit 1
fi
log "stage1: complete 2200+ index finished"
log "disk: $(df -h "$REPO" | tail -1)"

# ---------------------------------------------------------------- stage 2
write_status "RUNNING stage2 verify complete-2200+"
log "stage2: verifying complete index"
if python3 scripts/verify_complete_2200.py >>data/complete-2200-verify.log 2>&1; then
  log "stage2: verification passed"
  VERIFY_2200=passed
else
  log "stage2: VERIFICATION FAILED — see data/complete-2200-verify.log"
  VERIFY_2200=failed
fi

# ---------------------------------------------------------------- stage 3
write_status "RUNNING stage3 all-games (2200+ verify: $VERIFY_2200) since $(date -Is)"
log "stage3: all-games reference (lumbra.py)"
if ! run_stage all-games data/lumbra.sqlite data/lumbra-import.log python3 lumbra.py; then
  write_status "FAILED stage3 all-games did not reach phase=complete (2200+ verify: $VERIFY_2200)"
  log "stage3 failed; enrichment not started"
  exit 1
fi
log "stage3: all-games graph finished"
log "disk: $(df -h "$REPO" | tail -1)"

# ---------------------------------------------------------------- stage 4
write_status "RUNNING stage4 verify all-games"
log "stage4: verifying all-games graph"
if python3 - <<'PY' >>data/lumbra-verify.log 2>&1
import json, sqlite3
with sqlite3.connect('file:data/lumbra.sqlite?mode=ro', uri=True) as db:
    state = json.loads(db.execute('SELECT value FROM state WHERE id=1').fetchone()[0])
    retained = db.execute('SELECT COUNT(*) FROM retained').fetchone()[0]
    links = db.execute('SELECT COUNT(*) FROM links').fetchone()[0]
print('phase', state['phase'], 'accepted', state['accepted'],
      'retained', retained, 'links', links)
assert state['phase'] == 'complete' and not state.get('sample'), 'all-games graph incomplete'
assert retained > 0 and links > 0, 'retained graph empty'
print('all-games verification passed')
PY
then
  log "stage4: verification passed"
  VERIFY_ALL=passed
else
  log "stage4: VERIFICATION FAILED — see data/lumbra-verify.log"
  VERIFY_ALL=failed
fi

# ---------------------------------------------------------------- stage 5
if [ ! -r "$TOKEN_FILE" ]; then
  write_status "STOPPED stage5 token file unreadable: $TOKEN_FILE (2200+ verify: $VERIFY_2200, all-games verify: $VERIFY_ALL)"
  log "stage5: no readable Lichess token; enrichment not started"
  exit 1
fi
write_status "RUNNING stage5 Lichess enrichment since $(date -Is) (2200+ verify: $VERIFY_2200, all-games verify: $VERIFY_ALL)"
log "stage5: Lichess enrichment of the completed all-games retained graph"
python3 enrich_lumbra.py --token-file "$TOKEN_FILE" >>data/lumbra-enrichment.log 2>&1
log "stage5: enrichment exited code=$?"

write_status "DONE $(date -Is) (2200+ verify: $VERIFY_2200, all-games verify: $VERIFY_ALL, enrichment exit above)"
log "pipeline finished"
