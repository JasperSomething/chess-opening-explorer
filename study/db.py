"""Analysis database plumbing: schema, runs, read-only ingestion access."""
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA = Path(__file__).resolve().parent / 'schema.sql'

DEFAULT_ANALYSIS_DB = ROOT / 'data' / 'atlas-analysis.sqlite'

# Ingestion databases are opened read-only and never written by this layer.
INGEST = {
    'local2200': ROOT / 'data' / 'lumbra-2200.sqlite',          # frequent-position 2200+ (complete)
    'local2200complete': ROOT / 'data' / 'lumbra-2200-complete.sqlite',  # every 2200+ position (importing)
    'allgames': ROOT / 'data' / 'lumbra.sqlite',                # all games, threshold 100 (importing)
    'lichess': ROOT / 'data' / 'lumbra-lichess.sqlite',         # API cache (lichess + masters)
}

# How each ingestion source may be used. `expand` sources define the graph walk;
# `enrich` sources only annotate positions the walk already found.
SOURCE_POLICY = {
    'local2200': {'coverage': 'complete', 'expand': True},
    'local2200all': {'coverage': 'complete', 'expand': True},
    'lichess': {'coverage': 'partial', 'expand': True},    # cache covers the positions it fetched
    'masters': {'coverage': 'partial', 'expand': True},
    'local2200complete': {'coverage': 'partial', 'expand': False},
    'allgames': {'coverage': 'partial', 'expand': False},
}


def connect(path=DEFAULT_ANALYSIS_DB, create=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=60)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    if create:
        db.executescript(SCHEMA.read_text())
        db.commit()
    return db


def readonly(path):
    return sqlite3.connect('file:' + str(Path(path).resolve()) + '?mode=ro', uri=True, timeout=60)


def set_meta(db, key, value):
    db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))
    db.commit()


def get_meta(db, key, default=None):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else default


def start_run(db, domain, config, notes=''):
    db.execute('INSERT INTO analysis_run(domain, started, notes, config_json) VALUES(?,?,?,?)',
               (domain, time.strftime('%Y-%m-%dT%H:%M:%S%z'), notes, json.dumps(config, default=str)))
    db.commit()
    return db.execute('SELECT MAX(run_id) FROM analysis_run').fetchone()[0]


def finish_run(db, run_id):
    db.execute('UPDATE analysis_run SET finished=? WHERE run_id=?',
               (time.strftime('%Y-%m-%dT%H:%M:%S%z'), run_id))
    db.commit()
