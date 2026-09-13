"""Two-pass, resumable local PGN import. Approximate candidates, exact published counts."""
import argparse
from collections import deque
import hashlib
import json
import mmap
from pathlib import Path
import shutil
import sqlite3
import struct
import time
import fcntl
import chess
import chess.pgn
from explorer import key, board_for

ROOT = Path(__file__).resolve().parent


def fingerprint(board):
    # Same identity as canonical FEN, without allocating FENs for rare positions.
    ep = board.ep_square if board.has_legal_en_passant() else 64
    packed = struct.pack('<9Q2B', board.pawns, board.knights, board.bishops,
                         board.rooks, board.queens, board.kings,
                         board.occupied_co[0], board.occupied_co[1],
                         board.clean_castling_rights(), board.turn, ep)
    return hashlib.blake2b(packed, digest_size=16).digest()


class Candidates:
    """Two saturating counting arrays. Collisions only add false positives.

    Exact pass uses full canonical FEN, never a hash as position identity.
    Replaying an uncheckpointed batch can inflate candidates but not final counts.
    """
    def __init__(self, path, bits=27, threshold=100):
        self.size, self.threshold = 1 << bits, threshold
        if not 1 <= threshold <= 255: raise ValueError('Threshold must be 1..255')
        self.file = open(path, 'a+b')
        if self.file.seek(0, 2) == 0: self.file.truncate(self.size * 2)
        if self.file.tell() != self.size * 2:
            self.file.seek(0, 2)
            if self.file.tell() != self.size * 2: raise ValueError('Candidate size mismatch')
        self.map = mmap.mmap(self.file.fileno(), 0)
    def indices(self, digest):
        a, b = struct.unpack('<2Q', digest)
        return a & (self.size - 1), self.size + (b & (self.size - 1))
    def add(self, digest):
        for i in self.indices(digest):
            if self.map[i] < self.threshold: self.map[i] += 1
    def contains(self, digest):
        return all(self.map[i] >= self.threshold for i in self.indices(digest))
    def close(self):
        self.map.flush(); self.map.close(); self.file.close()


class Visitor(chess.pgn.BaseVisitor):
    def __init__(self, candidates, exact, minimum_rating=0):
        self.minimum_rating = minimum_rating
        self.candidates, self.exact = candidates, exact
        self.headers, self.seen, self.rows = {}, set(), {}
        self.error, self.strong, self.outcome = None, False, None
    def visit_header(self, tag, value): self.headers[tag] = value
    def end_headers(self):
        self.outcome = {'1-0': 0, '1/2-1/2': 1, '0-1': 2}.get(self.headers.get('Result'))
        if self.outcome is None: self.error = 'unknown_result'
        if self.headers.get('Variant', 'Standard') not in ('Standard', 'Chess', 'Normal'):
            self.error = 'variant'
        try:
            if 'FEN' in self.headers and key(chess.Board(self.headers['FEN'])) != key(chess.Board()):
                self.error = 'nonstandard_start'
        except ValueError: self.error = 'invalid_start'
        try: self.strong = min(int(self.headers.get('WhiteElo', '0')), int(self.headers.get('BlackElo', '0'))) >= 2200
        except ValueError: self.strong = False
        if self.minimum_rating and not self.strong and not self.error:
            self.error = 'below_rating_or_missing'
        if self.error: return chess.pgn.SKIP
    def begin_variation(self): return chess.pgn.SKIP
    def handle_error(self, error): self.error = 'parse_error'
    def visit_board(self, board):
        digest = fingerprint(board)
        if not self.exact: self.seen.add(digest)
        elif self.candidates.contains(digest):
            self.rows.setdefault(key(board), None)
    def visit_move(self, board, move):
        if not move:
            self.error = 'null_move'
            return
        if self.exact and self.candidates.contains(fingerprint(board)):
            k = key(board)
            # Each game contributes once per position, using its first outgoing move.
            if self.rows.get(k) is None:
                self.rows[k] = move.uci()
    def result(self): return self


def connect(path):
    db = sqlite3.connect(path, timeout=60)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS counts(position TEXT, uci TEXT,
      white INTEGER, draws INTEGER, black INTEGER,
      white2200 INTEGER, draws2200 INTEGER, black2200 INTEGER,
      PRIMARY KEY(position,uci)) WITHOUT ROWID;
    CREATE TABLE IF NOT EXISTS retained(position TEXT PRIMARY KEY, depth INTEGER);
    CREATE TABLE IF NOT EXISTS links(position TEXT, uci TEXT, target TEXT, PRIMARY KEY(position,uci));
    CREATE INDEX IF NOT EXISTS links_target ON links(target);
    ''')
    return db


def get_state(db):
    row = db.execute('SELECT value FROM state WHERE id=1').fetchone()
    return json.loads(row[0]) if row else None


def checkpoint(db, state):
    db.execute('INSERT OR REPLACE INTO state VALUES(1,?)', (json.dumps(state),))


def aggregate(db, game):
    values = [0] * 6
    values[game.outcome] = 1
    if game.strong: values[game.outcome + 3] = 1
    rows = []
    for k, uci in game.rows.items():
        rows.append((k, '', *values))  # Position total, including terminal games.
        if uci is not None: rows.append((k, uci, *values))
    db.executemany('''INSERT INTO counts VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(position,uci)
      DO UPDATE SET white=white+excluded.white,draws=draws+excluded.draws,black=black+excluded.black,
      white2200=white2200+excluded.white2200,draws2200=draws2200+excluded.draws2200,
      black2200=black2200+excluded.black2200''', rows)


def finalize(db, state):
    # Exact threshold closure, no depth cap. Rare move orders can transpose back in.
    with db:
        db.execute('DELETE FROM retained')
        db.execute('DELETE FROM links')
        initial = key(chess.Board())
        db.execute('INSERT INTO retained VALUES(?,0)', (initial,))
        queue, seen = deque([(initial, 0)]), {initial}
        while queue:
            k, depth = queue.popleft()
            for row in db.execute("SELECT uci FROM counts WHERE position=? AND uci!='' AND white+draws+black>=?", (k, state['threshold'])).fetchall():
                board = board_for(k); board.push_uci(row[0]); target = key(board)
                db.execute('INSERT INTO links VALUES(?,?,?)', (k, row[0], target))
                if target not in seen:
                    seen.add(target); queue.append((target, depth + 1))
                    db.execute('INSERT INTO retained VALUES(?,?)', (target, depth + 1))
        state['retained_positions'] = len(seen)
        state['phase'] = 'complete'
        state['finished'] = time.time()
        checkpoint(db, state)


def run(pgn, path, bits=27, threshold=100, limit=None, batch=1000, minimum_rating=0):
    pgn, path = Path(pgn).resolve(), Path(path).resolve()
    stat = pgn.stat()
    identity = {'file': str(pgn), 'bytes': stat.st_size, 'mtime_ns': stat.st_mtime_ns,
                'bits': bits, 'threshold': threshold, 'limit': limit, 'version': 1}
    if minimum_rating not in (0, 2200): raise ValueError('Supported rating selections: 0 or 2200')
    if minimum_rating: identity['minimum_rating'] = minimum_rating
    db = connect(path)
    state = get_state(db)
    if state and state['input'] != identity: raise ValueError('Input/settings changed: use a new database')
    if not state:
        state = {'input': identity, 'threshold': threshold, 'phase': 'candidates', 'games': 0,
                 'accepted': 0, 'strong': 0, 'excluded': {}, 'offset': 0, 'sample': limit is not None, 'started': time.time()}
        with db: checkpoint(db, state)
    if state['phase'] == 'complete': return state
    candidates = Candidates(str(path) + '.candidates', bits, threshold)
    try:
        for phase in ('candidates', 'exact'):
            if state['phase'] != phase: continue
            with pgn.open(encoding='utf-8-sig', errors='replace') as handle:
                handle.seek(state['offset'])
                while limit is None or state['games'] < limit:
                    if shutil.disk_usage(path.parent).free < 3 * 1024**3:
                        raise RuntimeError('Stopped with less than 3 GiB free; committed progress can resume')
                    game = chess.pgn.read_game(handle, Visitor=lambda: Visitor(candidates, phase == 'exact', minimum_rating))
                    if game is None: break
                    state['games'] += 1
                    if game.error:
                        state['excluded'][game.error] = state['excluded'].get(game.error, 0) + 1
                    else:
                        state['accepted'] += 1
                        state['strong'] += int(game.strong)
                        if phase == 'candidates':
                            for digest in game.seen: candidates.add(digest)
                        else: aggregate(db, game)
                    checkpoint_every = batch * 20 if phase == 'candidates' and bits >= 24 else batch
                    if state['games'] % checkpoint_every == 0:
                        state['offset'] = handle.tell(); state['updated'] = time.time()
                        if phase == 'candidates': candidates.map.flush()
                        checkpoint(db, state); db.commit()
                        print(json.dumps({k: state[k] for k in ('phase','games','accepted','strong','excluded','updated')}), flush=True)
                state['offset'] = 0
                if phase == 'candidates':
                    candidates.map.flush()
                    state.update(phase='exact', games=0, accepted=0, strong=0, excluded={})
                else: state['phase'] = 'finalizing'
                checkpoint(db, state); db.commit()
        if state['phase'] == 'finalizing': finalize(db, state)
        print(json.dumps(state), flush=True)
        return state
    finally:
        candidates.close(); db.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--pgn', type=Path, default=ROOT / 'data/LumbrasGigaBase_OTB_Complete.pgn')
    p.add_argument('--db', type=Path, default=ROOT / 'data/lumbra.sqlite')
    p.add_argument('--bits', type=int, default=27, help='Candidate memory: 2 * 2**bits bytes')
    p.add_argument('--minimum-rating', type=int, choices=[0, 2200], default=0)
    p.add_argument('--threshold', type=int, default=100)
    p.add_argument('--limit', type=int, help='Sample games only; use a separate database')
    args = p.parse_args()
    with open(str(args.db) + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(args.pgn, args.db, args.bits, args.threshold, args.limit, minimum_rating=args.minimum_rating)


def decorate(data, path, reference='all', strong_path=None):
    """Overlay local reference data; keep cached Lichess statistics untouched."""
    state, rows, retained = None, {}, False
    if Path(path).exists():
        db = sqlite3.connect('file:' + str(Path(path).resolve()) + '?mode=ro', uri=True, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN')
            state = get_state(db)
            rows = {r['uci']: dict(r) for r in db.execute('SELECT * FROM counts WHERE position=?', (data['key'],))}
            retained = bool(db.execute('SELECT 1 FROM retained WHERE position=?', (data['key'],)).fetchone())
            has_links = db.execute("SELECT 1 FROM sqlite_master WHERE name='links'").fetchone()
            data['parents'] = db.execute('SELECT COUNT(DISTINCT position) FROM links WHERE target=?', (data['key'],)).fetchone()[0] if has_links else 0
        finally: db.close()
    data['import'] = dict(state) if state else {'phase': 'not_started', 'games': 0}
    data['import']['progress_percent'] = min(100, 100 * state['offset'] / state['input']['bytes']) if state else 0
    data['import'].pop('input', None)  # Do not expose local source paths.
    data['local_retained'] = retained
    ready = state and state['phase'] in ('exact', 'finalizing', 'complete') and '' in rows
    fields = ('white', 'draws', 'black')
    def count(row, strong=False):
        return sum(row[f + ('2200' if strong else '')] for f in fields)
    totals = [sum(count(row, strong) for uci, row in rows.items() if uci) for strong in (False, True)]
    for move in data['moves']:
        row = rows.get(move['uci'])
        move['lumbra'] = count(row) if row else (0 if ready else None)
        move['lumbra2200'] = count(row, True) if row else (0 if ready else None)
        for field, denominator in zip(('lumbra', 'lumbra2200'), totals):
            n = move[field]
            move[field + '_percent'] = n / denominator * 100 if n is not None and denominator else None
        move['reference_percent'] = move['lumbra2200_percent' if reference == '2200' else 'lumbra_percent']
    data['local_totals'] = [count(rows[''], strong) for strong in (False, True)] if ready else [None, None]
    if strong_path and Path(strong_path).exists():
        import copy
        strong_data = decorate(copy.deepcopy(data), strong_path, '2200')
        data['strong_import'] = strong_data['import']
        if not state or state['phase'] != 'complete':
            strong_moves = {m['uci']: m for m in strong_data['moves']}
            for move in data['moves']:
                strong_move = strong_moves[move['uci']]
                move['lumbra2200'] = strong_move['lumbra2200']
                move['lumbra2200_percent'] = strong_move['lumbra2200_percent']
                if reference == '2200': move['reference_percent'] = move['lumbra2200_percent']
            data['local_totals'][1] = strong_data['local_totals'][1]
            if reference == '2200':
                data['parents'] = strong_data['parents']
                data['local_retained'] = strong_data['local_retained']
    field = 'lumbra2200' if reference == '2200' else 'lumbra'
    data['moves'].sort(key=lambda m: (-(m[field] or 0), -(m['lichess'] or 0), m['san']))
    return data
