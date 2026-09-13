"""Fill only missing Lichess snapshots for a completed local Lumbra graph."""
import argparse
import contextlib
import fcntl
import os
from pathlib import Path
import sqlite3
import explorer
import lumbra

ROOT = Path(__file__).resolve().parent


def enrich(local, cache, client, limit=None):
    with contextlib.closing(sqlite3.connect('file:' + str(Path(local).resolve()) + '?mode=ro', uri=True)) as reference:
        state = lumbra.get_state(reference)
        if not state or state['phase'] != 'complete' or state.get('sample'):
            raise ValueError('Finish the full local import before Lichess enrichment')
    with contextlib.closing(explorer.connect(cache)) as db:
        if not db.execute("SELECT 1 FROM meta WHERE key='poc_verified'").fetchone():
            raise ValueError('Run explorer.py poc on this cache first, or seed it from a verified cache')
        db.execute('ATTACH DATABASE ? AS reference', (str(local),))
        completed = 0
        # One cursor over the stable retained graph; no rescanning the queue per request.
        rows = db.execute('''SELECT r.position,r.depth FROM reference.retained r
          WHERE NOT EXISTS(SELECT 1 FROM snapshots s WHERE s.position=r.position AND s.source='lichess')
          ORDER BY r.depth,r.position''')
        for k, depth in rows:
            if limit is not None and completed >= limit: break
            payload = client.get('lichess', explorer.board_for(k))
            with db:
                db.execute('INSERT OR IGNORE INTO positions VALUES(?,?)', (k, depth))
            explorer.save(db, k, 'lichess', payload)
            completed += 1
            print(f'Lichess enriched {completed} positions this run, depth {depth}', flush=True)
        return completed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--local', type=Path, default=ROOT/'data/lumbra.sqlite')
    parser.add_argument('--db', type=Path, default=ROOT/'data/lumbra-lichess.sqlite')
    parser.add_argument('--seed', type=Path, help='Copy this verified existing API cache if --db does not exist')
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--delay', type=float, default=3)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    if args.delay < 3: parser.error('Use at least three seconds between requests')
    with open(str(args.db) + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not args.db.exists() and args.seed:
            with contextlib.closing(sqlite3.connect('file:' + str(args.seed.resolve()) + '?mode=ro', uri=True)) as old:
                with contextlib.closing(sqlite3.connect(args.db)) as new: old.backup(new)
        token = args.token_file.read_text().strip() if args.token_file else os.environ.get('LICHESS_TOKEN', '').strip()
        if not token: parser.error('Supply --token-file or LICHESS_TOKEN')
        enrich(args.local, args.db, explorer.Client(token, args.delay), args.limit)
