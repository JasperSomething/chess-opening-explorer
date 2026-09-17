#!/bin/bash
# Keep the opening-mainlines app on http://127.0.0.1:8790/ independent of any agent session.
# The dataset is built by scripts/build_mainlines.py; this only serves what exists.
# Usage: tmux new-session -d -s mainlines-ui /path/to/scripts/run-mainlines-ui.sh
cd /home/jasper/Documents/Codex/2026-09-13/build-a-local-chess-opening-explorer/outputs/chess-explorer || exit 1
exec python3 study/mainlines_server.py --port 8790 >> mainlines-ui.log 2>&1
