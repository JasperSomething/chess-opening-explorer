"""Targeted population acquisition: fetch exactly the frozen tiers 1-3, nothing else.

Coordination, decided from evidence rather than assumption:

* the cache `data/lumbra-lichess.sqlite` grows because `enrich_lumbra.py` walks the
  `retained` table of `data/lumbra.sqlite` ordered by depth — it is still at depth 7
  with ~128,000 positions ahead of it;
* **none** of the 1,003 frozen tier 1-3 positions are in that universe at all, so
  this job cannot duplicate the crawler's work; the only risk is two processes
  issuing requests at the same time, which is why the crawler is stopped for the
  duration and restarted afterwards (its queue is derived, so it resumes exactly);
* requests go through the same `explorer.Client`, so the spacing rule (>= 3 s) and
  the 429/5xx backoff rules are literally the same code, and responses are stored
  through the same `explorer.save`, so provenance (W/D/L, per-move counts, raw
  payload) is identical to the existing cache.

Two rules are enforced in code rather than promised:

1. a position that already has a Lichess snapshot is skipped and never requested —
   re-checked immediately before each request, so anything the crawler cached in the
   meantime is a cache hit, not a duplicate request;
2. every response is committed before the next request starts (both writes happen
   inside one transaction in `explorer.save`), so the run is resumable after any
   interruption.
"""
import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import explorer  # noqa: E402

from study import db as studydb  # noqa: E402

TIERS = ('1_prescriptive_template_scope', '2_frozen_template_sample',
         '3_evaluated_high_flow_campaign')
MIN_DELAY = 3.0
OUTCOMES = ('success', 'skipped_cached', 'failed')


def load_queue(queue_csv, tiers=TIERS, limit=None):
    """The frozen queue, in its frozen order, restricted to the approved tiers."""
    rows = []
    with open(queue_csv, newline='') as handle:
        for row in csv.DictReader(handle):
            if row['tier'] not in tiers:
                continue
            rows.append({'position_key': row['position_key'], 'tier': row['tier'],
                         'reach': float(row['reach'])})
    return rows[:limit] if limit else rows


def resolve_positions(db, keys):
    """Analysis position keys (hex) -> (fen, ply).

    The queue carries the analysis layer's 34-byte keys as hex, but the Lichess cache
    is keyed by FEN, so the translation happens here and every cache operation uses
    the FEN. A key with no domain row is left out rather than guessed at.
    """
    out = {}
    for key in keys:
        try:
            blob = bytes.fromhex(key)
        except ValueError:
            continue
        row = db.execute('SELECT fen, ply FROM position WHERE position_key=?',
                         (blob,)).fetchone()
        if row:
            out[key] = (row['fen'], row['ply'])
    return out


def acquire(queue, cache_path, token, delay=MIN_DELAY, log_path=None, verbose=True,
            client=None, analysis_path=None):
    """Fetch the queue. Returns {attempted, success, skipped_cached, failed, moves, games}."""
    if delay < MIN_DELAY:
        raise ValueError(f'the project rule is at least {MIN_DELAY} s between requests')
    client = client or explorer.Client(token, delay=delay)
    db = explorer.connect(str(cache_path))
    analysis_path = Path(analysis_path) if analysis_path else \
        ROOT / 'data' / 'atlas-analysis.sqlite'
    resolved = {}
    if analysis_path.exists():
        analysis = studydb.connect(analysis_path, create=False)
        resolved = resolve_positions(analysis, [row['position_key'] for row in queue])
        analysis.close()

    log = None
    if log_path:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not path.exists()
        log = open(path, 'a', newline='')
        if fresh:
            csv.writer(log).writerow(['index', 'position_key', 'tier', 'outcome',
                                      'snapshot_games', 'moves', 'error'])
        log.flush()

    totals = {'attempted': 0, 'success': 0, 'skipped_cached': 0, 'failed': 0,
              'moves': 0, 'games': 0}
    for index, row in enumerate(queue, start=1):
        key = row['position_key']
        if key not in resolved:
            totals['failed'] += 1
            _log(log, index, row, 'failed', None, None, 'no domain row for this key')
            continue
        fen, ply = resolved[key]
        # rule 1: never request what the cache already holds
        if db.execute("SELECT 1 FROM snapshots WHERE position=? AND source='lichess'",
                      (fen,)).fetchone():
            totals['skipped_cached'] += 1
            _log(log, index, row, 'skipped_cached', None, None, '')
            continue
        totals['attempted'] += 1
        try:
            payload = client.get('lichess', explorer.board_for(fen))
            with db:
                db.execute('INSERT OR IGNORE INTO positions VALUES(?,?)', (fen, ply))
            explorer.save(db, fen, 'lichess', payload)      # commits before the next request
        except Exception as exc:                             # counted, never fatal
            totals['failed'] += 1
            _log(log, index, row, 'failed', None, None, f'{type(exc).__name__}: {exc}')
            if verbose:
                print(f"{index}/{len(queue)} {key[:16]} FAILED {type(exc).__name__}: {exc}",
                      flush=True)
            continue
        games = payload['white'] + payload['draws'] + payload['black']
        totals['success'] += 1
        totals['moves'] += len(payload['moves'])
        totals['games'] += games
        _log(log, index, row, 'success', games, len(payload['moves']), '')
        if verbose and (index % 25 == 0 or index == len(queue)):
            print(f"  {index}/{len(queue)} success={totals['success']} "
                  f"cached={totals['skipped_cached']} failed={totals['failed']}",
                  flush=True)
    if log:
        log.close()
    db.close()
    return totals


def _log(handle, index, row, outcome, games, moves, error):
    if not handle:
        return
    csv.writer(handle).writerow([index, row['position_key'], row['tier'], outcome,
                                 games if games is not None else '',
                                 moves if moves is not None else '', error])
    handle.flush()


def coverage(db_path=ROOT / 'data/lumbra-lichess.sqlite'):
    """Cache-side summary: snapshots by source and how many came from this acquisition."""
    import sqlite3
    connection = sqlite3.connect('file:' + str(Path(db_path).resolve()) + '?mode=ro', uri=True)
    out = {'snapshots_lichess': connection.execute(
        "SELECT COUNT(*) FROM snapshots WHERE source='lichess'").fetchone()[0],
        'positions': connection.execute('SELECT COUNT(*) FROM positions').fetchone()[0]}
    connection.close()
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--queue', type=Path,
                        default=ROOT / 'analysis' / 'phase2-request-queue.csv')
    parser.add_argument('--cache', type=Path, default=ROOT / 'data' / 'lumbra-lichess.sqlite')
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--log', type=Path,
                        default=ROOT / 'analysis' / 'campaign' / 'acquire-log.csv')
    parser.add_argument('--delay', type=float, default=MIN_DELAY)
    parser.add_argument('--through-tier', type=int, default=3, choices=(1, 2, 3))
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()

    tiers = TIERS[:args.through_tier]
    queue = load_queue(args.queue, tiers=tiers, limit=args.limit)
    token = args.token_file.read_text().strip()
    print(json.dumps({'tiers': list(tiers), 'queued': len(queue),
                      'delay_seconds': args.delay,
                      'cache_before': coverage(args.cache)}, indent=1), flush=True)
    totals = acquire(queue, args.cache, token, delay=args.delay, log_path=args.log)
    print(json.dumps({'totals': totals, 'cache_after': coverage(args.cache)}, indent=1))


if __name__ == '__main__':
    main()
