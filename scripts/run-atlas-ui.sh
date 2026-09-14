#!/bin/bash
# Keep the Opening Atlas UI on http://127.0.0.1:8766/ independent of any agent session.
# Usage: tmux new-session -d -s atlas-ui /path/to/scripts/run-atlas-ui.sh
cd /home/jasper/Documents/Codex/2026-09-13/build-a-local-chess-opening-explorer/outputs/chess-explorer || exit 1
exec python3 explorer.py serve --db data/lumbra-lichess.sqlite --port 8766 >> server-v2.log 2>&1
