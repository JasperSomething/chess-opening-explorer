# Opening Atlas

**Transition in progress:** the full Lumbra OTB archive has been downloaded. The user selected all OTB games with no Elo cutoff. The bulk graph importer is not implemented yet; the app still uses the original Lichess Masters source. See [HANDOFF.md](HANDOFF.md) for the exact implementation state and next steps.

A local Python / python-chess / SQLite opening explorer. No JavaScript build system, CDN, chess service in the browser, or PGN corpus download. The UI reads the local database; only the crawler contacts Lichess.

## Run

Python 3.10+ on Linux/macOS (crawler locking uses `fcntl`).

```sh
git clone https://github.com/JasperSomething/chess-opening-explorer.git
cd chess-opening-explorer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python explorer.py serve
```

Open http://127.0.0.1:8765. The supplied `openings.sqlite` contains real API data and may still be filling. The UI and `status` explicitly distinguish partial coverage from a finished graph. The server binds only to loopback.

Click a piece and a destination, or click a continuation. Use Back/Forward, the arrow keys, or a move in the line. Choosing a different move after Back starts a new branch. Flip changes the board orientation. Promotions offer all four pieces. Unrecorded legal moves are playable, but unfetched statistics display `—`, not invented zero counts.

## Authentication and proof of concept

Both current endpoints require a Lichess personal access token with **no scopes**. Generate it at https://lichess.org/account/oauth/token. Keep it in an environment variable or a private local file; do not put it in this repository.

```sh
.venv/bin/python explorer.py poc --token-file /path/to/private/LICHESS_TOKEN
```

Alternatively set `LICHESS_TOKEN` locally and omit `--token-file`. It is never persisted in SQLite, logs, or frontend assets. The supported endpoint is `https://explorer.lichess.org`; the older `.ovh` address also returned 401 without authentication during verification.

The bounded POC fetches the initial position and every qualifying first-ply position, then samples four lines through ply four: open games, Queen's Gambit, and two transposing move orders. It checks response counts and legal moves, requests 256 versus 512 moves to detect truncation, and compares continuation sets across two move orders and direct canonical FEN queries. Successful verification is recorded in SQLite and is required before `crawl` will run. All POC data is reusable. It does **not** claim that a partial database is complete.

## Build or resume the full graph

```sh
.venv/bin/python explorer.py crawl --token-file /path/to/private/LICHESS_TOKEN
.venv/bin/python explorer.py status
```

No depth or position limit is applied by default. Crawl stops only when every discovered qualifying position has both sources, or on a reported error/interruption. `Ctrl-C` is safe; repeat the command to resume. One database admits only one crawler. SQLite WAL allows the UI to remain open during collection.

For a bounded run:

```sh
.venv/bin/python explorer.py crawl --token-file /path/to/private/LICHESS_TOKEN --limit 100
.venv/bin/python explorer.py crawl --token-file /path/to/private/LICHESS_TOKEN --max-ply 5
```

A depth boundary leaves deeper discovered positions queued. Removing the boundary resumes them. `--limit` counts positions completed in that invocation, not API calls. A full crawl can take many hours or days; its final size is discovered as it runs. Do not start multiple crawlers to evade rate limits.

Requests are serial, normally spaced at least three seconds apart. HTTP 429 waits at least 60 seconds, honors a longer Retry-After, and increases spacing. Network/server failures retry with backoff; retries are bounded and failure preserves progress. A 401/403 stops immediately. Rate limits vary; there is no assumed universal requests-per-minute quota.

## Update / rebuild

Resume fills missing data; it deliberately does not refetch completed snapshots. For fresh counts and exact threshold re-evaluation, build a new generation, retaining the old database for use while the replacement fills:

```sh
.venv/bin/python explorer.py poc --db updated.sqlite --token-file /path/to/private/LICHESS_TOKEN
.venv/bin/python explorer.py crawl --db updated.sqlite --token-file /path/to/private/LICHESS_TOKEN
.venv/bin/python explorer.py status --db updated.sqlite
.venv/bin/python explorer.py serve --db updated.sqlite --port 8766
```

Wait for `"complete": true` before treating a generation as complete. Snapshot timestamps are per position/source; the live API offers no atomic database-wide snapshot. Counts can change during a long crawl. This tool preserves each full response so differences can be inspected.

## Meaning of the graph and percentages

* Start from standard chess. Traverse **every Masters continuation with white + draws + black >= 100** and retain its target position. The retained graph is the closure of these edges, not a collection of independent lines. All returned moves—including those below 100—are stored at retained positions. A subthreshold move is displayed and playable, but does not itself expand the crawl.
* Position identity is piece placement, side to move, castling rights, and **legally capturable** en-passant square. Halfmove/fullmove counters are omitted. This matches the Explorer source's legal-en-passant position hashing. Repetitions and transpositions therefore share nodes; move history stays in the browser. This is an opening-statistics graph, not a repetition/50-move adjudicator.
* Masters uses the documented full default range starting in **1952**, with no upper year filter. The API rejects `since=0` and `since=1000`. This is the API's Masters corpus, not all historical chess games.
* Lichess explicitly includes all nine rating buckets and all six speeds, standard rated games, from 1952-01 through 3000-12. It covers what the aggregated Explorer indexes, not every game ever played on Lichess (for example, not casual games).
* `frequency = continuation count / sum(all returned continuation counts)`. This is the conditional probability of the next recorded move. Games ending at this position are excluded from the denominator. Position totals and per-move white/draw/black counts remain available, allowing a different denominator if desired.
* A move is major when its unrounded frequency is **>=20%**. Display rounding never determines the highlight.
* `moves=256` requests more moves than standard chess can legally offer, avoiding the default top-12 truncation. The POC additionally compares a larger request. Full raw payloads retain auxiliary rating/opening fields when present. Top/recent game samples are explicitly disabled; these are not continuation statistics.

## Files and schema

`explorer.py` contains canonicalization, the HTTP client, validation, persistence, BFS crawl, POC, CLI and local HTTP server. `static/` is plain HTML/CSS/JS. `tests/` is standard-library unittest.

SQLite tables:

| Table | Purpose |
|---|---|
| `positions` | Canonical key and shortest discovered depth |
| `snapshots` | Per-source counts, fetch time and full response JSON, keyed by position/source |
| `moves` | Per-source UCI/SAN edge, canonical target, W/D/L and full move JSON |
| `meta` | Filter configuration and successful POC marker |

Move targets can be outside the retained graph. The target intentionally has no foreign-key constraint to `positions`; the parent does. Each response and its edge replacement commit atomically. The persistent frontier is derived from missing snapshots. A crash between Masters and Lichess requests resumes the unfinished source and reconstructs child discovery.

Configuration is stored and checked on open; changing filters requires a new database. Keep database, WAL and SHM together while running, or stop writers before copying the SQLite file.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover thresholds, legal en-passant and castling identity, transpositions, cycles, restart/depth extension, interrupted source fetching, invalid responses, terminal-game denominators, missing versus zero statistics, illegal moves, 401 and 429 behavior. No live API calls or token are needed for these tests.

Official references: [Masters API](https://raw.githubusercontent.com/lichess-org/api/master/doc/specs/tags/openingexplorer/masters.yaml), [Lichess API](https://raw.githubusercontent.com/lichess-org/api/master/doc/specs/tags/openingexplorer/lichess.yaml), [rate-limit guidance](https://lichess.org/page/api-tips), [Explorer implementation](https://github.com/lichess-org/lila-openingexplorer).
