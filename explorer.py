"""Inspectable SQLite opening graph, serial crawler, and local HTTP application."""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib import request, parse, error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import chess

ROOT = Path(__file__).resolve().parent
BASE = 'https://explorer.lichess.org'
FILTERS = {'variant': 'standard', 'speeds': 'ultraBullet,bullet,blitz,rapid,classical,correspondence',
           'ratings': '0,1000,1200,1400,1600,1800,2000,2200,2500', 'since': '1952-01', 'until': '3000-12'}
CONFIG = {'version': 1, 'threshold': 100, 'base': BASE, 'lichess': FILTERS, 'masters_since': 1952}


def key(board):
    # Match Explorer's legal-en-passant position hashing; counters are not identity.
    return ' '.join(board.fen(en_passant='legal').split()[:4])


def board_for(k):
    return chess.Board(k + ' 0 1')


def total(stats):
    return sum(stats.get(k, 0) for k in ('white', 'draws', 'black'))


def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS positions(key TEXT PRIMARY KEY, depth INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS snapshots(
      position TEXT REFERENCES positions(key), source TEXT CHECK(source IN ('masters','lichess')),
      white INTEGER NOT NULL, draws INTEGER NOT NULL, black INTEGER NOT NULL,
      fetched TEXT NOT NULL, raw TEXT NOT NULL, PRIMARY KEY(position,source));
    CREATE TABLE IF NOT EXISTS moves(
      position TEXT REFERENCES positions(key), source TEXT NOT NULL, uci TEXT NOT NULL,
      san TEXT NOT NULL, target TEXT NOT NULL, white INTEGER NOT NULL,
      draws INTEGER NOT NULL, black INTEGER NOT NULL, raw TEXT NOT NULL,
      PRIMARY KEY(position,source,uci));
    CREATE INDEX IF NOT EXISTS move_target ON moves(target);
    ''')
    config = json.dumps(CONFIG, sort_keys=True)
    old = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
    if old and old[0] != config:
        raise ValueError('Database configuration differs. Use a new database for changed filters.')
    with db:
        db.execute("INSERT OR IGNORE INTO meta VALUES ('config',?)", (config,))
        db.execute('INSERT OR IGNORE INTO positions VALUES (?,0)', (key(chess.Board()),))
    return db


def validate(board, payload):
    for field in ('white', 'draws', 'black'):
        if type(payload.get(field)) is not int or payload[field] < 0:
            raise ValueError('Invalid Explorer count: ' + field)
    if not isinstance(payload.get('moves'), list):
        raise ValueError('Missing moves array')
    seen = set()
    for m in payload['moves']:
        for field in ('white', 'draws', 'black'):
            if type(m.get(field)) is not int or m[field] < 0:
                raise ValueError('Invalid move count')
        move = chess.Move.from_uci(m['uci'])
        if move not in board.legal_moves or m['uci'] in seen:
            raise ValueError('Illegal or duplicate API move: ' + m['uci'])
        seen.add(m['uci'])
    if sum(total(m) for m in payload['moves']) > total(payload):
        raise ValueError('Continuation counts exceed position total')


class Client:
    def __init__(self, token, delay=3.0, sleep=time.sleep):
        self.token, self.delay, self.sleep = token, delay, sleep
        self.last = 0

    def get(self, source, board, **overrides):
        params = {'fen': key(board) + ' 0 1', 'moves': 256, 'topGames': 0}
        params.update(FILTERS if source == 'lichess' else {'since': 1952})
        if source == 'lichess':
            params.update(recentGames=0, history='false')
        params.update(overrides)
        req = request.Request(BASE + '/' + source + '?' + parse.urlencode(params),
                              headers={'Authorization': 'Bearer ' + self.token,
                                       'User-Agent': 'local-opening-explorer/1.0', 'Accept': 'application/json'})
        for attempt in range(6):
            self.sleep(max(0, self.delay - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            try:
                with request.urlopen(req, timeout=90) as response:
                    payload = json.load(response)
                check_board = board.copy(stack=False)
                for uci in overrides.get("play", "").split(","):
                    if uci: check_board.push_uci(uci)
                validate(check_board, payload)
                return payload
            except error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise RuntimeError('Explorer authentication failed. Set LICHESS_TOKEN or --token-file.') from None
                if exc.code != 429 and exc.code < 500:
                    raise RuntimeError('Explorer HTTP ' + str(exc.code)) from None
                wait = max(60 if exc.code == 429 else 5, 5 * 2**attempt)
                retry = exc.headers.get('Retry-After', '')
                try:
                    wait = max(wait, float(retry))
                except ValueError:
                    try:
                        from email.utils import parsedate_to_datetime
                        wait = max(wait, parsedate_to_datetime(retry).timestamp() - time.time())
                    except (ValueError, TypeError):
                        pass
                if attempt == 5:
                    raise RuntimeError('Explorer unavailable after retries; resume later.') from None
                if exc.code == 429: self.delay = max(3.0, self.delay * 1.5)
                print(f'HTTP {exc.code}; waiting {wait:.0f}s before retry.', flush=True)
                self.sleep(wait)
            except (error.URLError, TimeoutError):
                if attempt == 5:
                    raise RuntimeError('Network failure; progress saved. Resume later.') from None
                self.sleep(5 * 2**attempt)


def save(db, k, source, payload):
    board = board_for(k)
    validate(board, payload)
    with db:
        db.execute('DELETE FROM moves WHERE position=? AND source=?', (k, source))
        db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?)',
                   (k, source, payload['white'], payload['draws'], payload['black'],
                    dt.datetime.now(dt.timezone.utc).isoformat(), json.dumps(payload)))
        for m in payload['moves']:
            move = chess.Move.from_uci(m['uci'])
            san = board.san(move)
            child = board.copy(stack=False)
            child.push(move)
            db.execute('INSERT INTO moves VALUES (?,?,?,?,?,?,?,?,?)',
                       (k, source, m['uci'], san, key(child), m['white'], m['draws'], m['black'], json.dumps(m)))


def expand(db, k, depth):
    with db:
        for row in db.execute("SELECT target FROM moves WHERE position=? AND source='masters' AND white+draws+black>=100", (k,)).fetchall():
            db.execute('INSERT INTO positions VALUES (?,?) ON CONFLICT(key) DO UPDATE SET depth=MIN(depth,excluded.depth)', (row[0], depth+1))


def crawl(db, client, max_ply=None, limit=None):
    # A persistent BFS frontier is derived from missing snapshots. No in-memory queue to lose.
    done = 0
    while limit is None or done < limit:
        row = db.execute('''SELECT p.* FROM positions p
          WHERE (? IS NULL OR depth<=?) AND
          (NOT EXISTS(SELECT 1 FROM snapshots s WHERE s.position=p.key AND source='masters') OR
           NOT EXISTS(SELECT 1 FROM snapshots s WHERE s.position=p.key AND source='lichess'))
          ORDER BY depth,key LIMIT 1''', (max_ply, max_ply)).fetchone()
        if row is None:
            break
        k, depth = row['key'], row['depth']
        for source in ('masters', 'lichess'):
            if not db.execute('SELECT 1 FROM snapshots WHERE position=? AND source=?', (k, source)).fetchone():
                save(db, k, source, client.get(source, board_for(k)))
            if source == 'masters':
                expand(db, k, depth)
        done += 1
        print(f'Fetched position {done}, ply {depth}; {status(db)}', flush=True)
    return done


def status(db):
    n = db.execute('SELECT COUNT(*) FROM positions').fetchone()[0]
    m = db.execute("SELECT COUNT(*) FROM snapshots WHERE source='masters'").fetchone()[0]
    l = db.execute("SELECT COUNT(*) FROM snapshots WHERE source='lichess'").fetchone()[0]
    return {'positions': n, 'masters_fetched': m, 'lichess_fetched': l,
            'complete': n == m == l}


def verify(db, client):
    crawl(db, client, max_ply=1)
    # Bounded POC: all first-ply roots plus selected four-ply lines.
    for line in [('e4', 'e5', 'Nf3', 'Nc6'), ('d4', 'd5', 'c4', 'e6'),
                 ('d4', 'Nf6', 'c4', 'e6'), ('c4', 'e6', 'd4', 'Nf6')]:
        board = chess.Board()
        for depth, san in enumerate(line, 1):
            move = board.parse_san(san)
            edge = db.execute("SELECT white+draws+black FROM moves WHERE position=? AND source='masters' AND uci=?", (key(board),move.uci())).fetchone()
            if not edge or edge[0] < 100:
                raise RuntimeError('POC sample no longer qualifies for retention; revise sample line.')
            board.push(move)
            k = key(board)
            with db:
                db.execute('INSERT INTO positions VALUES (?,?) ON CONFLICT(key) DO UPDATE SET depth=MIN(depth,excluded.depth)', (k,depth))
            for source in ('masters', 'lichess'):
                if not db.execute('SELECT 1 FROM snapshots WHERE position=? AND source=?', (k,source)).fetchone():
                    save(db,k,source,client.get(source,board))
            expand(db,k,depth)
    initial = chess.Board()
    for source in ('masters', 'lichess'):
        stored = json.loads(db.execute('SELECT raw FROM snapshots WHERE position=? AND source=?', (key(initial), source)).fetchone()[0])
        # Check the server is not silently applying the default 12-move truncation.
        if len(stored['moves']) <= 12 or total(stored) == 0:
            raise RuntimeError('POC failed: expected >12 initial continuations and positive counts.')
        large = client.get(source, initial, moves=512)
        if {m['uci'] for m in stored['moves']} != {m['uci'] for m in large['moves']}:
            raise RuntimeError('Move list differs at 256/512; investigate before scaling.')
    a, b = chess.Board(), chess.Board()
    for san in ('Nf3', 'Nf6', 'g3', 'g6'): a.push_san(san)
    for san in ('g3', 'g6', 'Nf3', 'Nf6'): b.push_san(san)
    assert key(a) == key(b)
    for source in ('masters', 'lichess'):
        x = client.get(source, initial, play='g1f3,g8f6,g2g3,g7g6')
        y = client.get(source, initial, play='g2g3,g7g6,g1f3,g8f6')
        z = client.get(source, a)
        # Counts can change during live queries; compare continuation identity here.
        if not ({m['uci'] for m in x['moves']} == {m['uci'] for m in y['moves']} == {m['uci'] for m in z['moves']}):
            raise RuntimeError('Live transposition check failed; investigate before scaling.')
    with db:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('poc_verified',?)", (dt.datetime.now(dt.timezone.utc).isoformat(),))
    print('POC verified: authenticated endpoints, full move lists, legal normalization, transpositions, shallow graph.')


def position(db, sequence):
    board, sans = chess.Board(), []
    for uci in sequence:
        move = chess.Move.from_uci(uci)
        if move not in board.legal_moves: raise ValueError('Illegal move: ' + uci)
        sans.append(board.san(move))
        board.push(move)
    k = key(board)
    snaps = {r['source']: dict(r) for r in db.execute('SELECT * FROM snapshots WHERE position=?', (k,))}
    recorded = {(r['source'], r['uci']): dict(r) for r in db.execute('SELECT * FROM moves WHERE position=?', (k,))}
    denominator = sum(total(m) for (s, _), m in recorded.items() if s == 'lichess')
    moves = []
    for move in board.legal_moves:
        uci = move.uci()
        m, l = recorded.get(('masters', uci)), recorded.get(('lichess', uci))
        lc = total(l) if l else (0 if 'lichess' in snaps else None)
        mc = total(m) if m else (0 if 'masters' in snaps else None)
        pct = lc / denominator * 100 if denominator and lc is not None else None
        moves.append({'uci': uci, 'san': board.san(move), 'masters': mc, 'lichess': lc,
                      'percent': pct, 'major': pct is not None and pct >= 20})
    moves.sort(key=lambda m: (-(m['masters'] or 0), -(m['lichess'] or 0), m['san']))
    parents = db.execute("SELECT COUNT(DISTINCT position) FROM moves WHERE target=? AND source='masters' AND white+draws+black>=100", (k,)).fetchone()[0]
    return {'key': k, 'fen': board.fen(), 'pieces': {chess.square_name(s): p.symbol() for s,p in board.piece_map().items()},
            'turn': 'White' if board.turn else 'Black', 'sans': sans, 'moves': moves, 'parents': parents,
            'retained': bool(db.execute('SELECT 1 FROM positions WHERE key=?', (k,)).fetchone()),
            'sources': {s: {'total': total(v), 'fetched': v['fetched']} for s,v in snaps.items()},
            'continuation_total': denominator, 'status': status(db)}


def serve(path, port, local_path=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = parse.urlparse(self.path)
            try:
                if url.path == '/api/position':
                    seq = parse.parse_qs(url.query).get('moves', [''])[0]
                    with contextlib.closing(connect(path)) as db:
                        db.execute('BEGIN')
                        data = position(db, seq.split(',') if seq else [])
                    from lumbra import decorate
                    reference = parse.parse_qs(url.query).get('reference', ['all'])[0]
                    reference_path = Path(local_path or ROOT / 'data/lumbra.sqlite')
                    strong_path = reference_path.with_name('lumbra-2200.sqlite') if reference_path.name == 'lumbra.sqlite' else None
                    data = decorate(data, reference_path, reference, strong_path)
                    content, mime = json.dumps(data).encode(), 'application/json'
                else:
                    name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}.get(url.path)
                    if not name:
                        self.send_error(404); return
                    content = (ROOT / 'static' / name).read_bytes()
                    mime = {'html':'text/html', 'js':'text/javascript', 'css':'text/css'}[name.split('.')[-1]]
                self.send_response(200)
                self.send_header('Content-Type', mime + '; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers(); self.wfile.write(content)
            except ValueError as exc:
                self.send_error(400, str(exc))
        def log_message(self, *args): pass
    connect(path).close()
    print(f'Open http://127.0.0.1:{port}', flush=True)
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['poc','crawl','status','serve'])
    parser.add_argument('--db', default=str(ROOT / 'openings.sqlite'))
    parser.add_argument('--local-db', type=Path, default=ROOT / 'data/lumbra.sqlite', help='Lumbra reference database for the UI')
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--max-ply', type=int)
    parser.add_argument('--limit', type=int, help='Maximum positions this run; resume with same DB')
    parser.add_argument('--delay', type=float, default=3.0)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if args.delay < 0 or (args.max_ply is not None and args.max_ply < 0) or (args.limit is not None and args.limit < 1):
        parser.error('Invalid negative argument or nonpositive limit')
    if args.command == 'serve': return serve(args.db, args.port, args.local_db)
    with contextlib.closing(connect(args.db)) as db:
        if args.command == 'status': print(json.dumps(status(db), indent=2)); return
        token = args.token_file.read_text().strip() if args.token_file else os.environ.get('LICHESS_TOKEN','').strip()
        if not token: parser.error('Set LICHESS_TOKEN or supply --token-file (no token is stored).')
        with open(args.db + '.lock', 'w') as lock:
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: parser.error('Another crawler is using this database.')
            client = Client(token, args.delay)
            if args.command == 'poc': verify(db, client)
            else:
                if not db.execute("SELECT 1 FROM meta WHERE key='poc_verified'").fetchone():
                    parser.error('Run poc successfully on this database before scaling.')
                crawl(db, client, args.max_ply, args.limit)
            print(json.dumps(status(db), indent=2))


if __name__ == '__main__':
    try: main()
    except (RuntimeError, ValueError) as exc: raise SystemExit(str(exc))
    except KeyboardInterrupt: print('\nStopped; committed progress is safe to resume.')
