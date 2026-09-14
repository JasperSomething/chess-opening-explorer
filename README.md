# Opening Atlas

A local chess opening explorer using Python, python-chess, SQLite and a plain HTML/JavaScript interface. Compare **all Lumbra OTB games** with **games where both players are rated 2200+**, alongside Lichess Opening Explorer continuation statistics.

The complete source download contains **10,355,488 games**. Downloading it is separate from importing it: the UI reports the import phase and only labels the local graph complete after both passes and graph construction finish. Lichess enrichment has separate coverage and can remain pending after the local reference is complete.

## Launch

Python 3.10+ on Linux/macOS (process locking uses `fcntl`).

```sh
git clone https://github.com/JasperSomething/chess-opening-explorer.git
cd chess-opening-explorer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python explorer.py serve --db data/lumbra-lichess.sqlite --port 8766
```

Open http://127.0.0.1:8766/. The server listens only on your computer. Databases and source games are **not included in Git**. The UI works during import; unavailable counts are `—`, and incomplete counts are explicitly labelled.

The dropdown displays only the selected group’s count and percentage: **All games / All games %** or **2200+ / 2200+ %**. It also changes move ordering. The board and surrounding panels use warm wood and brown tones. It preserves the current move sequence. Lichess counts, percentages and the >=20% highlight are separate. Click continuations or board squares; Back/Forward and arrow keys navigate your line. Identical board positions share statistics across different move orders.

## Download and build the local reference

Obtain the OTB Complete PGN archive from [Lumbra’s Gigabase](https://lumbrasgigabase.com/en/download-in-pgn-format-en/). This project’s downloaded release is 2026-07-08. Preserve publisher attribution and the data’s CC BY-NC-SA 4.0 terms; source metadata is in `data/lumbra-source.json`. The publisher’s OTB collection contains some correspondence, blitz and rapid records; see [DATA-AUDIT.md](DATA-AUDIT.md).

```sh
.venv/bin/pip install -r requirements-data.txt
.venv/bin/python scripts/extract_lumbra.py data/lumbra-otb-2026-07-08.7z
# Small verification build; never confuse this with full coverage:
.venv/bin/python lumbra.py --db data/sample.sqlite --limit 20000 --bits 24
# Full build, or resume the same build:
.venv/bin/python lumbra.py
```

Default input: `data/LumbrasGigaBase_OTB_Complete.pgn`. Default output: `data/lumbra.sqlite`. No ply limit is applied. The two passes replay the full mainline of every accepted game. This is a substantial offline job; leave the computer awake. It checkpoints automatically; repeat the same command after interruption. Only one importer may write the same generation.

The first pass uses a **256 MiB counting candidate filter**. Hash collisions may admit extra candidates, but never discard a qualifying position. The second pass computes **exact counts keyed by full canonical FEN**. No approximate counts are published. Finally, the importer traverses the closure of continuations played in at least 100 games from the standard starting board. Every continuation at retained positions is counted, including moves below 100. It does not discard a game’s remaining moves when its early move order is rare; later transpositions still count.

Candidate checkpoints occur every 20,000 games; exact counts and their input checkpoint commit together every 1,000 games. An interrupted candidate batch may be replayed, which can only add false-positive candidates. An interrupted exact batch rolls back. Keep the `.candidates` file with an unfinished import. The importer stops with an error before free disk falls below 3 GiB.

### Counting semantics

- A game contributes once to a position total, and its **first outgoing continuation** from that position is counted once. Repetition does not inflate game counts. Terminal games enter position totals but not next-move denominators.
- Both 2200+ requires numeric WhiteElo and BlackElo values >=2200. Missing ratings stay in all games but are excluded from this subset.
- Only standard-start, standard-chess games with known results and no reported move-parse errors are accepted. Unknown results, invalid games and nonstandard starts are counted in the exclusion report.
- Source game records are counted individually; the importer does not independently deduplicate the publisher’s collection.
- Position identity retains placement, turn, castling and legally capturable en-passant; it ignores move counters.
- The retained graph is based on the all-games threshold. The 2200+ view compares the same positions; it does not discard all-games positions with fewer than 100 strong-player games.

Progress and exclusions are stored as JSON in the database’s `state` table and reported by the UI. Final accepted totals can be lower than the header-only inventory because move validation happens during import.

## Lichess enrichment

Only aggregated statistics for local retained positions are requested. There is no download of the full Lichess PGN corpus.

If you already have this project’s verified `openings.sqlite` API cache:

```sh
.venv/bin/python enrich_lumbra.py --seed openings.sqlite --token-file /path/to/private/LICHESS_TOKEN
```

The seed is copied using SQLite backup only if `data/lumbra-lichess.sqlite` does not yet exist. This preserves the original database and reuses compatible snapshots. The command requires a completed, nonsample local graph and a verified API cache. For a fresh installation, create the cache with the bounded live proof of concept first:

```sh
.venv/bin/python explorer.py poc --db data/lumbra-lichess.sqlite --token-file /path/to/private/LICHESS_TOKEN
.venv/bin/python enrich_lumbra.py --token-file /path/to/private/LICHESS_TOKEN
```

Alternatively set `LICHESS_TOKEN` locally. No token is stored in Git, SQLite or frontend assets. Requests are serial with at least three seconds spacing; 429 responses wait at least 60 seconds, respect Retry-After, and slow subsequent requests. Repeating enrichment fills missing snapshots without refetching cached positions. No additional Masters requests are made by `enrich_lumbra.py`.

Lichess filters remain standard rated games, all six speeds and all nine rating groups, 1952-01 through 3000-12. Each response requests 256 moves, retaining complete W/D/L statistics and raw metadata. Percentages divide continuation counts by their sum, excluding games ending at the position. See [VERIFICATION.md](VERIFICATION.md) for the original API proof of concept.

## Rebuild and update

Resume uses the same PGN identity and settings; changed input requires a new generation:

```sh
.venv/bin/python lumbra.py --pgn data/new-release.pgn --db data/lumbra-next.sqlite
.venv/bin/python enrich_lumbra.py --local data/lumbra-next.sqlite --db data/next-cache.sqlite --seed data/lumbra-lichess.sqlite --token-file /path/to/private/LICHESS_TOKEN
.venv/bin/python explorer.py serve --local-db data/lumbra-next.sqlite --db data/next-cache.sqlite --port 8767
```

Cached Lichess snapshots retain their original timestamps. For entirely fresh Lichess counts, start a fresh cache with the proof of concept instead of seeding it. Do not copy a live SQLite file directly; use its backup API or stop writers first.

## Inspect and test

```sh
.venv/bin/python -m unittest discover -s tests -v
```

`lumbra.py` owns the local import, candidate filter and UI overlay. `enrich_lumbra.py` handles the Lichess-only queue. `explorer.py` contains normalization, the API client, the legacy Masters crawler, cache persistence and HTTP server. `static/` has no build step or external dependencies.

The local reference has `state`, `counts` (full canonical position + UCI, six W/D/L counts), `retained`, and `links` tables. The empty UCI denotes position totals. The separate API cache retains `positions`, `snapshots`, `moves` and `meta` tables. Original `explorer.py crawl/status` commands describe the legacy Masters graph, not completion of the local Lumbra import.

## Prioritize 2200+

To build both-2200+ games first, pause the all-games importer, then run:

```sh
.venv/bin/python lumbra.py --minimum-rating 2200 --db data/lumbra-2200.sqlite
# After it finishes, resume the all-games checkpoint:
.venv/bin/python lumbra.py
```

The rating filter skips excluded games before move replay. The independent 2200+ graph uses its own >=100 threshold and is displayed while the all-games graph is incomplete. Once the full all-games graph completes, its 2200+ statistics replace the priority view, covering the larger all-games position set. No missing values from the priority build are treated as all-games counts. The default UI selection is 2200+.

## Every 2200+ position (no frequency cutoff)

The 2200+ view now supports a separate, complete-position index. This includes rare openings and positions later in games, even if they occur only once. The all-games reference retains its existing 100-game threshold.

```sh
# Build or resume every position from both-2200+ standard-start games:
.venv/bin/python complete_2200.py
# The normal server discovers data/lumbra-2200-complete.sqlite automatically:
.venv/bin/python explorer.py serve --db data/lumbra-lichess.sqlite --port 8766
```

This importer makes one pass, filtering headers before replaying moves. It stores an exact 34-byte position key: four piece/color bitplanes, standard castling rights, side to move and legal en passant. These keys are lossless, not hashes. SQLite `edges` rows contain the key, packed move and W/D/L counts. Move zero represents a game ending at the position; summing all rows gives the position game count. Repeated positions still contribute only their first continuation per game. Source duplicates are not independently removed.

Counts and source offset checkpoint together every 1,000 scanned records. The importer requires 5 GiB free at checkpoints and stops safely if disk space falls below that reserve. Keep existing databases until the replacement completes. Use a new `--db` for changed source input. For a bounded verification run, use `--limit 20000 --db data/complete-2200-sample.sqlite`; a sample is never complete coverage.

During rebuilding, completed frequent-position counts stay visible. Previously unindexed positions can show clearly labelled partial counts from the new database. At completion, the new index becomes authoritative for 2200+ regardless of the all-games import state. A position absent from the completed index then means zero qualifying source games, rather than an indexing cutoff. This does not expand the Lichess API queue to every endgame position: its separate cached coverage remains visible.
