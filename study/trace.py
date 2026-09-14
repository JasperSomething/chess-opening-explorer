"""Compact per-game trace for honest game-level validation.

Designed now, run after the importer finishes. Nothing here replays the PGN; the
driver is implemented but explicitly gated.

What the trace must support (STUDY-CURRICULUM.md section 7):

* separation by GAME — a game is a row group, never split across train/test;
* chronological split as the primary validation (train on earlier games) with a
  random game-hash split as the secondary check;
* exact traversal through structural families and templates — every step carries
  the position ordinal and the structure ordinal;
* motif independence — motif support can be recomputed per game rather than from
  the pooled flow, so a motif repeated by one game cannot inflate support;
* per-game curriculum evaluation — walking a game step by step and accumulating
  the loss or gain of a curriculum's taught answers.

Representation: normalised rows with integer dictionaries, not JSON blobs.
Measured cost for this schema and these value distributions (`measured_row_cost`):
about 51 bytes per step and 254 bytes per game all-in including indexes. The whole
2200+ corpus projects to roughly 6.5 MB, so a binary blob format would save a few
megabytes at the cost of every query in the list above.
"""
import argparse
import hashlib
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from study import db as studydb  # noqa: E402

PRIMARY_SPLIT = 'chronological'
SECONDARY_SPLIT = 'random'

SCHEMA = """
CREATE TABLE IF NOT EXISTS trace_dict_position (
    ord_          INTEGER PRIMARY KEY,
    position_key  BLOB NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS trace_dict_structure (
    ord_          INTEGER PRIMARY KEY,
    structure_id  TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS trace_dict_move (
    ord_          INTEGER PRIMARY KEY,
    uci           TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS trace_dict_source (
    ord_          INTEGER PRIMARY KEY,
    source        TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS trace_game (
    game_id       INTEGER PRIMARY KEY,   -- stable hash of the source game identity
    source_ord    INTEGER NOT NULL,
    date_ord      INTEGER NOT NULL,      -- yyyymm, for the chronological split
    ratings_ord   INTEGER,               -- mean Elo bucket, NULL when unknown
    result_ord    INTEGER NOT NULL,      -- 0 loss, 1 draw, 2 win for the trace owner
    split_chrono  INTEGER NOT NULL,      -- 0 train, 1 test
    split_random  INTEGER NOT NULL,
    steps         INTEGER NOT NULL,
    plies         INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS trace_step (
    game_id       INTEGER NOT NULL,
    ply           INTEGER NOT NULL,
    position_ord  INTEGER NOT NULL,
    structure_ord INTEGER NOT NULL,
    uci_ord       INTEGER NOT NULL,      -- the move played from this position
    PRIMARY KEY (game_id, ply)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_trace_step_position ON trace_step(position_ord);
CREATE INDEX IF NOT EXISTS idx_trace_step_structure ON trace_step(structure_ord);
CREATE INDEX IF NOT EXISTS idx_trace_game_chrono ON trace_game(split_chrono, date_ord);
CREATE INDEX IF NOT EXISTS idx_trace_game_random ON trace_game(split_random);
"""


def ensure_schema(db):
    db.executescript(SCHEMA)
    db.commit()


# ------------------------------------------------------------------- splitting
def chronological_split(dates, train_fraction=0.7):
    """0 (train) for the earliest games, 1 (test) for the latest.

    `dates` is an iterable of (game_id, date_ord). The whole boundary month goes to
    test, so a single event cannot straddle the split.
    """
    ordered = sorted(dates, key=lambda pair: (pair[1], pair[0]))
    if not ordered:
        return {}
    cutoff = int(len(ordered) * train_fraction)
    boundary = ordered[min(cutoff, len(ordered) - 1)][1]
    return {game_id: (0 if date < boundary else 1) for game_id, date in ordered}


def random_split(game_ids, seed=1337, train_fraction=0.7):
    """Secondary check: game-level hash, stable for a given seed."""
    out = {}
    for game_id in game_ids:
        digest = hashlib.sha256(f'{seed}:{game_id}'.encode()).digest()
        value = int.from_bytes(digest[:8], 'big') / float(1 << 64)
        out[game_id] = 0 if value < train_fraction else 1
    return out


def split_counts(split):
    counts = defaultdict(int)
    for value in split.values():
        counts[value] += 1
    return {'train': counts[0], 'test': counts[1]}


# ----------------------------------------------------------------- dictionaries
class Dictionary:
    """Ordinal lookup with lazy insertion, so ids stay stable across runs."""

    def __init__(self, db, table, column):
        self.db, self.table, self.column = db, table, column
        self.forward, self.reverse = {}, {}
        for row in db.execute(f'SELECT ord_, {column} FROM {table}'):
            value = row[1]
            if isinstance(value, (bytes, bytearray)):
                value = bytes(value)
            self.forward[value] = row[0]
            self.reverse[row[0]] = value
        self.next_ord = (max(self.forward.values()) + 1) if self.forward else 0

    def get(self, value):
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value)
        if value in self.forward:
            return self.forward[value]
        ordinal = self.next_ord
        self.next_ord += 1
        self.forward[value] = ordinal
        self.reverse[ordinal] = value
        self.db.execute(f'INSERT OR IGNORE INTO {self.table}(ord_, {self.column}) VALUES(?,?)',
                        (ordinal, value))
        return ordinal


def dictionaries(db):
    ensure_schema(db)
    return {'position': Dictionary(db, 'trace_dict_position', 'position_key'),
            'structure': Dictionary(db, 'trace_dict_structure', 'structure_id'),
            'move': Dictionary(db, 'trace_dict_move', 'uci'),
            'source': Dictionary(db, 'trace_dict_source', 'source')}


# ------------------------------------------------------------------ game writer
def insert_game(db, dicts, game):
    """Insert one game. game: {game_id, source, date_ord, ratings_ord, result_ord,
    plies, steps: [(ply, position_key, structure_id, uci)]}.

    Split columns are written provisionally as 0 and recomputed by `assign_splits`
    once loading is finished, because a split is a property of the whole corpus.
    """
    steps = game['steps']
    db.execute('''INSERT OR REPLACE INTO trace_game(game_id, source_ord, date_ord, ratings_ord,
        result_ord, split_chrono, split_random, steps, plies) VALUES(?,?,?,?,?,?,?,?,?)''',
               (game['game_id'], dicts['source'].get(game['source']), game['date_ord'],
                game.get('ratings_ord'), game['result_ord'], 0, 0, len(steps), game['plies']))
    for ply, position_key, structure_id, uci in steps:
        db.execute('''INSERT OR REPLACE INTO trace_step(game_id, ply, position_ord,
            structure_ord, uci_ord) VALUES(?,?,?,?,?)''',
                   (game['game_id'], ply, dicts['position'].get(position_key),
                    dicts['structure'].get(structure_id), dicts['move'].get(uci)))


def assign_splits(db, train_fraction=0.7, seed=1337):
    """Recompute both splits over every stored game."""
    rows = list(db.execute('SELECT game_id, date_ord FROM trace_game'))
    chrono = chronological_split(rows, train_fraction)
    random_ = random_split([row[0] for row in rows], seed, train_fraction)
    for game_id, split in chrono.items():
        db.execute('UPDATE trace_game SET split_chrono=?, split_random=? WHERE game_id=?',
                   (split, random_[game_id], game_id))
    db.commit()
    return {'chronological': split_counts(chrono), 'random': split_counts(random_)}


# ------------------------------------------------------- per-game curriculum walk
def walked_games(db, items_by_position, split='chronological'):
    """Per-game curriculum evaluation that needs no engine.

    items_by_position: {position_key: [(item_id, set(taught_answers)), ...]}.
    Returns {game_id: {steps, covered, played_taught, split, result}} — `covered`
    says how much of the game the curriculum touches, `played_taught` says how
    often the game played what the curriculum teaches.
    """
    column = 'split_chrono' if split == 'chronological' else 'split_random'
    query = f'''SELECT s.game_id, d.position_key, m.uci, g.{column} AS split, g.result_ord
                FROM trace_step s
                JOIN trace_dict_position d ON d.ord_ = s.position_ord
                JOIN trace_dict_move m ON m.ord_ = s.uci_ord
                JOIN trace_game g ON g.game_id = s.game_id'''
    per_game = defaultdict(lambda: {'steps': 0, 'covered': 0, 'played_taught': 0,
                                    'split': None, 'result': None})
    for row in db.execute(query):
        entry = per_game[row['game_id']]
        entry['steps'] += 1
        entry['split'] = row['split']
        entry['result'] = row['result_ord']
        covering = items_by_position.get(bytes(row['position_key']))
        if covering:
            entry['covered'] += 1
            if any(row['uci'] in answers for _item, answers in covering):
                entry['played_taught'] += 1
    return per_game


def summarise_walk(per_game):
    """Aggregate a walk into the numbers a curriculum comparison needs."""
    totals = defaultdict(lambda: {'games': 0, 'steps': 0, 'covered': 0, 'played_taught': 0})
    for entry in per_game.values():
        bucket = totals[entry['split']]
        bucket['split'] = entry['split']
        bucket['games'] += 1
        bucket['steps'] += entry['steps']
        bucket['covered'] += entry['covered']
        bucket['played_taught'] += entry['played_taught']
    for bucket in totals.values():
        bucket['coverage_of_steps'] = (bucket['covered'] / bucket['steps']) if bucket['steps'] else None
        bucket['agreement_with_curriculum'] = (
            bucket['played_taught'] / bucket['covered']) if bucket['covered'] else None
    return dict(totals)


def curriculum_walk_with_evals(db, items_by_position, evals, fen_lookup):
    """Per-game expected loss under a curriculum, using cached evaluations.

    Compares the moves actually played with the curriculum's taught answers on the
    steps that have evaluations; the covered step count is reported, never hidden.
    """
    from study import metrics
    per_game = defaultdict(lambda: {'steps': 0, 'loss_played': 0.0, 'loss_taught': 0.0,
                                    'split': None})
    query = '''SELECT s.game_id, d.position_key, m.uci, g.split_chrono
               FROM trace_step s
               JOIN trace_dict_position d ON d.ord_ = s.position_ord
               JOIN trace_dict_move m ON m.ord_ = s.uci_ord
               JOIN trace_game g ON g.game_id = s.game_id'''
    for row in db.execute(query):
        key = bytes(row['position_key'])
        scores = evals.get(fen_lookup(key))
        covering = items_by_position.get(key)
        if not scores or not covering or row['uci'] not in scores:
            continue
        best = max(entry['ep_wp'] for entry in scores.values())
        bucket = per_game[row['game_id']]
        bucket['steps'] += 1
        bucket['split'] = row['split_chrono']
        bucket['loss_played'] += max(0.0, best - scores[row['uci']]['ep_wp'])
        taught = {u for _item, answers in covering for u in answers if u in scores}
        if taught:
            normalised, _total = metrics.renormalise({u: 1.0 for u in taught})
            bucket['loss_taught'] += sum(prob * max(0.0, best - scores[u]['ep_wp'])
                                         for u, prob in normalised.items())
    aggregate = defaultdict(lambda: {'games': 0, 'steps': 0, 'loss_played': 0.0,
                                     'loss_taught': 0.0})
    for entry in per_game.values():
        bucket = aggregate[entry['split']]
        bucket['games'] += 1
        bucket['steps'] += entry['steps']
        bucket['loss_played'] += entry['loss_played']
        bucket['loss_taught'] += entry['loss_taught']
    return {'per_game': dict(per_game), 'aggregate': dict(aggregate)}


# ------------------------------------------------------------------- projection
def measured_row_cost(games=30000, steps_per_game=5.27, seed=0):
    """Empirical bytes/step and bytes/game for this schema and value distribution."""
    import os
    import random
    import sqlite3
    import tempfile
    path = Path(tempfile.mkdtemp()) / 'trace_measure.sqlite'
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    rng = random.Random(seed)
    batch = []
    for game_id in range(games):
        count = max(1, int(rng.expovariate(1 / steps_per_game)))
        ply = 2
        for _ in range(count):
            ply += rng.randint(1, 3)
            batch.append((game_id, ply, rng.randrange(29876), rng.randrange(4065),
                          rng.randrange(26187)))
        connection.execute('''INSERT INTO trace_game(game_id, source_ord, date_ord, ratings_ord,
            result_ord, split_chrono, split_random, steps, plies) VALUES(?,?,?,?,?,?,?,?,?)''',
                           (game_id, 0, rng.randrange(20000, 202601), rng.randrange(4000), 1, 0, 0,
                            count, count * 2))
    connection.executemany('''INSERT INTO trace_step(game_id, ply, position_ord, structure_ord,
        uci_ord) VALUES(?,?,?,?,?)''', batch)
    connection.commit()
    connection.execute('VACUUM')
    connection.commit()
    steps = connection.execute('SELECT COUNT(*) FROM trace_step').fetchone()[0]
    size = path.stat().st_size
    connection.close()
    os.remove(path)
    return {'bytes_per_step': size / steps, 'bytes_per_game': size / games,
            'games': games, 'steps': steps, 'total_bytes': size}


def project(db, source='local2200', bytes_per_game=None, steps_per_game=None):
    """Projected trace size from the flow, not from the unreliable source counts.

    `position_flow.enter_mass` sums to the mean number of domain positions a game
    enters (5.270 for the 2200+ corpus) and is self-consistent with its own move
    distributions. `position_source.games` is deliberately not used: 8.2% of its
    children claim more games than their parent.
    """
    entry = db.execute('''SELECT SUM(games) FROM move_source ms JOIN position p USING(position_key)
                          WHERE p.is_entry=1 AND ms.source=?''', (source,)).fetchone()[0] or 0
    flow = db.execute('SELECT COUNT(*), SUM(enter_mass) FROM position_flow WHERE source=?',
                      (source,)).fetchone()
    mean_steps = steps_per_game if steps_per_game is not None else (flow[1] or 0.0)
    cost = bytes_per_game if bytes_per_game is not None else measured_row_cost()['bytes_per_game']
    return {'source': source, 'domain_games': int(entry), 'positions': flow[0],
            'mean_domain_positions_per_game': mean_steps, 'projected_steps': int(entry * mean_steps),
            'bytes_per_game': cost, 'projected_mb': entry * cost / 1e6}


# ---------------------------------------------------------------- replay driver
def iter_domain_games(pgn_path, positions_index, structure_of=None, max_ply=40):
    """Yield one record per game that enters the domain. NOT run by this module's CLI.

    This is the post-ingestion replay driver: one pass over the corpus, recording
    only games that reach a domain position. `positions_index` maps a 4-field FEN to
    a position key; `structure_of` maps a position key to its structure id, which is
    what makes family traversal exact.
    """
    import chess
    import chess.pgn
    with open(pgn_path, encoding='utf-8-sig', errors='replace') as handle:
        while True:
            game = chess.pgn.read_game(handle)
            if game is None:
                break
            identity = '|'.join((game.headers.get('Date', ''), game.headers.get('White', ''),
                                 game.headers.get('Black', ''), game.headers.get('Round', ''),
                                 game.headers.get('Event', '')))
            game_id = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:6], 'big')
            board = game.board()
            steps, ply = [], 0
            for move in game.mainline_moves():
                ply += 1
                if ply > max_ply:
                    break
                board.push(move)
                key = positions_index.get(' '.join(board.fen().split(' ')[:4]))
                if key is None:
                    continue
                structure = (structure_of or {}).get(key)
                if structure is None:
                    continue
                steps.append((ply, key, structure, move.uci()))
            if not steps:
                continue
            date = game.headers.get('Date', '')[:7].replace('.', '').replace('-', '')
            try:
                date_ord = int(date)
            except ValueError:
                date_ord = 0
            yield {'game_id': game_id, 'source': game.headers.get('Source', 'pgn'),
                   'date_ord': date_ord, 'ratings_ord': None, 'plies': ply,
                   'result_ord': 1, 'steps': steps}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=studydb.DEFAULT_ANALYSIS_DB)
    parser.add_argument('--source', default='local2200')
    parser.add_argument('--measure', action='store_true')
    args = parser.parse_args()
    if args.measure:
        print(measured_row_cost())
        return
    db = studydb.connect(args.db, create=False)
    print(project(db, args.source))
    db.close()


if __name__ == '__main__':
    main()
