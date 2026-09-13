# Engineering handoff: Opening Atlas

## User decisions

Build a local chess opening explorer in Python/python-chess with SQLite. Initially the source was Lichess Masters, retaining the closure of every continuation played in at least 100 games, with transpositions merged by canonical position. At every retained position, obtain ALL Lichess-game continuation counts and conditional next-move percentages; highlight >=20% without discarding other statistics.

The user has now chosen **Lumbra's full OTB collection, all games, no rating cutoff** as the local reference source. Download it once, compute reference statistics locally, and use the Lichess API only for enrichment. Do not download the multi-billion-game Lichess corpus. Label the reference source “Lumbra OTB”, not “Lichess Masters”. The old Lichess database is useful for cached enrichment and must be preserved.

## Implemented and verified

- `explorer.py`: canonical keys, serial authenticated API client, retries, SQLite persistence, BFS crawler, POC gate, local HTTP server.
- `static/`: interactive legal chessboard, move line, backward/forward, promotion selection, flip, clickable continuations, counts and >=20% highlights.
- Twelve offline tests, including normalization, transpositions, cycles, threshold boundaries, resumption, 401/429, missing versus zero counts and denominator semantics.
- Live authenticated POC checked initial position, first moves, selected four-ply lines, 256 vs 512 result limits and transposition move orders versus FEN queries.
- `scripts/audit_pgn.py`: fast header-only inventory. It does NOT validate moves or independently deduplicate.
- `scripts/extract_lumbra.py`: safe extraction helper; requires requirements-data.txt.

## Not implemented yet — do not imply these are finished

1. Bulk PGN-to-position-graph importer.
2. Lumbra integration/source configuration and UI labels.
3. Lichess-only enrichment for that graph and copying compatible cached Lichess snapshots.
4. Complete local graph or full Lichess enrichment.
5. Full bulk import remains the next engineering task. The private GitHub remote is JasperSomething/chess-opening-explorer.

The functioning application still uses the original `openings.sqlite` and original Masters crawler. `data/` contains downloaded source material, not an imported graph.

## Source acquisition

Publisher: https://lumbrasgigabase.com/en/download-in-pgn-format-en/
Collection: OTB Complete, release 2026-07-08.
Header audit completed: **10,355,488 games**. See DATA-AUDIT.md for exact counts and game-type caveats.
Archive: `data/lumbra-otb-2026-07-08.7z`, 1,584,100,410 bytes.
Expected member: `LumbrasGigaBase_OTB_Complete.pgn`, 8,625,140,207 bytes.
Metadata: `data/lumbra-source.json`.
Publisher lists CC BY-NC-SA 4.0 for its database. Preserve attribution. Do not put the archive, PGN, derived databases or credentials in Git. A source-code repository does not need the data bundle.

## Suggested next steps

1. Download, extraction and header inventory are complete. Read DATA-AUDIT.md, then start the importer POC. All games remain selected; do not introduce an Elo filter.
2. Build a small fixture-based importer POC and benchmark real data before launching all games.
3. Design a disk/memory-bounded exact aggregation pipeline. Avoid naïvely storing every unique FEN from ~10 million full games in SQLite: the index can be far larger than the PGN. Consider multiple passes or compact intermediate representations. Any approximate candidate filter must have no false negatives, followed by exact canonical-key counting. Published stats must be exact.
4. Count by canonical position globally, including transpositions. Do not stop parsing a game just because its current move order is rare: it can later transpose into a frequent position. Preserve castling and legal en-passant; ignore move counters.
5. Specify repetition and duplicate semantics explicitly. Count games rather than inadvertently multiplying counts when a game revisits a position. Decide treatment of invalid games, unknown results and nonstandard initial positions; report exclusions. A header audit is not evidence of valid moves.
6. After exact aggregation, traverse >=100 continuation edges from the normal starting board; retain ALL continuations at retained positions and their W/D/L. Canonical target nodes may exist outside the retained graph.
7. Build a new SQLite generation (`lumbra.sqlite`), retaining the old DB. Add explicit source/provenance/configuration. Never let the old crawler populate missing local reference stats with Lichess Masters data.
8. Copy only Lichess snapshots whose filters and canonical keys match. Start a single Lichess-only queue. Preserve throttling and POC verification. Prioritize useful positions if desired without changing the >=100 criterion.
9. Test canonical transpositions, exact counts, terminal games, invalid inputs, duplicates/repetitions, resume and coverage flags. Switch the UI only when it accurately labels partial/local coverage.
10. Continue work in the private GitHub repository `JasperSomething/chess-opening-explorer`. Credentials and local data are excluded. Git identity uses the authenticated GitHub username and its no-reply address.

## Known implementation details and limitations

- Canonical key = first four python-chess FEN fields, `en_passant='legal'`.
- Frequency denominator = sum of all continuation counts; excludes games ending at the position.
- HTTP API base: https://explorer.lichess.org . Both sources require Bearer auth, no Lichess token scopes needed.
- Three-second request spacing is our default, NOT a fixed published quota. Requests serial; 429 waits >=60 seconds and slows further. A live 429 recovery was observed.
- Lichess filters: standard, all six speeds, all nine rating groups, since 1952-01, until 3000-12.
- Current Masters API starts at 1952; since=0/1000 was rejected by server.
- Missing statistics are `null`/“—”; fetched-but-unrecorded legal moves are zero.
- Current configuration assumes Masters API. Refactor it before local-source integration.
- Original persistent frontier uses missing snapshots. At large size its repeated queries may need a dedicated indexed queue.
- fcntl crawler locking is Linux/macOS only. Server uses stdlib ThreadingHTTPServer, loopback only.
- Do not rely on a previous claim that a background process is still running: inspect logs/HTTP/locks and timestamps.

## Commands

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python explorer.py serve
.venv/bin/python explorer.py status
# Original crawler only (not the future Lumbra enrichment):
.venv/bin/python explorer.py crawl --token-file /path/to/private/LICHESS_TOKEN
# Data helpers:
.venv/bin/pip install -r requirements-data.txt
.venv/bin/python scripts/extract_lumbra.py data/lumbra-otb-2026-07-08.7z
.venv/bin/python scripts/audit_pgn.py data/LumbrasGigaBase_OTB_Complete.pgn data/lumbra-audit.json
```

For local machine paths, active sessions and secrets-file location, see the separate local `HERMES-HANDOFF.md` supplied to the user. It must not be published automatically.
